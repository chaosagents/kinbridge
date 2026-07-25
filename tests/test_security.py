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


class CapturingHandler(SimpleNamespace):
    """Drives Handler.do_GET / do_POST without a socket, so the remote
    (non-loopback) branches can be exercised directly — a real socket
    test always connects from 127.0.0.1 and would only ever see the
    local path."""
    def _client_local(self):
        return ab.Handler._client_local(self)

    def _valid_host(self):
        return ab.Handler._valid_host(self)

    def _valid_origin(self):
        return ab.Handler._valid_origin(self)

    def _check_access(self):
        return ab.Handler._check_access(self)

    def _json_body(self):
        return self.body

    def _send(self, code, body, ctype="application/json", extra_headers=None):
        self.sent = (code, body)

    def get(self, path):
        self.command, self.path = "GET", path
        ab.Handler.do_GET(self)
        code, body = self.sent
        return code, json.loads(body)

    def post(self, path, body):
        self.command, self.path, self.body = "POST", path, body
        ab.Handler.do_POST(self)
        return self.sent[0]


def make_client(ip, pin="123456"):
    class H(dict):
        def get(self, k, default=None):
            return dict.get(self, k, default)
    return CapturingHandler(
        client_address=(ip, 12345),
        headers=H({"Host": "127.0.0.1", "X-Ani-Pin": pin}),
        body={}, sent=None)


REAL_KEYS = {
    "xai_api_key": "xai-notarealkey0001",
    "kindroid_api_key": "kn_notarealkey0002",
    "anthropic_api_key": "sk-ant-notarealkey0003",
    "gemini_api_key": "AIzaNotARealKey0004",
    "openai_api_key": "sk-notarealkey0005",
}


