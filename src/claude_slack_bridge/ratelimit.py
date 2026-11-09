"""같은 토큰의 API 호출 간격과 429 대기 시각을 프로세스 사이에 공유한다."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import threading
import time

RATE_DIR = Path(os.environ.get("CLAUDE_SLACK_BRIDGE_CHANNELS_DIR")
                or os.path.expanduser("~/.claude-slack-bridge/channels"))
MIN_INTERVAL = 1.0
_LOCAL_LOCK = threading.Lock()


@contextmanager
def slot(token: str):
    # 원문 토큰은 파일명/본문에 남기지 않는다. flock을 지원하는 Unix에서 실행한다.
    import fcntl
    with _LOCAL_LOCK:
        RATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = hashlib.sha256(token.encode()).hexdigest()
        with open(RATE_DIR / f"rate-{name}.lock", "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0)
            try:
                deadline = float(fh.read() or 0)
            except ValueError:
                deadline = 0.0
            while deadline > time.time():
                time.sleep(min(60.0, deadline - time.time()))
            delay = [MIN_INTERVAL]
            try:
                yield lambda seconds: delay.__setitem__(0, max(delay[0], seconds))
            finally:
                fh.seek(0)
                fh.truncate()
                fh.write(str(time.time() + delay[0]))
                fh.flush()
                os.fsync(fh.fileno())
                fcntl.flock(fh, fcntl.LOCK_UN)
