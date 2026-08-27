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
import time
from importlib import resources

from . import chat
from . import config as cfg
from . import slack

MANIFEST_NAME = "slack-app-manifest.yaml"


def _die(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def _arg(argv: list[str], name: str) -> str | None:
    """--name value 한 쌍을 꺼낸다. 없으면 None."""
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def read_manifest() -> str:
    """패키지 안에 든 매니페스트를 읽는다.

    파일 경로가 아니라 패키지 리소스로 읽는 이유는, 설치 형태(디렉터리·wheel·
    zip)에 따라 경로가 달라지기 때문이다.
    """
    return resources.files(__package__).joinpath("data", MANIFEST_NAME).read_text(
        encoding="utf-8"
    )


STEP1_HEAD = """\
━━ 1단계 · Slack 에 봇 만들기 (브라우저) ━━

  여기서 "앱"은 프로그램이 아니라 워크스페이스에 등록하는 봇 계정이다.
  개발하거나 내려받을 것은 없다. 브라우저에서 양식을 채우면 끝난다.

  1) 브라우저로 https://api.slack.com/apps 를 연다
     - Slack 에 로그인돼 있어야 한다. 아니면 로그인 화면이 먼저 뜬다.
     - 앱을 만든 적이 없으면 목록이 비어 있다. 정상이다.

  2) 초록색 'Create New App' 버튼을 누른다

  3) 팝업에서 'From an app manifest' 를 고른다
     - 'From scratch' 가 아니다. 그쪽으로 만들면 봇 사용자가 생기지 않아
       나중에 Bot User OAuth Token 줄이 아예 나오지 않는다.

  4) 워크스페이스를 고르고 Next

  5) YAML 을 붙여넣는 칸이 나온다. 안에 있던 내용을 모두 지우고
     아래를 통째로 붙여넣는다.

"""

STEP1_TAIL = """\

  6) Next 를 누르면 요약 화면이 나온다  →  Create

  이제 앱이 만들어졌다. 아직 워크스페이스에 설치된 것은 아니다.
  설치와 토큰은 다음 단계다.

  * 봇 이름을 바꾸고 싶으면 위 YAML 대신 아래 출력을 붙여넣는다.

       claude-slack-bridge manifest --name "내 비서"
"""


def step1() -> str:
    """앱 생성 안내. 매니페스트를 그 자리에 끼워 넣는다.

    별도 명령으로 출력하게 하면 창을 오가며 순서를 잃는다. 붙여넣을 것이
    붙여넣으라는 문장 바로 아래 있어야 한다.
    """
    body = "\n".join("     " + ln for ln in read_manifest().splitlines())
    return STEP1_HEAD + body + STEP1_TAIL


STEP2 = """\
━━ 3단계 · 알림을 받을 곳 정하기 (Slack 앱) ━━

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


TOKEN_HELP = """\
━━ 2단계 · 설치하고 Bot User OAuth Token 받기 ━━

  1) https://api.slack.com/apps 에서 만든 앱을 연다
  2) 왼쪽 메뉴 'OAuth & Permissions' 를 클릭한다
  3) 페이지 맨 위 'OAuth Tokens for Your Workspace' 를 본다

     아직 토큰이 하나도 없다면 아직 설치 전이다.
       → 'Install to Workspace' 버튼을 누르고 권한 화면에서 Allow
       → 관리자 승인이 필요한 워크스페이스면 '요청됨' 에서 멈춘다.
          승인이 나기 전에는 토큰이 생기지 않는다.

  4) 설치가 끝나면 그 자리에 토큰이 나온다

       Bot User OAuth Token    xoxb-...   ←  이것을 복사한다
       User OAuth Token        xoxp-...   ←  이것이 아니다

     둘 다 있으면 위쪽이다. 아래쪽은 내 계정 자격이라 봇으로 동작하지 않는다.

  5) 토큰 오른쪽 'Copy' 를 눌러 그대로 붙여넣는다.

  * 'Bot User OAuth Token' 줄이 아예 없다면 앱에 봇 사용자가 없는 것이다.
    매니페스트로 만들지 않으면 그렇게 된다. 아래를 붙여넣어 앱을 다시 만드는
    편이 빠르다.

       claude-slack-bridge manifest
