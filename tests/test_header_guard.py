import unittest
from unittest.mock import patch

from claude_slack_bridge import slack


class HeaderGuardTests(unittest.TestCase):
    """머리글 갱신은 브리지가 올린 머리글 위에서만 한다."""

    def run_update(self, root_text):
        with patch.object(slack, "api", return_value={"messages": [{"text": root_text}]}), \
             patch.object(slack, "chat_update") as upd:
            done = slack.update_header("T", "C1", "1.0", "*새 라벨* — 대화를 엽니다.")
        return done, upd

    def test_updates_bridge_header(self):
        done, upd = self.run_update("*멘션 · x* — 대화를 엽니다. (마감 12:00)\n...")
        self.assertTrue(done)
        upd.assert_called_once()

    def test_updates_closed_header_on_reopen(self):
        done, upd = self.run_update("~*멘션 · x* — 종료됨 (13:59)~\n_Claude 세션 종료_")
        self.assertTrue(done)
        upd.assert_called_once()

    def test_leaves_bot_owned_post_alone(self):
        # 봇이 올린 일정 정리 글 — 머리글이 아니므로 덮지 않는다
        done, upd = self.run_update("*10/21(수) 일정* — 연차 1명, 오후 반반차 3명")
        self.assertFalse(done)
        upd.assert_not_called()

    def test_leaves_unreadable_root_alone(self):
        with patch.object(slack, "api", side_effect=slack.SlackError("not_in_channel", "conversations.history")), \
             patch.object(slack, "chat_update") as upd:
            self.assertFalse(slack.update_header("T", "C1", "1.0", "x"))
        upd.assert_not_called()


if __name__ == "__main__":
    unittest.main()
