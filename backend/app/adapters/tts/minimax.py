"""MiniMax (海螺 / speech-01) text-to-speech.

``POST /v1/t2a_v2?GroupId=...`` returns the audio **hex encoded** inside
``data.audio`` together with the exact duration in ``extra_info.audio_length``
(milliseconds), which we forward as the authoritative duration.
"""

from __future__ import annotations

import time

from app.adapters.base import (
    AdapterInfo,
    TTSAdapter,
    TTSRequest,
    TTSResult,
    VoiceInfo,
    request_json,
)
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.security import assert_credential

logger = get_logger(__name__)

FORMAT_MAP = {"mp3": "mp3", "pcm": "pcm", "flac": "flac", "wav": "wav"}


class MiniMaxTTSAdapter(TTSAdapter):
    id = "minimax"
    name = "MiniMax 语音"
    kind = "tts"
    default_voice = "female-shaonv"
    default_base_url = "https://api.minimax.chat/v1/t2a_v2"
    api_key_hint = "JWT / API Key"
    docs_url = "https://platform.minimaxi.com/document/T2A%20V2"
    notes = "需要 API Key + GroupId（在高级设置中填写 group_id）。"
    supports_speed = True
    speed_min = 0.5
    speed_max = 2.0
    formats = ("mp3", "pcm", "flac", "wav")
    max_chars = 5000
    voices = (
        VoiceInfo(id="female-shaonv", name="少女 (女)", gender="female", language="zh-CN", description="清脆少女音"),
        VoiceInfo(id="female-yujie", name="御姐 (女)", gender="female", language="zh-CN", description="成熟女声"),
        VoiceInfo(id="female-tianmei", name="甜美 (女)", gender="female", language="zh-CN", description="甜美女声"),
        VoiceInfo(id="male-qn-qingse", name="青涩青年 (男)", gender="male", language="zh-CN", description="青年男声"),
        VoiceInfo(id="male-qn-jingying", name="精英青年 (男)", gender="male", language="zh-CN", description="成熟男声"),
        VoiceInfo(id="presenter_male", name="男性主持人", gender="male", language="zh-CN", description="播报风格"),
        VoiceInfo(id="presenter_female", name="女性主持人", gender="female", language="zh-CN", description="播报风格"),
    )

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            id=self.id,
            name=self.name,
            kind="tts",
            api_key_hint=self.api_key_hint,
            docs_url=self.docs_url,
            notes=self.notes,
        )

    def resolve_base_url(self, base_url: str | None) -> str:
        return (base_url or self.default_base_url).rstrip("/")

    def normalize_format(self, audio_format: str | None) -> str:
        fmt = (audio_format or "mp3").lower()
        if fmt not in self.formats:
            raise AppError(
                ErrorCode.TTS_UNSUPPORTED_FORMAT,
                f"MiniMax 不支持音频格式 “{fmt}”，可选：{', '.join(self.formats)}。",
                detail={"provider": self.id, "format": fmt, "supported": list(self.formats)},
            )
        return fmt

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        api_key = assert_credential(request.api_key, kind="tts", provider=self.id)
        self.check_length(request.text)
        audio_format = self.normalize_format(request.audio_format)
        voice = request.voice or self.default_voice
        group_id = str(request.extra.get("group_id") or "").strip()

        url = self.resolve_base_url(request.base_url)
        if group_id:
            url = f"{url}?GroupId={group_id}"

        payload = {
            "model": request.model or "speech-01-turbo",
            "text": request.text,
            "stream": False,
            "voice_setting": {
                "voice_id": voice,
                "speed": round(self.clamp_speed(request.speed), 2),
                "vol": round(float(request.extra.get("volume", 1.0)), 2),
                "pitch": int(request.extra.get("pitch", 0)),
            },
            "audio_setting": {
                "sample_rate": int(request.extra.get("sample_rate", 32000)),
                "bitrate": int(request.extra.get("bitrate", 128000)),
                "format": FORMAT_MAP[audio_format],
            },
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        started = time.perf_counter()
        body, _ = await request_json(
            method="POST",
            url=url,
            kind="tts",
            provider=self.id,
            headers=headers,
            json_body=payload,
            timeout=settings.tts_timeout_seconds,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)

        if not isinstance(body, dict):
            raise AppError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                "MiniMax 返回了非预期的响应格式。",
                detail={"provider": self.id},
                retryable=True,
            )

        base_resp = body.get("base_resp") or {}
        status_code = base_resp.get("status_code")
        if status_code not in (0, None):
            message = base_resp.get("status_msg") or f"status_code={status_code}"
            if str(status_code) in {"1004", "1008", "2049"}:
                raise AppError(
                    ErrorCode.TTS_AUTH_FAILED,
                    f"MiniMax 鉴权失败：{message}。请检查 API Key 与 GroupId。",
                    detail={"provider": self.id, "upstream_code": status_code},
                )
            raise AppError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                f"MiniMax 合成失败：{message}",
                detail={"provider": self.id, "upstream_code": status_code},
                retryable=True,
            )

        hex_audio = (body.get("data") or {}).get("audio")
        if not hex_audio:
            raise AppError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                "MiniMax 未返回音频数据。",
                detail={"provider": self.id},
                retryable=True,
            )

        try:
            audio = bytes.fromhex(hex_audio)
        except ValueError as exc:
            raise AppError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                "MiniMax 返回的音频数据无法解码。",
                detail={"provider": self.id},
                retryable=True,
            ) from exc

        duration_ms = None
        audio_length = (body.get("extra_info") or {}).get("audio_length")
        if isinstance(audio_length, (int, float)) and audio_length > 0:
            duration_ms = int(audio_length)

        logger.info(
            "tts synthesis ok provider=%s voice=%s bytes=%s duration_ms=%s latency_ms=%s",
            self.id, voice, len(audio), duration_ms, latency_ms,
        )
        return TTSResult(
            audio=audio,
            provider=self.id,
            voice=voice,
            audio_format=audio_format,
            latency_ms=latency_ms,
            content_type=self.content_type_for(audio_format),
            duration_ms=duration_ms,
        )

    def describe(self) -> dict:
        payload = super().describe()
        payload["extra_fields"] = [
            {
                "key": "group_id",
                "label": "GroupId",
                "type": "string",
                "required": True,
                "placeholder": "MiniMax 控制台 → 账户管理 → 基本信息",
                "help": "部分账号必须携带 GroupId 才能调用 T2A 接口。",
            }
        ]
        return payload