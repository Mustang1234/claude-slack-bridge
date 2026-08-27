"""Slack Web API 얇은 클라이언트.

의존성을 늘리지 않으려고 표준 라이브러리만 쓴다. 설치하는 사람 입장에서
받을 것이 적을수록 좋고, 여기서 하는 일은 POST 한 번이 전부다.

수신도 폴링(conversations.replies)이라 인바운드 연결이 없다. 회사 방화벽 뒤에서
그대로 도는 이유가 이것이다.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://slack.com/api/"
TIMEOUT = 15

# Slack 은 text 를 40,000자까지 받지만 그 길이는 알림으로 쓸 물건이 아니다.
# 폰 알림에서 읽히는 선에서 자른다.
BODY_MAX = 3500

# 나가는 본문의 비밀값 차단.
#
# ntfy 시절에는 절대경로·내부 호스트명까지 막았다. 토픽명만 알면 누구나 읽는
# 공개 채널이었기 때문이다. Slack 비공개 채널은 인증된 초대자만 읽으므로 그
# 제약은 걷어낸다 — 경로와 티켓번호가 보여야 알림이 쓸모 있다.
#
# 반면 비밀값은 채널이 사적이어도 내보내지 않는다. 사고가 나면 되돌릴 수 없고,
# 애초에 알림 본문에 있을 이유가 없다.
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"), "개인키 블록"),
    (re.compile(r"://[^/\s]+:[^/\s]+@"), "URL 에 박힌 인증정보"),
    (re.compile(r"xox[abprs]-[A-Za-z0-9-]{12,}"), "Slack 토큰"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "GitHub 토큰"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS 액세스 키"),
]

# 실패했을 때 무엇을 해야 하는지가 바로 나와야 한다. Slack 이 주는 코드만으로는
# 설치하는 사람이 다음 행동을 못 정한다.
ERROR_HINTS = {
    "not_in_channel": "봇이 그 채널에 없습니다. Slack 에서 `/invite @Claude Bridge` 로 초대하세요.",
    "channel_not_found": "채널 ID 를 찾을 수 없습니다. 비공개 채널이면 봇을 먼저 초대해야 보입니다.",
    "invalid_auth": "토큰이 유효하지 않습니다. `xoxb-` 로 시작하는 Bot User OAuth Token 인지 확인하세요.",
    "account_inactive": "앱이 워크스페이스에서 비활성화됐습니다.",
    "missing_scope": "권한이 부족합니다. 매니페스트를 다시 반영하고 재설치해야 합니다.",
    "is_archived": "보관된 채널입니다.",
    "ratelimited": "요청이 너무 잦습니다. 잠시 후 다시 시도하세요.",
}


class SlackError(RuntimeError):
    def __init__(self, code: str, method: str, detail: str = ""):
        self.code = code
        self.method = method
        hint = ERROR_HINTS.get(code, "")
        msg = f"Slack {method} 실패: {code}"
        if hint:
            msg += f"\n  → {hint}"
        if detail:
            msg += f"\n  ({detail})"
        super().__init__(msg)


class BodyRejected(ValueError):
    """비밀값이 섞인 본문. 잘라내지 않고 거부한다 — 부분 유출도 유출이다."""


def scrub_check(text: str) -> None:
    for pattern, label in SECRET_PATTERNS:
        if pattern.search(text):
            raise BodyRejected(f"본문에 {label} 로 보이는 값이 있어 보내지 않았습니다.")


def api(token: str, method: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + method,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SlackError("http_error", method, f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise SlackError("network_error", method, str(e.reason)) from e

    if not data.get("ok"):
        raise SlackError(str(data.get("error", "unknown")), method, str(data.get("needed", "")))
    return data


def auth_test(token: str) -> dict:
    return api(token, "auth.test", {})


def post_message(token: str, channel: str, text: str, thread_ts: str | None = None) -> dict:
    scrub_check(text)
    if len(text) > BODY_MAX:
        text = text[: BODY_MAX - 20] + "\n… (잘림)"
    payload: dict = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return api(token, "chat.postMessage", payload)


def conversations_info(token: str, channel: str) -> dict:
    return api(token, "conversations.info", {"channel": channel})


def probe(token: str, channel: str) -> dict:
    """보낼 곳에 실제로 접근되는지 확인한다.

    DM 에는 conversations.info 를 쓰지 않는다. IM 에 대해서는 im:read 를 요구해
    invalid_arguments 로 떨어지는데, 그 스코프를 넣으면 이미 설치한 사람이 앱을
    다시 설치해야 한다. 대신 읽기를 한 번 해보는 것으로 접근 가능 여부를 대신한다
    (im:history 는 어차피 답장을 받으려고 이미 갖고 있다).
    """
    if channel.startswith("D"):
        conversations_history(token, channel, oldest="0")
        return {"kind": "dm", "label": "봇과의 DM", "ready": True}

    info = api(token, "conversations.info", {"channel": channel}).get("channel", {})
    name = info.get("name", "?")
    return {
        "kind": "channel",
        "label": f"#{name}",
        "name": name,
        "ready": bool(info.get("is_member")),
    }


def conversations_open(token: str, user_id: str) -> str:
    """봇과 사용자의 1:1 DM 을 열고 그 대화 ID(`D...`)를 돌려준다.

    채널은 워크스페이스가 앱 추가 권한을 관리자로 제한하면 봇을 초대할 수 없다.
    DM 은 채널 멤버십이 아니라서 초대라는 절차 자체가 없다. 권한이 막힌 회사
    워크스페이스에서 유일하게 남는 길이다.
    """
    return api(token, "conversations.open", {"users": user_id})["channel"]["id"]


def conversations_history(token: str, channel: str, oldest: str) -> list[dict]:
    """대화의 최근 메시지를 읽는다.

    DM 에서는 사람이 스레드가 아니라 그냥 아래에 이어 쓴다. 스레드 답글만 보면
    폰에서 보낸 말을 영영 못 본다.
    """
    payload = {"channel": channel, "limit": 100, "oldest": oldest}
    return api(token, "conversations.history", payload).get("messages", [])


def conversations_replies(
    token: str, channel: str, thread_ts: str, oldest: str | None = None
) -> list[dict]:
    payload: dict = {"channel": channel, "ts": thread_ts, "limit": 100}
    if oldest:
        payload["oldest"] = oldest
    return api(token, "conversations.replies", payload).get("messages", [])
