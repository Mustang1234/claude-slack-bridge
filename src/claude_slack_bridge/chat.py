"""폰과의 양방향 대화.

ntfy 시절에는 프로세스가 둘이었다. 채널을 붙들고 받아 적는 보유자와, 새 줄이
생기면 종료해서 Claude 를 깨우는 감시자. 브로커에 이력이 없어 누군가는 계속
받아 적고 있어야 했기 때문이다.

Slack 에는 이력이 남는다. 그래서 받아 적는 쪽이 통째로 사라지고, 깨우는 쪽만
남는다 — 감시자가 죽어 있는 동안 온 메시지도 스레드에 그대로 있으므로, 다시
띄우면 그동안 온 것을 본다.

마감은 파일에 둔다(threads.py). 감시자는 답글을 받으면 종료해서 세션을 깨우고
다시 뜨는데, 마감이 메모리에 있으면 그때마다 처음부터 다시 세어 사실상 없는
것이 된다.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from . import slack
from . import threads

POLL_INTERVAL = 5.0       # Slack 레이트리밋은 넉넉하다. ntfy 의 25초를 따를 이유가 없다.
WARN_LEAD = 10 * 60       # 마감 10분 전 예고
AUTO_EXTEND_HOURS = 2.0   # 예고 창에 답이 오면 자리에 있다는 뜻이므로 자동 연장
DEFAULT_HOURS = 10.0    # 하루 일과를 덮는 길이. 짧으면 자꾸 끊겨 되레 성가시다.

def attach(
    token: str,
    thread_ts: str,
    channel: str = "",
    hours: float | None = None,
    label: str | None = None,
) -> Chat:
    """이미 있는 스레드에 이 세션을 묶는다.

    스레드를 여는 것과 붙는 것은 다른 일이다. 이 구분이 없으면 두 경우가 막힌다.

      - 세션이 재시작되면 자기가 열어둔 스레드로 못 돌아온다. `_chat` 은 서버
        프로세스 메모리에 있고 서버는 세션과 함께 죽기 때문이다.
      - 다른 사람/다른 세션이 연 스레드를 이어받을 수 없다.

    둘 다 "머리글을 하나 더 올린다" 로 우회하면 폰에 같은 작업의 스레드가
    쌓인다. 붙는 길이 따로 있어야 한다.

    기록이 있으면 마감·라벨을 그대로 이어받고, 없으면(영속화 이전에 열린
    스레드) 지금 기준으로 새로 만든다.
    """
    global _chat
    state = threads.load(thread_ts) or {}
    if state.get("closed"):
        raise NoChat(f"이미 닫힌 스레드입니다: {thread_ts}")

    channel = channel or state.get("channel") or ""
    if not channel:
        raise NoChat("어느 대화의 스레드인지 알 수 없습니다. channel 을 함께 주세요.")

    label = label or state.get("label") or "Claude 세션"
    if hours is not None:
        deadline = time.time() + hours * 3600
    elif state.get("deadline"):
        deadline = float(state["deadline"])
    else:
        deadline = time.time() + DEFAULT_HOURS * 3600

    _chat = Chat(
        channel=channel,
        thread_ts=thread_ts,
        deadline=deadline,
        label=label,
        header=header_text(label, deadline),
    )
    threads.save(thread_ts, {
        **state,
        "channel": channel,
        "thread_ts": thread_ts,
        "label": label,
        "deadline": deadline,
        "warned": bool(state.get("warned")),
        "closed": False,
        "require_mention": state.get("require_mention", not channel.startswith("D")),
        "owner_id": state.get("owner_id", ""),
    })
    return _chat


def open_threads() -> list[dict]:
    """아직 닫히지 않은 스레드 기록. 붙을 대상을 찾을 때 쓴다."""
    out = []
    for path in sorted(threads.THREADS_DIR.glob("*.json")) if threads.THREADS_DIR.exists() else []:
        data = threads.load(path.stem)
        if data and not data.get("closed"):
            out.append(data)
    out.sort(key=lambda d: float(d.get("thread_ts") or 0))
    return out


CLOSE_MARK = "🔒 *종료된 스레드*"


@dataclass
class Chat:
    channel: str
    thread_ts: str          # 이 세션의 스레드. 슬롯 번호를 대신하는 식별자다.
    deadline: float
    label: str = ""
    header: str = ""        # 머리글 원문. 닫을 때 취소선을 그으려면 필요하다.
    warned: bool = False
    seen: set[str] = field(default_factory=set)

    @property
    def remaining(self) -> float:
        return self.deadline - time.time()


# 이 프로세스 = 이 Claude 세션. 그래서 전역 하나로 충분하다.
_chat: Chat | None = None


class NoChat(RuntimeError):
    pass


def current() -> Chat:
    if _chat is None:
        raise NoChat("열린 대화가 없습니다. 먼저 대화를 여세요.")
    return _chat


def fmt_remaining(seconds: float) -> str:
    m = max(0, int(seconds // 60))
    if m >= 60:
        return f"{m // 60}시간 {m % 60}분"
    # "0분 뒤 닫습니다" 는 말이 안 된다. 1분 미만은 그렇게 읽히게 쓴다.
    return f"{m}분" if m else "곧"


def parse_command(text: str):
    """폰에서 보낸 한 줄이 마감 조작 명령인지 본다.

    자리를 비운 사람이 마감을 바꾸려고 터미널로 돌아와야 한다면 그건 바꿀 수
    없는 것이나 마찬가지다. 스레드에 한 줄 적는 것으로 되어야 한다.

    명령만 적힌 줄일 때만 명령으로 친다. "연장해줘 그리고 이것도 봐줘" 처럼
    말이 섞이면 그건 나에게 하는 말이므로 그대로 전달한다.
    """
    t = (text or "").strip()
    if not t:
        return None

    if t in ("닫기", "종료", "닫아", "그만"):
        return ("close", None)

    m = re.fullmatch(r"마감\s*(\d{1,2}):(\d{2})", t)
    if m:
        return ("until", (int(m.group(1)), int(m.group(2))))

    m = re.fullmatch(r"(?:연장\s*)?(\d+(?:\.\d+)?)\s*시간(?:\s*연장)?", t)
    if m:
        return ("extend", float(m.group(1)))

    if t in ("연장", "연장해", "연장해줘"):
        return ("extend", AUTO_EXTEND_HOURS)

    m = re.fullmatch(r"(?:연장\s*)?(\d+)\s*분(?:\s*연장)?", t)
    if m:
        return ("extend", int(m.group(1)) / 60)

    return None


def deadline_from_hhmm(hour: int, minute: int, now: float | None = None) -> float:
    """오늘 그 시각. 이미 지났으면 내일 그 시각.

    시각만 주고 자정을 넘기면 "이미 지난 시각" 이 되어 즉시 마감돼 버린다.
    ntfy 때 실제로 그렇게 조용히 끊긴 적이 있다.
    """
    now = now or time.time()
    lt = time.localtime(now)
    target = time.mktime((
        lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1
    ))
    if target <= now:
        target += 86400
    return target


def header_text(label: str, deadline: float) -> str:
    until = time.strftime("%H:%M", time.localtime(deadline))
    return (
        f"*{label}* — 대화를 엽니다. (마감 {until})\n"
        "이 메시지의 스레드에 답글을 달면 이 세션이 이어받습니다."
    )


def open_chat(
    token: str, channel: str, hours: float, label: str | None, owner_id: str = ""
) -> Chat:
    """대화를 열어 이 세션에 묶는다.

    스레드 하나가 세션 하나다. ntfy 의 슬롯 여덟 개가 하던 일을 스레드가 하되,
    개수 제한이 없으므로 미리 만들어 둘 것도 반납할 것도 없다.
    """
    global _chat
    head = label or "Claude 세션"
    deadline = time.time() + hours * 3600
    res = slack.post_message(token, channel, header_text(head, deadline))

    _chat = Chat(
        channel=channel,
        thread_ts=res["ts"],
        deadline=deadline,
        label=head,
        header=header_text(head, deadline),
    )
    threads.save(_chat.thread_ts, {
        "channel": channel,
        "thread_ts": _chat.thread_ts,
        "label": head,
        "deadline": deadline,
        "warned": False,
        "closed": False,
        # 채널이면 멘션을 요구한다. DM 은 상대가 나뿐이라 필요 없다.
        "require_mention": not channel.startswith("D"),
        "owner_id": owner_id,
    })
    return _chat


def extend(token: str, hours: float) -> Chat:
    """마감을 미룬다. 머리글의 마감 시각도 같이 고쳐 쓴다."""
    chat = current()
    chat.deadline = max(chat.deadline, time.time()) + hours * 3600
    chat.warned = False
    threads.patch(chat.thread_ts, deadline=chat.deadline, warned=False)
    try:
        slack.chat_update(
            token, chat.channel, chat.thread_ts,
            header_text(chat.label or "Claude 세션", chat.deadline),
        )
    except slack.SlackError:
        # 머리글 갱신에 실패해도 마감 자체는 늘어난 상태다. 표시가 어긋나는 것과
        # 대화가 끊기는 것 중에는 전자가 낫다.
        pass
    return chat


def close_thread(token: str, channel: str, thread_ts: str, label: str, reason: str) -> None:
    """스레드를 닫는다 — 지우지 않고 표시만 남긴다.

    머리글에 취소선을 긋는 이유: 답글로만 "닫혔다" 고 적으면 스레드를 펼쳐야
    알 수 있다. 머리글이 그어져 있으면 대화 목록에서 바로 보인다.
    """
    stamp = time.strftime("%H:%M")
    try:
        slack.chat_update(
            token, channel, thread_ts,
            f"~*{label}* — 종료됨 ({stamp})~\n_{reason}_",
        )
    except slack.SlackError:
        pass
    try:
        slack.post_message(
            token, channel,
            f"{CLOSE_MARK} — {reason} ({stamp})\n이 스레드에 답글을 달아도 이제 읽지 않습니다.",
            thread_ts=thread_ts,
        )
    except slack.SlackError:
        # 닫는 길에 네트워크가 죽어도 상태는 정리한다. 못 알린 것보다 붙잡고
        # 있는 쪽이 나쁘다.
        pass
    threads.patch(thread_ts, closed=True, closed_at=time.time(), reason=reason)


def close_chat(token: str, reason: str = "작업이 끝났습니다") -> None:
    global _chat
    if _chat is None:
        return
    close_thread(token, _chat.channel, _chat.thread_ts, _chat.label or "Claude 세션", reason)
    _chat = None


def warn_text(remaining: float) -> str:
    return (
        f"{fmt_remaining(remaining)} 뒤 이 스레드를 닫습니다.\n"
        "답글을 주시면 2시간 연장됩니다. 그대로 두시면 예정대로 닫힙니다."
    )


def is_human(msg: dict, bot_user_id: str) -> bool:
    """내가 보낸 것을 걸러낸다.

    ntfy 에서는 내가 POST 한 것이 내 구독으로 되돌아와 자기 자신을 깨우는 루프가
    생겼고, 그걸 막으려고 발신 ID 파일과 1.2초 유예를 뒀다. Slack 은 메시지에
    발신 주체가 붙어 오므로 시간 유예 없이 결정적으로 걸러진다.
    """
    if msg.get("bot_id"):
        return False
    if msg.get("user") == bot_user_id:
        return False
    if msg.get("subtype"):          # 채널 입장/파일 공유 알림 등
        return False
    return bool(msg.get("text") or msg.get("files"))


def is_for_me(
    msg: dict,
    bot_user_id: str,
    owner_id: str = "",
    require_mention: bool = False,
) -> bool:
    """이 메시지를 내 세션에 대한 지시로 받아들일지 판정한다.

    DM 은 상대가 한 사람뿐이라 사람이 쓴 것이면 전부 내 말이다.

    채널은 다르다. 옆에서 오가는 대화까지 지시로 삼으면 남이 무심코 쓴 말이
    내 작업을 움직인다. 그래서 두 겹을 건다.

      - 봇을 @멘션한 것만 — 남이 봐도 "저건 봇한테 하는 말" 이 보인다
      - 소유자가 쓴 것만 — 채널 멤버 아무나 세션에 명령할 수는 없다

    소유자를 정해두지 않았으면 작성자 제한은 걸지 않는다. 설정이 없다는 이유로
    조용히 아무 말도 안 듣는 상태가 되면, 고장과 구분되지 않는다.
    """
    if not is_human(msg, bot_user_id):
        return False
    if owner_id and msg.get("user") != owner_id:
        return False
    if require_mention and f"<@{bot_user_id}>" not in (msg.get("text") or ""):
        return False
    return True


def strip_mention(text: str, bot_user_id: str) -> str:
    """본문에서 봇 멘션을 걷어낸다. 지시만 남기기 위해서다."""
    return re.sub(rf"<@{re.escape(bot_user_id)}>\s*", "", text or "").strip()


def describe(msg: dict) -> str:
    """본문과 첨부를 함께 남긴다.

    스크린샷을 붙여 "이거 정상이야?" 라고 묻는 경우가 있어서, 본문만 읽으면
    정작 볼 것을 놓친다.
    """
    parts = [msg.get("text", "").strip()]
    for f in msg.get("files", []) or []:
        name = f.get("name", "첨부")
        url = f.get("url_private", "")
        parts.append(f"[첨부] {name} {url}".rstrip())
    return "\n".join(p for p in parts if p)


def poll_new(token: str, chat: Chat) -> list[dict]:
    """스레드에서 아직 못 본 메시지를 가져온다.

    스레드만 본다. 그래야 세션 넷이 같은 DM 을 써도 서로의 답장을 집어가지 않는다.
    """
    msgs = slack.conversations_replies(token, chat.channel, chat.thread_ts)
    fresh = []
    for m in msgs:
        ts = m.get("ts", "")
        if not ts or ts in chat.seen or ts == chat.thread_ts:
            continue
        chat.seen.add(ts)
        fresh.append(m)
    fresh.sort(key=lambda m: float(m.get("ts", "0")))
    return fresh


def wait_reply(token: str, bot_user_id: str, timeout: float) -> tuple[str, list[dict]]:
    """답장이 올 때까지 블로킹한다.

    반환하는 상태는 셋이다.
      messages — 사람 답장이 왔다
      timeout  — 이번 대기 시간 안에는 안 왔다 (스레드는 그대로 살아있다)
      closed   — 마감이 지나 스레드가 닫혔다
    """
    chat = current()
    until = time.time() + timeout
    fails = 0

    while True:
        now = time.time()

        # 마감은 파일이 정본이다. 감시자나 다른 프로세스가 연장했을 수 있다.
        state = threads.load(chat.thread_ts) or {}
        if state.get("deadline"):
            chat.deadline = float(state["deadline"])
            chat.warned = bool(state.get("warned"))

        if chat.remaining <= 0:
            close_chat(token, "마감 시각 도달")
            return "closed", []

        if not chat.warned and chat.remaining <= WARN_LEAD:
            try:
                slack.post_message(
                    token, chat.channel, warn_text(chat.remaining),
                    thread_ts=chat.thread_ts,
                )
            except slack.SlackError:
                pass
            chat.warned = True
            threads.patch(chat.thread_ts, warned=True)

        try:
            fresh = [m for m in poll_new(token, chat) if is_human(m, bot_user_id)]
            fails = 0
        except slack.SlackError:
            # 단발 실패는 넘어간다. 커서를 그대로 두고 다음 주기에 다시 긁으므로
            # 유실이 아니라 지연이 된다.
            #
            # 다만 계속 실패하면 조용히 삼키지 않는다. 그러면 "답장 없음" 과
            # 구분이 안 되어, 사용자는 답을 보냈는데 기다리는 쪽은 아무 일도
            # 없다고 믿는 상태가 된다. 실제로 그렇게 새고 있었다.
            fails += 1
            if fails >= 3:
                raise
            fresh = []

        if fresh:
            if chat.warned:
                # 예고 창 안에 답이 왔다 = 자리에 있다는 신호다.
                extend(token, AUTO_EXTEND_HOURS)
            return "messages", fresh

        if now >= until:
            return "timeout", []

        time.sleep(min(POLL_INTERVAL, max(0.1, until - time.time())))
