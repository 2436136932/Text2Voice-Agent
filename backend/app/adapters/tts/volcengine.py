"""火山引擎（豆包）语音合成 - 大模型语音合成 HTTP 接口.

Wire format
-----------
``POST https://openspeech.bytedance.com/api/v1/tts``

* ``Authorization: Bearer;<access_token>`` (note the **semicolon**)
* JSON body with ``app`` / ``user`` / ``audio`` / ``request`` sections
* the audio comes back **base64 encoded** inside ``data``

Volcengine therefore needs three values, which we accept as:

===============  ==========================================
credential       where it goes
===============  ==========================================
access token     ``api_key`` (single-use, never persisted)
app id           ``extra.app_id`` (defaults to access token)
cluster          ``extra.cluster`` (default ``volcano_tts``)
===============  ==========================================
"""

from __future__ import annotations

import base64
import binascii
import time
import uuid

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

ENCODING_MAP = {
    "mp3": "mp3",
    "wav": "wav",
    "ogg": "ogg_opus",
    "opus": "ogg_opus",
    "pcm": "pcm",
}


class VolcengineTTSAdapter(TTSAdapter):
    id = "volcengine"
    name = "火山引擎 TTS"
    kind = "tts"
    default_voice = "zh_female_shuangkuaisisi_moon_bigtts"
    default_base_url = "https://openspeech.bytedance.com/api/v1/tts"
    api_key_hint = "Access Token（控制台语音技术 → 应用管理）"
    docs_url = "https://www.volcengine.com/docs/6561/79820"
    notes = "需要 Access Token + App ID；在高级设置中填写 app_id 与 cluster。"
    supports_speed = True
    speed_min = 0.2
    speed_max = 3.0
    formats = ("mp3", "wav", "ogg", "pcm")
    max_chars = 1024
    voices = (
        VoiceInfo(id="zh_female_shuangkuaisisi_moon_bigtts", name="爽快思思 (女)", gender="female", language="zh-CN", description="大模型音色，自然流畅"),
        VoiceInfo(id="zh_female_cancan_mars_bigtts", name="灿灿 (女)", gender="female", language="zh-CN", description="知性女声"),
        VoiceInfo(id="zh_male_M392_conversation_wvae_bigtts", name="阿虎 (男)", gender="male", language="zh-CN", description="对话风格男声"),
        VoiceInfo(id="zh_male_beijingxiaoye_moon_bigtts", name="北京小爷 (男)", gender="male", language="zh-CN", description="京腔男声"),
        VoiceInfo(id="zh_female_gaolengyujie_moon_bigtts", name="高冷御姐 (女)", gender="female", language="zh-CN", description="御姐音"),
        VoiceInfo(id="zh_male_yangguangqingnian_moon_bigtts", name="阳光青年 (男)", gender="male", language="zh-CN", description="阳光青年"),
        VoiceInfo(id="en_female_emotional_moon_bigtts", name="Emotional (EN)", gender="female", language="en-US", description="英文情感女声"),
        VoiceInfo(id="en_male_jackson_moon_bigtts", name="Jackson (EN)", gender="male", language="en-US", description="英文男声"),
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
                f"火山引擎 TTS 不支持音频格式 “{fmt}”，可选：{', '.join(self.formats)}。",
                detail={"provider": self.id, "format": fmt, "supported": list(self.formats)},
            )
        return fmt

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        access_token = assert_credential(request.api_key, kind="tts", provider=self.id)
        self.check_length(request.text)
        audio_format = self.normalize_format(request.audio_format)
        voice = request.voice or self.default_voice

        app_id = str(request.extra.get("app_id") or access_token).strip()
        cluster = str(request.extra.get("cluster") or "volcano_tts").strip()
        # Clamp to the vendor's documented range for ``speed_ratio``.
        speed_ratio = max(0.2, min(3.0, float(request.speed or 1.0)))

        payload = {
            "app": {"appid": app_id, "token": access_token, "cluster": cluster},
            "user": {"uid": str(request.extra.get("uid") or "text2voice-agent")},
            "audio": {
                "voice_type": voice,
                "encoding": ENCODING_MAP.get(audio_format, "mp3"),
                "speed_ratio": round(speed_ratio, 2),
                "volume_ratio": round(float(request.extra.get("volume", 1.0)), 2),
                "pitch_ratio": round(float(request.extra.get("pitch", 1.0)), 2),
            },
            "request": {
                "reqid": str(uuid.uuid4()),
                "text": request.text,
                "text_type": "plain",
                "operation": "query",
                "with_frontend": 1,
                "frontend_type": "unitTson",
            },
        }

        url = self.resolve_base_url(request.base_url)
        headers = {
            "Authorization": f"Bearer;{access_token}",
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
                "火山引擎返回了非预期的响应格式。",
                detail={"provider": self.id},
                retryable=True,
            )

        code = body.get("code")
        if code not in (0, 3000, None) and not body.get("data"):
            message = body.get("message") or f"code={code}"
            # 3001/3003 are credential problems, everything else is upstream.
            if str(code) in {"3001", "3003", "3005"}:
                raise AppError(
                    ErrorCode.TTS_AUTH_FAILED,
                    f"火山引擎鉴权失败：{message}。请检查 Access Token / App ID / cluster。",
                    detail={"provider": self.id, "upstream_code": code},
                )
            raise AppError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                f"火山引擎合成失败：{message}",
                detail={"provider": self.id, "upstream_code": code},
                retryable=True,
            )

        encoded = body.get("data")
        if not encoded:
            raise AppError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                "火山引擎未返回音频数据。",
                detail={"provider": self.id, "upstream_code": code},
                retryable=True,
            )

        try:
            audio = base64.b64decode(encoded)
        except (binascii.Error, ValueError) as exc:
            raise AppError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                "火山引擎返回的音频数据无法解码。",
                detail={"provider": self.id},
                retryable=True,
            ) from exc

        logger.info(
            "tts synthesis ok provider=%s voice=%s cluster=%s bytes=%s latency_ms=%s",
            self.id, voice, cluster, len(audio), latency_ms,
        )
        return TTSResult(
            audio=audio,
            provider=self.id,
            voice=voice,
            audio_format=audio_format,
            latency_ms=latency_ms,
            content_type=self.content_type_for(audio_format),
        )

    def describe(self) -> dict:
        payload = super().describe()
        payload["extra_fields"] = [
            {
                "key": "app_id",
                "label": "App ID",
                "type": "string",
                "required": True,
                "placeholder": "在火山引擎语音技术控制台创建应用后获取",
                "help": "留空时回退为使用 Access Token。",
            },
            {
                "key": "cluster",
                "label": "Cluster",
                "type": "string",
                "required": False,
                "placeholder": "volcano_tts",
                "help": "大模型语音合成通常为 volcano_tts。",
            },
        ]
        return payload