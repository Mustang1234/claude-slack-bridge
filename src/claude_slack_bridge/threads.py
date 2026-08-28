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
import subprocess
import time
import warnings
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows 에는 없다.
    fcntl = None

THREADS_DIR = Path(os.path.expanduser("~/.claude-slack-bridge/threads"))
KEEPER_PROTOCOL = "inbox-v1"
SIDECAR_SUFFIXES = (".inbox.jsonl", ".lock", ".keeper.log")


def _path(thread_ts: str) -> Path:
    # ts 에는 숫자와 점만 들어간다. 경로로 쓰기 전에 그것만 남긴다.
    safe = "".join(ch for ch in thread_ts if ch.isdigit() or ch == ".")
    return THREADS_DIR / f"{safe}.json"


def _sidecar_path(thread_ts: str, suffix: str) -> Path:
    return _path(thread_ts).with_suffix(suffix)


def save(thread_ts: str, data: dict) -> Path:
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(THREADS_DIR, stat.S_IRWXU)
    path = _path(thread_ts)
    # 지킴이와 감시자가 같은 임시 파일을 쓰면 한쪽의 os.replace 뒤에 다른 쪽
    # 임시 파일이 사라져 FileNotFoundError 로 죽는다. 최종 파일의 교체만 공유하고
    # 준비 중인 파일은 프로세스마다 따로 둔다.
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
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


@contextmanager
def _patch_lock(thread_ts: str):
    """replace 되어도 inode 가 유지되는 별도 파일로 상태 갱신을 직렬화한다."""
    lock = _sidecar_path(thread_ts, ".lock")
    try:
        THREADS_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(lock, "a", encoding="utf-8")
    except OSError as e:
        # 잠금 파일을 만들 수 없는 플랫폼에서도 기존의 원자적 replace 동작은
        # 유지한다. 실제 상태 저장 실패는 save()가 그대로 드러낸다.
        warnings.warn(f"상태 파일 잠금 준비 실패, 잠금 없이 계속합니다: {e}", RuntimeWarning)
        yield
        return

    locked = False
    if fcntl is None:
        warnings.warn(
            f"상태 파일 잠금을 지원하지 않아 잠금 없이 계속합니다: {lock}",
            RuntimeWarning,
        )
    else:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            locked = True
        except OSError as e:
            # 잠금 실패 때문에 감시 자체가 사라지는 것보다 경쟁 가능성을 남기고
            # 계속하는 편이 낫다. 대신 진단할 흔적은 반드시 stderr 에 남긴다.
            warnings.warn(f"상태 파일 잠금 실패, 잠금 없이 계속합니다: {e}", RuntimeWarning)
    try:
        yield
    finally:
        if locked:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError as e:
                warnings.warn(f"상태 파일 잠금 해제 실패: {e}", RuntimeWarning)
        try:
            fh.close()
        except OSError as e:
            warnings.warn(f"상태 파일 잠금 닫기 실패: {e}", RuntimeWarning)


def patch(thread_ts: str, **fields) -> dict:
    """일부 필드만 고친다.

    통째로 덮어쓰면, 다른 프로세스가 방금 바꾼 값(연장된 마감 같은 것)을
    되돌려버린다.
    """
    with _patch_lock(thread_ts):
        data = load(thread_ts) or {}
        # patch의 위치 인자와 같은 이름이라 호출부가 thread_ts 필드를 함께 넘길 수
        # 없다. 영속화 이전 기록을 처음 patch 할 때만 파일명에서 식별자를 복원한다.
        data.setdefault("thread_ts", thread_ts)
        data.update(fields)
        save(thread_ts, data)
        return data


