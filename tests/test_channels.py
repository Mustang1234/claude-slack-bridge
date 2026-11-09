import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_slack_bridge import channels, chat, config as cfg, server, slack, threads

BOT = "UBOT"
OWNER = "UOWNER"


def msg(ts, user=OWNER, text=f"<@{BOT}> 해줘", **extra):
    return {"ts": ts, "user": user, "text": text, **extra}


class PollTests(unittest.TestCase):
    def setUp(self):
        self.replies = {}

    def fetch(self, parent, oldest):
        return [m for m in self.replies.get(parent, []) if float(m["ts"]) >= float(oldest)]

    def run_poll(self, state, history, listeners=(), held=(), now=1000.0):
        return channels.poll(
            "C1", state, history, self.fetch, BOT, OWNER, list(listeners), set(held), now,
        )

    def test_first_run_skips_messages_older_than_backfill(self):
        now = 950.0 + channels.FIRST_BACKFILL + 10
        history = [msg("900.0", latest_reply="950.0")]
        self.replies["900.0"] = [msg("900.0"), msg("950.0")]
        found, state = self.run_poll({}, history, now=now)
        self.assertEqual(found, [])
        self.assertEqual(state["threads"], {"900.0": "950.0"})

    def test_first_run_picks_up_mentions_just_before_watch(self):
        # 채널에 초대되자마자 멘션하면 감시 등록보다 먼저 온다 — 처음 띄울 때 그것을 놓치지 않는다
        now = 10000.0
        history = [msg("5000.0", latest_reply="9900.0"), msg("9950.0")]
        self.replies["5000.0"] = [msg("5000.0"), msg("5100.0"), msg("9900.0")]
        found, state = self.run_poll({}, history, now=now)
        self.assertEqual(sorted(f["ts"] for f in found), ["9900.0", "9950.0"])
        self.assertEqual(state["threads"], {"5000.0": "9900.0", "9950.0": "9950.0"})

    def test_top_level_mention_is_emitted_with_own_ts_as_thread(self):
        found, state = self.run_poll({"cursor": "999.0"}, [msg("1001.0")])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["thread_ts"], "1001.0")
        self.assertFalse(found[0]["in_thread"])
        self.assertEqual(found[0]["text"], "해줘")
        self.assertEqual(state["cursor"], "1001.0")

    def test_first_mention_inside_someone_elses_thread(self):
        state = {"cursor": "999.0", "threads": {"900.0": "950.0"}}
        history = [msg("900.0", user="UOTHER", text="회의록", latest_reply="1002.0")]
        self.replies["900.0"] = [
            msg("900.0", user="UOTHER", text="회의록"),
            msg("950.0", user="UOTHER", text="옛 답글"),
            msg("1002.0", text=f"<@{BOT}> 이거 요약"),
        ]
        found, state = self.run_poll(state, history)
        self.assertEqual([f["ts"] for f in found], ["1002.0"])
        self.assertEqual(found[0]["thread_ts"], "900.0")
        self.assertTrue(found[0]["in_thread"])
        self.assertEqual(state["threads"]["900.0"], "1002.0")

    def test_held_thread_is_left_to_its_keeper(self):
        state = {"cursor": "999.0", "threads": {"900.0": "950.0"}}
        history = [msg("900.0", latest_reply="1002.0")]
        self.replies["900.0"] = [msg("1002.0")]
        found, _ = self.run_poll(state, history, held={"900.0"})
        self.assertEqual(found, [])

    def test_only_owner_and_listeners(self):
        state = {"cursor": "999.0"}
        history = [msg("1001.0", user="USTRANGER"), msg("1002.0", user="ULISTEN")]
        found, _ = self.run_poll(state, history, listeners=["ULISTEN"])
        self.assertEqual([f["user"] for f in found], ["ULISTEN"])

    def test_no_mention_no_event(self):
        found, _ = self.run_poll({"cursor": "999.0"}, [msg("1001.0", text="그냥 대화")])
        self.assertEqual(found, [])

    def test_old_threads_fall_out_of_state(self):
        old = channels.THREAD_WINDOW + 10
        state = {"cursor": "999.0", "threads": {"1.0": "2.0"}}
        _, state = self.run_poll(state, [], now=old + 1.0)
        self.assertEqual(state["threads"], {})


