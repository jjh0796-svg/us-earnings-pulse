import unittest
from unittest.mock import patch

import us_earnings_pulse as pulse


class SeenStateTests(unittest.TestCase):
    def test_new_low_cik_accession_survives_state_limit(self) -> None:
        old = [f"0001000000-26-{number:06d}" for number in range(pulse.MAX_SEEN)]
        amd = "0000002488-26-000182"
        state = {"seen": old}
        entry = {"acc": amd, "cik": 2488, "updated": "2026-09-29T00:00:00Z"}

        with patch.object(pulse, "fetch_feed_entries", return_value=[entry]), patch.object(
            pulse, "process_accession", return_value="sent"
        ) as process:
            sent = pulse.poll_once(state, {"AMD": "Advanced Micro Devices"}, {2488: "AMD"}, False)

        self.assertEqual(sent, 1)
        self.assertEqual(state["seen"][-1], amd)
        self.assertIn(amd, state["seen"])
        self.assertEqual(len(state["seen"]), pulse.MAX_SEEN)
        process.assert_called_once()

    def test_seen_state_deduplicates_without_sorting(self) -> None:
        normalized = pulse.normalize_seen(["later", "early", "later", "newest"])
        self.assertEqual(normalized, ["later", "early", "newest"])


if __name__ == "__main__":
    unittest.main()
