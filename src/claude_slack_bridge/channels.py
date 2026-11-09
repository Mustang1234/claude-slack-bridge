"""채널 단위 멘션 감시.

스레드 감시는 이미 연 스레드의 답글만 읽는다. 그래서 채널 최상위에서 봇을 처음
부르거나, 남이 연 스레드 안에서 봇을 처음 부르면 아무도 모른다. 이 모듈이 그 구멍을
메운다 — 채널마다 `monitor --channel <ID>` 하나가 돌면서 멘션을 찾아 inbox 에 적고
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
    return "".join(ch for ch in channel if ch.isalnum())


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

    def for_me(m: dict) -> bool:
        return chat.is_for_me(m, bot_user_id, owner_id, require_mention=True, listeners=listeners)

    for m in sorted(history, key=lambda m: float(m.get("ts", 0))):
        ts = str(m.get("ts", ""))
        if not ts:
            continue
        if float(ts) > float(cursor):
            cursor = ts
            if for_me(m):
                out.append(mention_record(channel, m, bot_user_id, None))

        latest = m.get("latest_reply")
        if not latest:
            continue
        last = seen.get(ts)
        if last is None and first:
            if float(latest) <= float(start):
                seen[ts] = latest    # 처음 띄울 때 FIRST_BACKFILL 보다 오래된 답글은 흘리지 않는다
                continue
            last = start
        if last is not None and float(latest) <= float(last):
            continue
        seen[ts] = latest
        if ts in held:
            continue
        replies = fetch_replies(ts, last or ts)
        for r in replies:
            rts = str(r.get("ts", ""))
            if rts == ts or (last and float(rts) <= float(last)):
                continue
            if chat.is_for_me(r, bot_user_id, owner_id, require_mention=True,
                              listeners=listeners, context=replies):
                out.append(mention_record(channel, r, bot_user_id, ts))

    cutoff = now - THREAD_WINDOW
    seen = {k: v for k, v in seen.items() if float(k) >= cutoff}
    return out, {"cursor": cursor, "threads": seen}


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
    """채널 Monitor 명령. channel 이 None 이면 --discover."""
    base = threads.keeper_command("0", subcommand="monitor")[:-2]   # "--thread 0" 을 뗀다
    return base + (["--channel", channel] if channel else ["--discover"])


def window_start(now: float | None = None) -> str:
    return f"{(now or time.time()) - THREAD_WINDOW:.6f}"
