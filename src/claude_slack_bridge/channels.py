"""채널 단위 멘션 감시.

스레드 감시는 이미 연 스레드의 답글만 읽는다. 그래서 채널 최상위에서 봇을 처음
부르거나, 남이 연 스레드 안에서 봇을 처음 부르면 아무도 모른다. 이 모듈이 그 구멍을
메운다 — `monitor --all` 하나가 등록된 채널을 돌면서 멘션을 찾아 inbox 에 적고
세션에 흘린다. 세션은 그 멘션의 스레드에 attach 해서 이후는 기존 지킴이에게 맡긴다.

스레드 안 멘션은 이렇게 찾는다. conversations.history 는 최상위 메시지만 주지만
스레드 부모에는 latest_reply 가 붙어 온다. 최근 THREAD_WINDOW 안의 부모 중
latest_reply 가 바뀐 것만 conversations.replies 로 연다. 브리지가 이미 쥔 스레드는
지킴이 몫이라 건너뛴다 — 둘이 같은 답글을 세션에 두 번 흘리지 않게.

커서는 파일에 둔다. 세션이 재시작돼 Monitor 가 다시 뜨면 끊긴 사이 온 멘션까지
이어서 본다. 처음 띄울 때는 FIRST_BACKFILL 만큼만 거슬러 본다 — 채널에 초대되자마자
멘션하면 감시 등록보다 멘션이 먼저 와서 놓치기 때문이다. 그보다 오래된 대화는 흘리지 않는다.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import stat
import time
from pathlib import Path

from . import chat, threads

CHANNELS_DIR = Path(
    os.environ.get("CLAUDE_SLACK_BRIDGE_CHANNELS_DIR")
    or os.path.expanduser("~/.claude-slack-bridge/channels")
)
POLL = 10.0
DISCOVER_POLL = 60.0
THREAD_WINDOW = 7 * 86400   # 부모가 이보다 오래된 스레드의 답글 멘션은 보지 않는다
FIRST_BACKFILL = 30 * 60    # 처음 띄울 때 거슬러 보는 시간


def _safe(channel: str) -> str:
    return "".join(ch for ch in channel if ch.isalnum() or ch in "-_")


def state_path(channel: str) -> Path:
    return CHANNELS_DIR / f"{_safe(channel)}.json"


def inbox_path(channel: str) -> Path:
    return CHANNELS_DIR / f"{_safe(channel)}.inbox.jsonl"


def lock_path(channel: str) -> Path:
    return CHANNELS_DIR / f"{_safe(channel)}.lock"


def _ensure_dir() -> None:
    CHANNELS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CHANNELS_DIR, stat.S_IRWXU)


def load_state(channel: str) -> dict:
    try:
        data = json.loads(state_path(channel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(channel: str, data: dict) -> None:
    _ensure_dir()
    path = state_path(channel)
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def append_inbox(channel: str, record: dict) -> Path:
    _ensure_dir()
    return threads.append_jsonl(inbox_path(channel), record)


def mention_record(channel: str, msg: dict, bot_user_id: str, parent: str | None) -> dict:
    """세션에 흘릴 한 줄. thread_ts 는 attach 할 스레드다 — 최상위면 그 메시지 자신."""
    return {
        "source": "channel",
        "mentioned": f"<@{bot_user_id}>" in (msg.get("text") or ""),
        "channel": channel,
        "ts": msg.get("ts", ""),
        "thread_ts": parent or msg.get("ts", ""),
        "in_thread": parent is not None,
        "user": msg.get("user", ""),
        "text": chat.strip_mention(msg.get("text", ""), bot_user_id),
        "summary": chat.describe(msg).replace("\n", " ")[:120],
        "files": len(msg.get("files") or []),
    }


def held_threads(channel: str) -> set[str]:
    """브리지가 이미 지키는(열린) 이 채널의 스레드."""
    return {
        str(t.get("thread_ts")) for t in chat.open_threads()
        if t.get("channel") == channel
    }


def poll(
    channel: str,
    state: dict,
    history: list[dict],
    fetch_replies,
    bot_user_id: str,
    owner_id: str | list[str],
    listeners: list[str],
    held: set[str],
    now: float,
    handle_message=None,
) -> tuple[list[dict], dict]:
    """한 번의 폴링 판정. Slack 호출은 밖에서 넣어 시험할 수 있게 한다.

    history: 최근 THREAD_WINDOW 의 최상위 메시지들.
    fetch_replies(parent_ts, oldest) -> 그 스레드 답글들.
    돌려주는 것은 (새 멘션 기록들, 다음 상태).
    """
    first = not state
    start = f"{now - FIRST_BACKFILL:.6f}"
    cursor = str(state.get("cursor") or start)
    seen: dict = dict(state.get("threads") or {})   # parent_ts -> 마지막으로 확인한 답글 ts
    out: list[dict] = []
    streaks = dict(state.get("bot_streaks") or {})

    def for_me(m: dict) -> bool:
        return chat.is_for_me(m, bot_user_id, owner_id, require_mention=True, listeners=listeners)

    for m in sorted(history, key=lambda m: float(m.get("ts", 0))):
        ts = str(m.get("ts", ""))
        if not ts:
            continue
        if float(ts) > float(cursor):
            cursor = ts
            if ts in held:
                continue
            if handle_message and handle_message(m, ts):
                pass
            elif for_me(m):
                out.append(mention_record(channel, m, bot_user_id, None))

        latest = m.get("latest_reply")
        if not latest:
            seen.setdefault(ts, ts)
            continue
        last = seen.get(ts)
        if last is None and first:
            if float(latest) <= float(start):
                seen[ts] = latest    # 처음 띄울 때 FIRST_BACKFILL 보다 오래된 답글은 흘리지 않는다
                continue
            last = start
        if last is not None and float(latest) <= float(last):
            continue
        if ts in held:
            continue
        replies = fetch_replies(ts, last or ts)
        fresh = [r for r in replies if str(r.get("ts")) != ts and float(r.get("ts", 0)) > float(last or ts)]
        seen[ts] = max([str(last or ts)] + [str(r["ts"]) for r in fresh], key=float)
        context = [{"ts": str(-i - 1), "bot_id": "prior"} for i in range(int(streaks.get(ts, 0)))] + fresh
        streak = int(streaks.get(ts, 0))
        for r in sorted(fresh, key=lambda m: float(m["ts"])):
            streak = min(chat.BOT_TURN_LIMIT, streak + 1) if r.get("bot_id") else 0
        streaks[ts] = streak
        for r in replies:
            rts = str(r.get("ts", ""))
            if rts == ts or (last and float(rts) <= float(last)):
                continue
            if handle_message and handle_message(r, ts):
                continue
            if chat.is_for_me(r, bot_user_id, owner_id, require_mention=True,
                              listeners=listeners, context=context):
                out.append(mention_record(channel, r, bot_user_id, ts))

    cutoff = now - THREAD_WINDOW
    seen = {k: v for k, v in seen.items() if float(k) >= cutoff}
    return out, {"cursor": cursor, "threads": seen, "bot_streaks": {k: v for k, v in streaks.items() if k in seen}}


def find_mention(message_ts: str) -> dict | None:
    """채널 inbox 에서 그 ts 의 멘션 한 건을 찾는다. attach 의 origin 판정용."""
    target = str(message_ts).strip()
    if not target or not CHANNELS_DIR.exists():
        return None
    for path in sorted(CHANNELS_DIR.glob("*.inbox.jsonl")):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict) and str(record.get("ts", "")) == target:
                        return record
        except OSError:
            continue
    return None


WATCHED_NAME = "watched.json"


def watched() -> dict[str, dict]:
    """감시하기로 등록된 채널. 세션이 재시작돼도 이 목록으로 감시를 다시 띄운다."""
    try:
        data = json.loads((CHANNELS_DIR / WATCHED_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def set_watched(channel: str, on: bool, name: str = "") -> dict[str, dict]:
    import fcntl
    _ensure_dir()
    with open(CHANNELS_DIR / "watched-list.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _set_watched(channel, on, name)


def _set_watched(channel: str, on: bool, name: str) -> dict[str, dict]:
    _ensure_dir()
    data = watched()
    if on:
        data[channel] = {"name": name or data.get(channel, {}).get("name", ""), "since": time.time()}
    else:
        data.pop(channel, None)
    path = CHANNELS_DIR / WATCHED_NAME
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return data


def monitor_command(channel: str | None) -> list[str]:
    """채널 Monitor 명령. None이면 통합 --all. 허용 예: Bash(*claude-slack-bridge monitor --all*)."""
    base = threads.keeper_command("0", subcommand="monitor")[:-2]   # "--thread 0" 을 뗀다
    return base + (["--channel", channel] if channel else ["--all"])


def window_start(now: float | None = None) -> str:
    return f"{(now or time.time()) - THREAD_WINDOW:.6f}"


TRACKED_NAME = "tracked.json"


def acquire_lock(channel: str):
    """채널별 단일 scanner. --all/--channel/--thread-poll 모두 같은 잠금."""
    import fcntl
    _ensure_dir()
    fh = open(lock_path(channel), "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def tracked() -> dict[str, dict]:
    return load_state("tracked")


def thread_key(channel: str, thread: str) -> str:
    # 채널 ID와 ts를 각각 보존해 1.23 / 12.3 같은 충돌을 피한다.
    if not channel.startswith(("C", "G")) or not channel.isalnum():
        raise ValueError("채널 ID는 C 또는 G로 시작해야 합니다")
    if not thread or any(c not in "0123456789." for c in thread) or thread.count(".") != 1:
        raise ValueError("스레드 ts는 숫자.숫자 형식이어야 합니다")
    float(thread)
    return f"thread-{channel}-{thread.replace('.', '_')}"


def set_tracked(channel: str, thread: str, on: bool, any_reply: bool = False) -> dict:
    import fcntl
    key = thread_key(channel, thread)
    _ensure_dir()
    with open(CHANNELS_DIR / "tracked-list.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = tracked()
        if on:
            data[key] = {"channel": channel, "thread_ts": thread, "any_reply": any_reply}
            save_state(key, {"cursor": f"{time.time():.6f}", "bot_streak": 0, "generation": time.time_ns()})
        else:
            data.pop(key, None)
            state_path(key).unlink(missing_ok=True)
        save_state("tracked", data)
        return data


def thread_poll(channel, thread, state, replies, bot_user_id, owners, listeners, any_reply=False, bot_id=""):
    """명시 추적은 나이 제한 없이 읽되, any-reply도 발신자 권한을 유지한다."""
    cursor = str(state["cursor"])
    out = []
    allowed = set(owners) | set(listeners)
    for message in sorted(replies, key=lambda m: float(m.get("ts", 0))):
        ts = str(message.get("ts") or "0")
        if float(ts) <= float(cursor):
            continue
        cursor = ts
        profile = message.get("bot_profile") or {}
        if bot_id and (message.get("bot_id") == bot_id or profile.get("id") == bot_id):
            continue
        if ts == thread or message.get("user") not in allowed:
            continue
        # 증분 조회 이전의 연속 봇 수를 이어 붙인다. 첫 조회는 0부터 센다.
        prefix = [{"ts": str(-i - 1), "bot_id": "prior"}
                  for i in range(int(state.get("bot_streak", 0)))]
        context = prefix + [m for m in replies if float(m.get("ts", 0)) > float(state["cursor"])
                            and str(m.get("ts")) != thread]
        if chat.is_for_me(message, bot_user_id, owners, require_mention=not any_reply,
                          listeners=listeners, context=context):
            record = mention_record(channel, message, bot_user_id, thread)
            record["files"] = message.get("files") or []
            out.append(record)
    streak = int(state.get("bot_streak", 0))
    for message in sorted(replies, key=lambda m: float(m.get("ts", 0))):
        if float(message.get("ts", 0)) > float(state["cursor"]) and str(message.get("ts")) != thread:
            streak = min(chat.BOT_TURN_LIMIT, streak + 1) if message.get("bot_id") else 0
    return out, {"cursor": cursor, "bot_streak": streak}


def deliver_once(channel, thread, message_ts, write):
    """지킴이/감시자의 durable inbox를 전달 근거로 삼아 인계 중복을 막는다."""
    import fcntl
    _ensure_dir()
    key = f"delivered-{channel}"
    with open(lock_path(key), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        delivered = recent_ledger(key)
        ts = str(message_ts)
        if ts in delivered:
            return False
        # 구버전 지킴이 기록 및 inbox append 후 ledger 저장 전 종료도 복구한다.
        already = any(str(m.get("ts")) == ts for m in threads.read_inbox(thread, 0))
        if not already:
            try:
                with open(inbox_path(channel), encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(record, dict) and str(record.get("ts")) == ts:
                            already = True
                            break
            except FileNotFoundError:
                pass
        if not already:
            write()
        delivered[ts] = time.time()
        save_state(key, delivered)
        return not already


def valid_tracked(on_error=None):
    result = []
    for key, item in tracked().items():
        try:
            if not isinstance(item, dict) or not isinstance(item.get("channel"), str) or not isinstance(item.get("thread_ts"), str):
                raise ValueError("잘못된 추적 항목")
            if key != thread_key(item["channel"], item["thread_ts"]):
                raise ValueError("추적 키와 대상 불일치")
            result.append(item)
        except Exception as exc:
            if on_error:
                on_error(f"tracked:{key}", exc)
    return result


def recent_ledger(key):
    """전달・명령 원장은 처리 시각 기준 7일만 보관한다. 옛 bool 값은 ts로 이관한다."""
    import math
    data = load_state(key)
    cutoff = time.time() - THREAD_WINDOW
    recent = {}
    for ts, value in data.items():
        try:
            stamp = float(ts if isinstance(value, bool) else value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(stamp) and stamp >= cutoff:
            recent[ts] = stamp
    if recent != data:
        save_state(key, recent)
    return recent


@contextmanager
def unchanged_tracking(key, snapshot):
    """조회 도중 해제/재등록됐다면 이전 조회가 새 커서를 덮어쓰지 않는다."""
    import fcntl
    _ensure_dir()
    with open(CHANNELS_DIR / "tracked-list.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield load_state(key) == snapshot
