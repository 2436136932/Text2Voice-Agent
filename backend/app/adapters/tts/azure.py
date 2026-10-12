"""Azure AI Speech (Cognitive Services) TTS.

Azure needs a **region** as well as a key: the endpoint is
``https://{region}.tts.speech.microsoft.com/cognitiveservices/v1``. The region
can be supplied through ``base_url`` (full template) or ``extra.region``.

The request body is SSML, which is what makes Azure interesting: rate, pitch
and volume are expressed inside the markup via ``<prosody>``.
"""

from __future__ import annotations

import time
from xml.sax.saxutils import escape

from app.adapters.base import (
    AdapterInfo,
    TTSAdapter,
    TTSRequest,
    TTSResult,
    VoiceInfo,
    request_bytes,
)
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.security import assert_credential

logger = get_logger(__name__)

#: our format -> Azure ``X-Microsoft-OutputFormat``
OUTPUT_FORMATS = {
    "mp3": "audio-24khz-48kbitrate-mono-mp3",
    "mp3_16k": "audio-16khz-32kbitrate-mono-mp3",
    "wav": "riff-24khz-16bit-mono-pcm",
    "ogg": "ogg-24khz-16bit-mono-opus",
    "opus": "ogg-24khz-16bit-mono-opus",
    "pcm": "raw-24khz-16bit-mono-pcm",
    "webm": "webm-24khz-16bit-mono-opus",
}

DEFAULT_REGION = "eastasia"