"""


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
        print(step1())
        print(TOKEN_HELP)
        print(STEP2)
        print("━━ 준비가 끝나면 다시 실행하세요 ━━\n")
        print("  claude-slack-bridge init\n")
        return

    print()
    # 입력을 화면에 남기지 않는다. 스크롤백과 녹화에 그대로 남기 때문이다.
    token = getpass.getpass("Bot User OAuth Token (입력은 표시되지 않습니다): ").strip()
    if not token:
        _die("토큰이 비었습니다.")
    # 토큰 종류를 접두사로 먼저 가른다. 종류가 틀리면 auth.test 는 통과하고
    # 정작 메시지를 보낼 때 권한 오류가 나서, 원인이 한참 뒤에 드러난다.
    if token.startswith("xoxp-"):
        _die("이건 사용자 토큰(User OAuth Token)입니다. 봇 토큰이 필요합니다.\n\n" + TOKEN_HELP)
    if token.startswith("xapp-"):
        _die("이건 앱 레벨 토큰(App-Level Token)입니다. 이 도구는 쓰지 않습니다.\n\n" + TOKEN_HELP)

    try:
        who = slack.auth_test(token)
    except slack.SlackError as e:
        _die(f"토큰 확인 실패.\n{e}\n\n" + TOKEN_HELP)

    # 접두사보다 이쪽이 확실하다. 봇 토큰이면 auth.test 응답에 bot_id 가 있고,
    # 사용자 토큰이면 없다. 접두사가 낯선 토큰도 여기서 걸린다.
    if not who.get("bot_id"):
        _die(
            f"이 토큰은 봇이 아니라 사용자({who.get('user')}) 자격입니다.\n\n"
            + TOKEN_HELP
        )

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
                "  → 3단계 3번처럼 채널 링크 URL 끝의 C... 문자열인지 확인하세요.\n"
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


def cmd_watch(argv: list[str]) -> None:
    """스레드에 사람 답글이 생길 때까지 기다렸다가 종료한다.

    MCP 툴은 에이전트가 불러야 도는 pull 이라, 다른 작업을 하는 동안 도착한
    메시지를 알아채지 못한다. 이 명령은 그 구멍을 메운다 — 백그라운드로 띄워
    두면 **프로세스가 끝나는 것 자체가 깨움 신호**가 된다.

    받아 적는 일은 하지 않는다. 그건 Slack 이 이미 하고 있다. ntfy 가 보유자와
    감시자를 둘로 나눠야 했던 이유(브로커에 이력이 없어 누군가 계속 받아 적어야
    했던 것)가 여기서는 사라진다. 이 프로세스가 죽어도 메시지는 스레드에 남아
    있으므로, 다시 띄우면 그동안 온 것을 그대로 본다.
    """
    thread = _arg(argv, "--thread")
    if not thread:
        _die("--thread <스레드 ts> 가 필요합니다. slack_chat_open 이 돌려준 값입니다.")
    max_hours = float(_arg(argv, "--max-hours") or 8)
    interval = float(_arg(argv, "--interval") or 5)

    conf = cfg.load()
    if conf is None:
        _die("설정이 없습니다.")

    bot_user_id = str(slack.auth_test(conf.bot_token).get("user_id", ""))

    # 지금 이미 있는 것은 "새 메시지"가 아니다. 시작 시점을 기준선으로 잡는다.
    try:
        seen = {
            m.get("ts", "")
            for m in slack.conversations_replies(conf.bot_token, conf.channel, thread)
        }
    except slack.SlackError as e:
        _die(f"스레드를 읽지 못했습니다.\n{e}")

    deadline = time.time() + max_hours * 3600
    while time.time() < deadline:
        time.sleep(interval)
        try:
            msgs = slack.conversations_replies(conf.bot_token, conf.channel, thread)
        except slack.SlackError:
            # 한 번의 네트워크 실패로 감시를 끝내지 않는다. 다음 주기에 다시 본다.
            continue

        fresh = [
            m for m in msgs
            if m.get("ts") not in seen and chat.is_human(m, bot_user_id)
        ]
        if fresh:
            for m in fresh:
                first = chat.describe(m).replace("\n", " ")[:120]
                print(f"MESSAGE\t{m.get('ts')}\t{first}")
            return
        seen.update(m.get("ts", "") for m in msgs)

    # 수명이 찼을 뿐 스레드는 멀쩡하다. 다시 띄우면 된다.
    print("WATCH_RECYCLE")


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
        target = slack.probe(conf.bot_token, conf.channel)
    except slack.SlackError as e:
        _die(f"\n확인 실패.\n{e}")

    print(f"워크스페이스: {who.get('team')}")
    print(f"봇: {who.get('user')}")
    print(f"받는 곳: {target['label']}")
    if target["kind"] == "channel" and not target["ready"]:
        _die(f"봇이 채널에 없습니다.\n  → /invite @{who.get('user')}")
    print("\n정상입니다.")


USAGE = """\
claude-slack-bridge — Claude Code 세션과 Slack 을 잇는 MCP 서버

  claude-slack-bridge            MCP 서버로 동작 (claude mcp add 가 이렇게 부른다)
  claude-slack-bridge init       설정 생성 — 준비가 안 됐으면 절차를 안내한다
  claude-slack-bridge manifest   Slack 콘솔에 붙여넣을 앱 매니페스트를 출력한다
                                 --name "이름" 으로 봇 이름을 바꿀 수 있다
  claude-slack-bridge token-help  Bot User OAuth Token 받는 법을 출력한다
  claude-slack-bridge watch --thread <ts>
                                 답글이 오면 종료한다. 백그라운드로 띄우면
                                 그 종료가 곧 깨움 신호가 된다
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
    elif cmd == "token-help":
        print(TOKEN_HELP)
    elif cmd == "watch":
        cmd_watch(argv[1:])
    elif cmd == "doctor":
        cmd_doctor(argv[1:])
    elif cmd in ("-h", "--help", "help"):
        print(USAGE)
    else:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)
