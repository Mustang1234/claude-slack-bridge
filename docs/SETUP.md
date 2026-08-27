# 설치

브라우저에서 하는 일과 터미널에서 하는 일이 나뉜다. 브라우저 쪽이 먼저다.

## 1. Slack 앱 등록 (브라우저, 약 3분)

여기서 "앱"은 프로그램이 아니라 **워크스페이스에 등록하는 봇 계정**이다. 개발하거나
설치할 것은 없고, 마지막에 토큰 문자열 하나를 받는 게 목적이다.

1. https://api.slack.com/apps 접속 (Slack 로그인 상태)
2. `Create New App` → `From an app manifest`
3. 워크스페이스 선택
4. `slack-app-manifest.yaml` 내용을 붙여넣고 `Next` → `Create`
5. 좌측 `OAuth & Permissions` → `Install to Workspace` → `Allow`
6. `Bot User OAuth Token` 복사 — `xoxb-` 로 시작한다

### 관리자 승인이 걸린 경우

회사 워크스페이스는 앱 설치에 관리자 승인을 요구하는 경우가 많다. 5번에서 설치 대신
"승인 요청됨" 안내가 뜨면 승인 전까지 토큰이 나오지 않는다. 요청 사유에는 아래 정도면
충분하다.

> 개발 작업 알림을 지정한 비공개 채널로 받기 위한 봇. 외부로 데이터를 보내지 않고,
> 초대된 채널에만 접근한다.

## 2. 받을 곳 만들기 (브라우저)

1. Slack 에서 비공개 채널을 하나 만든다 (예: `#claude-알림`)
2. 그 채널에서 `/invite @Claude Bridge` 로 봇을 초대한다
3. 채널 이름 우클릭 → `링크 복사` → URL 끝의 `C` 로 시작하는 문자열이 채널 ID 다

봇을 초대하지 않으면 메시지가 가지 않는다. 가장 흔한 실패 원인이다.

## 3. 설정 (터미널)

토큰은 파일에 둔다. 권한은 본인만 읽게 좁힌다.

```bash
mkdir -p ~/.claude-slack-bridge
cat > ~/.claude-slack-bridge/config.json <<'JSON'
{
  "bot_token": "xoxb-...",
  "channel": "C..."
}
JSON
chmod 600 ~/.claude-slack-bridge/config.json
```

환경변수 `SLACK_BOT_TOKEN` / `SLACK_CHANNEL` 이 있으면 그쪽이 우선한다.

## 4. Claude Code 에 붙이기

```bash
claude mcp add claude-slack-bridge -s user -- uvx claude-slack-bridge
```

`-s user` 는 모든 프로젝트에서 쓰겠다는 뜻이다. 알림은 어느 프로젝트에서 일하든
받아야 하므로 이 범위가 맞다.

설정이 없으면 조용히 아무것도 하지 않는다. clone 만 한 상태로도 흐름이 깨지지 않는다.
