"""MCP 서버.

Claude Code 가 stdio 로 이 프로세스를 띄우고, 세션이 끝나면 같이 죽는다.
호스팅도 포트도 없다.

세션마다 프로세스가 하나씩 뜨므로 세션 격리가 공짜로 따라온다. ntfy 시절
토픽 8개를 미리 만들어 두고 뺏어 쓰던 장치가 여기서는 필요 없는 이유다.
"""

from __future__ import annotations

import os
import subprocess

from mcp.server.mcpserver import MCPServer

from . import chat as chatmod
from . import config as cfg
from . import slack
from . import threads

INSTRUCTIONS = """\
자리를 비운 사용자의 폰(Slack)으로 알림을 보내는 다리다.

`slack_notify` 는 조사 완료·장시간 작업 종료·사용자 결정이 필요한 갈림길처럼
"지금 알아야 하는" 순간에만 쓴다. 즉답이나 수 초 만에 끝난 일에는 쓰지 않는다.

`slack_chat_open` 으로 대화를 열 때는 label 에 프로젝트와 작업명을 함께 적는다.
세션마다 스레드가 하나씩 생기는데 폰에서는 전부 같은 봇 이름으로 보이므로,
그 라벨이 어느 작업인지 가릴 유일한 단서다.

대화를 열었으면 **keeper-start 를 먼저, watch 를 다음에** 띄운다. 툴이 돌려주는
명령을 위에서부터 그대로 쓴다.

  - `keeper-start` — 지킴이를 떼어내 띄운다. Slack 답장을 파일에 받아 적고,
    마감·연장·"핑" 을 지킨다. Esc 를 눌러도 죽지 않는다.
  - `watch` — 백그라운드로 띄운다. 지킴이가 적은 파일만 보다가 답장이 오면 종료해
    나를 깨운다. 그때 결과를 확인하고 답한 뒤 **다시 띄운다.**

Slack 을 듣는 것은 지킴이 하나뿐이라, 순서를 뒤집으면 감시자는 볼 파일을 만드는
지킴이가 없어 `NO_KEEPER` 로 즉시 끝난다. `NO_KEEPER` 나 `KEEPER_GONE` 이 나오면
keeper-start 를 다시 띄운 뒤 watch 를 다시 띄운다. 마감을 상시 지키는 쪽도 지킴이
하나뿐이라, 죽은 채 두면 마감이 지나도 스레드가 닫히지 않는다. 감시자가 죽어 있어도
지킴이가 답장을 파일에 남기므로 다음 감시자가 이어받는다. 잃는 것은 즉시성뿐이다.
세션이 끝나면 지킴이가 부모의 죽음을 확인해 Slack 스레드도 닫는다.

세션이 재시작됐거나 다른 세션이 연 스레드를 이어받을 때는 `slack_chat_open` 이
아니라 `slack_chat_attach` 를 쓴다. 새로 열면 폰에 같은 작업의 스레드가 쌓인다.
붙을 대상은 `slack_chat_list` 로 찾는다.

스레드는 기본적으로 사용자와의 DM 에 열린다. 팀이 같이 봐야 하는 일이면
`channel="#이름"` 으로 지정한다. 채널에서는 봇을 @멘션한 소유자의 답글만 지시로
받는다 — 옆에서 오가는 대화가 작업을 움직이면 안 되기 때문이다.

기본 유지 시간은 10시간이다. 사용자는 스레드에 한 줄 적어 언제든 바꿀 수 있다 —
"연장 3시간", "마감 18:00", "닫기". 그런 줄은 감시자가 처리하고 세션을 깨우지
않으므로, 내가 따로 할 일은 없다.

설정이 없으면 조용히 아무것도 하지 않는다. 그것 때문에 작업을 멈추지 말 것.
"""

