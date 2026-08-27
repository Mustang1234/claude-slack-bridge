"""MCP 서버.

Claude Code 가 stdio 로 이 프로세스를 띄우고, 세션이 끝나면 같이 죽는다.
호스팅도 포트도 없다.

세션마다 프로세스가 하나씩 뜨므로 세션 격리가 공짜로 따라온다. ntfy 시절
토픽 8개를 미리 만들어 두고 뺏어 쓰던 장치가 여기서는 필요 없는 이유다.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

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
    version="0.1.0",
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
    try:
        res = slack.post_message(conf.bot_token, conf.channel, body)
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
        info = slack.conversations_info(conf.bot_token, conf.channel)
    except slack.SlackError as e:
        return f"확인 실패.\n{e}"

    ch = info.get("channel", {})
    member = ch.get("is_member")
    lines = [
        f"워크스페이스: {who.get('team', '?')}",
        f"봇: {who.get('user', '?')}",
        f"채널: #{ch.get('name', '?')} ({conf.channel})",
        f"봇 초대됨: {'예' if member else '아니오 — /invite 필요'}",
    ]
    return "\n".join(lines)


def main() -> None:
    server.run(transport="stdio")
