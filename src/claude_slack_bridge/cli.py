"""명령줄 진입점.

인자 없이 실행하면 MCP 서버로 동작한다. `claude mcp add` 가 그렇게 부른다.
사람이 직접 부르는 것은 `init`, `manifest`, `doctor` 셋이다.

`init` 은 안내까지 한다. uvx 로 설치한 사람에게는 레포가 없어서 문서 파일을
열어볼 수 없기 때문이다. 화면 안에서 끝나야 한다.
"""

from __future__ import annotations

import getpass
import re
import sys
from importlib import resources

from . import config as cfg
from . import slack

MANIFEST_NAME = "slack-app-manifest.yaml"


def _die(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def read_manifest() -> str:
    """패키지 안에 든 매니페스트를 읽는다.

    파일 경로가 아니라 패키지 리소스로 읽는 이유는, 설치 형태(디렉터리·wheel·
    zip)에 따라 경로가 달라지기 때문이다.
    """
    return resources.files(__package__).joinpath("data", MANIFEST_NAME).read_text(
        encoding="utf-8"
    )


STEP1 = """\
━━ 1단계 · Slack 에 봇 등록하고 토큰 받기 (브라우저) ━━

  여기서 "앱"은 프로그램이 아니라 워크스페이스에 등록하는 봇 계정이다.
  개발하거나 설치할 것은 없고, 마지막에 토큰 문자열 하나를 받는 게 목적이다.

  1) https://api.slack.com/apps 접속 (Slack 로그인 상태로)
  2) Create New App  →  From an app manifest
  3) 워크스페이스 선택  →  Next
  4) 아래 명령으로 매니페스트를 출력해 통째로 붙여넣기  →  Next  →  Create

       claude-slack-bridge manifest

     봇 이름을 바꾸고 싶으면 이렇게 한다 (기본값은 Claude Bridge):

       claude-slack-bridge manifest --name "내 비서"

  5) 왼쪽 메뉴 OAuth & Permissions  →  Install to Workspace  →  Allow
  6) Bot User OAuth Token 복사 (xoxb- 로 시작)

  * 5번에서 설치 대신 "관리자 승인 요청" 화면이 뜨면 회사가 막아둔 것이다.
    요청 사유 예시:
      개발 작업 알림을 지정한 비공개 채널로 받기 위한 봇.
      외부로 데이터를 보내지 않고, 초대된 채널에만 접근한다.
"""

STEP2 = """\
━━ 2단계 · 알림을 받을 채널 만들기 (Slack 앱) ━━

  1) 비공개 채널을 하나 만든다 (예: claude-알림)
  2) 그 채널에서  /invite @Claude Bridge  를 실행해 봇을 초대한다
  3) 채널명 우클릭 → 링크 복사 → URL 끝의 C 로 시작하는 문자열이 채널 ID

  * 2번을 빠뜨리는 것이 압도적인 1위 실패 원인이다. 초대하지 않으면 메시지가
    에러 없이 조용히 전달만 안 된다. 그래서 아래에서 초대 여부를 검사한다.

  * /invite 가 "수행할 수 없습니다" 로 거부되면 워크스페이스가 앱 추가를 관리자로
    제한한 것이다. 그때는 채널을 포기하고 봇과의 DM 으로 받으면 된다 — DM 은
    채널 멤버십이 아니라서 초대라는 절차가 없다. 아래에서 2) 를 고르면 된다.
"""


def rename_manifest(text: str, name: str) -> str:
    """매니페스트의 앱 이름과 봇 표시명을 바꾼다.

    yaml 파서를 끌어오지 않으려고 해당 두 줄만 갈아끼운다. 의존성을 하나 늘리는
    값이 이름 치환 하나에 미치지 못한다.
    """
    out = []
    for line in text.splitlines():
        if re.match(r"^  name: ", line):
            out.append(f"  name: {name}")
        elif re.match(r"^    display_name: ", line):
            out.append(f"    display_name: {name}")
        else:
            out.append(line)
    return "\n".join(out)


def cmd_manifest(argv: list[str]) -> None:
    text = read_manifest()
    if "--name" in argv:
        i = argv.index("--name")
        if i + 1 >= len(argv):
            _die("--name 뒤에 이름을 적어주세요.")
        name = argv[i + 1].strip()
        if not name:
            _die("이름이 비었습니다.")
        if len(name) > 35:
            _die(f"이름이 너무 깁니다({len(name)}자). Slack 앱 이름은 35자까지입니다.")
        text = rename_manifest(text, name)
    print(text)


def cmd_init(argv: list[str]) -> None:
    """설정을 만들고, 실제로 메시지가 도달하는지까지 확인한다."""
    print("Slack 설정을 만듭니다. 준비물은 봇 토큰과 채널 ID 둘입니다.\n")

    ready = input("이미 토큰과 채널 ID 가 있습니까? [y/N] ").strip().lower()
    if ready not in ("y", "yes"):
        print()
        print(STEP1)
        print(STEP2)
        print("━━ 준비가 끝나면 다시 실행하세요 ━━\n")
        print("  claude-slack-bridge init\n")
        return

    print()
    # 입력을 화면에 남기지 않는다. 스크롤백과 녹화에 그대로 남기 때문이다.
    token = getpass.getpass("Bot User OAuth Token (입력은 표시되지 않습니다): ").strip()
    if not token:
        _die("토큰이 비었습니다.")
    if not token.startswith("xoxb-"):
        print("  경고: xoxb- 로 시작하지 않습니다. 봇 토큰이 맞는지 확인하세요.\n")

    try:
        who = slack.auth_test(token)
    except slack.SlackError as e:
        _die(f"토큰 확인 실패.\n{e}\n\n  → 1단계 6번의 Bot User OAuth Token 을 다시 확인하세요.")
    print(f"  워크스페이스: {who.get('team')}")
    print(f"  봇 이름: {who.get('user')}\n")

    print("알림을 받을 곳을 고릅니다.")
    print("  1) 비공개 채널 — 봇을 초대해야 한다")
    print("  2) 봇과의 DM   — 초대가 필요 없다 (채널에 앱을 못 넣는 경우)")
    where = input("[1/2] ").strip()

    if where == "2":
        print()
        print("  Slack 에서 본인 프로필 → 더보기(⋮) → '멤버 ID 복사' 로 얻습니다.")
        user_id = input("내 멤버 ID (U 로 시작): ").strip()
        if not user_id:
            _die("멤버 ID 가 비었습니다.")
        try:
            channel = slack.conversations_open(token, user_id)
        except slack.SlackError as e:
            _die(f"DM 을 열지 못했습니다.\n{e}")
        print(f"  DM 대화: {channel}\n")
    else:
        channel = input("채널 ID (C 로 시작): ").strip()
        if not channel:
            _die("채널 ID 가 비었습니다.")

        try:
            info = slack.conversations_info(token, channel).get("channel", {})
        except slack.SlackError as e:
            _die(
                f"채널 확인 실패.\n{e}\n\n"
                "  → 2단계 3번처럼 채널 링크 URL 끝의 C... 문자열인지 확인하세요.\n"
                "     비공개 채널이면 봇을 먼저 초대해야 보입니다."
            )

        print(f"  채널: #{info.get('name')}")
        if not info.get("is_member"):
            _die(
                "  봇이 이 채널에 없습니다.\n\n"
                f"  → Slack 의 #{info.get('name')} 에서 다음을 실행한 뒤 다시 시도하세요.\n"
                f"       /invite @{who.get('user')}\n\n"
                "  이 명령이 '수행할 수 없습니다' 로 거부되면 워크스페이스가 앱 추가를\n"
                "  관리자로 제한한 것입니다. 그때는 init 을 다시 돌려 2) DM 을 고르세요."
            )
        print("  봇 초대됨: 예\n")

    try:
        slack.post_message(token, channel, "연결됐습니다. 이제 여기로 알림이 옵니다.")
    except slack.SlackError as e:
        _die(f"테스트 메시지 전송 실패.\n{e}")

    path = cfg.save(token, channel)
    print("테스트 메시지를 보냈습니다. 폰에서 확인해 보세요.")
    print(f"설정 저장: {path} (권한 600)\n")
    print("━━ 마지막 · Claude Code 에 붙이기 ━━\n")
    print("  claude mcp add claude-slack-bridge -s user -- uvx claude-slack-bridge\n")


def cmd_doctor(argv: list[str]) -> None:
    conf = cfg.load()
    if conf is None:
        _die(
            "설정이 없습니다.\n"
            "  → claude-slack-bridge init\n"
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
    if info.get("is_im"):
        # DM 에는 멤버십 개념이 없다. 초대 검사를 하면 항상 실패로 읽힌다.
        print("받는 곳: 봇과의 DM")
    else:
        print(f"채널명: #{info.get('name')}")
        if not info.get("is_member"):
            _die(f"봇이 채널에 없습니다.\n  → /invite @{who.get('user')}")
        print("봇 초대됨: 예")
    print("\n정상입니다.")


USAGE = """\
claude-slack-bridge — Claude Code 세션과 Slack 을 잇는 MCP 서버

  claude-slack-bridge            MCP 서버로 동작 (claude mcp add 가 이렇게 부른다)
  claude-slack-bridge init       설정 생성 — 준비가 안 됐으면 절차를 안내한다
  claude-slack-bridge manifest   Slack 콘솔에 붙여넣을 앱 매니페스트를 출력한다
                                 --name "이름" 으로 봇 이름을 바꿀 수 있다
  claude-slack-bridge doctor     현재 설정이 살아있는지 점검한다
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
    elif cmd == "manifest":
        cmd_manifest(argv[1:])
    elif cmd == "doctor":
        cmd_doctor(argv[1:])
    elif cmd in ("-h", "--help", "help"):
        print(USAGE)
    else:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)