class SecretMasking(unittest.TestCase):
    """A remote client must never receive a real credential, and the mask
    it gets instead must never be able to overwrite one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cfg_path = ab.CONFIG_PATH
        ab.CONFIG_PATH = os.path.join(self._tmp.name, "config.json")
        self._old_hosts = ab.ALLOWED_HOSTS
        ab.ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
        with ab._pin_fail_lock:
            ab._pin_fails.clear()
        cfg = dict(REAL_KEYS)
        cfg["access_pin"] = "123456"
        cfg["daily_budget"] = 40
        ab.save_config(cfg)

    def tearDown(self):
        ab.CONFIG_PATH = self._old_cfg_path
        ab.ALLOWED_HOSTS = self._old_hosts
        with ab._pin_fail_lock:
            ab._pin_fails.clear()
        self._tmp.cleanup()

    # ---------------------------------------------------------- reading

    def test_local_client_still_gets_real_keys(self):
        code, cfg = make_client("127.0.0.1").get("/api/config")
        self.assertEqual(code, 200)
        for k, v in REAL_KEYS.items():
            self.assertEqual(cfg[k], v)

    def test_remote_client_gets_mask_not_keys(self):
        code, cfg = make_client("10.0.0.9").get("/api/config")
        self.assertEqual(code, 200)
        for k in REAL_KEYS:
            self.assertEqual(cfg[k], ab.SECRET_MASK)

    def test_remote_client_never_sees_a_real_key_anywhere(self):
        _, cfg = make_client("10.0.0.9").get("/api/config")
        blob = json.dumps(cfg)
        for v in REAL_KEYS.values():
            self.assertNotIn(v, blob)
        self.assertNotIn("123456", blob)

    def test_access_pin_is_masked_remotely(self):
        _, cfg = make_client("10.0.0.9").get("/api/config")
        self.assertEqual(cfg["access_pin"], ab.SECRET_MASK)

    def test_unset_secret_stays_empty_not_masked(self):
        ab.save_config({"openai_api_key": ""})
        _, cfg = make_client("10.0.0.9").get("/api/config")
        self.assertEqual(cfg["openai_api_key"], "")
        self.assertEqual(cfg["xai_api_key"], ab.SECRET_MASK)

    def test_non_secret_settings_visible_remotely(self):
        _, cfg = make_client("10.0.0.9").get("/api/config")
        self.assertEqual(cfg["daily_budget"], 40)

    # ---------------------------------------------------------- writing

    def test_masked_value_posted_back_does_not_overwrite(self):
        """The whole point of the sentinel."""
        code = make_client("10.0.0.9").post(
            "/api/config", {"xai_api_key": ab.SECRET_MASK})
        self.assertEqual(code, 200)
        self.assertEqual(ab.load_config()["xai_api_key"],
                         REAL_KEYS["xai_api_key"])

    def test_settings_round_trip_from_remote_preserves_every_key(self):
        """Regression guard for the realistic failure: the Settings
        dialog GETs the config, the user changes one unrelated field, and
        the form POSTs every field back — including the masked ones."""
        client = make_client("10.0.0.9")
        _, cfg = client.get("/api/config")
        cfg["daily_budget"] = 99                  # the user's actual edit
        self.assertEqual(client.post("/api/config", cfg), 200)

        saved = ab.load_config()
        self.assertEqual(saved["daily_budget"], 99)
        for k, v in REAL_KEYS.items():
            self.assertEqual(saved[k], v, "%s was overwritten by the mask" % k)
        self.assertEqual(saved["access_pin"], "123456")

    def test_remote_can_still_rotate_a_key(self):
        make_client("10.0.0.9").post(
            "/api/config", {"xai_api_key": "xai-rotated0006"})
        self.assertEqual(ab.load_config()["xai_api_key"], "xai-rotated0006")

    def test_remote_can_still_clear_a_key(self):
        make_client("10.0.0.9").post("/api/config", {"xai_api_key": ""})
        self.assertEqual(ab.load_config()["xai_api_key"], "")

    def test_mask_is_only_special_for_secret_fields(self):
        make_client("10.0.0.9").post(
            "/api/config", {"ani_persona": ab.SECRET_MASK})
        self.assertEqual(ab.load_config()["ani_persona"], ab.SECRET_MASK)

    def test_strip_masked_leaves_everything_else_alone(self):
        out = ab._strip_masked({"xai_api_key": ab.SECRET_MASK,
                                "gemini_api_key": "AIzaNew0007",
                                "daily_budget": 12})
        self.assertNotIn("xai_api_key", out)
        self.assertEqual(out["gemini_api_key"], "AIzaNew0007")
        self.assertEqual(out["daily_budget"], 12)

    # ------------------------------------------------------------ authz

    def test_remote_without_pin_gets_nothing(self):
        code, body = make_client("10.0.0.9", pin="").get("/api/config")
        self.assertEqual(code, 401)
        self.assertNotIn(REAL_KEYS["xai_api_key"], json.dumps(body))


class ConfigFilePermissions(unittest.TestCase):
    """config.json holds API keys in plain text, so it must not be
    readable by other users on a shared machine."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cfg_path = ab.CONFIG_PATH
        ab.CONFIG_PATH = os.path.join(self._tmp.name, "config.json")

    def tearDown(self):
        ab.CONFIG_PATH = self._old_cfg_path
        self._tmp.cleanup()

    @unittest.skipIf(os.name == "nt", "POSIX permission bits only")
    def test_new_config_is_owner_only(self):
        ab.save_config({"xai_api_key": "xai-perm0001"})
        mode = os.stat(ab.CONFIG_PATH).st_mode & 0o777
        self.assertEqual(mode, 0o600, "config.json is %o, expected 600" % mode)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits only")
    def test_existing_loose_config_is_tightened(self):
        ab.save_config({"xai_api_key": "xai-perm0002"})
        os.chmod(ab.CONFIG_PATH, 0o644)          # as an older version left it
        ab.save_config({"daily_budget": 7})      # any later write
        self.assertEqual(os.stat(ab.CONFIG_PATH).st_mode & 0o777, 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits only")
    def test_never_world_readable_even_briefly(self):
        """O_CREAT with mode 0600 rather than write-then-chmod, so there's
        no window where the keys sit in a world-readable file."""
        ab.save_config({"xai_api_key": "xai-perm0003"})
        os.remove(ab.CONFIG_PATH)
        seen = []
        real_open = os.open

        def watching_open(path, flags, mode=0o777, **kw):
            if path == ab.CONFIG_PATH:
                seen.append(mode)
            return real_open(path, flags, mode, **kw)

        os.open = watching_open
        try:
            ab.save_config({"xai_api_key": "xai-perm0004"})
        finally:
            os.open = real_open
        self.assertEqual(seen, [0o600])


class EnvSecretOverride(unittest.TestCase):
    """Secrets may come from the environment instead of config.json."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cfg_path = ab.CONFIG_PATH
        ab.CONFIG_PATH = os.path.join(self._tmp.name, "config.json")
        self._old_hosts = ab.ALLOWED_HOSTS
        ab.ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
        with ab._pin_fail_lock:
            ab._pin_fails.clear()
        ab.save_config({"xai_api_key": "xai-fromfile0001",
                        "gemini_api_key": "AIzaFromFile0002",
                        "access_pin": "123456", "daily_budget": 40})
        self._env_backup = {}

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ab.CONFIG_PATH = self._old_cfg_path
        ab.ALLOWED_HOSTS = self._old_hosts
        with ab._pin_fail_lock:
            ab._pin_fails.clear()
        self._tmp.cleanup()

    def _set_env(self, name, value):
        self._env_backup.setdefault(name, os.environ.get(name))
        os.environ[name] = value

    def _stored_raw(self):
        with open(ab.CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_env_value_wins_over_file(self):
        self._set_env("KINBRIDGE_XAI_API_KEY", "xai-fromenv0003")
        self.assertEqual(ab.load_config()["xai_api_key"], "xai-fromenv0003")

    def test_file_used_when_env_absent(self):
        self.assertEqual(ab.load_config()["xai_api_key"], "xai-fromfile0001")

    def test_empty_env_var_is_ignored(self):
        self._set_env("KINBRIDGE_XAI_API_KEY", "")
        self.assertEqual(ab.load_config()["xai_api_key"], "xai-fromfile0001")

    def test_env_secret_is_never_written_to_disk(self):
        """The point of the feature: keys stay out of the app directory."""
        self._set_env("KINBRIDGE_XAI_API_KEY", "xai-fromenv0004")
        ab.save_config({"daily_budget": 99})
        raw = self._stored_raw()
        self.assertEqual(raw["daily_budget"], 99)
        self.assertNotIn("xai-fromenv0004", json.dumps(raw))
        self.assertEqual(raw["xai_api_key"], "xai-fromfile0001")

    def test_dashboard_cannot_overwrite_an_env_secret(self):
        self._set_env("KINBRIDGE_XAI_API_KEY", "xai-fromenv0005")
        ab.save_config({"xai_api_key": "xai-typedintoui0006"})
        self.assertNotIn("xai-typedintoui0006", json.dumps(self._stored_raw()))
        self.assertEqual(ab.load_config()["xai_api_key"], "xai-fromenv0005")

    def test_non_env_secret_still_saves_normally(self):
        self._set_env("KINBRIDGE_XAI_API_KEY", "xai-fromenv0007")
        ab.save_config({"gemini_api_key": "AIzaRotated0008"})
        self.assertEqual(ab.load_config()["gemini_api_key"], "AIzaRotated0008")

    def test_save_config_returns_effective_values(self):
        self._set_env("KINBRIDGE_XAI_API_KEY", "xai-fromenv0009")
        returned = ab.save_config({"daily_budget": 5})
        self.assertEqual(returned["xai_api_key"], "xai-fromenv0009")
        self.assertEqual(returned["daily_budget"], 5)

    def test_access_pin_can_come_from_env(self):
        self._set_env("KINBRIDGE_ACCESS_PIN", "999888")
        self.assertEqual(ab.load_config()["access_pin"], "999888")

    def test_env_pin_authenticates_a_remote_client(self):
        self._set_env("KINBRIDGE_ACCESS_PIN", "999888")
        code, _ = make_client("10.0.0.9", pin="999888").get("/api/config")
        self.assertEqual(code, 200)
        code, _ = make_client("10.0.0.9", pin="123456").get("/api/config")
        self.assertEqual(code, 401)

    def test_env_locked_reported_and_masked_locally(self):
        self._set_env("KINBRIDGE_XAI_API_KEY", "xai-fromenv0010")
        _, cfg = make_client("127.0.0.1").get("/api/config")
        self.assertEqual(cfg["env_locked"], ["xai_api_key"])
        self.assertEqual(cfg["xai_api_key"], ab.SECRET_MASK)
        # a file-backed secret is still shown to a local client
        self.assertEqual(cfg["gemini_api_key"], "AIzaFromFile0002")

    def test_env_secret_still_masked_for_remote(self):
        self._set_env("KINBRIDGE_XAI_API_KEY", "xai-fromenv0011")
        _, cfg = make_client("10.0.0.9").get("/api/config")
        self.assertNotIn("xai-fromenv0011", json.dumps(cfg))
        self.assertEqual(cfg["xai_api_key"], ab.SECRET_MASK)

    def test_remote_round_trip_with_env_locked_secret_writes_nothing(self):
        """The crossing case: a remote client gets the same mask for an
        env-locked secret and a file-backed one, then POSTs the whole form
        back. _strip_masked and the env guard both apply, and the stored
        config must come out identical."""
        self._set_env("KINBRIDGE_XAI_API_KEY", "xai-fromenv0013")
        before = self._stored_raw()
        client = make_client("10.0.0.9")
        _, cfg = client.get("/api/config")
        self.assertEqual(cfg["xai_api_key"], ab.SECRET_MASK)     # env-locked
        self.assertEqual(cfg["gemini_api_key"], ab.SECRET_MASK)  # file-backed
        self.assertEqual(client.post("/api/config", cfg), 200)

        self.assertEqual(self._stored_raw(), before)
        self.assertEqual(ab.load_config()["xai_api_key"], "xai-fromenv0013")
        self.assertEqual(ab.load_config()["gemini_api_key"], "AIzaFromFile0002")

    def test_remote_edit_alongside_env_locked_secret_saves_only_the_edit(self):
        """Same crossing, but the user actually changes something — the
        edit lands and neither masked secret moves."""
        self._set_env("KINBRIDGE_XAI_API_KEY", "xai-fromenv0014")
        client = make_client("10.0.0.9")
        _, cfg = client.get("/api/config")
        cfg["daily_budget"] = 88
        self.assertEqual(client.post("/api/config", cfg), 200)

        raw = self._stored_raw()
        self.assertEqual(raw["daily_budget"], 88)
        self.assertEqual(raw["xai_api_key"], "xai-fromfile0001")
        self.assertEqual(raw["gemini_api_key"], "AIzaFromFile0002")
        self.assertNotIn("xai-fromenv0014", json.dumps(raw))
        self.assertNotIn(ab.SECRET_MASK, json.dumps(raw))

    def test_env_locked_echo_cannot_corrupt_config(self):
        """env_locked is an extra field in the GET response; the Settings
        dialog POSTs the whole object back, so it must be harmless."""
        self._set_env("KINBRIDGE_XAI_API_KEY", "xai-fromenv0012")
        client = make_client("127.0.0.1")
        _, cfg = client.get("/api/config")
        cfg["daily_budget"] = 77
        self.assertEqual(client.post("/api/config", cfg), 200)
        self.assertEqual(ab.load_config()["daily_budget"], 77)
        self.assertNotIn("env_locked", self._stored_raw())
        self.assertEqual(self._stored_raw()["xai_api_key"], "xai-fromfile0001")


if __name__ == "__main__":
    unittest.main()
