"""설정 로딩.

토큰은 레포 안에 두지 않는다. 이 프로젝트는 공개될 예정이고, 값이 담긴 파일이
이력에 한 번만 들어가도 되돌릴 수 없다. 그래서 홈 디렉터리 밖에 두고 권한을 좁힌다.

토큰·채널 등은 환경변수 > 설정 파일이다 — 파일을 두기 곤란한 환경에서도 돌아야 한다.
소유자만은 설정 파일 값이 먼저고, 비어 있을 때만 SLACK_OWNER_ID를 쓴다. 파일 루트
owner_id 하나만으로 따로 설정하지 않아도 주인이 정해져야 하기 때문이다.

소유자는 여럿일 수 있다(`owner_ids`). 모두 같은 권한을 갖고, 목록의 첫 사람이 대표
소유자다 — ⚠️/✅ DM 을 받는다. 전원에게 보내면 같은 경고가 사람 수만큼 겹친다.
옛 설정의 `owner_id` 문자열도 그대로 읽는다.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.path.expanduser("~/.claude-slack-bridge"))
CONFIG_PATH = CONFIG_DIR / "config.json"
CHANNELS_PATH = CONFIG_DIR / "channels.json"


@dataclass
class Config:
    bot_token: str
    channel: str
    owner_id: str = ""      # 대표 소유자. 비면 채널 지시를 받지 않음.
    display_name: str = ""  # 메시지 발신자 이름 덮어쓰기(chat:write.customize 필요). 비면 봇 프로필 이름.
    icon_emoji: str = ""    # 발신자 아이콘 — ":robot_face:" 꼴. icon_url 보다 우선.
    icon_url: str = ""      # 발신자 아이콘 이미지 URL(https). 비면 앱 아이콘.
    co_owner_ids: list[str] = field(default_factory=list)  # 대표 외 소유자. 권한은 같다.

    @property
    def owner_ids(self) -> list[str]:
        """모든 소유자. 첫 사람이 대표다."""
        if not self.owner_id:
            return []
        return list(dict.fromkeys([self.owner_id, *self.co_owner_ids]))

    def is_owner(self, user_id: str) -> bool:
        return bool(user_id) and user_id in self.owner_ids

    @property
    def masked_token(self) -> str:
        """로그·화면 출력용. 토큰 전체는 어디에도 찍지 않는다."""
        t = self.bot_token
        return f"{t[:9]}...{t[-4:]}" if len(t) > 16 else "xoxb-***"


def load() -> Config | None:
    """설정을 읽는다. 없거나 불완전하면 None — 호출부는 조용히 no-op 한다."""
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("SLACK_CHANNEL", "").strip()

    raw = {}
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            raw = None
        if not isinstance(raw, dict):
            if not (token and channel):
                return None
            raw = {}

    if not (token and channel) and raw:
        token = token or str(raw.get("bot_token", "")).strip()
        channel = channel or str(raw.get("channel", "")).strip()
        display_name = str(raw.get("display_name", "")).strip()
        icon_emoji = str(raw.get("icon_emoji", "")).strip()
        icon_url = str(raw.get("icon_url", "")).strip()
    else:
        display_name = icon_emoji = icon_url = ""
    owners = parse_owner_ids(raw.get("owner_ids")) or parse_owner_ids(raw.get("owner_id"))
    owners = owners or parse_owner_ids(os.environ.get("SLACK_OWNER_ID", ""))
    display_name = os.environ.get("SLACK_DISPLAY_NAME", display_name).strip()
    icon_emoji = os.environ.get("SLACK_ICON_EMOJI", icon_emoji).strip()
    icon_url = os.environ.get("SLACK_ICON_URL", icon_url).strip()

    if not token or not channel:
        return None
    # 발신 정체는 전역 상태 — post_message 호출부 11곳이 토큰만 넘기므로 여기서 한 번 심는다.
    from . import slack

    slack.set_identity(display_name, icon_emoji, icon_url)
    return Config(
        bot_token=token,
        channel=channel,
        owner_id=owners[0] if owners else "",
        display_name=display_name,
        icon_emoji=icon_emoji,
        icon_url=icon_url,
        co_owner_ids=owners[1:],
    )


def parse_owner_ids(value) -> list[str]:
    """목록·쉼표 구분 문자열·문자열 하나를 모두 받아 중복 없는 ID 목록으로."""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = [v for v in value if isinstance(v, str)]
    else:
        return []
    return list(dict.fromkeys(v.strip() for v in items if v.strip()))


def save(token: str, channel: str, owner_id: str | list[str] = "") -> Path:
    """설정 파일을 쓰고 소유자만 읽게 잠근다."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # 디렉터리도 좁힌다. 파일 권한만 좁히고 디렉터리를 열어두면 의미가 반감된다.
    os.chmod(CONFIG_DIR, stat.S_IRWXU)

    owners = parse_owner_ids(owner_id)
    payload: dict = {"bot_token": token, "channel": channel, "owner_id": owners[0] if owners else ""}
    if len(owners) > 1:
        payload["owner_ids"] = owners
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    # 먼저 좁은 권한으로 만들고 쓴다. 쓰고 나서 chmod 하면 그 사이가 열려 있다.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)
    return CONFIG_PATH


def load_channels_with_error() -> tuple[dict[str, dict], str]:
    """채널별 설정과 읽기 경고를 돌려준다. 없거나 빈 파일은 정상적인 빈 설정이다."""
    try:
        text = CHANNELS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, ""
    except OSError as e:
        return {}, f"channels.json 을 읽지 못했습니다: {e}"
    if not text.strip():
        return {}, ""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        return {}, f"channels.json 이 손상됐습니다: {e.msg}"
    if not isinstance(raw, dict):
        return {}, "channels.json 이 손상됐습니다: 최상위 값이 객체가 아닙니다"

    channels = {}
    for channel, value in raw.items():
        if not isinstance(channel, str) or not isinstance(value, dict):
            continue
        listeners = value.get("listeners")
        if not isinstance(listeners, list):
            listeners = []
        channels[channel] = {
            **value,
            "listeners": list(dict.fromkeys(
                user_id for user_id in listeners if isinstance(user_id, str)
            )),
        }
    return channels, ""


def load_channels() -> dict[str, dict]:
    """채널별 설정. 파일이 없거나 비었거나 손상됐으면 빈 dict로 안전하게 진행한다."""
    return load_channels_with_error()[0]


def channel_listeners(channel: str) -> list[str]:
    return list(load_channels().get(channel, {}).get("listeners") or [])


def save_channel_listeners(channel: str, listeners: list[str]) -> Path:
    """한 채널의 listener만 바꾸고 다른 채널 설정은 보존해 원자적으로 저장한다."""
    channels = load_channels()
    entry = dict(channels.get(channel) or {})
    entry["listeners"] = list(dict.fromkeys(listeners))
    channels[channel] = entry

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, stat.S_IRWXU)
    tmp = CHANNELS_PATH.with_suffix(f".json.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(channels, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, CHANNELS_PATH)
    return CHANNELS_PATH
