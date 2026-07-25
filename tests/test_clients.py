"""API-client and parsing tests, run against a fake http layer.

These encode the Kindroid API's undocumented quirks (blank ai-response
bodies, missing sender_type, seconds-vs-milliseconds timestamps) so a
refactor can't silently lose the workarounds. No real network calls.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kinbridge as ab


def kin_cfg():
    return {"kindroid_api_key": "kn_test", "group_id": "grp_1"}


class FakeHttp:
    """Stands in for kinbridge.http_request; returns canned responses and
    records every call for assertions."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []  # (url, method, headers, body)

    def __call__(self, url, method="GET", headers=None, body=None, **kw):
        self.calls.append((url, method, headers, body))
        if not self.responses:
            raise AssertionError("unexpected extra http call: " + url)
        return self.responses.pop(0)


class KindroidGetTurn(unittest.TestCase):
    def test_plain_id(self):
        fake = FakeHttp(['"ai_abc123"'])
        with mock.patch.object(ab, "http_request", fake):
            self.assertEqual(ab.KindroidClient(kin_cfg()).get_turn(), "ai_abc123")

    def test_json_ai_id(self):
        fake = FakeHttp([json.dumps({"ai_id": "ai_json1"})])
        with mock.patch.object(ab, "http_request", fake):
            self.assertEqual(ab.KindroidClient(kin_cfg()).get_turn(), "ai_json1")

    def test_json_fallback_id_key(self):
        fake = FakeHttp([json.dumps({"id": "ai_json2"})])
        with mock.patch.object(ab, "http_request", fake):
            self.assertEqual(ab.KindroidClient(kin_cfg()).get_turn(), "ai_json2")

    def test_empty_means_user_seat(self):
        fake = FakeHttp([""])
        with mock.patch.object(ab, "http_request", fake):
            self.assertEqual(ab.KindroidClient(kin_cfg()).get_turn(), "")


class KindroidAiResponse(unittest.TestCase):
    def test_plain_text(self):
        fake = FakeHttp(['Hello from the tavern.'])
        with mock.patch.object(ab, "http_request", fake):
            self.assertEqual(ab.KindroidClient(kin_cfg()).ai_response("x"),
                             "Hello from the tavern.")

    def test_docs_lie_and_json_arrives(self):
        # Kindroid quirk: docs promise plain text, JSON sometimes shows up
        fake = FakeHttp([json.dumps({"message": "Surprise, I'm JSON."})])
        with mock.patch.object(ab, "http_request", fake):
            self.assertEqual(ab.KindroidClient(kin_cfg()).ai_response("x"),
                             "Surprise, I'm JSON.")

    def test_blank_body_returns_empty(self):
        # Kindroid quirk: the reply often lands in history a moment later,
        # so a blank body must come back as "" (the bridge then polls).
        fake = FakeHttp([""])
        with mock.patch.object(ab, "http_request", fake):
            self.assertEqual(ab.KindroidClient(kin_cfg()).ai_response("x"), "")


class KindroidTimestampProbe(unittest.TestCase):
    def _client_with_probe(self, probe_ts):
        fake = FakeHttp([
            json.dumps({"messages": [{"timestamp": probe_ts}]}),  # unit probe
            json.dumps({"messages": []}),                          # real fetch
        ])
        kin = ab.KindroidClient(kin_cfg())
        with mock.patch.object(ab, "http_request", fake):
            kin.recent_messages(minutes=10)
        return kin, fake

    def test_millisecond_accounts_detected(self):
        kin, fake = self._client_with_probe(1_750_000_000_000)
        self.assertEqual(kin._ts_unit, "ms")
        # the cursor sent to the API must also be in ms
        qs = fake.calls[-1][0].split("?", 1)[1]
        cursor = int(dict(p.split("=") for p in qs.split("&"))
                     ["start_after_timestamp"])
        self.assertGreater(cursor, 10 ** 12)

    def test_second_accounts_detected(self):
        kin, fake = self._client_with_probe(1_750_000_000)
        self.assertEqual(kin._ts_unit, "s")
        qs = fake.calls[-1][0].split("?", 1)[1]
        cursor = int(dict(p.split("=") for p in qs.split("&"))
                     ["start_after_timestamp"])
        self.assertLess(cursor, 10 ** 12)


