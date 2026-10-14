"""Real end-to-end: upload video -> trim -> enhance -> clone-generate.

Goes through the actual HTTP API with the user's source video, so ffmpeg's
silence-trim + loudnorm run on real speech (not a synthetic sine wave) and
the enhanced clip is fed to MiMo's voiceclone model.
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000/api/v1"
KEY = sys.argv[1]
VIDEO = sys.argv[2]


def call(path, data=None, content_type=None, method=None, timeout=180):
    request = urllib.request.Request(BASE + path, data=data, method=method)
    if content_type:
        request.add_header("Content-Type", content_type)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def upload(video_path):
    boundary = "----t2venh"
    with open(video_path, "rb") as handle:
        payload = handle.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="ref.mp4"\r\n'
        f"Content-Type: video/mp4\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    return call(
        "/media/inspect", data=body,
        content_type=f"multipart/form-data; boundary={boundary}", method="POST",
    )


print("== 1. upload real video ==")
info = upload(VIDEO)
raw_id = info["media_id"]
print(f"   media_id={raw_id[:12]}… kind={info.get('kind')} duration={info.get('duration_ms')}ms")

print("== 2. trim a speech-rich window (skip the intro) ==")
trimmed = call(
    "/media/trim",
    data=json.dumps({"media_id": raw_id, "start_ms": 20000, "end_ms": 32000}).encode(),
    content_type="application/json", method="POST",
)
clip_id = trimmed["media_id"]
print(f"   clip={clip_id[:12]}… bytes={trimmed.get('size_bytes')} dur={trimmed.get('duration_ms')}ms")

print("== 3. enhance (loudnorm + silence trim) on REAL speech ==")
try:
    enhanced = call(
        "/media/enhance",
        data=json.dumps({"media_id": clip_id}).encode(),
        content_type="application/json", method="POST",
    )
except urllib.error.HTTPError as exc:
    print(f"   !! enhance failed HTTP {exc.code}: {exc.read().decode()[:400]}")
    sys.exit(1)

enh_id = enhanced["media_id"]
print(f"   enhanced={enh_id[:12]}… bytes={enhanced.get('size_bytes')} "
      f"dur={enhanced.get('duration_ms')}ms warnings={enhanced.get('warnings')}")

# Sanity: silence removal should never grow the clip much, and the result
# must stay in the cloneable 3-10s band (or at least be >1s).
duration = enhanced.get("duration_ms") or 0
if duration < 1000:
    print("   !! enhanced clip is under 1s — unusable for cloning")
    sys.exit(1)
print(f"   duration check: {duration}ms OK")

print("== 4. clone-generate from the ENHANCED clip ==")
text = "优化后的音色应该这样朗读，声音清晰饱满，没有多余停顿。"
payload = {
    "text": text,
    "llm": {"provider": "mock", "model": "mock-echo-v1"},
    "tts": {
        "provider": "mimo", "api_key": KEY,
        "model": "mimo-v2.5-tts", "voice": "mimo_default", "format": "wav", "speed": 1.0,
    },
    "options": {
        "auto_save": True,
        "reuse_reply_text": text,
        "reference_media_id": enh_id,
    },
}
task = call("/tasks", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            content_type="application/json", method="POST")
task_id = task["task_id"]
print(f"   task={task_id[:12]}…")

state = {}
for _ in range(60):
    time.sleep(3)
    state = call(f"/tasks/{task_id}")
    print(f"   {state.get('status')} {state.get('stage')} {state.get('progress')}% "
          f"{(state.get('message') or '')[:44]}")
    if state.get("status") in {"succeeded", "failed", "canceled"}:
        break

print("== result ==")
print("status :", state.get("status"))
print("elapsed:", state.get("elapsed_ms"), "ms")
if state.get("error"):
    print("error  :", json.dumps(state["error"], ensure_ascii=False)[:400])
record_id = state.get("record_id")
if record_id:
    item = call(f"/history/{record_id}")
    print(f"audio  : {item.get('audio_bytes')} bytes / {item.get('duration_ms')}ms "
          f"degraded={item.get('degraded')}")
    print(f"reply  : {(item.get('reply_text') or '')[:40]}")
