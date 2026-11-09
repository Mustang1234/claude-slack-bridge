import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_slack_bridge import channels, chat, cli, config as cfg, server, slack, threads


class ParserTests(unittest.TestCase):
    def test_scoped_commands(self):
        for prefix, scope in (("", "thread"), ("채널 ", "channel"), ("channel ", "channel")):
            for word, kind in (("듣기", "listen"), ("listen", "listen"), ("그만", "unlisten"), ("unlisten", "unlisten")):
                with self.subTest(prefix=prefix, word=word):
                    self.assertEqual(
                        chat.parse_command(prefix + word + " <@UA> <@UB|name> <@UA>"),
                        (kind, ["UA", "UB"], scope),
                    )

    def test_existing_commands_and_invalid_input(self):
        cases = {
            "채널듣기 <@UA>": ("listen", ["UA"], "channel"),
            "채널그만 <@UA>": ("unlisten", ["UA"], "channel"),
            "누구 듣니": ("listeners", None, None),
            "누구 들어": ("listeners", None, None),
            "listeners": ("listeners", None, None),
            "핑": ("ping", None, None),
            "닫기": ("close", None, None),
            "마감 18:00": ("until", (18, 0), None),
            "연장 3시간": ("extend", 3.0, None),
            "연장": ("extend", chat.AUTO_EXTEND_HOURS, None),
            "연장 30분": ("extend", 0.5, None),
            "그만": None, "채널 그만": None, "듣기": None,
            "듣기 <@UA> 그리고 답해": None, "channel listen name": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(chat.parse_command(text), expected)


class ListenerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for target, value in (
            ("CONFIG_DIR", self.root),
            ("CONFIG_PATH", self.root / "config.json"),
            ("CHANNELS_PATH", self.root / "channels.json"),
        ):
            self.enterContext(patch.object(cfg, target, value))
        self.enterContext(patch.object(threads, "THREADS_DIR", self.root / "threads"))
        self.enterContext(patch.object(channels, "CHANNELS_DIR", self.root / "channels"))
        self.enterContext(patch.object(slack, "api", side_effect=AssertionError("Unexpected Slack API call")))
        self.post = self.enterContext(patch.object(slack, "post_message"))
        self.enterContext(patch.object(slack, "chat_update"))
        self.enterContext(patch.object(slack, "update_header", return_value=True))
        self.enterContext(patch.object(slack, "set_session_status", return_value=False))
        threads.save("100.0", {"channel": "CCHAN", "closed": False, "deadline": time.time() + 3600, "require_mention": True})
        threads.save("200.0", {"channel": "CCHAN", "closed": False, "deadline": time.time() + 3600, "require_mention": True})

    def command(self, text, actor="UOWNER", channel="CCHAN"):
        kind, value, scope = chat.parse_command(text)
        result = cli._apply_command(
            "test-token", channel, "100.0", "Test", kind, value,
            threads.load("100.0"), actor, "UOWNER", scope=scope,
        )
        self.assertFalse(result)
        return self.post.call_args.args[2]

    def test_thread_storage_and_reply(self):
        self.assertEqual(self.command("듣기 <@UA>"), "이 스레드에서 이제 <@UA> 의 말도 듣습니다.")
        self.assertEqual(threads.load("100.0")["listeners"], ["UA"])
        self.assertNotIn("listeners", threads.load("200.0"))
        self.assertFalse(cfg.CHANNELS_PATH.exists())
        self.command("듣기 <@UA> <@UB>")
        self.assertEqual(threads.load("100.0")["listeners"], ["UA", "UB"])
        cfg.save_channel_listeners("CCHAN", ["UC"])
        before = cfg.CHANNELS_PATH.read_bytes()
        self.command("그만 <@UA>")
        self.assertEqual(cfg.CHANNELS_PATH.read_bytes(), before)

    def test_channel_storage_and_reply(self):
        before = threads.load("100.0")
        self.assertEqual(self.command("채널 듣기 <@UA>"), "이 채널 전체에서 이제 <@UA> 의 말도 듣습니다.")
        self.assertEqual(cfg.channel_listeners("CCHAN"), ["UA"])
        self.assertEqual(threads.load("100.0"), before)
        self.assertEqual(self.command("채널 그만 <@UA>"), "<@UA> 채널 그만 완료.")
        self.assertEqual(cfg.channel_listeners("CCHAN"), [])

    def test_channel_unlisten_sweeps_open_threads_in_same_channel(self):
        cfg.save_channel_listeners("CCHAN", ["UA", "UB"])
        for ts, channel, closed in (
            ("100.0", "CCHAN", False), ("200.0", "CCHAN", False),
            ("300.0", "COTHER", False), ("400.0", "CCHAN", True),
        ):
            threads.save(ts, {"channel": channel, "closed": closed, "listeners": ["UA", "UB"]})
        unchanged = {ts: threads.load(ts) for ts in ("300.0", "400.0")}
        reply = self.command("채널그만 <@UA>")
        for ts in ("100.0", "200.0"):
            self.assertEqual(threads.load(ts)["listeners"], ["UB"])
        for ts, state in unchanged.items():
            self.assertEqual(threads.load(ts), state)
        self.assertEqual(cfg.channel_listeners("CCHAN"), ["UB"])
        self.assertEqual(reply, "<@UA> 채널 그만 완료. 스레드 2곳 그만 완료")

    def test_channel_unlisten_reports_each_person_in_order(self):
        cfg.save_channel_listeners("CCHAN", ["UA"])
        threads.patch("100.0", listeners=["UA", "UB"])
        threads.patch("200.0", listeners=["UB"])
        self.assertEqual(
            self.command("채널그만 <@UA> <@UB>"),
            "<@UA> 채널 그만 완료. 스레드 1곳 그만 완료\n"
            "<@UB> 는 채널 listener 가 아닙니다. 스레드 2곳 그만 완료",
        )
        self.assertEqual(threads.load("100.0")["listeners"], [])
        self.assertEqual(threads.load("200.0")["listeners"], [])
        self.assertEqual(cfg.channel_listeners("CCHAN"), [])

    def test_channel_unlisten_removes_thread_only_listener_and_skips_corrupt_files(self):
        threads.patch("100.0", listeners=["UA"])
        for name, content in (
            ("300.0.json", b"{broken"), ("400.0.json", b"[]"),
            ("500.0.json", b"\xff"),
            ("600.0.json", b'{"channel":"CCHAN","listeners":42}'),
            ("700.0.inbox.jsonl", b"not json"),
            ("700.0.lock", b"not json"), ("700.0.keeper.log", b"not json"),
        ):
            (threads.THREADS_DIR / name).write_bytes(content)
        with patch.object(threads, "load", wraps=threads.load) as load:
            reply = self.command("채널그만 <@UA>")
        self.assertEqual(threads.load("100.0")["listeners"], [])
        self.assertEqual(reply, "<@UA> 는 채널 listener 가 아닙니다. 스레드 1곳 그만 완료")
        self.assertNotIn("700.0", [call.args[0] for call in load.call_args_list])

    def test_channel_unlisten_continues_after_patch_failure(self):
        for ts in ("100.0", "200.0"):
            threads.patch(ts, listeners=["UA"])
        original_patch = threads.patch

        def fail_one(ts, **fields):
            if ts == "100.0":
                raise OSError("cannot write")
            return original_patch(ts, **fields)

        with patch.object(threads, "patch", side_effect=fail_one):
            reply = self.command("채널그만 <@UA>")
        self.assertEqual(threads.load("100.0")["listeners"], ["UA"])
        self.assertEqual(threads.load("200.0")["listeners"], [])
        self.assertEqual(reply, "<@UA> 는 채널 listener 가 아닙니다. 스레드 1곳 그만 완료")

    def test_unlisten_missing_and_mixed_members(self):
        self.command("듣기 <@UA>")
        self.command("채널 듣기 <@UA>")
        self.assertEqual(
            self.command("그만 <@UA> <@UB>"),
            "이 스레드에서 <@UA> 는 이제 듣지 않습니다.\n<@UB> 는 이 스레드 listener 가 아닙니다.",
        )
        self.assertEqual(cfg.channel_listeners("CCHAN"), ["UA"])
        self.assertEqual(self.command("채널 그만 <@UB>"), "<@UB> 는 채널 listener 가 아닙니다.")
        self.assertEqual(self.command("그만 <@UA>"), "<@UA> 는 이 스레드 listener 가 아닙니다.")

    def test_listing_both_scopes(self):
        self.assertEqual(self.command("누구 듣니"), "이 스레드에서 듣는 사람: 주인만\n채널 전체: 주인만")
        self.command("듣기 <@UA>")
        self.command("채널 듣기 <@UB>")
        self.assertEqual(self.command("listeners"), "이 스레드에서 듣는 사람: <@UOWNER>(주인), <@UA>\n채널 전체: <@UB>")

    def test_chat_list_shows_thread_and_channel_listeners(self):
        self.command("듣기 <@UA> <@UB>")
        self.command("채널 듣기 <@UC>")
        threads.patch("200.0", label="Missing")
        threads.save("300.0", {"thread_ts": "300.0", "channel": "COTHER", "listeners": [], "label": "Empty"})
        threads.save("400.0", {"thread_ts": "400.0", "channel": "DTEST", "listeners": ["UD"], "label": "DM"})
        with patch.object(threads, "inbox_keeper_alive", return_value=False), \
             patch.object(chat, "_chats", {}), \
             patch("time.strftime", return_value="12:00"):
            listing = server.slack_chat_list()
        self.assertEqual(listing.splitlines(), [
            "표시: ** 기본 스레드, * 이 세션이 쥔 스레드",
            "100.0  마감 12:00  지킴이없음    스레드 listeners=UA,UB  채널 전체 listeners=UC",
            "200.0  마감 12:00  지킴이없음  Missing  스레드 listeners=0  채널 전체 listeners=UC",
            "300.0  마감 12:00  지킴이없음  Empty  스레드 listeners=0  채널 전체 listeners=0",
            "400.0  마감 12:00  지킴이없음  DM",
        ])

    def test_owner_gate_and_dm(self):
        for text in ("듣기 <@UA>", "채널 듣기 <@UA>", "그만 <@UA>", "채널 그만 <@UA>", "listeners"):
            with self.subTest(text=text):
                self.assertEqual(self.command(text, actor="UA"), "주인만 할 수 있습니다.")
                self.assertEqual(self.command(text, channel="DTEST"), "DM 에서는 쓸 수 없습니다.")
        self.assertFalse(cfg.CHANNELS_PATH.exists())
        self.assertNotIn("listeners", threads.load("100.0"))

    def test_union_and_other_thread_isolation(self):
        self.command("듣기 <@UA> <@UB>")
        self.command("채널 듣기 <@UB>")
        for thread, allowed in (("100.0", {"UOWNER", "UA", "UB"}), ("200.0", {"UOWNER", "UB"})):
            listeners = cli._effective_listeners("CCHAN", threads.load(thread))
            self.assertEqual(len(listeners), len(set(listeners)))
            for user in ("UOWNER", "UA", "UB", "UOTHER"):
                message = {"user": user, "text": "<@UBOT> hello"}
                self.assertEqual(chat.is_for_me(message, "UBOT", "UOWNER", True, listeners), user in allowed)
                self.assertFalse(chat.is_for_me(message, "UBOT", "", True, listeners))
                self.assertFalse(chat.is_for_me({**message, "text": "hello"}, "UBOT", "UOWNER", True, listeners))
        self.assertEqual(cli._effective_listeners("COTHER", threads.load("200.0")), [])

    def test_bot_listener_rejects_missing_mention_owner_and_system_messages(self):
        base = {"user": "UAGENT", "bot_id": "BAGENT", "text": "<@UBOT> hello"}
        for message, owners in (
            ({**base, "text": "hello"}, ["UOWNER"]),
            (base, []),
            ({**base, "user": ""}, ["UOWNER"]),
            ({**base, "subtype": "message_changed"}, ["UOWNER"]),
            ({**base, "subtype": "file_share"}, ["UOWNER"]),
            ({"subtype": "bot_message", "text": "<@UBOT> legacy"}, ["UOWNER"]),
        ):
            with self.subTest(message=message, owners=owners):
                self.assertFalse(chat.is_for_me(message, "UBOT", owners, True, ["UAGENT"]))

    def test_bot_profile_only_requires_listener(self):
        message = {"user": "UAGENT", "bot_profile": {"id": "BAGENT"}, "text": "<@UBOT> hello"}
        self.assertTrue(chat.is_for_me(message, "UBOT", "UOWNER", True, ["UAGENT"]))
        self.assertFalse(chat.is_for_me(message, "UBOT", "UOWNER", True, []))

    def test_wait_reply_blocks_self_by_user_id(self):
        self.command("듣기 <@UBOT>")
        current = chat.Chat(channel="CCHAN", thread_ts="100.0", deadline=time.time() + 3600)
        message = {"user": "UBOT", "bot_id": "BBOT", "text": "<@UBOT> self"}
        with patch.object(chat, "current", return_value=current), \
             patch.object(cfg, "load", return_value=cfg.Config("test-token", "CCHAN", "UOWNER")), \
             patch.object(slack, "auth_test", return_value={"user_id": "UBOT"}) as auth, \
             patch.object(chat, "poll_new", return_value=[message]) as poll:
            self.assertEqual(chat.wait_reply("test-token", "UBOT", 0), ("timeout", []))
        auth.assert_not_called()
        poll.assert_called_once_with("test-token", current, context=[])

    def test_wait_reply_channel_owner_requires_mention(self):
        current = chat.Chat(channel="CCHAN", thread_ts="100.0", deadline=time.time() + 3600)
        with patch.object(chat, "current", return_value=current), \
             patch.object(cfg, "load", return_value=cfg.Config("test-token", "CCHAN", "UOWNER")), \
             patch.object(slack, "auth_test", return_value={"user_id": "UBOT", "bot_id": "BBOT"}):
            for text, accepted in (("<@UBOT> hello", True), ("hello", False)):
                with self.subTest(text=text):
                    message = {"user": "UOWNER", "text": text}
                    with patch.object(chat, "poll_new", return_value=[message]):
                        status, messages = chat.wait_reply("test-token", "UBOT", 0)
                    self.assertEqual(status, "messages" if accepted else "timeout")
                    self.assertEqual(messages, [message] if accepted else [])

    def test_bot_listener_commands_cannot_mutate_state(self):
        self.command("듣기 <@UAGENT>")
        before = threads.load("100.0")
        for command in ("핑", "연장", "마감 18:00", "닫기", "듣기 <@UA>", "그만 <@UAGENT>", "채널 듣기 <@UA>", "채널 그만 <@UA>"):
            msg = {"user": "UAGENT", "bot_id": "BAGENT", "text": "<@UBOT> " + command}
            self.assertTrue(chat.is_for_me(msg, "UBOT", "UOWNER", True, before["listeners"]))
            self.assertEqual(self.command(chat.strip_mention(msg["text"], "UBOT"), actor=msg["user"]), "주인만 할 수 있습니다.")
            self.assertEqual(threads.load("100.0"), before)
            self.assertFalse(cfg.CHANNELS_PATH.exists())

    def test_health_counts_only_allowed_bot_messages(self):
        for bot, text, expected in (("BA", "<@UBOT> hello", True), ("BA", "hello", False), ("BA", "<@UBOT> 닫기", False)):
            threads.patch("100.0", down_since=0)
            msg = {"ts": str(time.time() - 600), "user": "UA", "bot_id": bot, "text": text}
            with patch.object(threads, "session_listening", return_value=True):
                cli._check_session_health("test-token", "CCHAN", "100.0", [msg], "UBOT", "UOWNER", True, ["UA"], {})
            self.assertEqual(bool(threads.load("100.0").get("down_since")), expected)

    def test_wait_reply_uses_bot_listener_union(self):
        self.command("듣기 <@UA>")
        self.command("채널 듣기 <@UB>")
        current = chat.Chat(channel="CCHAN", thread_ts="100.0", deadline=time.time() + 3600)
        with patch.object(chat, "current", return_value=current), \
             patch.object(cfg, "load", return_value=cfg.Config("test-token", "CCHAN", "UOWNER")), \
             patch.object(slack, "auth_test", return_value={"bot_id": "BBOT"}):
            for user, text, bot, accepted in (
                ("UA", "<@UBOT> local", "BA", True),
                ("UB", "<@UBOT> shared", "BB", True),
                ("UA", "no mention", "BA", False),
                ("UOTHER", "<@UBOT> denied", "BC", False),
                ("UBOT", "<@UBOT> self", "BBOT", False),
            ):
                msg = {"user": user, "text": text, "bot_id": bot}
                with patch.object(chat, "poll_new", return_value=[msg]):
                    status, messages = chat.wait_reply("test-token", "UBOT", 0)
                self.assertEqual(status, "messages" if accepted else "timeout")
                self.assertEqual(messages, [msg] if accepted else [])

    def test_wait_reply_preserves_bot_turn_limit_across_seen_messages(self):
        self.command("듣기 <@UA>")
        history = [{"ts": str(101 + i), "user": "UA", "bot_id": "BA", "text": "<@UBOT> hello"}
                   for i in range(chat.BOT_TURN_LIMIT)]
        candidate = {"ts": "110.0", "user": "UA", "bot_id": "BA", "text": "<@UBOT> again"}
        current = chat.Chat(channel="CCHAN", thread_ts="100.0", deadline=time.time() + 3600,
                            seen={m["ts"] for m in history})
        with patch.object(chat, "current", return_value=current), \
             patch.object(cfg, "load", return_value=cfg.Config("test-token", "CCHAN", "UOWNER")), \
             patch.object(slack, "conversations_replies", return_value=history + [candidate]) as replies:
            self.assertEqual(chat.wait_reply("test-token", "UBOT", 0), ("timeout", []))
            human = {"ts": "111.0", "user": "UOWNER", "text": "reset"}
            later = {**candidate, "ts": "112.0"}
            replies.return_value = history + [candidate, human, later]
            self.assertEqual(chat.wait_reply("test-token", "UBOT", 0), ("messages", [later]))

    def test_wait_reply_uses_co_owners_and_stored_mention_policy(self):
        current = chat.Chat(channel="CCHAN", thread_ts="100.0", deadline=time.time() + 3600)
        with patch.object(chat, "current", return_value=current), \
             patch.object(cfg, "load", return_value=cfg.Config("test-token", "CCHAN", "UOWNER", co_owner_ids=["UCOOWNER"])):
            for require_mention, text, accepted in ((True, "<@UBOT> hello", True), (True, "hello", False), (False, "hello", False)):
                with self.subTest(require_mention=require_mention, text=text):
                    threads.patch("100.0", require_mention=require_mention)
                    message = {"user": "UCOOWNER", "text": text}
                    with patch.object(chat, "poll_new", return_value=[message]):
                        self.assertEqual(chat.wait_reply("test-token", "UBOT", 0),
                                         ("messages", [message]) if accepted else ("timeout", []))

    def test_wait_reply_missing_state_keeps_channel_filter(self):
        current = chat.Chat(channel="CCHAN", thread_ts="100.0", deadline=time.time() + 3600)
        with patch.object(chat, "current", return_value=current), \
             patch.object(cfg, "load", return_value=cfg.Config("test-token", "CCHAN", "UOWNER", co_owner_ids=["UCOOWNER"])), \
             patch.object(threads, "load", return_value=None):
            for user, text, accepted in (
                ("UOTHER", "hello", False),
                ("UOTHER", "<@UBOT> hello", False),
                ("UOWNER", "hello", False),
                ("UOWNER", "<@UBOT> hello", True),
                ("UCOOWNER", "hello", False),
                ("UCOOWNER", "<@UBOT> hello", True),
            ):
                with self.subTest(user=user, text=text):
                    message = {"user": user, "text": text}
                    with patch.object(chat, "poll_new", return_value=[message]):
                        self.assertEqual(chat.wait_reply("test-token", "UBOT", 0),
                                         ("messages", [message]) if accepted else ("timeout", []))


    def test_keeper_channel_requires_mention_even_when_state_false(self):
        threads.patch("100.0", require_mention=False)
        messages = [
            {"ts": "101.0", "user": "UOTHER", "text": "hello"},
            {"ts": "102.0", "user": "UOTHER", "text": "<@UBOT> hello"},
            {"ts": "103.0", "user": "UOWNER", "text": "hello"},
            {"ts": "104.0", "user": "UOWNER", "text": "<@UBOT> hello"},
            {"ts": "105.0", "user": "UCOOWNER", "text": "hello"},
            {"ts": "106.0", "user": "UCOOWNER", "text": "<@UBOT> hello"},
        ]
        with patch.object(cfg, "load", return_value=cfg.Config("test-token", "CCHAN", "UOWNER", co_owner_ids=["UCOOWNER"])), \
             patch.object(cli, "_keep_awake", return_value=None), \
             patch.object(slack, "auth_test", return_value={"user_id": "UBOT"}), \
             patch.object(slack, "conversations_replies", return_value=messages), \
             patch.object(cli, "_check_session_health"), \
             patch.object(cli.time, "sleep", side_effect=lambda _: threads.patch("100.0", closed=True)), \
             patch.object(cli, "_log"):
            cli.cmd_keeper(["--thread", "100.0"])
        self.assertEqual([m["ts"] for m in threads.read_inbox("100.0", 0)], ["104.0", "106.0"])


    def test_close_clears_thread_listeners_only(self):
        self.command("듣기 <@UA>")
        self.command("채널 듣기 <@UB>")
        self.assertTrue(chat.close_thread("test-token", "CCHAN", "100.0", "Test", "done", resumable=True))
        self.assertEqual(threads.load("100.0")["listeners"], [])
        self.assertEqual(cfg.channel_listeners("CCHAN"), ["UB"])
        threads.patch("100.0", closed=False)
        self.assertEqual(cli._effective_listeners("CCHAN", threads.load("100.0")), ["UB"])

    def test_deferred_close_preserves_listeners(self):
        self.command("듣기 <@UA>")
        self.post.side_effect = slack.SlackError("offline", "chat.postMessage")
        self.assertFalse(chat.close_thread("test-token", "CCHAN", "100.0", "Test", "done", only_if_notified=True))
        self.assertEqual(threads.load("100.0")["listeners"], ["UA"])
        self.assertFalse(threads.load("100.0")["closed"])

    def test_keeper_uses_union_and_scoped_parser(self):
        # Two polls: owner grants access, then the next poll admits the new listener.
        cfg.save_channel_listeners("CCHAN", ["UB"])
        messages = [
            [{"ts": "101.0", "user": "UOWNER", "text": "<@UBOT> 듣기 <@UA>"}],
            [{"ts": "102.0", "user": "UA", "bot_id": "BA", "text": "<@UBOT> local"},
             {"ts": "103.0", "user": "UB", "bot_id": "BB", "text": "<@UBOT> shared"},
             {"ts": "104.0", "user": "UOTHER", "text": "<@UBOT> denied"}],
        ]
        sleeps = []

        def sleep(_):
            sleeps.append(True)
            if len(sleeps) == 2:
                threads.patch("100.0", closed=True)

        with patch.object(cfg, "load", return_value=cfg.Config("test-token", "CCHAN", "UOWNER")), \
             patch.object(cli, "_keep_awake", return_value=None), \
             patch.object(slack, "auth_test", return_value={"user_id": "UBOT"}), \
             patch.object(slack, "conversations_replies", side_effect=messages) as replies, \
             patch.object(cli, "_check_session_health") as health, \
             patch.object(cli.time, "sleep", side_effect=sleep), \
             patch.object(cli, "_log"):
            cli.cmd_keeper(["--thread", "100.0"])
        self.assertEqual([m["user"] for m in threads.read_inbox("100.0", 0)], ["UA", "UB"])
        self.assertEqual(cfg.channel_listeners("CCHAN"), ["UB"])
        self.assertEqual(set(health.call_args.args[-2]), {"UA", "UB"})
        for call in replies.call_args_list:
            self.assertEqual(call.args, ("test-token", "CCHAN", "100.0"))


if __name__ == "__main__":
    unittest.main()
