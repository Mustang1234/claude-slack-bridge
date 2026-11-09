"""토큰·메서드별 시작 슬롯을 예약한다. HTTP와 대기는 잠금 밖에서 한다."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time

RATE_DIR = Path(os.environ.get("CLAUDE_SLACK_BRIDGE_CHANNELS_DIR")
                or os.path.expanduser("~/.claude-slack-bridge/channels"))
# Tier 3의 50회/분. 메서드별 예약으로 읽기가 알림 전송을 막지 않는다.
MIN_INTERVAL = 1.2
_LOCAL_LOCK = threading.Lock()


def retry_delay(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 30.0
    return min(300.0, max(0.0, value)) if math.isfinite(value) else 0.0


@contextmanager
def _state(token, method):
    import fcntl
    with _LOCAL_LOCK:
        RATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = hashlib.sha256(f"{token}:{method}".encode()).hexdigest()
        with open(RATE_DIR / f"rate-{name}.lock", "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                try:
                    raw = json.loads(fh.read() or "{}")
                except ValueError:
                    raw = {}
                if not isinstance(raw, dict):
                    raw = {}
                now = time.time()
                state = {}
                for key in ("next", "cooldown"):
                    try:
                        value = float(raw.get(key, 0))
                    except (ValueError, TypeError):
                        value = 0.0
                    state[key] = min(value, now + 300) if math.isfinite(value) and value >= 0 else 0.0
                yield state
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps(state))
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def slot(token, method="", cooldown_only=False):
    interval = 1.0 if method == "chat.postMessage" else MIN_INTERVAL
    with _state(token, method) as state:
        scheduled = max(time.time(), state["cooldown"], 0 if cooldown_only else state["next"])
        if not cooldown_only:
            state["next"] = scheduled + interval
    while True:
        delay = scheduled - time.time()
        if delay > 0:
            time.sleep(min(60.0, delay))
            continue
        # 예약 후 다른 호출이 받은 429도 반영한다. 대기자들은 다시 슬롯을 예약한다.
        with _state(token, method) as state:
            if state["cooldown"] > scheduled:
                scheduled = max(time.time(), state["cooldown"], 0 if cooldown_only else state["next"])
                if not cooldown_only:
                    state["next"] = scheduled + interval
                continue
        break

    def defer(seconds):
        with _state(token, method) as state:
            deadline = time.time() + retry_delay(seconds)
            state["cooldown"] = max(state["cooldown"], deadline)
            state["next"] = max(state["next"], deadline)

    yield defer
