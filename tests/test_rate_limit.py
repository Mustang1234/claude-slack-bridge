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
            self.assertEqual(sleeps, [7.0, 1.0])
            self.assertNotIn("test-token", ' '.join(p.name for p in Path(root).iterdir()))

    def test_replies_paginates_more_than_500(self):
        pages = [{"messages": [{"ts": str(i)} for i in range(500)],
                  "response_metadata": {"next_cursor": "next"}},
                 {"messages": [{"ts": "500"}], "response_metadata": {}}]
        with patch.object(slack, "api", side_effect=pages) as api:
            self.assertEqual(len(slack.conversations_replies("t", "C1", "1", limit=500)), 501)
        self.assertEqual(api.call_args.args[2]["cursor"], "next")
        self.assertTrue(api.call_args.kwargs["form"])
