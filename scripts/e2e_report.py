"""Summarise a finished task: terminal state plus the audio it produced."""

from __future__ import annotations

import json
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: e2e_report.py <task-json>", file=sys.stderr)
        return 2

    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)

    print("status  :", data.get("status"))
    print("stage   :", data.get("stage"))
    print("elapsed :", data.get("elapsed_ms"), "ms")

    result = data.get("result") or {}
    for key in ("audio_url", "audio_bytes", "duration_ms", "audio_format", "degraded"):
        if key in result:
            print(f"{key:12}:", result[key])
    if result.get("degraded_reason"):
        print("degraded_reason:", result["degraded_reason"])
    if result.get("reply_text"):
        print("reply_text :", result["reply_text"][:100])

    error = data.get("error")
    if error:
        print("error   :", json.dumps(error, ensure_ascii=False)[:500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
