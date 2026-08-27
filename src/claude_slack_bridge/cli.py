"""명령줄 진입점.

인자 없이 실행하면 MCP 서버로 동작한다. `claude mcp add` 가 그렇게 부르기 때문이다.
사람이 직접 부르는 건 `init` 과 `doctor` 둘뿐이다.
"""

from __future__ import annotations

import getpass
import sys

from . import config as cfg
from . import slack


def _die(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def cmd_init(argv: list[str]) -> None:
    """설정 파일을 만들고, 실제로 메시지가 도달하는지까지 확인한다.

    확인을 끝까지 하는 이유가 있다. 봇을 채널에 초대하지 않는 것이 압도적인 1위
    실패 원인인데, 그 경우 에러 없이 조용히 전달만 안 된다. 설치 시점에 잡지
    않으면 정작 자리를 비웠을 때 "알림이 안 왔다"로 알게 된다.
    """
    print("Slack 설정을 만듭니다. 준비물은 봇 토큰과 채널 ID 둘입니다.")
    print("(docs/SETUP.md 의 1~2단계를 먼저 끝내야 합니다)\n")

    # 입력을 화면에 남기지 않는다. 스크롤백과 녹화에 그대로 남기 때문이다.
    token = getpass.getpass("Bot User OAuth Token (xoxb-...): ").strip()
    if not token:
        _die("토큰이 비었습니다.")
    if not token.startswith("xoxb-"):
        print("경고: xoxb- 로 시작하지 않습니다. 봇 토큰이 맞는지 확인하세요.\n")

    try:
        who = slack.auth_test(token)
    except slack.SlackError as e:
        _die(f"토큰 확인 실패.\n{e}")
    print(f"  워크스페이스: {who.get('team')}")
    print(f"  봇 이름: {who.get('user')}\n")

    channel = input("채널 ID (C 로 시작): ").strip()
    if not channel:
        _die("채널 ID 가 비었습니다.")

    try:
        info = slack.conversations_info(token, channel).get("channel", {})
    except slack.SlackError as e:
        _die(f"채널 확인 실패.\n{e}")

    print(f"  채널: #{info.get('name')}")
    if not info.get("is_member"):
        _die(
            "봇이 이 채널에 없습니다.\n"
            f"  → Slack 의 #{info.get('name')} 에서 `/invite @{who.get('user')}` 를 실행한 뒤 다시 시도하세요."
        )
    print("  봇 초대됨: 예\n")

    try:
        slack.post_message(token, channel, "연결됐습니다. 이제 여기로 알림이 옵니다.")
    except slack.SlackError as e:
        _die(f"테스트 메시지 전송 실패.\n{e}")

    path = cfg.save(token, channel)
    print(f"테스트 메시지를 보냈습니다. 폰에서 확인해 보세요.")
    print(f"설정 저장: {path} (권한 600)\n")
    print("이제 Claude Code 에 붙입니다:")
    print("  claude mcp add claude-slack-bridge -s user -- uvx claude-slack-bridge")


def cmd_doctor(argv: list[str]) -> None:
    conf = cfg.load()
    if conf is None:
        _die(
            "설정이 없습니다.\n"
            "  → uvx claude-slack-bridge init\n"
            "  (또는 SLACK_BOT_TOKEN / SLACK_CHANNEL 환경변수)"
        )
    print(f"토큰: {conf.masked_token}")
    print(f"채널: {conf.channel}")
    try:
        who = slack.auth_test(conf.bot_token)
        info = slack.conversations_info(conf.bot_token, conf.channel).get("channel", {})
    except slack.SlackError as e:
        _die(f"\n확인 실패.\n{e}")

    print(f"워크스페이스: {who.get('team')}")
    print(f"봇: {who.get('user')}")
    print(f"채널명: #{info.get('name')}")
    if not info.get("is_member"):
        _die("봇이 채널에 없습니다 — /invite 가 필요합니다.")
    print("봇 초대됨: 예")
    print("\n정상입니다.")


USAGE = """\
claude-slack-bridge — Claude Code 세션과 Slack 을 잇는 MCP 서버

  claude-slack-bridge          MCP 서버로 동작 (claude mcp add 가 이렇게 부른다)
  claude-slack-bridge init     설정 생성 + 연결 확인 + 테스트 메시지
  claude-slack-bridge doctor   현재 설정이 살아있는지 점검
"""


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] == "serve":
        from .server import main as serve

        serve()
        return

    cmd = argv[0]
    if cmd == "init":
        cmd_init(argv[1:])
    elif cmd == "doctor":
        cmd_doctor(argv[1:])
    elif cmd in ("-h", "--help", "help"):
        print(USAGE)
    else:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)