class SenderTypeFallback(unittest.TestCase):
    # Kindroid quirk: real payloads sometimes omit sender_type entirely.
    def test_explicit_ai(self):
        self.assertTrue(ab.Bridge._is_ai_message({"sender_type": "ai"}))

    def test_missing_sender_type_falls_back_to_sender(self):
        self.assertTrue(ab.Bridge._is_ai_message({"sender": "ai_abc"}))

    def test_user_not_ai(self):
        self.assertFalse(ab.Bridge._is_ai_message({"sender": "user"}))
        self.assertFalse(ab.Bridge._is_ai_message({"sender": "You"}))

    def test_empty_not_ai(self):
        self.assertFalse(ab.Bridge._is_ai_message({}))


class SeatPrefixStripping(unittest.TestCase):
    def test_strips_bridge_prefix(self):
        self.assertEqual(ab.Bridge._strip_prefix("(Ani): hi there"), "hi there")

    def test_leaves_plain_text(self):
        self.assertEqual(ab.Bridge._strip_prefix("no prefix here"),
                         "no prefix here")

    def test_ignores_overlong_parens(self):
        text = "(" + "x" * 60 + "): not a prefix"
        self.assertEqual(ab.Bridge._strip_prefix(text), text)


class BidParsing(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(ab.Bridge._parse_bid("PASS"), (None, ""))

    def test_bid_with_reason(self):
        bid, reason = ab.Bridge._parse_bid("BID 7: I have a plot twist")
        self.assertEqual(bid, 7)
        self.assertEqual(reason, "I have a plot twist")

    def test_bid_clamped_to_ten(self):
        bid, _ = ab.Bridge._parse_bid("BID 99: overexcited")
        self.assertEqual(bid, 10)

    def test_garbage_is_pass(self):
        self.assertEqual(ab.Bridge._parse_bid(""), (None, ""))
        self.assertEqual(ab.Bridge._parse_bid("I dunno maybe"), (None, ""))

    def test_multiline_uses_first_line(self):
        bid, _ = ab.Bridge._parse_bid("BID 3: quick joke\nignore this")
        self.assertEqual(bid, 3)


class GeminiUrlSafety(unittest.TestCase):
    def _url_for_model(self, model):
        fake = FakeHttp([json.dumps({
            "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
            "usageMetadata": {}})])
        cli = ab.GeminiClient({"gemini_api_key": "AIzaTest",
                               "gemini_model": model})
        with mock.patch.object(ab, "http_request", fake):
            cli.chat("sys", "hello")
        return fake.calls[0][0]

    def test_normal_model_untouched(self):
        url = self._url_for_model("gemini-3.5-flash")
        self.assertIn("/models/gemini-3.5-flash:generateContent", url)

    def test_hostile_model_string_cannot_change_path(self):
        url = self._url_for_model("../evil?x=1")
        self.assertNotIn("../", url)
        self.assertNotIn("?x=1", url)
        self.assertIn("%2F", url)  # the slash was percent-encoded, not a path


class PublicNameDisambiguation(unittest.TestCase):
    def test_twin_gets_kin_suffix(self):
        with mock.patch.object(ab, "load_config",
                               return_value={"ani_name": "Ani"}):
            b = ab.Bridge()
            self.assertEqual(b._public_name("Ani"), "Ani (Kin)")
            self.assertEqual(b._public_name("ani"), "ani (Kin)")

    def test_other_names_untouched(self):
        with mock.patch.object(ab, "load_config",
                               return_value={"ani_name": "Ani"}):
            b = ab.Bridge()
            self.assertEqual(b._public_name("Rook"), "Rook")

    def test_blank_name_placeholder(self):
        with mock.patch.object(ab, "load_config",
                               return_value={"ani_name": "Ani"}):
            b = ab.Bridge()
            self.assertEqual(b._public_name(""), "Group member")


if __name__ == "__main__":
    unittest.main()
