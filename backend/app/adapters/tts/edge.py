"""Microsoft Edge "read aloud" TTS - free, no API key required.

This provider talks to the same public endpoint that Microsoft Edge's
"朗读此页" feature uses. It is genuinely useful for a zero-cost demo, but it
is an **unofficial** interface, so:

* the ``websockets`` package is an *optional* dependency - the provider is
  hidden from ``GET /providers`` when it is not installed
  (``pip install websockets`` enables it);
* failures degrade gracefully through the standard error taxonomy.

Protocol summary (WSS):

1. connect to ``.../readaloud/edge/v1?TrustedClientToken=<public token>``
2. send a ``speech.config`` text frame describing the output format
3. send an ``ssml`` text frame containing the markup
4. read binary frames: ``[2-byte BE header length][header][audio payload]``
   until a ``turn.end`` text frame arrives
"""

from __future__ import annotations

import asyncio
import time
import uuid
from xml.sax.saxutils import escape

from app.adapters.base import (
    AdapterInfo,
    TTSAdapter,
    TTSRequest,
    TTSResult,
    VoiceInfo,
)
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger

logger = get_logger(__name__)

TRUSTED_CLIENT_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
WSS_BASE = "wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1"

OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
CHROMIUM_VERSION = "130.0.2849.68"


class EdgeTTSAdapter(TTSAdapter):
    id = "edge"
    name = "Edge 免费 TTS (无需密钥)"
    kind = "tts"
    default_voice = "zh-CN-XiaoxiaoNeural"
    default_base_url = WSS_BASE
    api_key_hint = "(无需密钥)"
    docs_url = "https://github.com/rany2/edge-tts"
    notes = "使用微软 Edge 朗读接口，完全免费且无需密钥；需要安装可选依赖 websockets。"
    supports_speed = True
    speed_min = 0.5
    speed_max = 2.0
    formats = ("mp3",)
    max_chars = 3000
    requires_api_key = False
    requires_extra = "websockets"
    voices = (
        VoiceInfo(id="zh-CN-XiaoxiaoNeural", name="晓晓 (女)", gender="female", language="zh-CN", description="温暖自然，推荐"),
        VoiceInfo(id="zh-CN-XiaoyiNeural", name="晓伊 (女)", gender="female", language="zh-CN", description="活泼年轻"),
        VoiceInfo(id="zh-CN-YunxiNeural", name="云希 (男)", gender="male", language="zh-CN", description="阳光少年"),
        VoiceInfo(id="zh-CN-YunjianNeural", name="云健 (男)", gender="male", language="zh-CN", description="浑厚有力"),
        VoiceInfo(id="zh-CN-YunyangNeural", name="云扬 (男)", gender="male", language="zh-CN", description="专业播报"),
        VoiceInfo(id="zh-CN-YunxiaNeural", name="云夏 (男童)", gender="male", language="zh-CN", description="童声"),
        VoiceInfo(id="zh-TW-HsiaoChenNeural", name="曉臻 (女)", gender="female", language="zh-TW", description="台湾国语"),
        VoiceInfo(id="en-US-AriaNeural", name="Aria (US)", gender="female", language="en-US", description="美式女声"),
        VoiceInfo(id="en-US-GuyNeural", name="Guy (US)", gender="male", language="en-US", description="美式男声"),
        VoiceInfo(id="ja-JP-NanamiNeural", name="Nanami (JA)", gender="female", language="ja-JP", description="日语女声"),
    )

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            id=self.id,
            name=self.name,
            kind="tts",
            requires_api_key=False,
            api_key_hint=self.api_key_hint,
            docs_url=self.docs_url,
            notes=self.notes,
        )

    def resolve_base_url(self, base_url: str | None) -> str:
        return (base_url or self.default_base_url).rstrip("/")

    # -- SSML ---------------------------------------------------------------
    def build_ssml(self, request: TTSRequest, voice: str) -> str:
        speed = self.clamp_speed(request.speed)
        rate_percent = int(round((speed - 1.0) * 100))
        return (
            "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='zh-CN'>"
            f"<voice name='{escape(voice)}'>"
            f"<prosody rate='{rate_percent:+d}%'>{escape(request.text)}</prosody>"
            "</voice></speak>"
        )

    def _connect_url(self) -> str:
        connection_id = uuid.uuid4().hex
        return (
            f"{WSS_BASE}?TrustedClientToken={TRUSTED_CLIENT_TOKEN}"
            f"&Sec-MS-GEC-Version=1-{CHROMIUM_VERSION}&ConnectionId={connection_id}"
        )

    def _blocking_synthesize(self, request: TTSRequest, voice: str, timeout: float) -> bytes:
        """Run the websocket conversation synchronously (called in a thread)."""
        try:
            from websockets.sync.client import connect
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise AppError(
                ErrorCode.TTS_ERROR,
                "Edge TTS 需要可选依赖 websockets，请执行 pip install websockets 后重启服务。",
                detail={"provider": self.id},
            ) from exc

        headers = {
            "Origin": "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
            ),
        }
        config_payload = (
            '{"context":{"synthesis":{"audio":{"metadataoptions":'
            '{"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},'
            f'"outputFormat":"{OUTPUT_FORMAT}"'
            "}}}}"
        )
        config_message = (
            "X-Timestamp:{ts}\r\n"
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n{payload}"
        ).format(ts=time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)"), payload=config_payload)

        request_id = uuid.uuid4().hex
        ssml_message = (
            "X-RequestId:{rid}\r\n"
            "Content-Type:application/ssml+xml\r\n"
            "X-Timestamp:{ts}Z\r\n"
            "Path:ssml\r\n\r\n{ssml}"
        ).format(
            rid=request_id,
            ts=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            ssml=self.build_ssml(request, voice),
        )

        chunks: list[bytes] = []
        try:
            with connect(
                self._connect_url(),
                additional_headers=headers,
                open_timeout=timeout,
                close_timeout=5,
            ) as websocket:
                websocket.send(config_message)
                websocket.send(ssml_message)
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AppError(
                            ErrorCode.TTS_TIMEOUT,
                            "Edge TTS 合成超时，请稍后重试。",
                            detail={"provider": self.id},
                            retryable=True,
                        )
                    frame = websocket.recv(timeout=remaining)
                    if isinstance(frame, str):
                        if "Path:turn.end" in frame:
                            break
                        continue
                    # Binary frame: [2-byte BE header length][header][audio]
                    if len(frame) < 2:
                        continue
                    header_length = int.from_bytes(frame[:2], "big")
                    payload = frame[2 + header_length :]
                    if payload:
                        chunks.append(payload)
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized to AppError
            from app.adapters.base import map_upstream_error

            raise map_upstream_error(
                kind="tts", provider=self.id, exception=exc, endpoint="wss://speech.platform.bing.com"
            ) from exc

        if not chunks:
            raise AppError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                "Edge TTS 未返回音频数据，请检查音色名称是否正确。",
                detail={"provider": self.id, "voice": voice},
                retryable=True,
            )
        return b"".join(chunks)

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        self.check_length(request.text)
        audio_format = self.normalize_format(request.audio_format)
        voice = request.voice or self.default_voice

        started = time.perf_counter()
        audio = await asyncio.to_thread(
            self._blocking_synthesize, request, voice, 60.0
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "tts synthesis ok provider=%s voice=%s bytes=%s latency_ms=%s",
            self.id, voice, len(audio), latency_ms,
        )
        return TTSResult(
            audio=audio,
            provider=self.id,
            voice=voice,
            audio_format=audio_format,
            latency_ms=latency_ms,
            content_type="audio/mpeg",
        )