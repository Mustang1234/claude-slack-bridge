import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_slack_bridge import channels, chat, cli, config as cfg, slack, threads


def message(ts, user="UOWNER", text="<@UBOT> hello", **extra):
    return {"ts": str(ts), "user": user, "text": text, **extra}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.enterContext(patch.object(channels, "CHANNELS_DIR", root / "ch"))
        self.enterContext(patch.object(threads, "THREADS_DIR", root / "threads"))
        for name, value in (("CONFIG_DIR", root), ("CONFIG_PATH", root / "config.json"),
                            ("CHANNELS_PATH", root / "listeners.json")):
            self.enterContext(patch.object(cfg, name, value))
        self.conf = cfg.Config("test-token", "C1", "UOWNER", co_owner_ids=["UCOOWNER"])
        self.enterContext(patch.object(cfg, "load", return_value=self.conf))
        self.enterContext(patch.object(slack, "api", side_effect=AssertionError("Unexpected API call")))
        self.enterContext(patch.object(slack, "auth_test", return_value={"user_id": "UBOT"}))
        self.enterContext(patch.object(channels, "held_threads", return_value=set()))
        self.enterContext(patch.object(slack, "set_session_status", return_value=False))
        self.post = self.enterContext(patch.object(slack, "post_message"))
        self.emit = self.enterContext(patch.object(cli, "_emit_json"))

    def test_all_discovers_joins_and_removes_channels(self):
        channels.set_watched("COLD", True)
        pages = [[{"id": "C1", "name": "one", "private": False},
                  {"id": "C2", "name": "two", "private": True}],
                 [{"id": "C2", "name": "two", "private": True},
                  {"id": "C3", "name": "three", "private": False}]]
        cycles = []
        def sleep(seconds):
            if seconds == 10:
                cycles.append(True)
                if len(cycles) == 2:
                    raise KeyboardInterrupt
        with patch.object(slack, "my_conversations", side_effect=pages), \
             patch.object(cli, "_poll_channel_once") as poll, \
             patch.object(cli.time, "monotonic", side_effect=[0, 0, 100, 100]), \
             patch.object(cli.time, "sleep", side_effect=sleep):
            cli.cmd_monitor(["--all"])
        self.assertEqual([c.args[1] for c in poll.call_args_list], ["C1", "C2", "C2", "C3"])
        self.assertEqual(set(channels.watched()), {"C2", "C3"})
        events = [c.args[0] for c in self.emit.call_args_list]
        self.assertTrue(any(e.get("event") == "CHANNEL_GONE" and e["channel"] == "COLD" for e in events))
        self.assertTrue(any(e.get("event") == "CHANNEL_GONE" and e["channel"] == "C1" for e in events))
        self.assertTrue(any(e.get("event") == "CHANNEL" and e["channel"] == "C3" and e["new"] for e in events))

    def test_channel_lock_prevents_overlapping_all(self):
        lock = channels.acquire_lock("C1")
        self.addCleanup(lock.close)
        with patch.object(slack, "my_conversations", return_value=[{"id": "C1", "name": "one", "private": False}]), \
             patch.object(cli, "_poll_channel_once") as poll, \
             patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt):
            cli.cmd_monitor(["--all"])
        poll.assert_not_called()
        self.assertIn({"event": "ALREADY_WATCHING", "channel": "C1"}, [c.args[0] for c in self.emit.call_args_list])

    def test_channel_and_all_use_same_scanner(self):
        for args in (["--channel", "C1"], ["--all"]):
            with self.subTest(args=args), \
                 patch.object(slack, "my_conversations", return_value=[{"id": "C1", "name": "one", "private": False}]), \
                 patch.object(cli, "_poll_channel_once") as poll, \
                 patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt):
                cli.cmd_monitor(args)
            poll.assert_called_once_with(self.conf, "C1", "UBOT")

    def test_tracked_management_and_all_poll(self):
        cli.cmd_monitor(["--track", "C1", "1.0", "--any-reply"])
        self.assertTrue(next(iter(channels.tracked().values()))["any_reply"])
        with patch.object(slack, "my_conversations", return_value=[{"id": "C1", "name": "one", "private": False}]), \
             patch.object(cli, "_poll_channel_once"), \
             patch.object(cli, "_poll_thread_once") as poll, \
             patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt):
            cli.cmd_monitor(["--all"])
        poll.assert_called_once_with(self.conf, "C1", "1.0", "UBOT", True)
        cli.cmd_monitor(["--untrack", "C1", "1.0"])
        self.assertEqual(channels.tracked(), {})

    def test_thread_filter_requires_identity_even_any_reply(self):
        replies = [message(2), message(3, text="no mention"), message(4, "ULISTEN"),
                   message(5, "ULISTEN", "no mention"), message(6, "UOTHER"),
                   message(7, "UBOT", bot_id="BSELF"), message(8, "UCOOWNER"),
                   message(9, "UAGENT", bot_id="BAGENT"),
                   message(10, "UOTHER", "no mention"), message(11, "UUNKNOWN", bot_id="BOTHER")]
        for any_reply, expected in ((False, ["2", "4", "8", "9"]), (True, ["2", "3", "4", "5", "8", "9"])):
            out, state = channels.thread_poll("C1", "1.0", {"cursor": "1"}, replies, "UBOT",
                                               self.conf.owner_ids, ["ULISTEN", "UAGENT"], any_reply)
            self.assertEqual([m["ts"] for m in out], expected)
            self.assertEqual(state["cursor"], "11")

    def test_thread_initial_cursor_restart_and_files(self):
        old = message(99)
        new = message(101, files=[{"name": "image", "url_private": "attachment"}])
        with patch.object(cli.time, "time", return_value=100), \
             patch.object(slack, "conversations_replies", return_value=[old]):
            cli._poll_thread_once(self.conf, "C1", "1.0", "UBOT")
        self.emit.assert_not_called()
        with patch.object(slack, "conversations_replies", return_value=[old, new]):
            cli._poll_thread_once(self.conf, "C1", "1.0", "UBOT")
            cli._poll_thread_once(self.conf, "C1", "1.0", "UBOT")
        self.emit.assert_called_once()
        self.assertEqual(self.emit.call_args.args[0]["files"], new["files"])
        self.assertEqual(channels.load_state(channels.thread_key("C1", "1.0"))["cursor"], "101")

    def test_thread_uses_channel_and_local_listeners(self):
        cfg.save_channel_listeners("C1", ["UA"])
        threads.save("1.0", {"channel": "C1", "closed": True, "listeners": ["UB"]})
        channels.save_state(channels.thread_key("C1", "1.0"), {"cursor": "1"})
        with patch.object(slack, "conversations_replies", return_value=[message(2,"UA"),message(3,"UB"),message(4,"UC")]):
            cli._poll_thread_once(self.conf, "C1", "1.0", "UBOT")
        self.assertEqual([c.args[0]["user"] for c in self.emit.call_args_list], ["UA", "UB"])

    def test_held_thread_never_fetches(self):
        with patch.object(channels, "held_threads", return_value={"1.0"}), \
             patch.object(slack, "conversations_replies") as fetch:
            cli._poll_thread_once(self.conf, "C1", "1.0", "UBOT")
        fetch.assert_not_called()
        self.emit.assert_not_called()

    def test_channel_skips_explicit_tracked_thread(self):
        channels.set_tracked("C1", "1.0", True)
        channels.save_state("C1", {"cursor": "0", "threads": {}})
        with patch.object(slack, "conversations_history", return_value=[message(1.0, latest_reply="2.0")]), \
             patch.object(slack, "conversations_replies") as fetch:
            cli._poll_channel_once(self.conf, "C1", "UBOT")
        fetch.assert_not_called()
        self.emit.assert_not_called()

    def test_dedup_between_channel_and_thread_cursor(self):
        channels.save_state("C1", {"cursor": "100", "threads": {"1.0": "100"}})
        channels.save_state(channels.thread_key("C1", "1.0"), {"cursor": "100"})
        replies = [message(101)]
        with patch.object(slack, "conversations_history", return_value=[message(1.0, latest_reply="101")]), \
             patch.object(slack, "conversations_replies", return_value=replies):
            cli._poll_channel_once(self.conf, "C1", "UBOT")
            cli._poll_thread_once(self.conf, "C1", "1.0", "UBOT")
        self.emit.assert_called_once()

    def test_channel_commands_gate_and_no_inbox(self):
        channels.save_state("C1", {"cursor": "100", "threads": {}})
        cfg.save_channel_listeners("C1", ["ULISTEN"])
        history = [message(101, text="<@UBOT> 채널 듣기 <@UA>"),
                   message(102, "UA"),
                   message(103, "ULISTEN", "<@UBOT> 채널 듣기 <@UEVIL>"),
                   message(104, "UOTHER", "<@UBOT> 채널 그만 <@UA>"),
                   message(105, "UCOOWNER", "<@UBOT> 채널 그만 <@UA>"),
                   message(106, "UA")]
        with patch.object(slack, "conversations_history", return_value=history):
            cli._poll_channel_once(self.conf, "C1", "UBOT")
        self.assertEqual(cfg.channel_listeners("C1"), ["ULISTEN"])
        self.assertEqual([c.args[0]["ts"] for c in self.emit.call_args_list], ["102"])
        replies = [c.args[2] for c in self.post.call_args_list]
        self.assertEqual(replies, ["이 채널 전체에서 이제 <@UA> 의 말도 듣습니다.",
                                   "주인만 할 수 있습니다.", "주인만 할 수 있습니다.", "<@UA> 채널 그만 완료."])
        self.assertEqual(len(channels.inbox_path("C1").read_text().splitlines()), 1)

    def test_thread_poll_dispatch_uses_shared_scanner(self):
        with patch.object(cli, "_monitor_scanners") as scanner:
            cli.cmd_monitor(["--thread-poll", "C1", "1.0", "--any-reply", "--interval", "30"])
        scanner.assert_called_once_with(self.conf, 30.0, channel="C1", thread="1.0", any_reply=True)

    def test_channel_gone_error_removes_watched(self):
        channels.set_watched("C1", True)
        with patch.object(cli, "_poll_channel_once", side_effect=slack.SlackError("not_in_channel", "history")):
            cli.cmd_monitor(["--channel", "C1"])
        self.assertNotIn("C1", channels.watched())
        self.assertEqual(self.emit.call_args.args[0]["event"], "CHANNEL_GONE")
        lock = channels.acquire_lock("C1")
        self.assertIsNotNone(lock)
        lock.close()

    def test_publish_retry_does_not_duplicate_already_delivered_record(self):
        record = channels.mention_record("C1", message(101), "UBOT", "1.0")
        with patch.object(slack, "set_session_status", side_effect=slack.SlackError("network_error", "status")):
            with self.assertRaises(slack.SlackError):
                cli._publish_records(self.conf, "C1", [record])
        cli._publish_records(self.conf, "C1", [record])
        self.emit.assert_called_once()

    def test_thread_fetch_failure_keeps_initial_cursor(self):
        with patch.object(cli.time, "time", return_value=100), \
             patch.object(slack, "conversations_replies", side_effect=slack.SlackError("network_error", "replies")):
            with self.assertRaises(slack.SlackError):
                cli._poll_thread_once(self.conf, "C1", "1.0", "UBOT")
        with patch.object(slack, "conversations_replies", return_value=[message(101)]):
            cli._poll_thread_once(self.conf, "C1", "1.0", "UBOT")
        self.assertEqual(self.emit.call_args.args[0]["ts"], "101")

    def test_reply_command_uses_parent_and_held_is_skipped(self):
        channels.save_state("C1", {"cursor": "100", "threads": {"1.0": "100"}})
        with patch.object(slack, "conversations_history", return_value=[message(1.0,latest_reply="101")]), \
             patch.object(slack, "conversations_replies", return_value=[message(101,text="<@UBOT> 채널 듣기 <@UA>")]):
            cli._poll_channel_once(self.conf, "C1", "UBOT")
        self.assertEqual(self.post.call_args.kwargs["thread_ts"], "1.0")
        self.emit.assert_not_called()
        self.assertEqual(cfg.channel_listeners("C1"), ["UA"])