class AzureSpeechAdapter(TTSAdapter):
    id = "azure"
    name = "Azure Speech"
    kind = "tts"
    default_voice = "zh-CN-XiaoxiaoNeural"
    api_key_hint = "Azure 语音服务订阅密钥（32 位十六进制）"
    docs_url = "https://learn.microsoft.com/azure/ai-services/speech-service/rest-text-to-speech"
    notes = "需要在高级设置中填写 region（如 eastasia / eastus）；使用 SSML 控制语速与音调。"
    supports_speed = True
    speed_min = 0.5
    speed_max = 2.0
    formats = ("mp3", "wav", "ogg", "pcm", "webm")
    max_chars = 5000
    voices = (
        VoiceInfo(id="zh-CN-XiaoxiaoNeural", name="晓晓 (女)", gender="female", language="zh-CN", description="温暖自然，推荐"),
        VoiceInfo(id="zh-CN-XiaoyiNeural", name="晓伊 (女)", gender="female", language="zh-CN", description="活泼年轻"),
        VoiceInfo(id="zh-CN-YunxiNeural", name="云希 (男)", gender="male", language="zh-CN", description="阳光少年"),
        VoiceInfo(id="zh-CN-YunjianNeural", name="云健 (男)", gender="male", language="zh-CN", description="浑厚有力"),
        VoiceInfo(id="zh-CN-YunyangNeural", name="云扬 (男)", gender="male", language="zh-CN", description="专业播报"),
        VoiceInfo(id="zh-CN-liaoning-XiaobeiNeural", name="晓北 (东北话)", gender="female", language="zh-CN", description="东北方言"),
        VoiceInfo(id="zh-CN-shaanxi-XiaoniNeural", name="晓妮 (陕西话)", gender="female", language="zh-CN", description="陕西方言"),
        VoiceInfo(id="en-US-JennyNeural", name="Jenny (US)", gender="female", language="en-US", description="美式女声"),
        VoiceInfo(id="en-US-GuyNeural", name="Guy (US)", gender="male", language="en-US", description="美式男声"),
        VoiceInfo(id="en-GB-SoniaNeural", name="Sonia (UK)", gender="female", language="en-GB", description="英式女声"),
        VoiceInfo(id="ja-JP-NanamiNeural", name="Nanami (JA)", gender="female", language="ja-JP", description="日语女声"),
    )

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            id=self.id,
            name=self.name,
            kind="tts",
            requires_base_url=False,
            supports_base_url=True,
            api_key_hint=self.api_key_hint,
            docs_url=self.docs_url,
            notes=self.notes,
        )

    # -- endpoint resolution ------------------------------------------------
    def resolve_region(self, request: TTSRequest) -> str:
        region = str(request.extra.get("region") or "").strip()
        if region:
            return region
        base = (request.base_url or "").strip()
        if base:
            # Accept either a bare region ("eastasia") or a full template URL.
            if "://" not in base:
                return base.strip("/")
            host = base.split("://", 1)[1].split("/", 1)[0]
            if host.endswith(".tts.speech.microsoft.com"):
                return host[: -len(".tts.speech.microsoft.com")]
            return host.split(".")[0]
        return DEFAULT_REGION

    def resolve_base_url(self, base_url: str | None) -> str:
        if base_url and "://" in base_url:
            return base_url.rstrip("/")
        region = (base_url or DEFAULT_REGION).strip("/")
        return f"https://{region}.tts.speech.microsoft.com"

    # -- SSML ---------------------------------------------------------------
    def build_ssml(self, request: TTSRequest, voice: str, language: str) -> str:
        speed = self.clamp_speed(request.speed)
        rate_percent = int(round((speed - 1.0) * 100))
        rate = f"{rate_percent:+d}%"

        pitch_percent = int(round(float(request.extra.get("pitch", 0.0)) * 100))
        volume_percent = int(round(float(request.extra.get("volume", 0.0)) * 100))
        style = str(request.extra.get("style") or "").strip()
        style_degree = float(request.extra.get("style_degree", 1.0))

        inner = escape(request.text)
        prosody_attrs = [f'rate="{rate}"']
        if pitch_percent:
            prosody_attrs.append(f'pitch="{pitch_percent:+d}%"')
        if volume_percent:
            prosody_attrs.append(f'volume="{volume_percent:+d}%"')
        inner = f"<prosody {' '.join(prosody_attrs)}>{inner}</prosody>"

        if style:
            inner = (
                f'<mstts:express-as style="{escape(style)}" styledegree="{style_degree}">'
                f"{inner}</mstts:express-as>"
            )

        return (
            "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
            "xmlns:mstts='https://www.w3.org/2001/mstts' xml:lang='"
            f"{escape(language)}'>"
            f"<voice name='{escape(voice)}'>{inner}</voice>"
            "</speak>"
        )

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        api_key = assert_credential(request.api_key, kind="tts", provider=self.id)
        self.check_length(request.text)
        audio_format = self.normalize_format(request.audio_format)
        voice = request.voice or self.default_voice
        language = request.language or ("zh-CN" if voice.startswith("zh-") else "en-US")

        output_format = OUTPUT_FORMATS.get(audio_format)
        if output_format is None:
            raise AppError(
                ErrorCode.TTS_UNSUPPORTED_FORMAT,
                f"Azure Speech 不支持音频格式 “{audio_format}”。",
                detail={"provider": self.id, "format": audio_format},
            )

        region = self.resolve_region(request)
        url = f"{self.resolve_base_url(request.base_url)}/cognitiveservices/v1"
        headers = {
            "Ocp-Apim-Subscription-Key": api_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": output_format,
            "User-Agent": "Text2Voice-Agent",
        }
        ssml = self.build_ssml(request, voice, language)

        started = time.perf_counter()
        content, content_type, _ = await request_bytes(
            method="POST",
            url=url,
            kind="tts",
            provider=self.id,
            headers=headers,
            data=ssml.encode("utf-8"),
            timeout=settings.tts_timeout_seconds,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "tts synthesis ok provider=%s region=%s voice=%s bytes=%s latency_ms=%s",
            self.id, region, voice, len(content), latency_ms,
        )
        return TTSResult(
            audio=content,
            provider=self.id,
            voice=voice,
            audio_format=audio_format,
            latency_ms=latency_ms,
            content_type=content_type or self.content_type_for(audio_format),
        )

    def describe(self) -> dict:
        payload = super().describe()
        payload["default_region"] = DEFAULT_REGION
        payload["extra_fields"] = [
            {
                "key": "region",
                "label": "区域 (region)",
                "type": "string",
                "required": True,
                "placeholder": DEFAULT_REGION,
                "help": "Azure 语音资源的区域，例如 eastasia、eastus、southeastasia。",
            },
            {
                "key": "style",
                "label": "说话风格 (style)",
                "type": "string",
                "required": False,
                "placeholder": "cheerful / sad / newscast…",
                "help": "部分中文音色支持 mstts:express-as 风格。",
            },
        ]
        return payload