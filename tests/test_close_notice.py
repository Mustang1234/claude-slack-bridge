import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_slack_bridge import chat, slack, threads


class CloseNoticeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(threads, "THREADS_DIR", Path(temp.name)))
        self.enterContext(patch.object(chat, "_chat", None))
        self.enterContext(patch.object(chat, "_chats", {}))
        self.enterContext(patch.object(slack, "api", side_effect=AssertionError("Unexpected Slack API call")))
        self.auth = self.enterContext(patch.object(slack, "auth_test", return_value={"user_id": "UBOT", "bot_id": "BBOT"}))
        self.replies = self.enterContext(patch.object(slack, "conversations_replies", return_value=[{"ts": "100.0", "user": "UBOT"}]))
        self.update = self.enterContext(patch.object(slack, "chat_update"))
        self.post = self.enterContext(patch.object(slack, "post_message", return_value={"ts": "200.0"}))
        self.enterContext(patch.object(slack, "set_session_status"))
        threads.save("100.0", {"thread_ts": "100.0", "channel": "CCHAN", "closed": False})

    def close(self):
        return chat.close_thread("test-token", "CCHAN", "100.0", "Test", "마감 시각 도달",
                                 resumable=True, only_if_notified=True)

    def test_uneditable_header_closes_with_one_notice(self):
        threads.patch("100.0", header_editable=False)
        self.update.side_effect = slack.SlackError("cant_update_message", "chat.update")
        self.assertTrue(self.close())
        self.assertTrue(threads.load("100.0")["closed"])
        self.update.assert_not_called()
        self.post.assert_called_once()

    def test_header_failure_retries_without_reposting_notice(self):
        threads.patch("100.0", header_editable=True)
        self.update.side_effect = [slack.SlackError("temporary", "test"), None]
        self.assertFalse(self.close())
        self.assertFalse(threads.load("100.0")["closed"])
        self.assertTrue(threads.load("100.0").get("close_notice_posted"))
        self.assertTrue(self.close())
        self.assertEqual(self.update.call_count, 2)
        self.post.assert_called_once()

    def test_notice_failure_retries_without_updating_header_again(self):
        self.post.side_effect = [slack.SlackError("temporary", "test"), {"ts": "200.0"}]
        self.assertFalse(self.close())
        self.assertTrue(self.close())
        self.update.assert_called_once()
        self.assertEqual(self.post.call_count, 2)

    def test_legacy_state_still_attempts_header_update(self):
        self.update.side_effect = slack.SlackError("cant_update_message", "chat.update")
        self.assertFalse(self.close())
        self.update.assert_called_once()
        self.post.assert_called_once()
        self.assertFalse(threads.load("100.0")["closed"])

    def test_attach_checks_parent_author(self):
        for parent, expected in (
            ({"user": "UBOT"}, True),
            ({"user": "UOWNER"}, False),
            ({"bot_id": "BBOT"}, True),
            ({"user": "UOTHER", "bot_id": "BOTHER"}, False),
        ):
            with self.subTest(parent=parent):
                chat._chats.clear()
                threads.save("100.0", {"channel": "CCHAN"})
                self.replies.return_value = [{"ts": "101.0", "user": "UBOT"}, {"ts": "100.0", **parent}]
                chat.attach("test-token", "100.0")
                self.assertIs(threads.load("100.0").get("header_editable"), expected)

    def test_attach_lookup_failure_leaves_key_absent(self):
        for failing in (self.auth, self.replies):
            with self.subTest(failing=failing):
                chat._chats.clear()
                threads.save("100.0", {"channel": "CCHAN"})
                failing.side_effect = slack.SlackError("temporary", "test")
                chat.attach("test-token", "100.0")
                self.assertNotIn("header_editable", threads.load("100.0"))
                failing.side_effect = None

    def test_attach_missing_parent_leaves_key_absent(self):
        self.replies.return_value = [{"ts": "101.0", "user": "UBOT"}]
        chat.attach("test-token", "100.0")
        self.assertNotIn("header_editable", threads.load("100.0"))

    def test_attach_reuses_saved_classification(self):
        threads.patch("100.0", header_editable=False)
        chat.attach("test-token", "100.0")
        self.auth.assert_not_called()
        self.replies.assert_not_called()

    def test_reopen_resets_both_close_markers(self):
        self.assertTrue(self.close())
        chat.attach("test-token", "100.0")
        state = threads.load("100.0")
        self.assertFalse(state.get("close_notice_posted"))
        self.assertFalse(state.get("close_header_updated"))
        self.post.reset_mock()
        self.update.reset_mock()
        self.assertTrue(self.close())
        self.post.assert_called_once()
        self.update.assert_called_once()

    def test_open_marks_header_editable(self):
        opened = chat.open_chat("test-token", "CCHAN", 1, "Test")
        self.assertIs(threads.load(opened.thread_ts).get("header_editable"), True)


if __name__ == "__main__":
    unittest.main()
