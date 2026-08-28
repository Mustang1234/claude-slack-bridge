"""명령줄 진입점.

인자 없이 실행하면 MCP 서버로 동작한다. `claude mcp add` 가 그렇게 부른다.
사람이 직접 부르는 것은 `init`, `manifest`, `doctor` 셋이다.

`init` 은 안내까지 한다. uvx 로 설치한 사람에게는 레포가 없어서 문서 파일을
열어볼 수 없기 때문이다. 화면 안에서 끝나야 한다.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
import time
from importlib import resources

from . import chat
from . import config as cfg
from . import slack
from . import threads

MANIFEST_NAME = "slack-app-manifest.yaml"

# 받자마자 알리지 않고 이만큼 기다린다. 그 사이 진짜 답이 붙으면 알리지 않는다 —
# 매번 붙는 "받았습니다" 는 정보가 아니라 소음이다.
ACK_DELAY = 10.0


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

    owner_id = ""
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
        owner_id = user_id
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
        # 채널에서는 아무나 세션에 지시할 수 없어야 한다. 누구의 멘션을 내 말로
        # 받을지 여기서 정해둔다. 비워두면 제한 없이 받는다.
        print("  채널에서는 봇을 @멘션한 지정된 사람의 답글만 지시로 받습니다.")
        print("  Slack 프로필 → 더보기(⋮) → '멤버 ID 복사' 로 얻습니다.")
        owner_id = input("  내 멤버 ID (U 로 시작, 비우면 제한 없음): ").strip()
        print()

    try:
        slack.post_message(token, channel, "연결됐습니다. 이제 여기로 알림이 옵니다.")
    except slack.SlackError as e:
        _die(f"테스트 메시지 전송 실패.\n{e}")

    path = cfg.save(token, channel, owner_id)
    print("테스트 메시지를 보냈습니다. 폰에서 확인해 보세요.")
    print(f"설정 저장: {path} (권한 600)\n")
    print("━━ 마지막 · Claude Code 에 붙이기 ━━\n")
    print("  claude mcp add claude-slack-bridge -s user -- uvx claude-slack-bridge\n")


def _keep_awake() -> "subprocess.Popen | None":
    """감시하는 동안 맥이 잠들지 않게 한다.

    맥이 자면 폴링이 멈춘다. 매체를 ntfy 에서 Slack 으로 바꿔도 이건 그대로다 —
    잠든 컴퓨터는 누구도 깨우지 못한다.

    `-w <내 pid>` 로 내 수명에 묶는다. 감시자가 죽으면 커널이 알아서 풀어주므로
    고아 assertion 이 남지 않는다.
    """
    if sys.platform != "darwin" or not shutil.which("caffeinate"):
        return None
    try:
        return subprocess.Popen(
            ["caffeinate", "-i", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None


def _apply_command(token, channel, thread, label, kind, value, state) -> bool:
    """폰에서 온 마감 조작 명령을 처리한다. 닫혔으면 True."""
    now = time.time()
    if kind == "ping":
        # 감시자가 살아있는지 사용자가 직접 확인하는 길.
        #
        # 감시자가 죽어 있으면 이 줄에 아무 응답이 없다 — 그 침묵이 곧 답이다.
        # 죽은 감시자는 "나 죽었다" 고 말할 수 없으므로, 확인은 살아있는 쪽이
        # 대답하는 형태여야 한다.
        until = time.strftime("%H:%M", time.localtime(float(state.get("deadline") or now)))
        try:
            slack.post_message(
                token, channel,
                f"_살아 있습니다. 마감 {until} "
                f"({chat.fmt_remaining(float(state.get('deadline') or now) - now)} 남음)._",
                thread_ts=thread,
            )
        except slack.SlackError:
            pass
        return False

    if kind == "close":
        chat.close_thread(token, channel, thread, label, "폰에서 닫음")
        return True

    if kind == "until":
        hour, minute = value
        new_deadline = chat.deadline_from_hhmm(hour, minute, now)
    else:
        base = max(float(state.get("deadline") or now), now)
        new_deadline = base + float(value) * 3600

    threads.patch(thread, deadline=new_deadline, warned=False)
    try:
        slack.chat_update(token, channel, thread, chat.header_text(label, new_deadline))
        slack.post_message(
            token, channel,
            f"마감을 {time.strftime('%H:%M', time.localtime(new_deadline))} 로 바꿨습니다 "
            f"({chat.fmt_remaining(new_deadline - now)} 남음).",
            thread_ts=thread,
        )
    except slack.SlackError:
        pass
    return False


def cmd_watch(argv: list[str]) -> None:
    """지킴이가 받아 적은 답글을 보고 종료해서 세션을 깨운다.

    MCP 툴은 에이전트가 불러야 도는 pull 이라, 다른 작업을 하는 동안 도착한
    메시지를 알아채지 못한다. 이 명령이 그 구멍을 메운다 — 백그라운드로 띄워
    두면 **프로세스가 끝나는 것 자체가 깨움 신호**가 된다.

    Slack 은 떼어낸 지킴이 하나만 읽는다. 감시자까지 네트워크를 읽으면 같은 API를
    중복 호출할 뿐 아니라 상태 파일도 함께 밀게 된다. 감시자는 durable inbox 만
    보므로 Esc 에 죽어도 다음 감시자가 그동안 받아 적힌 말을 이어받는다.
    """
    thread = _arg(argv, "--thread")
    if not thread:
        _die("--thread <스레드 ts> 가 필요합니다. slack_chat_open 이 돌려준 값입니다.")
    # 지킴이(5초)와 달리 여기는 로컬 파일만 읽는다. 네트워크도 API 예산도 쓰지
    # 않으므로 촘촘히 봐도 비용이 없고, 그만큼 깨우는 시각이 당겨진다.
    interval = float(_arg(argv, "--interval") or 1)

    state = threads.load(thread)
    if not state:
        _die(
            f"이 스레드의 기록이 없습니다: {thread}\n"
            "  → slack_chat_open 으로 연 스레드여야 합니다."
        )
    if state.get("closed"):
        _die("이미 닫힌 스레드입니다.")

    # 감시자가 둘 뜨면 같은 답글로 세션을 두 번 깨우고, 커서를 서로 덮어쓴다.
    # 실측: 같은 스레드에 둘을 띄웠더니 한 메시지로 두 번 깨어났다.
    other = state.get("watcher_pid")
    if other != os.getpid() and threads.watcher_alive(thread):
        print(f"ALREADY_WATCHING\t{other}")
        return

    if not threads.inbox_keeper_alive(thread):
        print("NO_KEEPER")
        return

    awake = _keep_awake()
    state = threads.patch(thread, watcher_pid=os.getpid())

    # 이 커서는 Slack 수신 위치가 아니라 세션에 전달을 끝낸 위치다. 지킴이의
    # keeper_seen_ts 와 섞으면 지킴이가 적어둔 말을 감시자가 건너뛸 수 있다.
    last_seen = float(state.get("last_seen_ts") or 0)

    # 생존·닫힘 확인은 ps 를 띄운다. 답장 확인(파일 한 번 읽기)만큼 자주 돌리면
    # 매초 프로세스를 포크하게 되므로, 촘촘해진 것은 답장 쪽만이고 이쪽은 원래
    # 주기를 지킨다.
    health_every = 5.0
    last_health = 0.0

    try:
        while True:
            now = time.monotonic()
            if now - last_health >= health_every:
                last_health = now
                state = threads.load(thread) or state
                if state.get("closed"):
                    print("CLOSED")
                    return

                if not threads.inbox_keeper_alive(thread):
                    print("KEEPER_GONE")
                    return

            fresh = threads.read_inbox(thread, last_seen)
            if fresh:
                for record in fresh:
                    print(
                        f"MESSAGE\t{record.get('ts')}\t{record.get('summary', '')}",
                        flush=True,
                    )
                last_seen = max(float(record.get("ts", 0)) for record in fresh)
                threads.patch(thread, last_seen_ts=last_seen)
                return

            time.sleep(interval)
    finally:
        if awake and awake.poll() is None:
            awake.terminate()
        # 내가 기록한 것일 때만 지운다. 남이 이미 이어받았으면 그쪽 기록이다.
        st = threads.load(thread) or {}
        if st.get("watcher_pid") == os.getpid():
            threads.patch(thread, watcher_pid=None)


def cmd_keeper(argv: list[str]) -> None:
    """스레드를 지킨다 — 깨우지는 않는다.

    감시자(watch)는 harness 가 들고 있어서 사용자가 Esc 를 누르면 같이 죽는다.
    그런데 떼어내면(setsid) 이번엔 종료가 깨움 신호로 쓰이지 못한다. 한 프로세스가
    둘을 겸할 수 없어서 역할을 나눈다.

    지킴이는 깨우지 않으므로 떼어내도 잃을 것이 없다. Esc 를 눌러도 살아남아
    마감을 지키고, 사용자가 적은 "연장 3시간"·"핑" 에 답하고, 새 메시지에
    "받았습니다" 를 남긴다. 세션이 죽으면 부모를 잃은 것을 보고 스스로 끝낸다.

    커서는 감시자와 따로 쓴다. 같은 값을 두 프로세스가 밀면 한쪽이 본 것을
    다른 쪽이 못 본 것으로 만든다.
    """
    thread = _arg(argv, "--thread")
    if not thread:
        _die("--thread <스레드 ts> 가 필요합니다.")
    interval = float(_arg(argv, "--interval") or 5)
    parent = _arg(argv, "--parent-pid")

    conf = cfg.load()
    if conf is None:
        _die("설정이 없습니다.")
    state = threads.load(thread)
    if not state:
        _die(f"이 스레드의 기록이 없습니다: {thread}")
    if state.get("closed"):
        _die("이미 닫힌 스레드입니다.")

    channel = state["channel"]
    label = state.get("label") or "Claude 세션"
    awake = _keep_awake()
    state = threads.patch(
        thread,
        keeper_pid=os.getpid(),
        keeper_protocol=threads.KEEPER_PROTOCOL,
    )

    try:
        # keeper-start 는 프로세스가 살아 있는지만 확인하고 돌아온다. 인증이 느린 날
        # PID 기록까지 늦추면 바로 뒤의 watch가 지킴이가 없다고 오판한다.
        bot_user_id = str(slack.auth_test(conf.bot_token).get("user_id", ""))
        require_mention = bool(state.get("require_mention"))
        owner_id = state.get("owner_id") or conf.owner_id
        seen = float(state.get("keeper_seen_ts") or state.get("last_seen_ts") or thread)
        pending: list[float] = []   # 수신확인을 보낼지 유예 중인 메시지들
        while True:
            if parent and not _pid_alive(int(parent)):
                print("PARENT_GONE")
                current = threads.load(thread) or state
                if current.get("closed"):
                    return
                last_seen = float(current.get("last_seen_ts") or 0)
                unread = threads.read_inbox(thread, last_seen)
                reason = "Claude 세션 종료"
                if unread:
                    reason += f"\n읽지 못한 메시지: {len(unread)}건"
                if not chat.close_thread(
                    conf.bot_token, channel, thread, label, reason,
                ):
                    print("PARENT_CLOSE_FAILED")
                return

            state = threads.load(thread) or state
            if state.get("closed"):
                print("CLOSED")
                return
            deadline = float(state.get("deadline") or 0)
            remaining = deadline - time.time()

            if remaining <= 0:
                chat.close_thread(conf.bot_token, channel, thread, label, "마감 시각 도달")
                print("DEADLINE_CLOSED")
                return

            if not state.get("warned") and remaining <= chat.WARN_LEAD:
                try:
                    slack.post_message(
                        conf.bot_token, channel, chat.warn_text(remaining), thread_ts=thread
                    )
                    threads.patch(thread, warned=True)
                except slack.SlackError:
                    pass

            try:
                msgs = slack.conversations_replies(conf.bot_token, channel, thread)
            except slack.SlackError:
                # 지킴이는 어지간해서는 죽지 않아야 한다. 죽으면 Esc 뒤에 폰이
                # 완전한 침묵이 되고, 그때는 확인할 방법도 함께 사라진다.
                time.sleep(interval)
                continue

            fresh = [
                m for m in msgs
                if float(m.get("ts", 0)) > seen
                and chat.is_for_me(m, bot_user_id, owner_id, require_mention)
            ]
            fresh.sort(key=lambda m: float(m.get("ts", 0)))

            for m in fresh:
                message_ts = float(m.get("ts", 0))
                cmd = chat.parse_command(chat.strip_mention(m.get("text", ""), bot_user_id))
                if cmd:
                    if _apply_command(
                        conf.bot_token, channel, thread, label,
                        cmd[0], cmd[1], threads.load(thread) or state,
                    ):
                        print("CLOSED_BY_USER")
                        return
                    seen = message_ts
                    threads.patch(thread, keeper_seen_ts=seen)
                    continue

                summary = chat.describe(m).replace("\n", " ")[:120]
                threads.append_inbox(thread, m, summary)
                # append와 fsync가 끝난 뒤에만 Slack 커서를 민다. 이 순서가 뒤집히면
                # 둘 사이의 크래시가 메시지를 영구히 건너뛰게 한다.
                seen = message_ts
                threads.patch(thread, keeper_seen_ts=seen)

                # 예고를 본 뒤 온 일반 답글은 자리에 있다는 신호다. inbox 기록과
                # 커서 전진을 먼저 끝내야 연장 도중 죽어도 메시지 자체는 남는다.
                current = threads.load(thread) or state
                if current.get("warned"):
                    base = max(float(current.get("deadline") or time.time()), time.time())
                    new_deadline = base + chat.AUTO_EXTEND_HOURS * 3600
                    threads.patch(thread, deadline=new_deadline, warned=False)
                    try:
                        slack.chat_update(
                            conf.bot_token, channel, thread,
                            chat.header_text(label, new_deadline),
                        )
                    except slack.SlackError:
                        pass

                # 지금 답하지 않고 적어둔다. 대부분은 몇 초 안에 진짜 답이
                # 붙으므로, 그때 "받았습니다" 는 소음일 뿐이다.
                pending.append(message_ts)

            # 유예가 지난 것 중 아직 답이 안 붙은 것만 알린다.
            still = []
            for ts in pending:
                if time.time() - ts < ACK_DELAY:
                    still.append(ts)
                    continue
                answered = any(
                    m.get("bot_id") and float(m.get("ts", 0)) > ts for m in msgs
                )
                if answered:
                    continue
                note = (
                    "_받았습니다. 작업 중이라 답이 조금 늦어집니다._"
                    if threads.watcher_alive(thread)
                    else "_받았습니다. 세션이 지금 듣고 있지 않아 답이 늦어질 수 있습니다._"
                )
                try:
                    slack.post_message(conf.bot_token, channel, note, thread_ts=thread)
                except slack.SlackError:
                    still.append(ts)   # 못 보냈으면 다음 주기에 다시 시도
            pending = still

            time.sleep(interval)
    finally:
        if awake and awake.poll() is None:
            awake.terminate()
        st = threads.load(thread) or {}
        if st.get("keeper_pid") == os.getpid():
            threads.patch(thread, keeper_pid=None, keeper_protocol=None)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # 판정 불가를 죽음으로 보면 살아 있는 대화를 닫는다. 마감까지 스레드가
        # 남는 편이 사용자의 말을 조기에 끊는 것보다 복구하기 쉽다.
        return True
    return True


def cmd_keeper_start(argv: list[str]) -> None:
    """지킴이를 떼어내서 띄운다.

    macOS 에는 setsid 가 없다. 파이썬의 start_new_session 으로 새 세션을 만들어
    떼어낸다 — 셸이나 harness 가 프로세스 그룹째 정리해도 살아남는다.

    부모 pid 를 넘겨서, 세션이 죽으면 지킴이도 스스로 끝나게 한다. 안 그러면
    아무도 안 읽는 스레드를 영원히 지키는 프로세스가 남는다.
    """
    thread = _arg(argv, "--thread")
    if not thread:
        _die("--thread <스레드 ts> 가 필요합니다.")

    if threads.inbox_keeper_alive(thread):
        print(f"ALREADY_KEEPING\t{(threads.load(thread) or {}).get('keeper_pid')}")
        return
    if threads.keeper_alive(thread):
        stale_pid = (threads.load(thread) or {}).get("keeper_pid")
        print(f"STALE_KEEPER\t{stale_pid}")
        print("기존 지킴이 프로세스를 끝낸 뒤 keeper-start 를 다시 실행하세요.")
        return

    cmd = [sys.argv[0], "keeper", "--thread", thread]
    for flag in ("--interval", "--parent-pid"):
        val = _arg(argv, flag)
        if val:
            cmd += [flag, val]
    # 부모를 자동으로 잡지 않는다. keeper-start 를 감싼 셸·uvx 는 곧바로
    # 사라지므로 그것을 부모로 삼으면 지킴이가 즉시 PARENT_GONE 으로 끝난다.
    # 세션 pid 를 아는 호출자만 --parent-pid 로 명시한다. 없으면 마감까지 산다 —
    # 마감이 있으므로 영원히 남지는 않는다.

    log = threads.THREADS_DIR / f"{thread}.keeper.log"
    threads.THREADS_DIR.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as fh:
        proc = subprocess.Popen(
            cmd, stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    # 기동 직후 죽는 경우(설정 없음 등)를 바로 드러낸다.
    time.sleep(1.5)
    if proc.poll() is not None:
        _die(f"지킴이가 바로 종료됐습니다. 로그: {log}")
    print(f"KEEPING\t{proc.pid}")


def cmd_targets(argv: list[str]) -> None:
    """보낼 수 있는 곳을 보여준다.

    어디로 보낼 수 있는지 모르는 상태에서 채널 이름을 찍어보는 것은 추측이다.
    """
    conf = cfg.load()
    if conf is None:
        _die("설정이 없습니다.")
    print(f"기본값: {conf.channel}" + ("  (봇과의 DM)" if conf.channel.startswith("D") else ""))
    if conf.owner_id:
        print(f"소유자: {conf.owner_id}  (채널에서는 이 사람의 멘션만 지시로 받음)")
    convs = slack.my_conversations(conf.bot_token)
    if not convs:
        print("\n봇이 들어가 있는 채널이 없습니다.")
        print("  → 채널에서 쓰려면 그 채널에서 /invite 로 봇을 초대하세요.")
        return
    print("\n봇이 들어가 있는 채널:")
    for c in convs:
        print(f"  {'비공개' if c['private'] else '공개  '}  #{c['name']:<24} {c['id']}")


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
  claude-slack-bridge keeper-start --thread <ts>
                                 지킴이를 떼어내 띄운다. 마감·연장·핑을 지키며
                                 Esc 에 죽지 않는다 (깨우지는 않는다)
  claude-slack-bridge targets    보낼 수 있는 곳(기본 DM · 초대된 채널)을 본다
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
    elif cmd == "targets":
        cmd_targets(argv[1:])
    elif cmd == "keeper":
        cmd_keeper(argv[1:])
    elif cmd == "keeper-start":
        cmd_keeper_start(argv[1:])
    elif cmd == "doctor":
        cmd_doctor(argv[1:])
    elif cmd in ("-h", "--help", "help"):
        print(USAGE)
    else:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)
