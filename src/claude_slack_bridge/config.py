"""설정 로딩.

토큰은 레포 안에 두지 않는다. 이 프로젝트는 공개될 예정이고, 값이 담긴 파일이
이력에 한 번만 들어가도 되돌릴 수 없다. 그래서 홈 디렉터리 밖에 두고 권한을 좁힌다.

우선순위는 환경변수 > 설정 파일이다. CI 나 컨테이너처럼 파일을 두기 곤란한
환경에서 환경변수만으로 돌 수 있어야 하기 때문이다.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(os.path.expanduser("~/.claude-slack-bridge"))
CONFIG_PATH = CONFIG_DIR / "config.json"


@dataclass
class Config:
    bot_token: str
    channel: str
    owner_id: str = ""      # 채널에서 내 지시로 인정할 사람. 비면 제한 없음.

    @property
    def masked_token(self) -> str:
        """로그·화면 출력용. 토큰 전체는 어디에도 찍지 않는다."""
        t = self.bot_token
        return f"{t[:9]}...{t[-4:]}" if len(t) > 16 else "xoxb-***"


def load() -> Config | None:
    """설정을 읽는다. 없거나 불완전하면 None — 호출부는 조용히 no-op 한다."""
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("SLACK_CHANNEL", "").strip()

    if not (token and channel) and CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        token = token or str(raw.get("bot_token", "")).strip()
        channel = channel or str(raw.get("channel", "")).strip()
        owner = str(raw.get("owner_id", "")).strip()
    else:
        owner = ""
    owner = os.environ.get("SLACK_OWNER_ID", owner).strip()

    if not token or not channel:
        return None
    return Config(bot_token=token, channel=channel, owner_id=owner)


def save(token: str, channel: str, owner_id: str = "") -> Path:
    """설정 파일을 쓰고 소유자만 읽게 잠근다."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # 디렉터리도 좁힌다. 파일 권한만 좁히고 디렉터리를 열어두면 의미가 반감된다.
    os.chmod(CONFIG_DIR, stat.S_IRWXU)

    payload = {"bot_token": token, "channel": channel, "owner_id": owner_id}
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    # 먼저 좁은 권한으로 만들고 쓴다. 쓰고 나서 chmod 하면 그 사이가 열려 있다.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)
    return CONFIG_PATH
