import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from claude_slack_bridge import ratelimit, slack


class RateTests(unittest.TestCase):
    def test_retry_after_is_shared_between_calls(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            now = [100.0]
            sleeps = []
            def sleep(seconds):
                sleeps.append(seconds)
                now[0] += seconds
            response = unittest.mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({"ok": True}).encode()
            error = HTTPError("https://invalid.test", 429, "limited", {"Retry-After": "7"}, io.BytesIO())
            with patch.object(ratelimit, "RATE_DIR", Path(root)), \
                 patch.object(ratelimit.time, "time", side_effect=lambda: now[0]), \
                 patch.object(ratelimit.time, "sleep", side_effect=sleep), \
                 patch.object(slack.urllib.request, "urlopen", side_effect=[error, response, response]):
                slack.api("test-token", "conversations.history", {})
                slack.api("test-token", "auth.test", {})
            self.assertEqual(sleeps, [7.0])
            self.assertNotIn("test-token", ' '.join(p.name for p in Path(root).iterdir()))

    def test_replies_paginates_more_than_500(self):
        pages = [{"messages": [{"ts": str(i)} for i in range(500)],
                  "response_metadata": {"next_cursor": "next"}},
                 {"messages": [{"ts": "500"}], "response_metadata": {}}]
        with patch.object(slack, "api", side_effect=pages) as api:
            self.assertEqual(len(slack.conversations_replies("t", "C1", "1", limit=500)), 501)
        self.assertEqual(api.call_args.args[2]["cursor"], "next")
        self.assertTrue(api.call_args.kwargs["form"])

    def test_header_lookup_stays_single_page(self):
        with patch.object(slack, "api", return_value={"messages": [{"ts": "1"}],
                          "response_metadata": {"next_cursor": "unused"}}) as api:
            self.assertEqual(slack.conversations_replies("t", "C1", "1", limit=1), [{"ts": "1"}])
        api.assert_called_once()

    def test_discovery_reads_all_membership_pages(self):
        with patch.object(slack, "api", side_effect=[
            {"channels": [{"id": "C1", "name": "one"}], "response_metadata": {"next_cursor": "next"}},
            {"channels": [{"id": "C2", "name": "two"}]}]):
            self.assertEqual([c["id"] for c in slack.my_conversations("t")], ["C1", "C2"])


    def test_http_calls_overlap_without_locks(self):
        import threading
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            barrier = threading.Barrier(2)
            failures = []
            def http(*args):
                barrier.wait(timeout=3)
                return {"ok": True}
            def call():
                try:
                    slack.api("test-token", "test", {})
                except BaseException as exc:
                    failures.append(exc)
            with patch.object(ratelimit, "RATE_DIR", Path(root)), \
                 patch.object(ratelimit, "MIN_INTERVAL", 0.01), \
                 patch.object(slack, "_api_once", side_effect=http):
                workers = [threading.Thread(target=call) for _ in range(2)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=5)
                self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(failures, [])

    def test_reservations_are_start_intervals_not_completion_intervals(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            now = [100.0]
            starts = []
            def http(*args):
                starts.append(now[0])
                now[0] += 2
                return {}
            with patch.object(ratelimit, "RATE_DIR", Path(root)), \
                 patch.object(ratelimit.time, "time", side_effect=lambda: now[0]), \
                 patch.object(ratelimit.time, "sleep", side_effect=lambda n: now.__setitem__(0, now[0]+n)), \
                 patch.object(slack, "_api_once", side_effect=http):
                slack.api("t", "read", {})
                slack.api("t", "read", {})
            self.assertEqual(starts, [100, 102])

    def test_short_calls_reserve_spacing(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            now = [100.0]
            with patch.object(ratelimit, "RATE_DIR", Path(root)), \
                 patch.object(ratelimit.time, "time", side_effect=lambda: now[0]), \
                 patch.object(ratelimit.time, "sleep", side_effect=lambda n: now.__setitem__(0, now[0]+n)):
                with ratelimit.slot("t", "read"):
                    pass
                with ratelimit.slot("t", "read"):
                    self.assertAlmostEqual(now[0], 101.2)
                with ratelimit.slot("t", "write"):
                    self.assertAlmostEqual(now[0], 101.2)

    def test_retry_values_are_bounded(self):
        for value, expected in ((-1, 0), (0, 0), (999999, 300), ("inf", 0),
                                ("NaN", 0), ("broken", 30)):
            self.assertEqual(ratelimit.retry_delay(value), expected)

    def test_corrupt_deadlines_are_ignored(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            with patch.object(ratelimit, "RATE_DIR", Path(root)):
                with ratelimit.slot("t"):
                    pass
                path = next(Path(root).glob("*.lock"))
                for raw in ('{"next": Infinity, "cooldown": NaN}', 'nonsense',
                            '{"next": "bad"}', '999999999999999999999'):
                    path.write_text(raw)
                    with patch.object(ratelimit.time, "sleep", side_effect=AssertionError("Unexpected wait")):
                        with ratelimit.slot("t"):
                            pass

    def test_retry_updates_existing_reservations_before_http(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            now = [100.0]
            injected = [False]
            def sleep(seconds):
                if not injected[0]:
                    injected[0] = True
                    with ratelimit._state("t", "read") as state:
                        state["cooldown"] = 107
                        state["next"] = 107
                now[0] += seconds
            with patch.object(ratelimit, "RATE_DIR", Path(root)), \
                 patch.object(ratelimit.time, "time", side_effect=lambda: now[0]), \
                 patch.object(ratelimit.time, "sleep", side_effect=sleep):
                with ratelimit.slot("t", "read"):
                    pass
                with ratelimit.slot("t", "read"):
                    self.assertEqual(now[0], 107)

    def test_defer_clamps_actual_persisted_deadline(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            with patch.object(ratelimit, "RATE_DIR", Path(root)), \
                 patch.object(ratelimit.time, "time", return_value=100):
                with ratelimit.slot("t", "read") as defer:
                    defer(999999)
                with ratelimit._state("t", "read") as state:
                    self.assertEqual(state["cooldown"], 400)
                    self.assertEqual(state["next"], 400)

    def test_six_keepers_ignore_monitor_reservations_every_five_seconds(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            now = [100.0]
            calls = []
            def http(token, method, payload, form):
                calls.append((now[0], payload.get("ts")))
                return {"messages": [], "response_metadata": {"next_cursor": "more"}}
            with patch.object(ratelimit, "RATE_DIR", Path(root)), \
                 patch.object(ratelimit.time, "time", side_effect=lambda: now[0]), \
                 patch.object(ratelimit.time, "sleep", side_effect=AssertionError("Keeper must not reserve")), \
                 patch.object(slack, "_api_once", side_effect=http):
                for tick in (100, 105, 110):
                    now[0] = tick
                    with ratelimit._state("t", "conversations.replies") as state:
                        state["next"] = tick + 200  # --all의 밀린 예약
                    for i in range(6):
                        slack.conversations_replies("t", "C1", str(i), cooldown_only=True)
                self.assertEqual([time for time, _ in calls], [100]*6+[105]*6+[110]*6)

    def test_keeper_still_obeys_shared_cooldown(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            now = [100.0]
            with patch.object(ratelimit, "RATE_DIR", Path(root)), \
                 patch.object(ratelimit.time, "time", side_effect=lambda: now[0]), \
                 patch.object(ratelimit.time, "sleep", side_effect=lambda n: now.__setitem__(0, now[0]+n)):
                with ratelimit._state("t", "read") as state:
                    state.update(next=300, cooldown=107)
                with ratelimit.slot("t", "read", cooldown_only=True):
                    self.assertEqual(now[0], 107)

    def test_far_future_next_is_clamped_not_reset(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            with patch.object(ratelimit, "RATE_DIR", Path(root)), patch.object(ratelimit.time, "time", return_value=100):
                with ratelimit._state("t", "read") as state:
                    state["next"] = 1000000
                with ratelimit._state("t", "read") as state:
                    self.assertEqual(state["next"], 400)
