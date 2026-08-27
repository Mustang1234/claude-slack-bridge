"""스레드 상태를 파일로 보관한다.

메모리에 두면 안 되는 이유가 둘이다.

하나, 감시자는 답글을 받으면 **종료해서** 세션을 깨운다. 다시 띄울 때 마감이
언제였는지 모르면 그때부터 4시간을 새로 세게 된다. 마감이 사실상 없어진다.

둘, 마감을 아는 쪽(MCP 서버)과 마감을 지키는 쪽(감시자)이 다른 프로세스다.
`slack_chat_extend` 로 연장한 것이 감시자에게 보이지 않으면, 연장해 두고도
감시자가 예정대로 스레드를 닫아버린다.

파일 하나가 그 둘을 잇는다.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

THREADS_DIR = Path(os.path.expanduser("~/.claude-slack-bridge/threads"))


def _path(thread_ts: str) -> Path:
    # ts 에는 숫자와 점만 들어간다. 경로로 쓰기 전에 그것만 남긴다.
    safe = "".join(ch for ch in thread_ts if ch.isdigit() or ch == ".")
    return THREADS_DIR / f"{safe}.json"


def save(thread_ts: str, data: dict) -> Path:
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(THREADS_DIR, stat.S_IRWXU)
    path = _path(thread_ts)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def load(thread_ts: str) -> dict | None:
    path = _path(thread_ts)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def patch(thread_ts: str, **fields) -> dict:
    """일부 필드만 고친다.

    통째로 덮어쓰면, 다른 프로세스가 방금 바꾼 값(연장된 마감 같은 것)을
    되돌려버린다.
    """
    data = load(thread_ts) or {}
    data.update(fields)
    save(thread_ts, data)
    return data


def drop(thread_ts: str) -> None:
    try:
        _path(thread_ts).unlink()
    except FileNotFoundError:
        pass


def sweep(max_age_days: int = 7) -> int:
    """닫힌 지 오래된 기록을 지운다.

    스레드 자체는 Slack 에 남는다. 여기서 지우는 것은 로컬 기록뿐이라
    대화 내용이 사라지지 않는다.
    """
    if not THREADS_DIR.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for path in THREADS_DIR.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
