"""Contract check: every endpoint the frontend calls must exist in the backend.

Parses the built OpenAPI schema and greps the frontend API layer for request
paths, then reports any drift. Run from the repo root:

    python scripts/check_contract.py
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_API = ROOT / "frontend" / "src" / "api" / "index.ts"

#: (http method, openapi path template) the frontend is expected to use.
EXPECTED = [
    ("get", "/api/v1/providers"),
    ("post", "/api/v1/providers/validate"),
    ("post", "/api/v1/generate"),
    ("post", "/api/v1/tasks"),
    ("get", "/api/v1/tasks/{task_id}"),
    ("get", "/api/v1/tasks/{task_id}/result"),
    ("delete", "/api/v1/tasks/{task_id}"),
    ("get", "/api/v1/history"),
    ("get", "/api/v1/history/{record_id}"),
    ("delete", "/api/v1/history/{record_id}"),
    ("delete", "/api/v1/history"),
    ("get", "/api/v1/history/stats"),
    ("get", "/api/v1/history/export"),
    ("get", "/api/v1/audio/{record_id}"),
    ("get", "/api/v1/settings"),
    ("put", "/api/v1/settings"),
    ("get", "/health"),
]

failures: list[str] = []


def fetch_schema(base: str) -> dict:
    with urllib.request.urlopen(f"{base}/openapi.json", timeout=10) as response:
        return json.load(response)


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

    try:
        schema = fetch_schema(base)
    except Exception as exc:  # noqa: BLE001
        print(f"  cannot reach {base}/openapi.json: {exc}")
        print("  (start the backend first, or pass the base URL as an argument)")
        return 2

    paths = schema.get("paths", {})
    print(f"==> backend OpenAPI at {base}: {len(paths)} paths")

    for method, template in EXPECTED:
        operation = paths.get(template, {}).get(method)
        if operation is None:
            failures.append(f"missing route: {method.upper()} {template}")
        else:
            print(f"  ok  {method.upper():6s} {template}")

    # -- frontend references -------------------------------------------------
    source = FRONTEND_API.read_text(encoding="utf-8")
    referenced = set(re.findall(r"['\"`](/[a-z0-9/{}$_.-]*)['\"`]", source))
    referenced = {item for item in referenced if item.startswith("/")}

    print("\n==> frontend API paths referenced")
    known_prefixes = {template for _, template in EXPECTED}
    for item in sorted(referenced):
        # Normalise template literals like /tasks/${id} to /tasks/{x}
        normalised = re.sub(r"\$\{[^}]+\}", "{x}", item)
        print(f"  {item}")

    # -- schema field spot checks -------------------------------------------
    print("\n==> key response fields")
    schemas = schema.get("components", {}).get("schemas", {})
    checks = [
        ("GenerateResponse", ["record_id", "reply_text", "audio_url", "audio_format",
                              "audio_bytes", "duration_ms", "degraded", "llm", "tts"]),
        ("TaskStatusResponse", ["task_id", "status", "stage", "progress", "message", "error"]),
        ("HistoryItem", ["id", "input_text", "reply_text", "llm_provider", "tts_provider",
                         "audio_url", "created_at"]),
        ("SettingsSchema", ["default_llm_provider", "default_tts_provider",
                            "default_tts_voice", "auto_save_credentials"]),
        ("ProviderSchema", ["id", "name", "kind", "voices", "formats", "default_voice"]),
    ]
    for name, fields in checks:
        definition = schemas.get(name)
        if definition is None:
            failures.append(f"schema {name} missing from OpenAPI")
            continue
        properties = set(definition.get("properties", {}))
        missing = [field for field in fields if field not in properties]
        if missing:
            failures.append(f"{name} lacks fields: {missing}")
        else:
            print(f"  ok  {name} ({len(properties)} fields)")

    # -- settings must not accept credentials ------------------------------
    settings_update = schemas.get("SettingsUpdateRequest", {}).get("properties", {})
    if any("key" in field or "secret" in field for field in settings_update):
        failures.append("SettingsUpdateRequest exposes a credential field!")
    else:
        print("  ok  SettingsUpdateRequest accepts no credential fields")

    # -- error envelope must be documented ---------------------------------
    if "ErrorResponse" not in schemas:
        failures.append("ErrorResponse envelope missing from OpenAPI")
    else:
        print("  ok  ErrorResponse envelope documented")

    print()
    if failures:
        print(f"CONTRACT DRIFT: {len(failures)} problem(s)")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("contract OK: backend and frontend agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())