server = MCPServer(
    name="claude-slack-bridge",
    version="0.22.1",
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
def slack_notify(
    text: str, title: str | None = None, channel: str | None = None
) -> str:
    """알림을 보낸다.

    Args:
        text: 보낼 내용. 무슨 작업이 어떻게 끝났는지 한 줄로.
        title: 앞에 굵게 붙일 라벨. 프로젝트나 태스크 이름.
        channel: 보낼 곳. 생략하면 열린 스레드, 없으면 설정의 기본값.
    """
    conf = cfg.load()
    if conf is None:
        return SETUP_HINT

    body = f"*{title}*\n{text}" if title else text
    # 대화가 열려 있으면 그 스레드로 보낸다. 알림과 답장이 한 자리에 모여야
    # 폰에서 맥락이 끊기지 않는다.
    # 목적지를 명시하면 그곳으로 (스레드에 묶지 않는다). 아니면 열린 스레드.
    if channel:
        try:
            dest = slack.resolve_target(conf.bot_token, channel, conf.channel)
        except slack.SlackError as e:
            return f"보내지 못했습니다.\n{e}"
        thread = None
    else:
        dest = chatmod._chat.channel if chatmod._chat else conf.channel
        thread = chatmod._chat.thread_ts if chatmod._chat else None

    # 감시자가 없으면 답장이 바로 읽히지 않는다. 그 사실을 **폰에서** 알아야
    # 한다 — 답을 적어놓고 읽히는 줄 아는 것이 제일 나쁘다.
    unwatched = bool(thread) and not threads.watcher_alive(thread)
    if unwatched:
        body += "\n\n_지금은 감시 중이 아닙니다. 여기 답글을 달아도 바로 읽히지 않습니다._"
    try:
        res = slack.post_message(conf.bot_token, dest, body, thread_ts=thread)
    except slack.BodyRejected as e:
        return f"보내지 않았습니다 — {e}"
    except slack.SlackError as e:
        return f"보내지 못했습니다.\n{e}"
    sent = f"보냈습니다 (ts={res.get('ts', '?')})"
    if unwatched:
        sent += (
            "\n경고: 이 스레드를 지키는 감시자가 없습니다. 사용자가 답글을 달아도"
            " 알아채지 못합니다.\n  → claude-slack-bridge watch --thread "
            f"{thread} 를 백그라운드로 띄우세요."
        )
    return sent


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


def _keeper_parent_arg() -> str:
    self_pid = os.getpid()
    fallback = f" --parent-pid {self_pid}"
    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return fallback
    if result.returncode != 0:
        return fallback

    processes = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        try:
            pid, parent = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        processes[pid] = (parent, fields[2])

    # 실측에서는 claude와 MCP 서버 사이에 uvx가 있고, 셸까지 끼는 경우도 있었다.
    # 실행 파일 이름을 정확히 비교해야 이름에 claude가 든 이 서버를 세션으로
    # 오인하지 않는다.
    pid = self_pid
    for _ in range(6):
        process = processes.get(pid)
        if not process:
            break
        parent, command = process
        executable = command.lstrip().split(None, 1)[0]
        if os.path.basename(executable) == "claude":
            return f" --parent-pid {pid}"
        if parent <= 1 or parent == pid:
            break
        pid = parent

    # 세션을 못 찾았을 때 중간 래퍼를 찍으면 래퍼만 먼저 죽어 대화를 조기에
    # 닫을 수 있다. 세션과 함께 죽는 MCP 서버 자신이 더 안전한 대리다.
    return fallback


@server.tool(
    name="slack_chat_open",
    title="폰과 대화 열기",
    description=(
        "Slack 에 스레드를 하나 열고 이 세션에 묶는다. 사용자가 자리를 비우면서 "
        "폰으로 이어서 얘기하자고 할 때 쓴다. label 에는 반드시 지금 무슨 일을 "
        "하고 있는지 — 프로젝트와 작업명을 함께 — 적는다. 사용자의 폰에는 세션이 "
        "여럿 떠 있어도 전부 같은 봇 이름으로 보이므로, 이 라벨이 어느 작업의 "
        "스레드인지 알아볼 유일한 단서다."
    ),
)
def slack_chat_open(
    hours: float = 10.0, label: str | None = None, channel: str | None = None
) -> str:
    """대화를 연다.

    Args:
        hours: 스레드를 유지할 시간. 기본 10시간.
        channel: 스레드를 열 곳. 생략하면 설정의 기본값(대개 사용자와의 DM).
            팀이 같이 봐야 하는 일이면 "#채널명" 으로 지정한다. 채널에서는
            봇을 @멘션한 소유자의 답글만 지시로 받는다.
        label: 스레드 첫 줄에 붙일 라벨. "프로젝트 · 작업명" 형태로 적는다.
            예: "cafegate 마이그 · 목록 정렬 전수조사".
            생략하면 작업 디렉터리 이름이 들어가는데, 같은 레포에서 작업을
            둘 돌리면 구분되지 않으므로 되도록 직접 적는다.
    """
    conf = cfg.load()
    if conf is None:
        return SETUP_HINT
    # 라벨이 없으면 작업 디렉터리 이름을 쓴다. 세션이 여럿일 때 폰에서 스레드를
    # 구분하는 유일한 단서라, 비워두면 넷 다 같은 이름으로 보인다.
    label = label or os.path.basename(os.getcwd()) or None
    try:
        target = slack.resolve_target(conf.bot_token, channel or "", conf.channel)
        c = chatmod.open_chat(conf.bot_token, target, hours, label, conf.owner_id)
    except slack.SlackError as e:
        return f"열지 못했습니다.\n{e}"
    # thread ts 를 돌려주는 이유: 이 프로세스 밖에서 도는 감시자(watch)가
    # 어느 스레드를 지켜볼지 알아야 한다. MCP 툴은 내가 부를 때만 도는 pull 이라
    # 그것만으로는 작업 중에 오는 메시지를 알아채지 못한다.
    where = "DM" if c.channel.startswith("D") else c.channel
    parent_arg = _keeper_parent_arg()
    return (
        f"열렸습니다({where}). 마감까지 {chatmod.fmt_remaining(c.remaining)} 남았습니다.\n"
        f"thread={c.thread_ts}\n"
        "다음 둘을 반드시 이 순서대로 띄운다. 뒤집으면 watch가 NO_KEEPER로 즉시 끝난다:\n"
        f"  claude-slack-bridge keeper-start --thread {c.thread_ts}{parent_arg}   (떼어냄 — Esc 에 안 죽음)\n"
        f"  claude-slack-bridge watch --thread {c.thread_ts}   (백그라운드 — 나를 깨움)"
    )


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
    name="slack_chat_attach",
    title="기존 스레드에 붙기",
    description=(
        "이미 있는 Slack 스레드에 이 세션을 묶는다. 세션이 재시작돼 자기가 열어둔 "
        "스레드로 돌아갈 때, 또는 다른 세션이 연 스레드를 이어받을 때 쓴다. "
        "머리글을 새로 올리지 않으므로 폰에 같은 작업의 스레드가 쌓이지 않는다."
    ),
)
def slack_chat_attach(
    thread_ts: str,
    channel: str | None = None,
    hours: float | None = None,
    label: str | None = None,
) -> str:
    """기존 스레드에 붙는다.

    Args:
        thread_ts: 붙을 스레드의 ts. Slack 링크 끝의 p1787803636465309 는
            1787803636.465309 로 읽는다(뒤에서 여섯 자리 앞에 점).
        channel: 그 스레드가 있는 대화. 기록이 있으면 생략해도 된다.
        hours: 마감을 다시 잡을 때만. 생략하면 기록된 마감을 잇는다.
        label: 라벨을 바꿀 때만.
    """
    conf = cfg.load()
    if conf is None:
        return SETUP_HINT
    try:
        # 기록이 없는 스레드(영속화 이전에 열린 것)에도 붙을 수 있어야 한다.
        # 그때는 설정의 기본 대화에 있다고 본다 — 대개 맞고, 틀리면 읽기가
        # 실패하면서 바로 드러난다.
        target = slack.resolve_target(conf.bot_token, channel or "", conf.channel)
        c = chatmod.attach(conf.bot_token, thread_ts.strip(), target, hours, label)
    except (chatmod.NoChat, slack.SlackError) as e:
        return f"붙지 못했습니다.\n{e}"

    where = "DM" if c.channel.startswith("D") else c.channel
    parent_arg = _keeper_parent_arg()
    return (
        f"붙었습니다({where}). 마감까지 {chatmod.fmt_remaining(c.remaining)} 남았습니다.\n"
        f"thread={c.thread_ts}\n"
        "다음 둘을 반드시 이 순서대로 띄우세요. 뒤집으면 watch가 NO_KEEPER로 즉시 끝납니다:\n"
        f"  claude-slack-bridge keeper-start --thread {c.thread_ts}{parent_arg}\n"
        f"  claude-slack-bridge watch --thread {c.thread_ts}"
    )


