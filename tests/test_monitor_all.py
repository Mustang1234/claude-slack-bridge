import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_slack_bridge import channels, chat, cli, config as cfg, server, slack, threads


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
        channels.save_state("discovery", {"ids": ["COLD"]})
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
        channels.set_watched("C1", True)
        lock = channels.acquire_lock("C1")
        self.addCleanup(lock.close)
        with patch.object(slack, "my_conversations", return_value=[{"id": "C1", "name": "one", "private": False}]), \
             patch.object(cli, "_poll_channel_once") as poll, \
             patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt):
            cli.cmd_monitor(["--all"])
        poll.assert_not_called()
        self.assertIn({"event": "ALREADY_WATCHING", "channel": "C1"}, [c.args[0] for c in self.emit.call_args_list])

    def test_channel_and_all_use_same_scanner(self):
        channels.set_watched("C1", True)
        for args in (["--channel", "C1"], ["--all"]):
            with self.subTest(args=args), \
                 patch.object(slack, "my_conversations", return_value=[{"id": "C1", "name": "one", "private": False}]), \
                 patch.object(cli, "_poll_channel_once") as poll, \
                 patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt):
                cli.cmd_monitor(args)
            poll.assert_called_once_with(self.conf, "C1", "UBOT", tracked=[])

    def test_tracked_management_and_all_poll(self):
        channels.set_watched("C1", True)
        cli.cmd_monitor(["--track", "C1", "1.0", "--any-reply"])
        self.assertTrue(next(iter(channels.tracked().values()))["any_reply"])
        with patch.object(slack, "my_conversations", return_value=[{"id": "C1", "name": "one", "private": False}]), \
             patch.object(cli, "_poll_channel_once"), \
             patch.object(cli, "_poll_thread_once") as poll, \
             patch.object(cli.time, "sleep", side_effect=lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()) if seconds == 10 else None):
            cli.cmd_monitor(["--all"])
        poll.assert_called_once_with(self.conf, "C1", "1.0", "UBOT", True, bot_id="")
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

    def test_all_emits_channel_records_for_multiple_channels(self):
        channels.set_watched("C1", True)
        channels.set_watched("C2", True)
        conversations = [{"id": c, "name": c, "private": False} for c in ("C1", "C2")]
        def sleep(seconds):
            if seconds == 10:
                raise KeyboardInterrupt
        with patch.object(slack, "my_conversations", return_value=conversations), \
             patch.object(slack, "conversations_history", return_value=[message(1001)]), \
             patch.object(cli.time, "time", return_value=1000), \
             patch.object(cli.time, "sleep", side_effect=sleep):
            cli.cmd_monitor(["--all"])
        records = [c.args[0] for c in self.emit.call_args_list if c.args[0].get("source") == "channel"]
        self.assertEqual([r["channel"] for r in records], ["C1", "C2"])
        self.assertTrue(all(r["ts"] == "1001" and r["thread_ts"] == "1001" for r in records))

    def test_unwatched_channel_stays_unwatched_after_restart(self):
        channels.save_state("discovery", {"ids": ["C1"]})
        with patch.object(slack, "my_conversations", return_value=[{"id": "C1", "name": "one", "private": False}]), \
             patch.object(cli, "_poll_channel_once") as poll, \
             patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt):
            cli.cmd_monitor(["--all"])
        poll.assert_not_called()
        self.assertEqual(channels.watched(), {})

    def test_mcp_list_returns_one_all_command(self):
        channels.set_watched("C1", True)
        channels.set_watched("C2", True)
        text = server.slack_channel_list()
        self.assertEqual(text.count("--all"), 1)
        self.assertNotIn("--channel", text)

    def test_reply_command_uses_parent_and_held_is_skipped(self):
        channels.save_state("C1", {"cursor": "100", "threads": {"1.0": "100"}})
        with patch.object(slack, "conversations_history", return_value=[message(1.0,latest_reply="101")]), \
             patch.object(slack, "conversations_replies", return_value=[message(101,text="<@UBOT> 채널 듣기 <@UA>")]):
            cli._poll_channel_once(self.conf, "C1", "UBOT")
        self.assertEqual(self.post.call_args.kwargs["thread_ts"], "1.0")
        self.emit.assert_not_called()
        self.assertEqual(cfg.channel_listeners("C1"), ["UA"])

    def test_thread_poll_rejects_self_by_bot_identity(self):
        for any_reply in (False, True):
            for extra in ({"bot_id": "BSELF"}, {"bot_profile": {"id": "BSELF"}}):
                with self.subTest(any_reply=any_reply, extra=extra):
                    out, state = channels.thread_poll("C1", "1.0", {"cursor": "1"},
                        [message(2, "UALIAS", **extra)], "UBOT", ["UOWNER"], ["UALIAS"],
                        any_reply, bot_id="BSELF")
                    self.assertEqual(out, [])
                    self.assertEqual(state["cursor"], "2")

    def test_discovery_after_empty_first_scan_marks_join_as_new(self):
        with patch.object(slack, "my_conversations", return_value=[{"id": "C1", "name": "one", "private": False}]):
            cli._discover_once(self.conf, set(), register=True, initialized=True)
        self.assertTrue(self.emit.call_args.args[0]["new"])


    def test_standalone_lock_conflict_emits_once_and_exits(self):
        for args, key in ((["--channel", "C1"], "C1"),
                          (["--thread-poll", "C1", "1.0"], channels.thread_key("C1", "1.0"))):
            with self.subTest(args=args):
                self.emit.reset_mock()
                lock = channels.acquire_lock(key)
                try:
                    with patch.object(cli.time, "sleep", side_effect=AssertionError("Must exit")):
                        cli.cmd_monitor(args)
                    self.assertEqual(self.emit.call_count, 1)
                    self.assertEqual(self.emit.call_args.args[0]["event"], "ALREADY_WATCHING")
                finally:
                    lock.close()

    def test_all_reports_conflict_once_then_rejoins(self):
        channels.set_watched("C1", True)
        lock = channels.acquire_lock("C1")
        self.addCleanup(lock.close)
        rounds = []
        def sleep(seconds):
            if seconds == 10:
                rounds.append(1)
                if len(rounds) == 2:
                    lock.close()
                if len(rounds) == 3:
                    raise KeyboardInterrupt
        with patch.object(slack, "my_conversations", return_value=[{"id": "C1", "name": "one", "private": False}]), \
             patch.object(cli, "_poll_channel_once") as poll, \
             patch.object(cli.time, "sleep", side_effect=sleep):
            cli.cmd_monitor(["--all"])
        events = [call.args[0]["event"] for call in self.emit.call_args_list]
        self.assertEqual(events.count("ALREADY_WATCHING"), 1)
        poll.assert_called_once()

    def test_thread_and_channel_scanners_use_independent_locks(self):
        channels.set_watched("C1", True)
        for key, args, method in (
            ("C1", ["--thread-poll", "C1", "1.0"], "_poll_thread_once"),
            (channels.thread_key("C1", "1.0"), ["--all"], "_poll_channel_once"),
        ):
            with self.subTest(args=args):
                lock = channels.acquire_lock(key)
                try:
                    with patch.object(slack, "my_conversations", return_value=[{"id": "C1", "name": "one", "private": False}]), \
                         patch.object(cli, method) as poll, \
                         patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt):
                        cli.cmd_monitor(args)
                    poll.assert_called_once()
                finally:
                    lock.close()

    def test_all_tracked_uses_same_thread_lock_as_standalone(self):
        channels.set_watched("C1", True)
        channels.set_tracked("C1", "1.0", True)
        lock = channels.acquire_lock(channels.thread_key("C1", "1.0"))
        self.addCleanup(lock.close)
        def sleep(seconds):
            if seconds == 10:
                raise KeyboardInterrupt
        with patch.object(slack, "my_conversations", return_value=[{"id": "C1", "name": "one", "private": False}]), \
             patch.object(cli, "_poll_channel_once") as channel_poll, \
             patch.object(cli, "_poll_thread_once") as thread_poll, \
             patch.object(cli.time, "sleep", side_effect=sleep):
            cli.cmd_monitor(["--all"])
        channel_poll.assert_called_once()
        thread_poll.assert_not_called()

    def test_thread_fetch_passes_oldest_and_ignores_parent(self):
        channels.save_state(channels.thread_key("C1", "1.0"), {"cursor": "100"})
        with patch.object(slack, "conversations_replies", return_value=[message("1.0"), message(101)]) as fetch:
            cli._poll_thread_once(self.conf, "C1", "1.0", "UBOT")
        fetch.assert_called_once_with("test-token", "C1", "1.0", oldest="100")
        self.assertEqual([c.args[0]["ts"] for c in self.emit.call_args_list], ["101"])

    def test_held_cursor_is_not_advanced_without_delivery(self):
        key = channels.thread_key("C1", "1.0")
        channels.save_state(key, {"cursor": "100", "bot_streak": 2})
        with patch.object(channels, "held_threads", return_value={"1.0"}):
            cli._poll_thread_once(self.conf, "C1", "1.0", "UBOT")
        self.assertEqual(channels.load_state(key), {"cursor": "100", "bot_streak": 2})

    def _run_test_keeper(self, messages):
        threads.save("1.0", {"channel": "C1", "deadline": time.time()+3600,
                             "require_mention": True, "closed": False})
        with patch.object(cli, "_keep_awake", return_value=None), \
             patch.object(slack, "conversations_replies", return_value=messages), \
             patch.object(cli, "_check_session_health"), \
             patch.object(cli, "_log"), \
             patch.object(cli.time, "sleep", side_effect=lambda _: threads.patch("1.0", closed=True)):
            cli.cmd_keeper(["--thread", "1.0"])

    def test_keeper_to_thread_poll_handoff_deduplicates(self):
        m = message(101)
        self._run_test_keeper([m])
        self.assertEqual([m["ts"] for m in threads.read_inbox("1.0", 0)], ["101"])
        channels.save_state(channels.thread_key("C1", "1.0"), {"cursor": "100"})
        with patch.object(slack, "conversations_replies", return_value=[m, message(102)]):
            cli._poll_thread_once(self.conf, "C1", "1.0", "UBOT")
        self.assertEqual([c.args[0]["ts"] for c in self.emit.call_args_list], ["102"])

    def test_channel_to_keeper_handoff_deduplicates(self):
        m = message(101)
        cli._publish_records(self.conf, "C1", [channels.mention_record("C1", m, "UBOT", "1.0")])
        self._run_test_keeper([m, message(102)])
        self.assertEqual([m["ts"] for m in threads.read_inbox("1.0", 0)], ["102"])

    def test_legacy_keeper_inbox_counts_as_delivered(self):
        threads.append_inbox("1.0", message(101), "hello")
        cli._publish_records(self.conf, "C1", [channels.mention_record("C1", message(101), "UBOT", "1.0")])
        self.emit.assert_not_called()
        self.assertTrue(channels.load_state("delivered-C1")["101"])

    def test_target_errors_do_not_stop_other_channels_or_threads(self):
        channels.set_watched("C1", True)
        channels.set_watched("C2", True)
        channels.set_tracked("C1", "1.0", True)
        def sleep(seconds):
            if seconds == 10:
                raise KeyboardInterrupt
        for exc in (OSError("disk"), ValueError("json"), slack.SlackError("network_error", "history")):
            with self.subTest(exc=exc), \
                 patch.object(slack, "my_conversations", return_value=[{"id": c, "name": c, "private": False} for c in ("C1", "C2")]), \
                 patch.object(cli, "_poll_channel_once", side_effect=[exc, None]) as poll, \
                 patch.object(cli, "_poll_thread_once") as thread_poll, \
                 patch.object(cli.time, "sleep", side_effect=sleep), \
                 patch.object(cli, "print") as log:
                cli.cmd_monitor(["--all"])
                self.assertEqual([call.args[1] for call in poll.call_args_list], ["C1", "C2"])
                thread_poll.assert_called_once()
                self.assertIn("SLACK_UNREACHABLE", log.call_args.args[0])

    def test_incremental_bot_streak_survives_restart_and_old_parent(self):
        state = {"cursor": "100", "bot_streak": 4}
        first, state = channels.thread_poll("C1", "1.0", state,
            [message("1.0"), message(101, "UAGENT", bot_id="BOTHER")], "UBOT",
            ["UOWNER"], ["UAGENT"])
        self.assertEqual(len(first), 1)
        channels.save_state("test-streak", state)
        second, state = channels.thread_poll("C1", "1.0", channels.load_state("test-streak"),
            [message("1.0"), message(102, "UAGENT", bot_id="BOTHER")], "UBOT",
            ["UOWNER"], ["UAGENT"])
        self.assertEqual(second, [])
        third, state = channels.thread_poll("C1", "1.0", state,
            [message(103), message(104, "UAGENT", bot_id="BOTHER")], "UBOT",
            ["UOWNER"], ["UAGENT"])
        self.assertEqual(len(third), 2)

    def test_startup_always_requires_thread_monitor(self):
        current = chat.Chat(channel="C1", thread_ts="1.0", deadline=time.time()+3600)
        text = server._startup_lines(current, "ready")
        self.assertIn("--thread", text)
        self.assertNotIn("통합 Monitor가 없을 때만", text)

    def test_versions_match(self):
        import claude_slack_bridge
        import tomllib
        project = Path(__file__).resolve().parents[1]
        version = tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"]
        self.assertEqual(claude_slack_bridge.__version__, version)
        self.assertIn('version="' + version + '"', (project / "src/claude_slack_bridge/server.py").read_text())

    def test_concurrent_handoff_writes_only_one_inbox(self):
        import threading
        barrier = threading.Barrier(2)
        failures = []
        def deliver(keeper):
            try:
                barrier.wait(timeout=3)
                if keeper:
                    channels.deliver_once("C1", "1.0", "101",
                        lambda: threads.append_inbox("1.0", message(101), "hello"))
                else:
                    cli._publish_records(self.conf, "C1", [channels.mention_record("C1", message(101), "UBOT", "1.0")])
            except BaseException as exc:
                failures.append(exc)
        workers = [threading.Thread(target=deliver, args=(keeper,)) for keeper in (True, False)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        self.assertEqual(len(threads.read_inbox("1.0", 0)) + self.emit.call_count, 1)

    def test_one_tracked_thread_error_does_not_skip_next_thread(self):
        channels.set_watched("C1", True)
        channels.set_tracked("C1", "1.0", True)
        channels.set_tracked("C1", "2.0", True)
        def sleep(seconds):
            if seconds == 10:
                raise KeyboardInterrupt
        with patch.object(slack, "my_conversations", return_value=[{"id": "C1", "name": "one", "private": False}]), \
             patch.object(cli, "_poll_channel_once"), \
             patch.object(cli, "_poll_thread_once", side_effect=[OSError("disk"), None]) as poll, \
             patch.object(cli.time, "sleep", side_effect=sleep), \
             patch.object(cli, "print"):
            cli.cmd_monitor(["--all"])
        self.assertEqual([call.args[2] for call in poll.call_args_list], ["1.0", "2.0"])

    def test_first_discovery_does_not_register_existing_membership(self):
        with patch.object(slack, "my_conversations", return_value=[{"id": "C1", "name": "one", "private": False}]), \
             patch.object(cli, "_poll_channel_once") as poll, \
             patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt):
            cli.cmd_monitor(["--all"])
        self.assertEqual(channels.watched(), {})
        self.assertEqual(channels.load_state("discovery")["ids"], ["C1"])
        poll.assert_not_called()

    def test_retrack_resets_cursor_and_untrack_deletes_state(self):
        key = channels.thread_key("C1", "1.0")
        channels.save_state(key, {"cursor": "100"})
        channels.set_tracked("C1", "1.0", False)
        self.assertFalse(channels.state_path(key).exists())
        with patch.object(channels.time, "time", return_value=200):
            channels.set_tracked("C1", "1.0", True)
        with patch.object(slack, "conversations_replies", return_value=[message(150), message(201)]):
            cli._poll_thread_once(self.conf, "C1", "1.0", "UBOT")
        self.assertEqual([call.args[0]["ts"] for call in self.emit.call_args_list], ["201"])

    def test_rejoining_channel_starts_at_now(self):
        channels.save_state("C1", {"cursor": "10", "threads": {"1.0": "10"}})
        with patch.object(cli.time, "time", return_value=200), \
             patch.object(slack, "my_conversations", return_value=[{"id": "C1", "name": "one", "private": False}]):
            cli._discover_once(self.conf, set(), register=True, initialized=True)
        self.assertEqual(channels.load_state("C1"), {"cursor": "200.000000", "threads": {}})

    def test_first_listener_bot_reply_is_received(self):
        found, state = channels.thread_poll("C1", "1.0", {"cursor": "100"},
            [message(101, "UAGENT", bot_id="BOTHER")], "UBOT", ["UOWNER"], ["UAGENT"])
        self.assertEqual([m["ts"] for m in found], ["101"])
        self.assertEqual(state["bot_streak"], 1)

    def test_channel_incremental_history_and_replies_are_bounded(self):
        channels.save_state("C1", {"cursor": "100", "threads": {"1.0": "90"}})
        with patch.object(slack, "conversations_history", return_value=[message(101)]) as history, \
             patch.object(slack, "conversations_replies", return_value=[message(95)]) as replies:
            cli._poll_channel_once(self.conf, "C1", "UBOT")
        self.assertEqual(history.call_count, 1)
        self.assertEqual(history.call_args.args[2], "100")
        self.assertEqual(history.call_args.kwargs["max_pages"], 1)
        # 이번 시각 기준 7일 이내인 부모만 순회한다.
        with patch.object(cli.time, "time", return_value=200), \
             patch.object(slack, "conversations_history", return_value=[]), \
             patch.object(slack, "conversations_replies", return_value=[message(95)]) as replies:
            channels.save_state("C1", {"cursor": "100", "threads": {"1.0": "90"}})
            cli._poll_channel_once(self.conf, "C1", "UBOT")
        replies.assert_called_once_with("test-token", "C1", "1.0", oldest="90", max_pages=3)
        self.assertEqual(channels.load_state("C1")["threads"]["1.0"], "95")

    def test_all_has_no_per_target_sleep(self):
        channels.set_watched("C1", True)
        channels.set_watched("C2", True)
        with patch.object(slack, "my_conversations", return_value=[{"id": c, "name": c, "private": False} for c in ("C1", "C2")]), \
             patch.object(cli, "_poll_channel_once") as poll, \
             patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt) as sleep:
            cli.cmd_monitor(["--all", "--interval", "30"])
        self.assertEqual(poll.call_count, 2)
        sleep.assert_called_once_with(30)

    def test_history_page_continuation_does_not_skip_older_new_posts(self):
        channels.save_state("C1", {"cursor": "100", "threads": {}})
        def history(token, channel, oldest, **kwargs):
            paging = kwargs["page_state"]
            if not paging.get("cursor"):
                paging["cursor"] = "next"
                return [message(120)]
            self.assertEqual(oldest, "100")
            paging["cursor"] = ""
            return [message(110)]
        with patch.object(slack, "conversations_history", side_effect=history), \
             patch.object(slack, "conversations_replies", return_value=[]):
            cli._poll_channel_once(self.conf, "C1", "UBOT")
            self.assertEqual(channels.load_state("C1")["cursor"], "100")
            cli._poll_channel_once(self.conf, "C1", "UBOT")
        self.assertEqual(channels.load_state("C1")["cursor"], "120")
        self.assertEqual([c.args[0]["ts"] for c in self.emit.call_args_list], ["120", "110"])
