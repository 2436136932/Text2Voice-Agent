"""Build the request body for :mod:`scripts.e2e_generate.sh`.

Kept in Python (rather than a shell heredoc) because MSYS rewrites the quotes
inside JSON, which makes the server reject the body as unparsable.
"""

from __future__ import annotations

import json
import sys

#: Override any of these from the environment to test a different provider.
DEFAULTS = {
    "text": "愿你逐梦三万里，愿你剑挡百万师。",
    "llm_provider": "custom",
    "llm_model": "deepseek-v4-flash-vision",
    "llm_base_url": "https://api.deepseek.com",
    "tts_provider": "mimo",
    "tts_model": "mimo-v2.5-tts",
    "tts_voice": "mimo_default",
    "tts_format": "wav",
}


def main() -> int:
    import os

    if len(sys.argv) < 3:
        print("usage: e2e_payload.py <api-key> <output-path>", file=sys.stderr)
        return 2

    api_key, out_path = sys.argv[1], sys.argv[2]
    cfg = {key: os.environ.get(f"T2V_{key.upper()}", value) for key, value in DEFAULTS.items()}

    payload = {
        "text": cfg["text"],
        "llm": {
            "provider": cfg["llm_provider"],
            "api_key": api_key,
            "model": cfg["llm_model"],
            "base_url": cfg["llm_base_url"],
            "temperature": 0.7,
            "max_tokens": 512,
        },
        "tts": {
            "provider": cfg["tts_provider"],
            "api_key": api_key,
            "model": cfg["tts_model"],
            "voice": cfg["tts_voice"],
            "format": cfg["tts_format"],
            "speed": 1.0,
        },
        "options": {"auto_save": True, "include_audio_base64": False},
    }

    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
