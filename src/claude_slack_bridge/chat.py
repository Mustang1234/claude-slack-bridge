"""폰과의 양방향 대화.

ntfy 시절에는 프로세스가 둘이었다. 채널을 붙들고 받아 적는 보유자와, 새 줄이
생기면 종료해서 Claude 를 깨우는 감시자. 한 프로세스가 둘을 겸하면 깨우려고
죽는 순간 채널도 끊기기 때문이었다.

MCP 에서는 그 분리가 필요 없다. 툴 호출이 그냥 블로킹했다 돌아오면 되므로
`wait_reply` 하나로 끝난다. inbox 파일도, 감시자 재기동도, 에코 유예도 없다.

세션 상태를 모듈 전역에 두는 것도 같은 이유다. 이 프로세스는 Claude 세션 하나에
붙어서 살고 세션이 끝나면 같이 죽는다. 세션 격리가 공짜다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import slack

POLL_INTERVAL = 5.0       # Slack 레이트리밋은 넉넉하다. ntfy 의 25초를 따를 이유가 없다.
WARN_LEAD = 10 * 60       # 마감 10분 전 예고
AUTO_EXTEND = 2 * 3600    # 예고 창에 답이 오면 자리에 있다는 뜻이므로 자동 연장
DEFAULT_HOURS = 4.0


@dataclass
class Chat:
    channel: str
    thread_ts: str          # DM 에서는 비어 있다 (아래 is_im 주석 참고)
    deadline: float
    is_im: bool = False
    anchor_ts: str = ""     # 이 시각 이후의 메시지만 읽는다
    warned: bool = False
    seen: set[str] = field(default_factory=set)

    @property
    def remaining(self) -> float:
        return self.deadline - time.time()

    @property
    def reply_thread(self) -> str | None:
        """보낼 때 붙일 thread_ts.

        채널에서는 스레드로 묶어야 다른 세션과 섞이지 않는다. DM 은 상대가
        나 하나뿐이라 섞일 일이 없고, 오히려 스레드로 묶으면 폰에서 답장하려면
        스레드를 열어야 해서 번거로워진다.
        """
        return self.thread_ts or None


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
    return f"{m // 60}시간 {m % 60}분" if m >= 60 else f"{m}분"


def open_chat(token: str, channel: str, hours: float, label: str | None) -> Chat:
    """대화를 열어 이 세션에 묶는다.

    채널이면 스레드를 파고, DM 이면 그냥 대화 자체를 쓴다.
    """
    global _chat
    is_im = channel.startswith("D")
    head = label or "Claude 세션"
    guide = "여기에 답장하면 세션이 이어받습니다." if is_im else "이 스레드에 답글을 달면 세션이 이어받습니다."
    res = slack.post_message(token, channel, f"*{head}* — 대화를 엽니다.\n{guide}")
    _chat = Chat(
        channel=channel,
        thread_ts="" if is_im else res["ts"],
        deadline=time.time() + hours * 3600,
        is_im=is_im,
        anchor_ts=res["ts"],
    )
    return _chat


def close_chat(token: str, reason: str = "대화를 닫습니다.") -> None:
    global _chat
    if _chat is None:
        return
    try:
        slack.post_message(token, _chat.channel, reason, thread_ts=_chat.reply_thread)
    except slack.SlackError:
        # 닫는 길에 네트워크가 죽어도 상태는 정리한다. 못 알린 것보다 붙잡고
        # 있는 쪽이 나쁘다.
        pass
    _chat = None


def extend(hours: float) -> Chat:
    chat = current()
    chat.deadline = max(chat.deadline, time.time()) + hours * 3600
    chat.warned = False
    return chat


def is_human(msg: dict, bot_user_id: str) -> bool:
    """내가 보낸 것과 스레드 안내문을 걸러낸다.

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
    """아직 못 본 메시지를 가져온다."""
    if chat.is_im:
        msgs = slack.conversations_history(token, chat.channel, oldest=chat.anchor_ts)
    else:
        msgs = slack.conversations_replies(token, chat.channel, chat.thread_ts)

    fresh = []
    for m in msgs:
        ts = m.get("ts", "")
        if not ts or ts in chat.seen or ts == chat.anchor_ts:
            continue
        chat.seen.add(ts)
        fresh.append(m)
    # history 는 최신순으로 준다. 사람이 보낸 순서대로 읽어야 맥락이 맞는다.
    fresh.sort(key=lambda m: float(m.get("ts", "0")))
    return fresh


def wait_reply(token: str, bot_user_id: str, timeout: float) -> tuple[str, list[dict]]:
    """답장이 올 때까지 블로킹한다.

    반환하는 상태는 셋이다.
      messages — 사람 답장이 왔다
      timeout  — 이번 대기 시간 안에는 안 왔다 (채널은 그대로 살아있다)
      closed   — 마감이 지나 채널이 닫혔다
    """
    chat = current()
    until = time.time() + timeout

    # 첫 폴링 전에 밀린 것부터 본다. 대기에 들어가기 직전 도착한 답장을
    # timeout 만큼 묵히지 않기 위해서다.
    while True:
        now = time.time()

        if chat.remaining <= 0:
            close_chat(token, "마감 시각이 지나 닫습니다.")
            return "closed", []

        if not chat.warned and chat.remaining <= WARN_LEAD:
            try:
                slack.post_message(
                    token,
                    chat.channel,
                    f"{fmt_remaining(chat.remaining)} 뒤 닫힙니다. 답글을 주시면 연장됩니다.",
                    thread_ts=chat.reply_thread,
                )
            except slack.SlackError:
                pass
            chat.warned = True

        try:
            fresh = [m for m in poll_new(token, chat) if is_human(m, bot_user_id)]
        except slack.SlackError:
            # 한 번의 네트워크 실패로 대화를 끝내지 않는다. 커서를 그대로 두고
            # 다음 주기에 다시 긁으므로 유실이 아니라 지연이 된다.
            fresh = []

        if fresh:
            if chat.warned:
                # 예고 창 안에 답이 왔다 = 자리에 있다는 신호다.
                extend(AUTO_EXTEND / 3600)
            return "messages", fresh

        if now >= until:
            return "timeout", []

        time.sleep(min(POLL_INTERVAL, max(0.1, until - time.time())))