def append_inbox(thread_ts: str, msg: dict, summary: str) -> Path:
    """지킴이가 받은 한 메시지를 append-only inbox 에 내구성 있게 남긴다."""
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(THREADS_DIR, stat.S_IRWXU)
    path = _sidecar_path(thread_ts, ".inbox.jsonl")
    record = {
        "ts": msg.get("ts", ""),
        "user": msg.get("user", ""),
        "text": msg.get("text", ""),
        "summary": summary,
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    # 쓰는 도중 강제종료되면 마지막 JSON 한 줄만 반쪽으로 남을 수 있다. 다음
    # 기록을 그대로 붙이면 둘이 한 줄이 되어 새 메시지까지 못 읽으므로, 끊긴 줄은
    # 개행으로 격리하고 완전한 새 줄을 시작한다.
    separator = ""
    try:
        with open(path, "rb") as existing:
            existing.seek(0, os.SEEK_END)
            if existing.tell():
                existing.seek(-1, os.SEEK_END)
                if existing.read(1) != b"\n":
                    separator = "\n"
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(separator + line)
        fh.flush()
        # 커서를 먼저 밀었다가 이 줄이 디스크에 남지 않으면 복구할 길이 없다.
        # fsync 완료를 keeper_seen_ts 전진의 선행 조건으로 둔다.
        os.fsync(fh.fileno())
    return path


def read_inbox(thread_ts: str, after_ts: float) -> list[dict]:
    """커서 뒤의 inbox 메시지를 기록된 순서 그대로 읽는다."""
    path = _sidecar_path(thread_ts, ".inbox.jsonl")
    try:
        fh = open(path, "r", encoding="utf-8")
    except FileNotFoundError:
        return []

    out = []
    seen = set()
    with fh:
        for line in fh:
            try:
                record = json.loads(line)
                record_ts = float(record.get("ts", 0)) if isinstance(record, dict) else 0
                if record_ts <= after_ts or record_ts in seen:
                    continue
            except (json.JSONDecodeError, TypeError, ValueError):
                # 한 줄이 깨져도 뒤에 완전히 기록된 메시지까지 버리면 append-only 로
                # 남긴 이점이 없다. 손상된 줄만 격리한다.
                continue
            # append와 keeper_seen_ts 사이에 죽으면 Slack 재조회로 같은 메시지를 다시
            # 쓸 수 있다. 이는 유실을 막기 위한 at-least-once 결과이므로 읽을 때 거른다.
            seen.add(record_ts)
            out.append(record)
    return out


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
            if path.stat().st_mtime >= cutoff:
                continue
            state = load(path.stem)
            # 오래됐다는 이유만으로 열린 스레드를 지우면 아직 전달하지 않은 inbox도
            # 함께 사라진다. 읽을 수 없는 기록도 닫힘을 증명할 수 없으므로 남긴다.
            if not state or not state.get("closed"):
                continue
            path.unlink()
            # 상태 파일만 지우면 같은 스레드의 수신 기록과 지킴이 로그가 영원히
            # 남는다. 상태의 수명에 딸린 파일은 같은 시점에 함께 거둔다.
            for suffix in SIDECAR_SUFFIXES:
                try:
                    _sidecar_path(path.stem, suffix).unlink()
                except FileNotFoundError:
                    pass
            for tmp in THREADS_DIR.glob(f"{path.name}.*.tmp"):
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
            removed += 1
        except OSError:
            continue

    # 나이만 보고 지우면 조용히 살아 있는 스레드의 파일도 사라진다. 짝 상태가
    # 이미 없고 sidecar 자체도 오래된 경우만 이전 sweep·수동 삭제의 고아로 본다.
    for suffix in SIDECAR_SUFFIXES:
        for sidecar in THREADS_DIR.glob(f"*{suffix}"):
            thread_ts = sidecar.name[:-len(suffix)]
            try:
                if _path(thread_ts).exists() or sidecar.stat().st_mtime >= cutoff:
                    continue
                sidecar.unlink()
            except OSError:
                continue
    return removed


def _proc_is(pid, needle: str) -> bool:
    if not pid:
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "claude-slack-bridge" in out and needle in out


def keeper_alive(thread_ts: str) -> bool:
    """이 스레드를 지키는 지킴이가 돌고 있는지."""
    return _proc_is((load(thread_ts) or {}).get("keeper_pid"), "keeper")


def inbox_keeper_alive(thread_ts: str) -> bool:
    """durable inbox 프로토콜을 쓰는 지킴이가 실제로 돌고 있는지."""
    state = load(thread_ts) or {}
    return (
        state.get("keeper_protocol") == KEEPER_PROTOCOL
        and _proc_is(state.get("keeper_pid"), "keeper")
    )


def watcher_alive(thread_ts: str) -> bool:
    """이 스레드를 지키는 감시자가 실제로 돌고 있는지.

    pid 만 보고 판정하면 안 된다. 죽은 감시자의 pid 를 다른 프로세스가 이미
    차지했을 수 있고, 그러면 살아있다고 오판해 감시를 영영 안 띄운다.
    명령줄에 이 도구 이름이 있는지까지 대조한다.
    """
    pid = (load(thread_ts) or {}).get("watcher_pid")
    if not pid:
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "claude-slack-bridge" in out and "watch" in out


def inbox_tailed(thread_ts: str) -> bool:
    """inbox 를 열어 둔 리더(Monitor 의 tail 류)가 붙어 있는지.

    Monitor 방식 수신자는 상태 파일에 아무것도 등록하지 않으므로 pid 로는
    보이지 않는다. 등록을 요구하는 대신 파일을 열어 둔 프로세스의 존재로
    듣는 중임을 판정한다. 지킴이 자신은 append 순간에만 잠깐 여니 자기
    pid 는 제외한다.
    """
    path = _sidecar_path(thread_ts, ".inbox.jsonl")
    if not path.exists():
        return False
    try:
        proc = subprocess.run(
            ["lsof", "-t", "--", str(path)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    pids = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    pids.discard(str(os.getpid()))
    return bool(pids)


def session_listening(thread_ts: str) -> bool:
    """세션 쪽 수신자가 듣고 있는지 — 폴백 watch 또는 inbox 를 tail 하는 Monitor."""
    return watcher_alive(thread_ts) or inbox_tailed(thread_ts)
