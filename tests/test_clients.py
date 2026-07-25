"""API-client and parsing tests, run against a fake http layer.

These encode the Kindroid API's undocumented quirks (blank ai-response
bodies, missing sender_type, seconds-vs-milliseconds timestamps) so a
refactor can't silently lose the workarounds. No real network calls.
"""
import email.message
import io
import json
import os
import sys
import unittest
import urllib.error
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


class FakeResponse:
    """Minimal stand-in for the object urlopen() yields."""

    def __init__(self, payload=b""):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class HttpRequestBase(unittest.TestCase):
    """http_request is the single chokepoint every provider call goes
    through, so its retry and error mapping decide whether a provider
    hiccup surfaces as a clean status message or an unhandled traceback.

    _debug is stubbed throughout: unstubbed it appends to api_debug.log
    next to the app, and a test suite must not write there.
    """

    def setUp(self):
        self.sleeps = []
        self.notices = []
        patches = [
            mock.patch.object(ab, "_debug", lambda *a, **k: None),
            mock.patch.object(ab, "_notify", self.notices.append),
            mock.patch.object(ab.time, "sleep", self.sleeps.append),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def http_error(self, code, detail=b"", retry_after=None, reason="Boom",
                   url="https://api.x.ai/v1/chat/completions"):
        """HTTPError is file-like; unclosed instances emit ResourceWarning
        and would clutter every CI log, so close them on teardown."""
        hdrs = email.message.Message()
        if retry_after is not None:
            hdrs["Retry-After"] = str(retry_after)
        err = urllib.error.HTTPError(url, code, reason, hdrs, io.BytesIO(detail))
        self.addCleanup(err.close)
        return err

    def urlopen_returning(self, *results):
        """Each result is bytes (success) or an exception instance."""
        self.opened = []

        def fake(req, timeout=None):
            self.opened.append((req, timeout))
            r = results[len(self.opened) - 1]
            if isinstance(r, Exception):
                raise r
            return FakeResponse(r)

        return mock.patch.object(ab.urllib.request, "urlopen", fake)


class HttpRequestSuccess(HttpRequestBase):
    def test_returns_decoded_body(self):
        with self.urlopen_returning(b"hello there"):
            self.assertEqual(ab.http_request("https://api.x.ai/v1/x"),
                             "hello there")

    def test_undecodable_bytes_do_not_crash(self):
        with self.urlopen_returning(b"caf\xff\xfe"):
            out = ab.http_request("https://api.x.ai/v1/x")
        self.assertTrue(out.startswith("caf"))

    def test_body_is_json_encoded_with_content_type(self):
        with self.urlopen_returning(b"ok"):
            ab.http_request("https://api.x.ai/v1/x", method="POST",
                            body={"model": "grok-4"})
        req = self.opened[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data.decode()), {"model": "grok-4"})
        self.assertEqual(req.get_header("Content-type"), "application/json")

    def test_no_body_means_no_payload(self):
        with self.urlopen_returning(b"ok"):
            ab.http_request("https://api.x.ai/v1/x")
        self.assertIsNone(self.opened[0][0].data)

    def test_headers_and_timeout_passed_through(self):
        with self.urlopen_returning(b"ok"):
            ab.http_request("https://api.x.ai/v1/x",
                            headers={"Authorization": "Bearer k"}, timeout=7)
        req, timeout = self.opened[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer k")
        self.assertEqual(timeout, 7)


class HttpRequestErrors(HttpRequestBase):
    def test_client_error_raises_apierror_with_status(self):
        with self.urlopen_returning(self.http_error(401, b"bad key")):
            with self.assertRaises(ab.ApiError) as cm:
                ab.http_request("https://api.x.ai/v1/x")
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("401", str(cm.exception))

    def test_error_detail_is_surfaced(self):
        with self.urlopen_returning(self.http_error(400, b"model not found")):
            with self.assertRaises(ab.ApiError) as cm:
                ab.http_request("https://api.x.ai/v1/x")
        self.assertIn("model not found", str(cm.exception))

    def test_client_error_is_not_retried(self):
        with self.urlopen_returning(self.http_error(401, b"nope")):
            with self.assertRaises(ab.ApiError):
                ab.http_request("https://api.x.ai/v1/x")
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(self.sleeps, [])

    def test_unreachable_host_is_a_friendly_error(self):
        with self.urlopen_returning(urllib.error.URLError("no route to host")):
            with self.assertRaises(ab.ApiError) as cm:
                ab.http_request("https://api.x.ai/v1/x")
        self.assertIn("Could not reach", str(cm.exception))
        self.assertIn("internet connection", str(cm.exception))


class HttpRequestRetries(HttpRequestBase):
    def test_429_retries_then_succeeds(self):
        with self.urlopen_returning(self.http_error(429), b"finally"):
            self.assertEqual(ab.http_request("https://api.x.ai/v1/x"), "finally")
        self.assertEqual(len(self.opened), 2)
        self.assertEqual(len(self.sleeps), 1)

    def test_503_retries_then_succeeds(self):
        with self.urlopen_returning(self.http_error(503), b"back up"):
            self.assertEqual(ab.http_request("https://api.x.ai/v1/x"), "back up")
        self.assertEqual(len(self.opened), 2)

    def test_retry_after_header_is_honoured(self):
        with self.urlopen_returning(self.http_error(429, retry_after=30), b"ok"):
            ab.http_request("https://api.x.ai/v1/x")
        self.assertEqual(self.sleeps, [30])

    def test_retry_after_is_clamped_to_90s(self):
        with self.urlopen_returning(self.http_error(429, retry_after=99999), b"ok"):
            ab.http_request("https://api.x.ai/v1/x")
        self.assertEqual(self.sleeps, [90])

    def test_retry_after_has_a_5s_floor(self):
        with self.urlopen_returning(self.http_error(429, retry_after=1), b"ok"):
            ab.http_request("https://api.x.ai/v1/x")
        self.assertEqual(self.sleeps, [5])

    def test_garbage_retry_after_falls_back_to_backoff(self):
        with self.urlopen_returning(self.http_error(429, retry_after="soon"),
                                    self.http_error(429, retry_after="soon"),
                                    b"ok"):
            ab.http_request("https://api.x.ai/v1/x")
        self.assertEqual(self.sleeps, [10, 20])   # 10 * 2**attempt

    def test_retries_are_exhausted_then_it_raises(self):
        errs = [self.http_error(429) for _ in range(4)]
        with self.urlopen_returning(*errs):
            with self.assertRaises(ab.ApiError) as cm:
                ab.http_request("https://api.x.ai/v1/x")
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(len(self.opened), 4)      # 1 + 3 retries
        self.assertEqual(self.sleeps, [10, 20, 40])

    def test_retries_can_be_disabled(self):
        with self.urlopen_returning(self.http_error(429)):
            with self.assertRaises(ab.ApiError):
                ab.http_request("https://api.x.ai/v1/x", retries=0)
        self.assertEqual(len(self.opened), 1)

    def test_busy_notice_names_the_right_provider(self):
        for url, provider in [("https://api.x.ai/v1/x", "xAI"),
                              ("https://api.anthropic.com/v1/messages", "Anthropic"),
                              ("https://api.kindroid.ai/v1/send-message", "Kindroid")]:
            self.notices.clear()
            with self.urlopen_returning(self.http_error(429, url=url), b"ok"):
                ab.http_request(url)
            self.assertTrue(self.notices, "no status update for " + provider)
            self.assertIn(provider, self.notices[0])


class GrokChatParsing(unittest.TestCase):
    CFG = {"xai_api_key": "xai-test", "xai_model": "grok-4"}

    def _chat(self, payload, cfg=None):
        fake = FakeHttp([payload])
        cli = ab.GrokClient(dict(cfg or self.CFG))
        with mock.patch.object(ab, "http_request", fake):
            return cli, fake, cli.chat([{"role": "user", "content": "hi"}])

    def test_returns_stripped_content(self):
        _, _, out = self._chat(json.dumps(
            {"choices": [{"message": {"content": "  spoken line  "}}]}))
        self.assertEqual(out, "spoken line")

    def test_records_token_usage(self):
        cli, _, _ = self._chat(json.dumps({
            "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 34}}))
        self.assertEqual(cli.last_usage, (120, 34))

    def test_missing_usage_defaults_to_zero(self):
        cli, _, _ = self._chat(json.dumps(
            {"choices": [{"message": {"content": "x"}}]}))
        self.assertEqual(cli.last_usage, (0, 0))

    def test_service_tier_recorded(self):
        cli, _, _ = self._chat(json.dumps({
            "choices": [{"message": {"content": "x"}}],
            "service_tier": "priority"}))
        self.assertEqual(cli.last_tier, "priority")

    def test_priority_tier_is_sent(self):
        cfg = dict(self.CFG, xai_service_tier="priority")
        _, fake, _ = self._chat(json.dumps(
            {"choices": [{"message": {"content": "x"}}]}), cfg)
        self.assertEqual(fake.calls[0][3]["service_tier"], "priority")

    def test_default_tier_is_not_sent(self):
        _, fake, _ = self._chat(json.dumps(
            {"choices": [{"message": {"content": "x"}}]}))
        self.assertNotIn("service_tier", fake.calls[0][3])

    def test_missing_key_fails_before_any_request(self):
        fake = FakeHttp([])          # any call would raise AssertionError
        cli = ab.GrokClient({"xai_api_key": "  ", "xai_model": "grok-4"})
        with mock.patch.object(ab, "http_request", fake):
            with self.assertRaises(ab.ApiError) as cm:
                cli.chat([])
        self.assertEqual(fake.calls, [])
        self.assertIn("Settings", str(cm.exception))

    def test_malformed_json_raises_apierror(self):
        with self.assertRaises(ab.ApiError) as cm:
            self._chat("<html>502 Bad Gateway</html>")
        self.assertIn("Unexpected reply", str(cm.exception))

    def test_missing_choices_raises_apierror(self):
        with self.assertRaises(ab.ApiError):
            self._chat(json.dumps({"choices": []}))


class ClaudeChatParsing(unittest.TestCase):
    CFG = {"anthropic_api_key": "sk-ant-test", "claude_model": "claude-sonnet-4-6"}

    def _chat(self, payload):
        fake = FakeHttp([payload])
        cli = ab.ClaudeClient(dict(self.CFG))
        with mock.patch.object(ab, "http_request", fake):
            return cli, fake, cli.chat("system", "user text")

    def test_returns_text_block(self):
        _, _, out = self._chat(json.dumps(
            {"content": [{"type": "text", "text": " hello "}]}))
        self.assertEqual(out, "hello")

    def test_joins_multiple_text_blocks(self):
        _, _, out = self._chat(json.dumps({"content": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"}]}))
        self.assertEqual(out, "first\nsecond")

    def test_ignores_non_text_blocks(self):
        _, _, out = self._chat(json.dumps({"content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "said aloud"}]}))
        self.assertEqual(out, "said aloud")

    def test_records_input_output_tokens(self):
        cli, _, _ = self._chat(json.dumps({
            "content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": 11, "output_tokens": 22}}))
        self.assertEqual(cli.last_usage, (11, 22))

    def test_text_only_thinking_reply_is_an_error(self):
        """No usable text must not surface as an empty guest message."""
        with self.assertRaises(ab.ApiError):
            self._chat(json.dumps(
                {"content": [{"type": "thinking", "thinking": "hmm"}]}))

    def test_missing_key_fails_before_any_request(self):
        fake = FakeHttp([])
        cli = ab.ClaudeClient({"anthropic_api_key": ""})
        with mock.patch.object(ab, "http_request", fake):
            with self.assertRaises(ab.ApiError):
                cli.chat("s", "u")
        self.assertEqual(fake.calls, [])

    def test_malformed_json_raises_apierror(self):
        with self.assertRaises(ab.ApiError) as cm:
            self._chat("not json at all")
        self.assertIn("Anthropic", str(cm.exception))


class OpenAIChatParsing(unittest.TestCase):
    CFG = {"openai_api_key": "sk-test", "chatgpt_model": "gpt-5.4"}

    def _chat(self, payload):
        fake = FakeHttp([payload])
        cli = ab.OpenAIClient(dict(self.CFG))
        with mock.patch.object(ab, "http_request", fake):
            return cli, fake, cli.chat("system", "user text")

    def test_returns_stripped_content(self):
        _, _, out = self._chat(json.dumps(
            {"choices": [{"message": {"content": " a line "}}]}))
        self.assertEqual(out, "a line")

    def test_records_token_usage(self):
        cli, _, _ = self._chat(json.dumps({
            "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 6}}))
        self.assertEqual(cli.last_usage, (5, 6))

    def test_sends_system_and_user_messages(self):
        _, fake, _ = self._chat(json.dumps(
            {"choices": [{"message": {"content": "x"}}]}))
        msgs = fake.calls[0][3]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])

    def test_missing_key_fails_before_any_request(self):
        fake = FakeHttp([])
        cli = ab.OpenAIClient({"openai_api_key": ""})
        with mock.patch.object(ab, "http_request", fake):
            with self.assertRaises(ab.ApiError):
                cli.chat("s", "u")
        self.assertEqual(fake.calls, [])

    def test_malformed_json_raises_apierror(self):
        with self.assertRaises(ab.ApiError) as cm:
            self._chat("502 Bad Gateway")
        self.assertIn("OpenAI", str(cm.exception))


class FetchModels(unittest.TestCase):
    """The Settings "Scan" button. Each provider returns a different
    shape and needs different filtering, and the whole point is to keep
    non-chat models out of the dropdowns."""

    def _fetch(self, provider, cfg, payload):
        fake = FakeHttp([payload])
        with mock.patch.object(ab, "http_request", fake):
            models = ab.fetch_models(provider, cfg)
        return fake, models

    @staticmethod
    def _data(*ids):
        return json.dumps({"data": [{"id": i} for i in ids]})

    # ------------------------------------------------------------- xai

    def test_xai_keeps_only_grok_models(self):
        _, models = self._fetch("xai", {"xai_api_key": "k"},
                                self._data("grok-4", "grok-3-mini",
                                           "some-other-model"))
        self.assertEqual(models, ["grok-4", "grok-3-mini"])

    def test_xai_grok_match_is_case_insensitive(self):
        _, models = self._fetch("xai", {"xai_api_key": "k"},
                                self._data("GROK-Beta"))
        self.assertEqual(models, ["GROK-Beta"])

    def test_xai_request_shape(self):
        fake, _ = self._fetch("xai", {"xai_api_key": "k"}, self._data("grok-4"))
        url, method, headers, _ = fake.calls[0]
        self.assertEqual(url, "https://api.x.ai/v1/models")
        self.assertEqual(method, "GET")
        self.assertEqual(headers["Authorization"], "Bearer k")

    # ------------------------------------------------------- anthropic

    def test_anthropic_returns_every_id(self):
        _, models = self._fetch("anthropic", {"anthropic_api_key": "k"},
                                self._data("claude-sonnet-4-6", "claude-opus-4-1"))
        self.assertEqual(models, ["claude-sonnet-4-6", "claude-opus-4-1"])

    def test_anthropic_sends_version_header(self):
        fake, _ = self._fetch("anthropic", {"anthropic_api_key": "k"},
                              self._data("claude-sonnet-4-6"))
        url, _, headers, _ = fake.calls[0]
        self.assertIn("limit=100", url)
        self.assertEqual(headers["x-api-key"], "k")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")

    # ---------------------------------------------------------- openai

    def test_openai_keeps_chat_models(self):
        _, models = self._fetch("openai", {"openai_api_key": "k"},
                                self._data("gpt-5.4", "chatgpt-4o-latest"))
        self.assertEqual(models, ["gpt-5.4", "chatgpt-4o-latest"])

    def test_openai_keeps_o_series(self):
        _, models = self._fetch("openai", {"openai_api_key": "k"},
                                self._data("o3", "o4-mini"))
        self.assertEqual(models, ["o4-mini", "o3"])

    def test_openai_drops_non_chat_modalities(self):
        _, models = self._fetch("openai", {"openai_api_key": "k"}, self._data(
            "gpt-5.4",
            "gpt-4o-audio-preview", "gpt-4o-realtime-preview",
            "gpt-image-1", "gpt-4o-mini-tts", "gpt-4o-transcribe",
            "text-embedding-3-large", "omni-moderation-latest",
            "gpt-4o-search-preview", "dall-e-3", "whisper-1"))
        self.assertEqual(models, ["gpt-5.4"])

    def test_openai_drops_unrelated_prefixes(self):
        _, models = self._fetch("openai", {"openai_api_key": "k"},
                                self._data("gpt-5.4", "babbage-002"))
        self.assertEqual(models, ["gpt-5.4"])

    # ---------------------------------------------------------- gemini

    def _gemini(self, *models):
        return json.dumps({"models": list(models)})

    def test_gemini_requires_generate_content_support(self):
        _, models = self._fetch("gemini", {"gemini_api_key": "k"}, self._gemini(
            {"name": "models/gemini-3.5-flash",
             "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-embedding-001",
             "supportedGenerationMethods": ["embedContent"]}))
        self.assertEqual(models, ["gemini-3.5-flash"])

    def test_gemini_strips_the_models_prefix(self):
        _, models = self._fetch("gemini", {"gemini_api_key": "k"}, self._gemini(
            {"name": "models/gemini-3-pro",
             "supportedGenerationMethods": ["generateContent"]}))
        self.assertEqual(models, ["gemini-3-pro"])

    def test_gemini_tolerates_missing_methods_field(self):
        with self.assertRaises(ab.ApiError):   # nothing usable -> error, not crash
            self._fetch("gemini", {"gemini_api_key": "k"},
                        self._gemini({"name": "models/gemini-x"}))

    def test_gemini_request_shape(self):
        fake, _ = self._fetch("gemini", {"gemini_api_key": "k"}, self._gemini(
            {"name": "models/gemini-3.5-flash",
             "supportedGenerationMethods": ["generateContent"]}))
        url, _, headers, _ = fake.calls[0]
        self.assertIn("generativelanguage.googleapis.com", url)
        self.assertEqual(headers["x-goog-api-key"], "k")

    # --------------------------------------------------------- general

    def test_results_are_deduped_and_sorted_descending(self):
        _, models = self._fetch("xai", {"xai_api_key": "k"},
                                self._data("grok-3", "grok-4", "grok-3"))
        self.assertEqual(models, ["grok-4", "grok-3"])

    def test_blank_ids_are_dropped(self):
        _, models = self._fetch("anthropic", {"anthropic_api_key": "k"},
                                self._data("claude-sonnet-4-6", ""))
        self.assertEqual(models, ["claude-sonnet-4-6"])

    def test_no_usable_models_is_an_error(self):
        with self.assertRaises(ab.ApiError) as cm:
            self._fetch("xai", {"xai_api_key": "k"}, self._data("some-other-model"))
        self.assertIn("no usable models", str(cm.exception))

    def test_unknown_provider_rejected(self):
        fake = FakeHttp([])
        with mock.patch.object(ab, "http_request", fake):
            with self.assertRaises(ab.ApiError) as cm:
                ab.fetch_models("mistral", {})
        self.assertIn("Unknown provider", str(cm.exception))
        self.assertEqual(fake.calls, [])

    def test_every_provider_demands_its_key_before_scanning(self):
        for provider, field in [("xai", "xai_api_key"),
                                ("anthropic", "anthropic_api_key"),
                                ("openai", "openai_api_key"),
                                ("gemini", "gemini_api_key")]:
            for cfg in ({}, {field: "   "}):
                fake = FakeHttp([])   # a request would raise AssertionError
                with mock.patch.object(ab, "http_request", fake):
                    with self.assertRaises(ab.ApiError) as cm:
                        ab.fetch_models(provider, cfg)
                self.assertIn("then scan", str(cm.exception))
                self.assertEqual(fake.calls, [], provider + " called the API")


if __name__ == "__main__":
    unittest.main()