class AttachOriginTests(unittest.TestCase):
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
        self.enterContext(patch.object(channels, "CHANNELS_DIR", root / "channels"))
        self.enterContext(patch.object(chat, "_chat", None))
        self.enterContext(patch.object(chat, "_chats", {}))
        self.enterContext(patch.object(server, "_OWNED", set()))
        self.enterContext(patch.object(cfg, "load", return_value=cfg.Config("t", "DDEFAULT", OWNER)))
        self.enterContext(patch.object(server, "_start_keeper", return_value="test keeper"))
        self.enterContext(patch.object(slack, "resolve_target", side_effect=lambda token, target, default: target or default))
        self.enterContext(patch.object(slack, "assert_member"))
        self.enterContext(patch.object(slack, "chat_update"))
        self.enterContext(patch.object(slack, "update_header", return_value=True))
        self.enterContext(patch.object(slack, "set_session_status"))
        self.enterContext(patch.object(slack, "post_message", return_value={"ts": "300.0"}))
        self.enterContext(patch.object(slack, "auth_test", return_value={"user_id": BOT}))
        self.enterContext(patch.object(slack, "conversations_replies", return_value=[]))
        self.enterContext(patch.object(slack, "api", side_effect=AssertionError("Unexpected Slack API call")))
        # 리스너가 남의 스레드 안에서 부른 멘션
        channels.append_inbox("C1", channels.mention_record(
            "C1", msg("1002.0", user="ULISTEN"), BOT, "900.0"))

    def test_listener_mention_attaches_to_its_own_thread(self):
        out = server.slack_chat_attach("900.0", channel="C1", origin="1002.0")
        self.assertIn("붙었습니다", out)

    def test_listener_mention_cannot_attach_elsewhere(self):
        out = server.slack_chat_attach("777.0", channel="C1", origin="1002.0")
        self.assertIn("않았습니다", out)
        self.assertIn("900.0", out)

    def test_mention_reopens_closed_thread_and_advances_keeper_cursor(self):
        threads.save("900.0", {"thread_ts": "900.0", "channel": "C1", "closed": True,
                               "resumable": False, "reason": "작업이 끝났습니다",
                               "deadline": time.time() - 10, "keeper_seen_ts": 950.0})
        out = server.slack_chat_attach("900.0", channel="C1", origin="1002.0")
        self.assertIn("붙었습니다", out)
        state = threads.load("900.0")
        self.assertFalse(state["closed"])
        self.assertEqual(state["keeper_seen_ts"], 1002.0)

    def test_terminal_attach_still_refuses_finished_thread(self):
        threads.save("900.0", {"thread_ts": "900.0", "channel": "C1", "closed": True,
                               "resumable": False, "reason": "작업이 끝났습니다"})
        out = server.slack_chat_attach("900.0", channel="C1", origin="terminal")
        self.assertIn("이미 닫힌 스레드", out)

    def test_channel_mention_cannot_switch(self):
        out = server.slack_chat_switch(channel="C1", origin="1002.0")
        self.assertIn("않았습니다", out)


class WatchedListTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(channels, "CHANNELS_DIR", Path(temp.name) / "ch"))

    def test_watch_list_survives_and_unwatch_removes(self):
        channels.set_watched("C1", True, "team")
        channels.set_watched("C2", True)
        self.assertEqual(set(channels.watched()), {"C1", "C2"})
        channels.set_watched("C1", False)
        self.assertEqual(set(channels.watched()), {"C2"})

    def test_monitor_command_shape(self):
        cmd = channels.monitor_command("C1")
        self.assertEqual(cmd[-3:], ["monitor", "--channel", "C1"])
        self.assertEqual(channels.monitor_command(None)[-2:], ["monitor", "--all"])


class MultiOwnerTests(unittest.TestCase):
    def test_parse_owner_ids_accepts_list_csv_and_single(self):
        self.assertEqual(cfg.parse_owner_ids(["UA", "UB", "UA"]), ["UA", "UB"])
        self.assertEqual(cfg.parse_owner_ids("UA, UB"), ["UA", "UB"])
        self.assertEqual(cfg.parse_owner_ids("UA"), ["UA"])
        self.assertEqual(cfg.parse_owner_ids(None), [])

    def test_first_owner_is_primary_and_all_are_owners(self):
        c = cfg.Config("t", "C", "UA", co_owner_ids=["UB"])
        self.assertEqual(c.owner_ids, ["UA", "UB"])
        self.assertTrue(c.is_owner("UB"))
        self.assertFalse(c.is_owner("UC"))

    def test_any_owner_mention_passes_filter(self):
        m = msg("1.0", user="UB")
        self.assertTrue(chat.is_for_me(m, BOT, ["UA", "UB"], require_mention=True))
        self.assertFalse(chat.is_for_me(m, BOT, "UA", require_mention=True))


class BotListenTests(unittest.TestCase):
    BOT2 = "UOTHERBOT"

    def bot(self, ts, text=f"<@{BOT}> 안녕"):
        return {"ts": ts, "user": self.BOT2, "bot_id": "B2", "text": text}

    def test_unlisted_bot_is_ignored(self):
        self.assertFalse(chat.is_for_me(self.bot("1.0"), BOT, OWNER, require_mention=True))

    def test_listened_bot_passes(self):
        self.assertTrue(chat.is_for_me(self.bot("1.0"), BOT, OWNER, require_mention=True,
                                       listeners=[self.BOT2]))

    def test_my_own_messages_never_pass(self):
        mine = {"ts": "1.0", "user": BOT, "bot_id": "B1", "text": f"<@{BOT}>"}
        self.assertFalse(chat.is_for_me(mine, BOT, OWNER, require_mention=True, listeners=[BOT]))

    def test_bot_loop_is_cut_after_limit_and_human_resets(self):
        thread = [msg("1.0")] + [self.bot(f"{i}.0") for i in range(2, 2 + chat.BOT_TURN_LIMIT)]
        nxt = self.bot(f"{2 + chat.BOT_TURN_LIMIT}.0")
        self.assertFalse(chat.is_for_me(nxt, BOT, OWNER, require_mention=True,
                                        listeners=[self.BOT2], context=thread + [nxt]))
        human = msg(f"{2 + chat.BOT_TURN_LIMIT}.5")
        later = self.bot(f"{3 + chat.BOT_TURN_LIMIT}.0")
        self.assertTrue(chat.is_for_me(later, BOT, OWNER, require_mention=True,
                                       listeners=[self.BOT2], context=thread + [nxt, human, later]))


if __name__ == "__main__":
    unittest.main()
