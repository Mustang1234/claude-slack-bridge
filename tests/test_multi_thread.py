import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_slack_bridge import chat, cli, config as cfg, server, slack, threads


class MultiThreadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name, value in (
            ("CONFIG_DIR", root), ("CONFIG_PATH", root / "config.json"),
            ("CHANNELS_PATH", root / "channels.json"),
        ):
            self.enterContext(patch.object(cfg, name, value))
        self.enterContext(patch.object(threads, "THREADS_DIR", root / "threads"))
        self.enterContext(patch.object(chat, "_chat", None))
        self.enterContext(patch.object(chat, "_chats", {}))
        self.enterContext(patch.object(server, "_OWNED", set()))
        self.enterContext(patch.object(cfg, "load", return_value=cfg.Config("test-token", "DDEFAULT", "UOWNER")))
        self.enterContext(patch.object(server, "_start_keeper", return_value="test keeper"))
        self.enterContext(patch.object(slack, "resolve_target", side_effect=lambda token, target, default: target or default))
        self.enterContext(patch.object(slack, "assert_member"))
        self.enterContext(patch.object(slack, "auth_test", return_value={"user_id": "UBOT"}))
        self.enterContext(patch.object(slack, "conversations_replies", return_value=[]))
        self.enterContext(patch.object(slack, "chat_update"))
        self.enterContext(patch.object(slack, "set_session_status"))
        self.post = self.enterContext(patch.object(slack, "post_message", return_value={"ts": "300.0"}))
        for ts, channel in (("100.0", "DA"), ("200.0", "DB")):
            threads.save(ts, {"thread_ts": ts, "channel": channel, "closed": False,
                              "deadline": time.time() + 7200, "label": ts})

    def attach_two(self):
        for ts in ("100.0", "200.0"):
            self.assertIn("붙었습니다", server.slack_chat_attach(ts, origin="terminal"))
        self.assertEqual(list(chat._chats), ["100.0", "200.0"])
        self.assertEqual(chat.current().thread_ts, "200.0")

    def test_attach_two_defaults_to_second(self):
        self.attach_two()

    def test_notify_first_preserves_second(self):
        self.attach_two()
        second = threads.load("200.0")
        server.slack_notify("첫째 답", thread_ts="100.0")
        self.post.assert_called_once_with("test-token", "DA", "첫째 답", thread_ts="100.0")
        self.assertEqual(threads.load("100.0")["session_reply_ts"], 300.0)
        self.assertEqual(threads.load("200.0"), second)

    def test_close_second_defaults_to_first(self):
        self.attach_two()
        result = server.slack_chat_close(thread_ts="200.0")
        self.assertIn("남은 스레드: 100.0", result)
        self.assertEqual(chat.current().thread_ts, "100.0")
        self.assertEqual(list(chat._chats), ["100.0"])
        self.assertTrue(threads.load("200.0")["closed"])
        self.assertFalse(threads.load("100.0")["closed"])
        self.assertIn("쥔 스레드 없음", server.slack_chat_close())

    def test_close_skips_externally_closed_thread(self):
        self.attach_two()
        threads.patch("100.0", closed=True)
        server.slack_chat_close(thread_ts="200.0")
        self.assertIsNone(chat._chat)
        self.assertEqual(chat._chats, {})

    def test_notify_rejects_externally_closed_thread(self):
        self.attach_two()
        threads.patch("100.0", closed=True)
        result = server.slack_notify("답", thread_ts="100.0")
        self.assertEqual(result, "이미 닫힌 스레드입니다: 100.0")
        self.post.assert_not_called()
        self.assertNotIn("100.0", chat._chats)
        self.assertNotIn("session_reply_ts", threads.load("100.0"))

    def test_default_notify_rejects_closed_and_promotes_live_thread(self):
        self.attach_two()
        threads.patch("200.0", closed=True)
        self.assertEqual(server.slack_notify("답"), "이미 닫힌 스레드입니다: 200.0")
        self.post.assert_not_called()
        self.assertEqual(chat.current().thread_ts, "100.0")
        self.assertNotIn("200.0", chat._chats)

    def test_monitor_keeper_lines_identify_thread(self):
        for status, prefix in (("KEEPING", "KEEPER_REVIVED"),
                               ("STALE_KEEPER", "KEEPER_REVIVE_FAILED"),
                               ("DIED", "KEEPER_REVIVE_FAILED")):
            with self.subTest(status=status), patch.object(
                threads, "spawn_keeper", return_value=(status, 42)
            ):
                line = cli._monitor_keeper_line("100.0")
                self.assertTrue(line.startswith(prefix + "\t"))
                self.assertTrue(line.endswith("\tthread=100.0"))
        with patch.object(threads, "spawn_keeper", side_effect=OSError("failed")):
            self.assertEqual(cli._monitor_keeper_line("100.0"),
                             "KEEPER_REVIVE_FAILED\tfailed\tthread=100.0")

    def test_unknown_and_conflicting_destinations_do_not_post(self):
        self.attach_two()
        with self.assertRaisesRegex(chat.NoChat, "이 세션이 쥔 스레드가 아닙니다: 999.0"):
            chat.current("999.0")
        with self.assertRaises(chat.NoChat):
            chat.close_chat("test-token", thread_ts="999.0")
        self.assertIn("쥔 스레드가 아닙니다", server.slack_notify("x", thread_ts="999.0"))
        self.assertIn("동시에", server.slack_notify("x", channel="DA", thread_ts="100.0"))
        self.post.assert_not_called()
        self.assertEqual(chat.current().thread_ts, "200.0")

    def test_reattach_preserves_object_and_promotes_default(self):
        self.attach_two()
        first = chat.current("100.0")
        first.seen.add("101.0")
        before = threads.load("100.0")["deadline"]
        result = server.slack_chat_attach("100.0", label="새 라벨", hours=1,
                                          current_thread_ts="100.0", origin="terminal")
        self.assertIn("붙었습니다", result)
        self.assertIs(chat.current(), first)
        self.assertEqual(first.seen, {"101.0"})
        self.assertEqual(first.label, "새 라벨")
        self.assertEqual(threads.load("100.0")["deadline"], before)
        self.assertEqual(list(chat._chats), ["200.0", "100.0"])
        with patch.object(threads, "inbox_keeper_alive", return_value=True):
            listing = server.slack_chat_list()
        self.assertIn("** 100.0", listing)
        self.assertIn("* 200.0", listing)
        server.slack_chat_close("100.0")
        self.assertEqual(chat.current().thread_ts, "200.0")

    def test_default_notify_and_explicit_channel(self):
        self.attach_two()
        server.slack_notify("기본")
        self.post.assert_called_with("test-token", "DB", "기본", thread_ts="200.0")
        before = threads.load("200.0")
        server.slack_notify("별도", channel="DC")
        self.post.assert_called_with("test-token", "DC", "별도", thread_ts=None)
        self.assertEqual(threads.load("200.0"), before)

    def test_wait_deadline_closes_selected_thread_only(self):
        self.attach_two()
        threads.patch("100.0", deadline=time.time() - 1)
        with patch.object(server, "_bot_user_id", return_value="UBOT"):
            self.assertIn("닫혔습니다", server.slack_wait_reply(0, thread_ts="100.0"))
        self.assertEqual(chat.current().thread_ts, "200.0")
        self.assertFalse(threads.load("200.0")["closed"])

    def test_wait_autoextend_and_timeout_use_selected_thread(self):
        self.attach_two()
        threads.patch("100.0", deadline=time.time() + 60)
        second = threads.load("200.0")
        with patch.object(server, "_bot_user_id", return_value="UBOT"), patch.object(
            slack, "conversations_replies", return_value=[{"ts": "101.0", "user": "UOWNER", "text": "답"}]
        ) as replies:
            self.assertEqual(server.slack_wait_reply(0, thread_ts="100.0"), "답")
            replies.assert_called_once_with("test-token", "DA", "100.0")
        self.assertGreater(threads.load("100.0")["deadline"], time.time() + 7000)
        self.assertEqual(threads.load("200.0"), second)
        with patch.object(server, "_bot_user_id", return_value="UBOT"), patch.object(
            slack, "conversations_replies", return_value=[]
        ):
            self.assertIn("아직 답장이 없습니다", server.slack_wait_reply(0, thread_ts="100.0"))

    def test_switch_closes_default_and_preserves_other_thread(self):
        self.attach_two()
        result = server.slack_chat_switch(channel="DC", current_thread_ts="200.0", origin="terminal")
        self.assertIn("옮겼습니다", result)
        self.assertEqual(list(chat._chats), ["100.0", "300.0"])
        self.assertEqual(chat.current().thread_ts, "300.0")
        self.assertTrue(threads.load("200.0")["closed"])
        self.assertFalse(threads.load("100.0")["closed"])

    def test_monitor_tags_messages_and_close_event(self):
        output = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        self.addCleanup(output.close)
        cycles = []

        def tick(_):
            if cycles:
                raise KeyboardInterrupt
            cycles.append(True)
            path = threads.ensure_inbox("100.0")
            with path.open("ab") as stream:
                stream.write(b'{"ts":"101.0","text":"hello","thread_ts":"wrong"}\n')
                stream.write(b'{"event":"THREAD_CLOSED"}\n')

        with patch.object(cli.sys, "stdout", output), patch.object(cli.time, "sleep", side_effect=tick), \
             patch.object(cli.time, "monotonic", return_value=0):
            cli.cmd_monitor(["--thread", "100.0"])
        rows = [json.loads(line) for line in output.buffer.getvalue().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["thread_ts"] == "100.0" for row in rows))
        self.assertEqual(rows[0]["text"], "hello")
        self.assertEqual(rows[1]["event"], "THREAD_CLOSED")


if __name__ == "__main__":
    unittest.main()
