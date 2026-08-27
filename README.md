# claude-slack-bridge

Claude Code 세션과 Slack 을 잇는 MCP 서버. 자리를 비운 사이 세션이 먼저 알림을
보내고, 폰에서 이어서 확인한다.

특정 프로젝트에 종속되지 않는다. `claude mcp add` 한 줄이면 어느 Claude Code 에서든
붙는다.

## 왜 서버가 필요 없나

"MCP 서버"의 서버는 호스팅하는 서버가 아니라 **내 컴퓨터에서 도는 프로세스**다.
Claude Code 가 필요할 때 띄우고 세션이 끝나면 같이 죽는다. 배포도, 포트 개방도,
공인 IP 도 없다.

Slack 수신도 폴링(`conversations.replies`)이라 **바깥에서 내 컴퓨터로 들어오는
연결이 없다.** 방화벽 뒤에서 그대로 동작하고, 받을 토큰도 봇 토큰(`xoxb-`) 하나뿐이다.
Socket Mode 나 Events API 를 쓰면 토큰이 둘로 늘고 설치 단계가 길어지는데, 그만한
값을 하지 않는다.

## 설치

```bash
# 1) Slack 앱 등록 + 토큰 발급 + 채널 준비 — docs/SETUP.md 참고
# 2) 설정하고 연결까지 확인
uvx --from git+https://github.com/Mustang1234/claude-slack-bridge claude-slack-bridge init

# 3) Claude Code 에 붙이기
claude mcp add claude-slack-bridge -s user -- \
  uvx --from git+https://github.com/Mustang1234/claude-slack-bridge claude-slack-bridge
```

`-s user` 는 모든 프로젝트에서 쓰겠다는 뜻이다. 알림은 어느 프로젝트에서 일하든
받아야 하므로 이 범위가 맞다.

자세한 절차는 [docs/SETUP.md](docs/SETUP.md) 에 있다.

## 명령

| 명령 | 하는 일 |
|---|---|
| `claude-slack-bridge` | MCP 서버로 동작 (`claude mcp add` 가 이렇게 부른다) |
| `claude-slack-bridge init` | 설정 생성 + 연결 확인 + 테스트 메시지 발송 |
| `claude-slack-bridge manifest` | Slack 콘솔에 붙여넣을 앱 매니페스트 출력 |
| `claude-slack-bridge doctor` | 현재 설정이 살아있는지 점검 |

`init` 은 준비가 안 된 사람에게 **절차부터 안내한다.** uvx 로 설치하면 레포가 없어서
문서 파일을 열어볼 수 없으므로, 필요한 것이 전부 터미널 안에서 끝나야 한다.
매니페스트도 패키지 안에 들어 있어 `manifest` 명령으로 꺼내 쓴다.

`init` 은 설정 파일만 만들고 끝내지 않는다. 토큰이 살아있는지, **봇이 채널에 실제로
초대돼 있는지**까지 확인하고 테스트 메시지를 보낸다. 초대를 빠뜨리는 것이 압도적인
1위 실패 원인인데, 그 경우 에러 없이 조용히 전달만 안 되기 때문이다.

## 제공하는 툴

| 툴 | 하는 일 |
|---|---|
| `slack_notify` | 한 줄 알림을 보낸다. 대화가 열려 있으면 그 스레드로 간다 |
| `slack_check` | 토큰·채널·봇 초대 상태를 확인한다 |
| `slack_chat_open` | 스레드를 열어 이 세션에 묶는다 (기본 4시간) |
| `slack_wait_reply` | 답글이 올 때까지 기다렸다 돌려준다 |
| `slack_chat_extend` | 마감을 미룬다 |
| `slack_chat_close` | 스레드를 닫는다 |

`slack_wait_reply` 가 그냥 **블로킹**한다는 점이 구조를 단순하게 만든다. 채널을
붙드는 프로세스와 세션을 깨우는 프로세스를 따로 둘 필요가 없고, 중간 파일도 없다.

마감 10분 전에 스레드로 예고하고, 그 창 안에 답글이 오면 자리에 있다는 뜻이므로
2시간 자동 연장한다. 조용하다는 이유만으로는 닫지 않는다.

## 설정

토큰은 **레포 안에 두지 않는다.**

```
~/.claude-slack-bridge/config.json   (권한 600)
```

환경변수 `SLACK_BOT_TOKEN` / `SLACK_CHANNEL` 이 있으면 그쪽이 우선한다.
설정이 없으면 조용히 아무것도 하지 않으므로, 설치만 해두고 나중에 설정해도 된다.

## 나가는 본문 규칙

비밀값(개인키 블록, URL 에 박힌 인증정보, Slack/GitHub/AWS 토큰)이 섞이면 **보내지
않고 거부한다.** 잘라서 보내지 않는다 — 부분 유출도 유출이다.

반면 파일 경로나 티켓 번호는 막지 않는다. 비공개 채널은 초대받은 사람만 읽으므로,
그런 것까지 가리면 알림이 쓸모를 잃는다.

## 상태

알림 발신과 양방향 대화 모두 구현됐다. 실제 Slack 워크스페이스에 붙여 폰까지
도달하는지는 아직 확인 전이다.
