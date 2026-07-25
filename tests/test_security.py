"""Security-behavior tests: host allowlist, CSRF origin check, PIN lockout.

Run from the repo root:  python3 -m unittest discover tests
No network access and no real API keys are used anywhere in this suite.
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kinbridge as ab


class HostHeaderParsing(unittest.TestCase):
    def test_plain_host(self):
        self.assertEqual(ab._host_from_header("localhost"), "localhost")

    def test_host_with_port(self):
        self.assertEqual(ab._host_from_header("127.0.0.1:8770"), "127.0.0.1")

    def test_ipv6_with_port(self):
        self.assertEqual(ab._host_from_header("[::1]:8770"), "::1")

    def test_case_insensitive(self):
        self.assertEqual(ab._host_from_header("LocalHost:8770"), "localhost")

    def test_missing(self):
        self.assertEqual(ab._host_from_header(None), "")
        self.assertEqual(ab._host_from_header(""), "")


class AllowedHostsBuild(unittest.TestCase):
    def test_local_only_by_default(self):
        hosts = ab._build_allowed_hosts({"allow_lan": False, "extra_hosts": ""},
                                        ["192.168.1.5"])
        self.assertIn("127.0.0.1", hosts)
        self.assertIn("localhost", hosts)
        self.assertNotIn("192.168.1.5", hosts)

    def test_lan_adds_ips(self):
        hosts = ab._build_allowed_hosts({"allow_lan": True, "extra_hosts": ""},
                                        ["192.168.1.5"])
        self.assertIn("192.168.1.5", hosts)

    def test_extra_hosts_parsed(self):
        hosts = ab._build_allowed_hosts(
            {"allow_lan": False,
             "extra_hosts": "my-pc.tail1234.ts.net, 100.64.0.7 ,"},
            [])
        self.assertIn("my-pc.tail1234.ts.net", hosts)
        self.assertIn("100.64.0.7", hosts)


class FakeAccessHandler(SimpleNamespace):
    """Just enough of a request object to exercise Handler._check_access
    and Handler._valid_origin without a socket."""
    def _client_local(self):
        return ab.Handler._client_local(self)

    def _valid_origin(self):
        return ab.Handler._valid_origin(self)

    def _check_access(self):
        return ab.Handler._check_access(self)


def make_req(ip="10.0.0.9", command="POST", path="/api/state",
             headers=None):
    class H(dict):
        def get(self, k, default=None):
            return dict.get(self, k, default)
    return FakeAccessHandler(client_address=(ip, 12345), command=command,
                             path=path, headers=H(headers or {}))


class PinAndOriginChecks(unittest.TestCase):
    def setUp(self):
        # isolate config in a temp dir so tests never touch a real config.json
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cfg_path = ab.CONFIG_PATH
        ab.CONFIG_PATH = os.path.join(self._tmp.name, "config.json")
        ab.save_config({"access_pin": "123456"})
        with ab._pin_fail_lock:
            ab._pin_fails.clear()
        self._old_hosts = ab.ALLOWED_HOSTS
        ab.ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

    def tearDown(self):
        ab.CONFIG_PATH = self._old_cfg_path
        ab.ALLOWED_HOSTS = self._old_hosts
        with ab._pin_fail_lock:
            ab._pin_fails.clear()
        self._tmp.cleanup()

    # --- CSRF / Origin ---

    def test_local_post_with_evil_origin_blocked(self):
        req = make_req(ip="127.0.0.1",
                       headers={"Origin": "https://evil.example"})
        code, _, payload = req._check_access()
        self.assertEqual(code, 403)
        self.assertIn("cross-site", payload["error"])

    def test_local_post_with_good_origin_allowed(self):
        req = make_req(ip="127.0.0.1",
                       headers={"Origin": "http://127.0.0.1:8770"})
        self.assertIsNone(req._check_access())

    def test_local_post_with_no_origin_allowed(self):
        # curl / scripts send no Origin; locals are trusted without one
        req = make_req(ip="127.0.0.1")
        self.assertIsNone(req._check_access())

    def test_referer_checked_when_no_origin(self):
        req = make_req(ip="127.0.0.1",
                       headers={"Referer": "https://evil.example/attack.html"})
        code, _, _ = req._check_access()
        self.assertEqual(code, 403)

    def test_get_not_origin_checked(self):
        # GETs are safe (no state change) and images/prefetches send weird
        # origins; only POSTs get the origin gate.
        req = make_req(ip="127.0.0.1", command="GET",
                       headers={"Origin": "https://evil.example"})
        self.assertIsNone(req._check_access())

    # --- PIN auth for remote clients ---

    def test_remote_without_pin_401(self):
        code, _, _ = make_req()._check_access()
        self.assertEqual(code, 401)

    def test_missing_pin_does_not_count_toward_lockout(self):
        # the dashboard polls /api/state every 2s before the user has
        # entered the PIN; those 401s must not lock the phone out
        ip = "10.0.0.8"
        for _ in range(10):
            code, _, _ = make_req(ip=ip)._check_access()
            self.assertEqual(code, 401)
        req = make_req(ip=ip, headers={"X-Ani-Pin": "123456"})
        self.assertIsNone(req._check_access())

    def test_remote_with_correct_pin_ok(self):
        req = make_req(headers={"X-Ani-Pin": "123456"})
        self.assertIsNone(req._check_access())

    def test_remote_with_pin_in_query_ok(self):
        req = make_req(path="/api/export?pin=123456")
        self.assertIsNone(req._check_access())

    def test_no_pin_configured_means_no_remote_access(self):
        ab.save_config({"access_pin": ""})
        code, _, _ = make_req(headers={"X-Ani-Pin": ""})._check_access()
        self.assertEqual(code, 401)

    # --- brute-force lockout ---

    def test_lockout_after_failures(self):
        ip = "10.0.0.9"
        bad = make_req(ip=ip, headers={"X-Ani-Pin": "000000"})
        code, _, _ = bad._check_access()
        self.assertEqual(code, 401)
        # second try inside the 2s backoff window is throttled
        code, hdrs, payload = make_req(ip=ip,
                                       headers={"X-Ani-Pin": "123456"})._check_access()
        self.assertEqual(code, 429)
        self.assertIn("Retry-After", hdrs)

    def test_backoff_grows_and_caps(self):
        ip = "10.0.0.10"
        for _ in range(20):
            ab._pin_record_failure(ip)
        wait = ab._pin_seconds_locked(ip)
        self.assertLessEqual(wait, ab.PIN_LOCKOUT_CAP_SECONDS)
        self.assertGreater(wait, ab.PIN_LOCKOUT_CAP_SECONDS - 5)

    def test_success_clears_lockout(self):
        ip = "10.0.0.11"
        ab._pin_record_failure(ip)
        ab._pin_clear_failures(ip)
        self.assertEqual(ab._pin_seconds_locked(ip), 0)

    def test_lockout_is_per_ip(self):
        ab._pin_record_failure("10.0.0.12")
        self.assertEqual(ab._pin_seconds_locked("10.0.0.13"), 0)


class LiveServer(unittest.TestCase):
    """End-to-end over a real socket: host allowlist + CSRF on the wire."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old_cfg_path = ab.CONFIG_PATH
        ab.CONFIG_PATH = os.path.join(cls._tmp.name, "config.json")
        cls._old_hosts = ab.ALLOWED_HOSTS
        ab.ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ab.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        ab.CONFIG_PATH = cls._old_cfg_path
        ab.ALLOWED_HOSTS = cls._old_hosts
        cls._tmp.cleanup()

    def _request(self, method="GET", path="/api/state", headers=None,
                 body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            hdrs = dict(headers or {})
            payload = json.dumps(body).encode() if body is not None else None
            if payload is not None:
                hdrs.setdefault("Content-Type", "application/json")
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_normal_state_request_ok(self):
        status, data = self._request()
        self.assertEqual(status, 200)
        self.assertIn(b"running", data)

    def test_dashboard_page_served(self):
        status, data = self._request(path="/")
        self.assertEqual(status, 200)
        self.assertIn(b"Kinbridge", data)

    def test_rebinding_host_rejected(self):
        status, data = self._request(headers={"Host": "attacker.example.com"})
        self.assertEqual(status, 400)
        self.assertIn(b"invalid host", data)

    def test_rebinding_host_rejected_even_for_page(self):
        status, _ = self._request(path="/",
                                  headers={"Host": "attacker.example.com"})
        self.assertEqual(status, 400)

    def test_cross_site_post_rejected(self):
        status, data = self._request(
            method="POST", path="/api/stop",
            headers={"Origin": "https://evil.example"}, body={})
        self.assertEqual(status, 403)
        self.assertIn(b"cross-site", data)

    def test_same_origin_post_ok(self):
        status, _ = self._request(
            method="POST", path="/api/stop",
            headers={"Origin": "http://127.0.0.1:%d" % self.port}, body={})
        self.assertEqual(status, 200)

    def test_unknown_api_path_404(self):
        status, _ = self._request(path="/api/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
