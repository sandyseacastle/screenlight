"""Stand-in for beacon/helper.py in tests: same JSON-lines protocol, no Bluetooth, no root.

Environment:
  FAKE_MODE    ok (default) | exit126 | crash_after_hello | adapter_off | slow_capture | slow_ready
  FAKE_BUSY    comma-separated instances that another program grabs after hello (advertise -> slot_busy)
  FAKE_ACTIVE  comma-separated advertising instances already in use by "another program"
  FAKE_FRAMES  comma-separated hex frames to emit on capture
  FAKE_LOG     file to append every request to (one JSON object per line)
"""
import json
import os
import sys

mode = os.environ.get("FAKE_MODE", "ok")
active = [int(x) for x in os.environ.get("FAKE_ACTIVE", "").split(",") if x]
frames = [x for x in os.environ.get("FAKE_FRAMES", "").split(",") if x]
log = os.environ.get("FAKE_LOG")
busy = [int(x) for x in os.environ.get("FAKE_BUSY", "").split(",") if x]


def say(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


if mode == "exit126":
    sys.exit(126)
if mode == "slow_ready":  # the user is still typing the password
    import time
    time.sleep(2)
say({"event": "ready"})
for line in sys.stdin:
    request = json.loads(line)
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(request) + "\n")
    op = request["op"]
    if op == "hello":
        say({"ok": True, "instances": 4, "active": active, "max_len": 31})
        if mode == "crash_after_hello":
            sys.exit(1)
    elif op == "advertise" and request["slot"] in busy:
        say({"ok": False, "error": "slot_busy", "detail": "in use by another program"})
    elif op == "advertise" and mode == "adapter_off":
        say({"ok": False, "error": "adapter_off", "detail": "not powered"})
    elif op == "capture":
        if mode == "slow_capture":
            import time
            time.sleep(request["seconds"])
        for frame in frames:
            say({"event": "frame", "frame": frame})
        say({"ok": True})
    else:
        say({"ok": True})
