"""MCP 서버.

Claude Code 가 stdio 로 이 프로세스를 띄우고, 세션이 끝나면 같이 죽는다.
호스팅도 포트도 없다.

세션마다 프로세스가 하나씩 뜨므로 세션 격리가 공짜로 따라온다. ntfy 시절
토픽 8개를 미리 만들어 두고 뺏어 쓰던 장치가 여기서는 필요 없는 이유다.
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer

from . import chat as chatmod
from . import config as cfg
from . import slack

INSTRUCTIONS = """\
자리를 비운 사용자의 폰(Slack)으로 알림을 보내는 다리다.

`slack_notify` 는 조사 완료·장시간 작업 종료·사용자 결정이 필요한 갈림길처럼
"지금 알아야 하는" 순간에만 쓴다. 즉답이나 수 초 만에 끝난 일에는 쓰지 않는다.

설정이 없으면 조용히 아무것도 하지 않는다. 그것 때문에 작업을 멈추지 말 것.
"""

server = MCPServer(
    name="claude-slack-bridge",
    version="0.9.0",
    instructions=INSTRUCTIONS,
)

SETUP_HINT = (
    "Slack 설정이 없어 알림을 건너뜁니다. "
    "`uvx claude-slack-bridge init` 으로 한 번만 설정하면 됩니다."
)


@server.tool(
    name="slack_notify",
    title="폰으로 알림 보내기",
    description=(
        "사용자의 Slack 채널로 한 줄 알림을 보낸다. 자리를 비웠을 수 있고 "
        "지금 알 가치가 있는 일에만 쓴다. 비밀값(토큰·개인키)이 섞이면 거부된다."
    ),
)
def slack_notify(text: str, title: str | None = None) -> str:
    """알림을 보낸다.

    Args:
        text: 보낼 내용. 무슨 작업이 어떻게 끝났는지 한 줄로.
        title: 앞에 굵게 붙일 라벨. 프로젝트나 태스크 이름.
    """
    conf = cfg.load()
    if conf is None:
        return SETUP_HINT

    body = f"*{title}*\n{text}" if title else text
    # 대화가 열려 있으면 그 스레드로 보낸다. 알림과 답장이 한 자리에 모여야
    # 폰에서 맥락이 끊기지 않는다.
    thread = chatmod._chat.reply_thread if chatmod._chat else None
    try:
        res = slack.post_message(conf.bot_token, conf.channel, body, thread_ts=thread)
    except slack.BodyRejected as e:
        return f"보내지 않았습니다 — {e}"
    except slack.SlackError as e:
        return f"보내지 못했습니다.\n{e}"
    return f"보냈습니다 (ts={res.get('ts', '?')})"


@server.tool(
    name="slack_check",
    title="Slack 연결 확인",
    description="토큰과 채널이 살아있는지, 봇이 채널에 초대돼 있는지 확인한다.",
)
def slack_check() -> str:
    conf = cfg.load()
    if conf is None:
        return SETUP_HINT
    try:
        who = slack.auth_test(conf.bot_token)
        target = slack.probe(conf.bot_token, conf.channel)
    except slack.SlackError as e:
        return f"확인 실패.\n{e}"

    lines = [
        f"워크스페이스: {who.get('team', '?')}",
        f"봇: {who.get('user', '?')}",
        f"받는 곳: {target['label']} ({conf.channel})",
    ]
    if target["kind"] == "channel":
        lines.append(f"봇 초대됨: {'예' if target['ready'] else '아니오 — /invite 필요'}")
    return "\n".join(lines)


def _bot_user_id(token: str) -> str:
    """내가 보낸 메시지를 걸러내려면 내 user id 를 알아야 한다."""
    global _BOT_ID
    if not _BOT_ID:
        _BOT_ID = str(slack.auth_test(token).get("user_id", ""))
    return _BOT_ID


_BOT_ID = ""


@server.tool(
    name="slack_chat_open",
    title="폰과 대화 열기",
    description=(
        "Slack 에 스레드를 하나 열고 이 세션에 묶는다. 사용자가 자리를 비우면서 "
        "폰으로 이어서 얘기하자고 할 때 쓴다."
    ),
)
def slack_chat_open(hours: float = 4.0, label: str | None = None) -> str:
    """대화를 연다.

    Args:
        hours: 채널을 유지할 시간. 기본 4시간.
        label: 스레드 첫 줄에 붙일 라벨. 프로젝트나 태스크 이름.
    """
    conf = cfg.load()
    if conf is None:
        return SETUP_HINT
    # 라벨이 없으면 작업 디렉터리 이름을 쓴다. 세션이 여럿일 때 폰에서 스레드를
    # 구분하는 유일한 단서라, 비워두면 넷 다 같은 이름으로 보인다.
    label = label or os.path.basename(os.getcwd()) or None
    try:
        c = chatmod.open_chat(conf.bot_token, conf.channel, hours, label)
    except slack.SlackError as e:
        return f"열지 못했습니다.\n{e}"
    return f"열렸습니다. 마감까지 {chatmod.fmt_remaining(c.remaining)} 남았습니다."


@server.tool(
    name="slack_wait_reply",
    title="폰 답장 기다리기",
    description=(
        "열린 스레드에 사용자 답글이 올 때까지 기다렸다가 돌려준다. 시간 안에 "
        "안 오면 timeout 으로 돌아오며, 채널은 그대로 살아있다."
    ),
)
def slack_wait_reply(timeout_seconds: int = 600) -> str:
    """답장을 기다린다.

    Args:
        timeout_seconds: 이번 대기의 최대 시간(초). 기본 600.
    """
    conf = cfg.load()
    if conf is None:
        return SETUP_HINT
    try:
        state, msgs = chatmod.wait_reply(
            conf.bot_token, _bot_user_id(conf.bot_token), float(timeout_seconds)
        )
    except chatmod.NoChat as e:
        return str(e)
    except slack.SlackError as e:
        return f"대기 중 오류.\n{e}"

    if state == "closed":
        return "마감이 지나 대화가 닫혔습니다."
    if state == "timeout":
        c = chatmod.current()
        return f"아직 답장이 없습니다. (마감까지 {chatmod.fmt_remaining(c.remaining)})"
    return "\n---\n".join(chatmod.describe(m) for m in msgs)


@server.tool(
    name="slack_chat_extend",
    title="대화 시간 연장",
    description="열린 대화의 마감을 미룬다.",
)
def slack_chat_extend(hours: float = 2.0) -> str:
    try:
        c = chatmod.extend(hours)
    except chatmod.NoChat as e:
        return str(e)
    return f"연장했습니다. 마감까지 {chatmod.fmt_remaining(c.remaining)}."


@server.tool(
    name="slack_chat_close",
    title="대화 닫기",
    description="열린 스레드를 닫는다.",
)
def slack_chat_close() -> str:
    conf = cfg.load()
    if conf is None:
        return SETUP_HINT
    chatmod.close_chat(conf.bot_token)
    return "닫았습니다."


def main() -> None:
    server.run(transport="stdio")