@server.tool(
    name="slack_chat_list",
    title="열려 있는 스레드 보기",
    description="아직 닫히지 않은 스레드를 보여준다. 붙을 대상을 찾을 때 쓴다.",
)
def slack_chat_list() -> str:
    import time as _t

    rows = chatmod.open_threads()
    if not rows:
        return "열려 있는 스레드 기록이 없습니다."
    out = []
    for r in rows:
        ts = r.get("thread_ts", "?")
        until = _t.strftime("%H:%M", _t.localtime(float(r.get("deadline") or 0)))
        watched = "감시중" if threads.watcher_alive(ts) else "감시없음"
        out.append(f"{ts}  마감 {until}  {watched}  {r.get('label', '')}")
    return "\n".join(out)

@server.tool(
    name="slack_chat_extend",
    title="대화 시간 연장",
    description="열린 대화의 마감을 미룬다.",
)
def slack_chat_extend(hours: float = 2.0) -> str:
    conf = cfg.load()
    if conf is None:
        return SETUP_HINT
    try:
        c = chatmod.extend(conf.bot_token, hours)
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
    thread_ts = chatmod._chat.thread_ts if chatmod._chat else None
    chatmod.close_chat(conf.bot_token)
    if thread_ts:
        return (
            "닫았습니다. 머리글에 취소선을 그었습니다.\n"
            "돌고 있는 감시자가 있으면 함께 내려주세요 "
            f"(watch --thread {thread_ts})."
        )
    return "닫았습니다."


def main() -> None:
    server.run(transport="stdio")
