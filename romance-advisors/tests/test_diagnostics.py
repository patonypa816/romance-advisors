import unittest
from unittest.mock import patch

from romance.diagnostics import PerformanceMetrics


class TimingTests(unittest.TestCase):
    def test_durations_ignore_wall_clock_changes_and_reject_bodies_and_urls(self):
        metrics = PerformanceMetrics(limit=2)
        with patch("romance.diagnostics.time.monotonic", return_value=10), \
                patch("romance.diagnostics.time.time", return_value=1):
            metrics.record("sender_wait_end", started=7, job_id="r:reaction", round_id="r",
                           url="https://private.invalid", text="private content", code="https://private.invalid")
        event = metrics.snapshot()[0]
        self.assertEqual(event["elapsed_seconds"], 3)
        self.assertEqual(event["time"], 1)
        self.assertEqual(event["job_id"], "r:reaction")
        self.assertNotIn("url", event)
        self.assertNotIn("text", event)
        self.assertNotIn("code", event)
        metrics.record("one")
        metrics.record("two")
        snapshot = metrics.snapshot()
        self.assertEqual([row["seq"] for row in snapshot], [2, 3])
        snapshot[0]["stage"] = "mutated"
        self.assertEqual(metrics.snapshot()[0]["stage"], "one")
