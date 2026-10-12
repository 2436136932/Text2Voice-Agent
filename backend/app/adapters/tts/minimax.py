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
    VoiceCloneRequest,
    VoiceCloneResult,
    VoiceInfo,
    request_json,
)
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.security import assert_credential

logger = get_logger(__name__)

FORMAT_MAP = {"mp3": "mp3", "pcm": "pcm", "flac": "flac", "wav": "wav"}

#: ``{base}/files/upload`` and ``{base}/voice_clone`` both hang off the same
#: API root as ``t2a_v2``; we derive it from the configured endpoint so a
#: self-hosted proxy keeps working.
_DEFAULT_API_ROOT = "https://api.minimax.chat/v1"


def _safe_voice_id(name: str) -> str:
    """Derive a vendor-legal ``voice_id`` from the user's display name.

    MiniMax restricts voice ids to letters, digits and ``-``/``_`` (and caps
    the length), so a raw Chinese display name cannot be sent verbatim.
    """
    ascii_name = "".join(ch if (ch.isalnum() and ch.isascii()) or ch in "-_" else "_" for ch in name)
    ascii_name = ascii_name.strip("_-")[:50]
    if not ascii_name:
        import uuid

        ascii_name = f"voice_{uuid.uuid4().hex[:12]}"
    return ascii_name


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
    # -- voice cloning ------------------------------------------------------
    supports_voice_clone = True
    supports_reference_audio = False
    clone_models = (
        {"id": "speech-01-turbo", "name": "speech-01-turbo (克隆音色推荐)"},
        {"id": "speech-01-240228", "name": "speech-01-240228 (克隆音色·稳定版)"},
    )
    clone_hint = (
        "MiniMax 克隆需要先在高级设置中填写 GroupId；"
        "参考音频建议 10 秒以上、单人、无背景音乐。"
        "克隆完成后返回的 voice_id 会出现在音色列表的「我的音色」中。"
    )
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

    def _api_root(self, base_url: str | None) -> str:
        """API root that ``files/upload`` and ``voice_clone`` hang off.

        The configured endpoint points at ``.../t2a_v2``; the clone endpoints
        live next to it, so strip the trailing ``t2a_v2`` when present.
        """
        resolved = self.resolve_base_url(base_url)
        suffix = "/t2a_v2"
        if resolved.endswith(suffix):
            return resolved[: -len(suffix)]
        return resolved or _DEFAULT_API_ROOT

    async def clone_voice(self, request: VoiceCloneRequest) -> VoiceCloneResult:
        """Clone in two steps: upload the clip, then register it as a voice.

        MiniMax cannot clone straight from bytes - the file has to exist
        server-side first and be referenced by ``file_id``. Both calls are
        implemented here so a single ``POST /voices/clone`` from the UI does
        the whole dance.
        """
        api_key = assert_credential(request.api_key, kind="tts", provider=self.id)
        if not request.audio:
            raise AppError(
                ErrorCode.VOICE_CLONE_FAILED,
                "参考音频为空，无法克隆。",
                detail={
                    "provider": self.id,
                    "suggestion": "请上传一段 10 秒以上的单人清晰语音后重试。",
                },
            )

        group_id = str(request.extra.get("group_id") or "").strip()
        root = self._api_root(request.base_url)
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        base_resp_hint = {
            "provider": self.id,
            "suggestion": (
                "请检查 API Key 是否正确、以及是否已在高级设置中填写 GroupId"
                "（MiniMax 控制台 → 账户管理 → 基本信息）。"
            ),
        }

        # -- step 1: upload the reference clip ------------------------------
        upload_url = f"{root}/files/upload"
        if group_id:
            upload_url = f"{upload_url}?GroupId={group_id}"
        upload_body, _ = await request_json(
            method="POST",
            url=upload_url,
            kind="tts",
            provider=self.id,
            headers=headers,
            data={"purpose": "voice_clone"},
            files={
                "file": (
                    request.audio_filename or "reference.wav",
                    request.audio,
                    "application/octet-stream",
                )
            },
            timeout=settings.tts_timeout_seconds,
        )

        if not isinstance(upload_body, dict):
            raise AppError(
                ErrorCode.VOICE_CLONE_FAILED,
                "MiniMax 文件上传返回了非预期的响应格式。",
                detail={**base_resp_hint},
            )

        upload_status = (upload_body.get("base_resp") or {}).get("status_code")
        if upload_status not in (0, None):
            message = (upload_body.get("base_resp") or {}).get("status_msg") or "未知错误"
            raise AppError(
                ErrorCode.VOICE_CLONE_FAILED,
                f"MiniMax 参考音频上传失败：{message}",
                detail={**base_resp_hint, "upstream_code": upload_status},
            )

        file_id = str((upload_body.get("file") or {}).get("file_id") or "").strip()
        if not file_id:
            # Some responses return the id at the top level instead.
            file_id = str(upload_body.get("file_id") or "").strip()
        if not file_id:
            raise AppError(
                ErrorCode.VOICE_CLONE_FAILED,
                "MiniMax 未返回 file_id，参考音频上传失败。",
                detail={**base_resp_hint},
            )

        # -- step 2: register the cloned voice -------------------------------
        clone_url = f"{root}/voice_clone"
        if group_id:
            clone_url = f"{clone_url}?GroupId={group_id}"
        clone_body, _ = await request_json(
            method="POST",
            url=clone_url,
            kind="tts",
            provider=self.id,
            headers={**headers, "Content-Type": "application/json"},
            json_body={"file_id": file_id, "voice_id": _safe_voice_id(request.name)},
            timeout=settings.tts_timeout_seconds,
        )

        if not isinstance(clone_body, dict):
            raise AppError(
                ErrorCode.VOICE_CLONE_FAILED,
                "MiniMax 克隆接口返回了非预期的响应格式。",
                detail={**base_resp_hint},
            )

        clone_status = (clone_body.get("base_resp") or {}).get("status_code")
        if clone_status not in (0, None):
            message = (clone_body.get("base_resp") or {}).get("status_msg") or "未知错误"
            raise AppError(
                ErrorCode.VOICE_CLONE_FAILED,
                f"MiniMax 声音克隆失败：{message}",
                detail={**base_resp_hint, "upstream_code": clone_status},
            )

        voice_id = str(clone_body.get("voice_id") or "").strip()
        if not voice_id:
            raise AppError(
                ErrorCode.VOICE_CLONE_FAILED,
                "MiniMax 未返回 voice_id，克隆失败。",
                detail={**base_resp_hint},
            )

        duration_ms = request.extra.get("duration_ms")
        return VoiceCloneResult(
            voice_id=voice_id,
            provider=self.id,
            name=request.name,
            model=request.model or "speech-01-turbo",
            duration_ms=int(duration_ms) if isinstance(duration_ms, (int, float)) else None,
            extra={"file_id": file_id},
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