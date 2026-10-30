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


class SummaryRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        pulse.PENDING.clear()

    def test_failed_summary_is_sent_then_filled_in(self) -> None:
        brief = {"revenue": "$3.79B", "revenue_yoy": "+19%", "eps": "$4.20", "eps_basis": "비GAAP",
                 "eps_gaap": None, "guide_revenue": None, "guide_margin": None, "note": None,
                 "period_end": None, "fiscal_label": "FY2026 3Q"}
        with patch.object(pulse, "filing_meta", return_value={"items": "2.02,9.01", "date": "x"}), \
                patch.object(pulse, "press_release_text", return_value=("press release", "https://sec/x")), \
                patch.object(pulse, "summarize", side_effect=[None, brief]), \
                patch.object(pulse, "market_context", return_value={}), \
                patch.object(pulse, "send", return_value=77) as send, \
                patch.object(pulse, "edit", return_value=True) as edit:
            pulse.process_accession(1, "0001-26-1", "NXPI", "NXP", "2026-10-27T20:23:00Z", False)
            self.assertIn("요약 준비 중", send.call_args[0][0])
            state: dict = {"pending": list(pulse.PENDING)}
            pulse.PENDING.clear()
            # 재시도 간격 전에는 손대지 않는다
            self.assertEqual(pulse.retry_pending(state, False, now=state["pending"][0]["last"] + 10), 0)
            fixed = pulse.retry_pending(state, False, now=state["pending"][0]["last"] + pulse.RETRY_GAP_SECONDS)
        self.assertEqual(fixed, 1)
        self.assertEqual(state["pending"], [])
        mid, text = edit.call_args[0][:2]
        self.assertEqual(mid, 77)
        self.assertIn("$3.79B", text)
        self.assertNotIn("요약 준비 중", text)

    def test_gives_up_with_final_failure_text(self) -> None:
        state = {"pending": [{"kind": "8k", "message_id": 5, "cik": 1, "acc": "a", "ticker": "MRVL",
                              "name": "마벨", "when": "05:05 KST", "labels": "중요 계약 체결(1.01)",
                              "tries": pulse.RETRY_MAX_TRIES - 1, "since": 0.0, "last": 0.0}]}
        with patch.object(pulse, "filing_text", return_value=("doc", "https://sec/y")), \
                patch.object(pulse, "summarize_event", return_value=None), \
                patch.object(pulse, "edit", return_value=True) as edit:
            pulse.retry_pending(state, False, now=pulse.RETRY_GAP_SECONDS + 1.0)
        self.assertEqual(state["pending"], [])
        self.assertIn(pulse.SUMMARY_FAILED, edit.call_args[0][1])


class RoutingTests(unittest.TestCase):
    """실적 속보는 EARNINGS_CHAT_ID(반도체방), 비실적 8-K는 TELEGRAM_CHAT_ID(개인방)."""

    def test_earnings_goes_to_earnings_chat_and_8k_stays(self) -> None:
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "-100personal", "EARNINGS_CHAT_ID": "-100semi"}
        with patch.dict(pulse.os.environ, env, clear=False):
            self.assertEqual(pulse._bot("earnings")[1], "-100semi")
            self.assertEqual(pulse._bot("8k")[1], "-100personal")

    def test_without_earnings_chat_everything_uses_default(self) -> None:
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "-100personal", "EARNINGS_CHAT_ID": ""}
        with patch.dict(pulse.os.environ, env, clear=False):
            self.assertEqual(pulse._bot("earnings")[1], "-100personal")
            self.assertEqual(pulse._bot("8k")[1], "-100personal")

    def test_material_8k_is_sent_with_8k_kind(self) -> None:
        brief = {"headline": "대형 계약", "points": ["10억달러"], "material": True, "why": "x"}
        with patch.object(pulse, "filing_text", return_value=("doc", "https://sec/z")), \
                patch.object(pulse, "summarize_event", return_value=brief), \
                patch.object(pulse, "send", return_value=9) as send:
            pulse.process_material(1, "a", "MRVL", "마벨", "2026-10-27T20:23:00Z", "1.01,9.01", False)
        self.assertEqual(send.call_args.kwargs.get("kind"), "8k")

    def test_retry_edit_keeps_original_room(self) -> None:
        state = {"pending": [{"kind": "8k", "message_id": 5, "cik": 1, "acc": "a", "ticker": "MRVL",
                              "name": "마벨", "when": "05:05 KST", "labels": "중요 계약 체결(1.01)",
                              "tries": 0, "since": 0.0, "last": 0.0}]}
        brief = {"headline": "h", "points": [], "material": True, "why": "x"}
        with patch.object(pulse, "filing_text", return_value=("doc", "https://sec/y")), \
                patch.object(pulse, "summarize_event", return_value=brief), \
                patch.object(pulse, "edit", return_value=True) as edit:
            pulse.retry_pending(state, False, now=pulse.RETRY_GAP_SECONDS + 1.0)
        self.assertEqual(edit.call_args.kwargs.get("kind"), "8k")


class QuoteTests(unittest.TestCase):
    def test_quotes_rendered_max_two(self) -> None:
        brief = {"revenue": "$1B", "eps": "$1.00", "eps_basis": "GAAP", "period_end": None,
                 "quotes": ["수요가 강하다 — CEO", "마진 개선 — CFO", "세 번째"]}
        text = pulse.build_message("엔비디아", "NVDA", brief, None, "05:05 KST", None)
        self.assertIn("💬 <b>경영진</b>", text)
        self.assertIn("수요가 강하다", text)
        self.assertNotIn("세 번째", text)

    def test_no_quotes_section_when_empty(self) -> None:
        brief = {"revenue": "$1B", "eps": None, "period_end": None, "quotes": []}
        text = pulse.build_message("엔비디아", "NVDA", brief, None, "05:05 KST", None)
        self.assertNotIn("경영진", text)


if __name__ == "__main__":
    unittest.main()
