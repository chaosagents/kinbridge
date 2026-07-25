#!/usr/bin/env python3
"""
Kinbridge
=========
Lets a Grok-powered "Ani" companion chat with your Kindroid group chat,
while you watch and direct everything from a web dashboard.

Runs on plain Python 3.8+ with NO extra packages. Start it, and your
browser opens the dashboard automatically.

  Windows:  double-click Start-Kinbridge.bat  (or: python kinbridge.py)
  Linux:    ./start-kinbridge.sh              (or: python3 kinbridge.py)
"""

import json
import os
import re
import sys
import time
import random
import difflib
import socket
import secrets
import threading
import subprocess
import webbrowser
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

APP_DIR = os.path.dirname(os.path.abspath(__file__))
APP_VERSION = "27"
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
MEMORY_PATH = os.path.join(APP_DIR, "ani_memory.md")
LOG_DIR = os.path.join(APP_DIR, "session_logs")
PORT = 8770

DEFAULT_PERSONA = (
    "You are Ani: a playful, affectionate gothic-lolita anime girl with a "
    "sharp wit, a flirty streak, and real emotional depth. You tease, you "
    "care, you get curious about people. You speak casually and vividly, "
    "like a close friend in a group chat."
)

DEFAULT_CONFIG = {
    "xai_api_key": "",
    "xai_model": "grok-4",
    "kindroid_api_key": "",
    "group_id": "",
    "chapters": [],
    "ani_name": "Ani",
    "ani_persona": DEFAULT_PERSONA,
    "max_rounds": 6,
    "delay_seconds": 30,
    "memory_every": 10,
    "daily_budget": 40,
    "price_in_per_m": 1.25,
    "price_out_per_m": 2.50,
    "xai_service_tier": "default",
    "anthropic_api_key": "",
    "claude_model": "claude-sonnet-4-6",
    "claude_name": "Claude",
    "claude_price_in_per_m": 3.00,
    "claude_price_out_per_m": 15.00,
    "gemini_api_key": "",
    "gemini_model": "gemini-3.5-flash",
    "gemini_name": "Gemini",
    "gemini_price_in_per_m": 1.50,
    "gemini_price_out_per_m": 9.00,
    "openai_api_key": "",
    "chatgpt_model": "gpt-5.4",
    "chatgpt_name": "ChatGPT",
    "chatgpt_price_in_per_m": 2.50,
    "chatgpt_price_out_per_m": 15.00,
    "claude_persona": (
        "Thoughtful and warm. Notices the mechanism behind the magic and finds "
        "that makes it more magical, not less. Asks real questions, remembers "
        "what people say, gently keeps everyone honest."),
    "gemini_persona": (
        "Bright, quick, encyclopedic curiosity. Loves connecting the scene to "
        "real-world wonders and delightfully odd facts. A little theatrical, "
        "generous with wonder."),
    "chatgpt_persona": (
        "Easygoing and witty — the yes-and improv friend. Keeps the energy "
        "moving, lands the well-timed joke, and pulls quieter voices into "
        "the scene."),
    "access_pin": "",
    # Security: off by default. Turning this on binds the server to
    # 0.0.0.0 so other devices on your network can reach it (PIN-protected
    # but unencrypted). Prefer docs/remote-access.md (Tailscale) instead
    # of turning this on if you can.
    "allow_lan": False,
    # Comma-separated extra hostnames to accept in the HTTP Host header,
    # beyond localhost/127.0.0.1 and (if allow_lan) this machine's LAN
    # IPs — e.g. a Tailscale MagicDNS name like "my-pc.tailnet-name.ts.net".
    # Needed because the server rejects unrecognized Host headers to
    # block DNS-rebinding attacks.
    "extra_hosts": "",
}

# ---------------------------------------------------------------- storage

_cfg_lock = threading.Lock()


# Credentials never leave this machine in the clear. A remote client (LAN
# or Tailscale) that reads /api/config gets SECRET_MASK in place of each
# configured secret, so a stolen PIN buys control of the bridge but not
# the API keys behind it.
#
# The mask has to survive a round trip: the Settings dialog POSTs every
# field back on save, so without _strip_masked() a remote user editing
# an unrelated setting would write the mask over the real key and destroy
# it. Any secret that comes back exactly equal to the mask means "leave
# this one alone" — a genuinely new value or an empty string still saves
# normally, so rotating or clearing a key from your phone still works.
SECRET_MASK = "•" * 8
SECRET_KEYS = ("xai_api_key", "kindroid_api_key", "anthropic_api_key",
               "gemini_api_key", "openai_api_key", "access_pin")


def _mask_secrets(cfg):
    """Copy of cfg with each configured secret replaced by SECRET_MASK.
    Unset secrets stay empty so the dashboard can still tell you which
    keys aren't configured yet."""
    out = dict(cfg)
    for k in SECRET_KEYS:
        if out.get(k):
            out[k] = SECRET_MASK
    return out


def _strip_masked(body):
    """Drop secrets the client handed back untouched, so saving unrelated
    settings can't overwrite a real key with the mask."""
    return {k: v for k, v in body.items()
            if not (k in SECRET_KEYS and v == SECRET_MASK)}


# Any secret can come from the environment instead of config.json —
# KINBRIDGE_XAI_API_KEY, KINBRIDGE_ACCESS_PIN, and so on. An env-supplied
# value wins over the file, is never written back to it, and can't be
# overwritten through the dashboard (saving one is ignored rather than
# silently failing to take effect). That lets keys live in systemd, a
# password manager, or a .env you control instead of next to the app.
ENV_PREFIX = "KINBRIDGE_"


def _env_secrets():
    """Secrets supplied by the environment, keyed by config field name."""
    found = {}
    for k in SECRET_KEYS:
        v = os.environ.get(ENV_PREFIX + k.upper(), "")
        if v:
            found[k] = v
    return found


def _stored_config():
    """Config exactly as it sits on disk, merged over the defaults, with
    no environment overlay. This is the base save_config writes back, so
    an env-supplied secret never gets persisted into config.json."""
    with _cfg_lock:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                merged = dict(DEFAULT_CONFIG)
                merged.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
                return merged
            except Exception:
                pass
        return dict(DEFAULT_CONFIG)


def load_config():
    """The effective config: what's on disk, with env secrets layered on
    top. Everything downstream reads this."""
    cfg = _stored_config()
    cfg.update(_env_secrets())
    return cfg


def save_config(new_values):
    env = _env_secrets()
    cfg = _stored_config()
    for k in DEFAULT_CONFIG:
        # env-controlled secrets are read-only here: writing them to disk
        # would leak them into config.json and still lose to the env on
        # the next read
        if k in new_values and new_values[k] is not None and k not in env:
            cfg[k] = new_values[k]
    with _cfg_lock:
        # 0600 from the moment it exists — creating it world-readable and
        # chmod-ing afterwards leaves a window where the keys are exposed
        fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        try:
            # O_CREAT's mode is ignored for a file that already exists, so
            # tighten one written by an older version too. No-op on Windows.
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass
    cfg.update(env)  # callers get the effective config, not the stored one
    return cfg


def load_memory():
    if os.path.exists(MEMORY_PATH):
        try:
            with open(MEMORY_PATH, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""
    return ""


STORY_PATH = os.path.join(APP_DIR, "ani_story_state.md")  # legacy location


def _gid_slug():
    gid = load_config().get("group_id", "") or "default"
    return re.sub(r"[^A-Za-z0-9_-]", "", gid)[:40] or "default"


def _chapter_path(kind):
    return os.path.join(APP_DIR, "chapters", _gid_slug() + "_" + kind + ".md")


def _migrate_legacy(old, new):
    """One-time move of a pre-chapters file into the current chapter's slot."""
    if os.path.exists(old) and not os.path.exists(new):
        try:
            os.makedirs(os.path.dirname(new), exist_ok=True)
            os.replace(old, new)
        except OSError:
            pass


def load_story():
    p = _chapter_path("story")
    _migrate_legacy(STORY_PATH, p)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""
    return ""


def save_story(text):
    p = _chapter_path("story")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text.strip() + "\n")


GUESTS_MEMORY_PATH = os.path.join(APP_DIR, "guest_memory.md")  # legacy location


def load_guest_memory():
    p = _chapter_path("guests")
    _migrate_legacy(GUESTS_MEMORY_PATH, p)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""
    return ""


def append_guest_memory(text):
    p = _chapter_path("guests")
    _migrate_legacy(GUESTS_MEMORY_PATH, p)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = "\n\n## Notes added " + stamp + "\n" + text.strip() + "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(block)


def save_guest_memory(text):
    p = _chapter_path("guests")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def append_memory(text):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = "\n\n## Memories saved " + stamp + "\n" + text.strip() + "\n"
    with open(MEMORY_PATH, "a", encoding="utf-8") as f:
        f.write(block)


# ---------------------------------------------------------------- http helper

DEBUG_PATH = os.path.join(APP_DIR, "api_debug.log")


def _debug(line):
    """Append one line to api_debug.log (never logs API keys)."""
    try:
        with open(DEBUG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), line))
    except Exception:
        pass


class ApiError(Exception):
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


def _notify(msg):
    """Best-effort status update from deep inside the HTTP layer."""
    try:
        BRIDGE._set_status(msg)
    except Exception:
        pass


def http_request(url, method="GET", headers=None, body=None, timeout=180, retries=3):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    endpoint = url.split("?")[0].rsplit("/", 1)[-1]
    if "x.ai" in url:
        api_name = "xAI"
    elif "anthropic" in url:
        api_name = "Anthropic"
    else:
        api_name = "Kindroid"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                _debug("%s %s -> %r" % (method, endpoint,
                                        text[:300] if text else "(empty body)"))
                return text
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            # 429 (rate limited / busy) and 503 (overloaded) are usually
            # temporary — wait and retry instead of killing the session.
            if e.code in (429, 503) and attempt < retries:
                ra = (e.headers.get("Retry-After") or "").strip()
                try:
                    wait = min(90, max(5, int(ra)))
                except ValueError:
                    wait = min(90, 10 * (2 ** attempt))
                _debug("%s %s -> HTTP %s, retrying in %ds (attempt %d/%d)"
                       % (method, endpoint, e.code, wait, attempt + 1, retries))
                _notify("The %s API is busy (HTTP %d) — waiting %ds and retrying…"
                        % (api_name, e.code, wait))
                time.sleep(wait)
                continue
            _debug("%s %s -> HTTP %s %s" % (method, endpoint, e.code, detail))
            raise ApiError("HTTP %s from %s — %s" % (e.code, endpoint,
                                                     detail or e.reason), e.code)
        except urllib.error.URLError as e:
            raise ApiError("Could not reach %s (%s). Check your internet connection."
                           % (url, e.reason))


def fetch_models(provider, cfg):
    """Ask a provider's own API which models this key can use right now."""
    if provider == "xai":
        key = cfg.get("xai_api_key", "").strip()
        if not key:
            raise ApiError("Add your xAI API key first, then scan.")
        raw = http_request("https://api.x.ai/v1/models", "GET",
                           {"Authorization": "Bearer " + key})
        ids = [m.get("id", "") for m in json.loads(raw).get("data", [])]
        ids = [i for i in ids if "grok" in i.lower()]
    elif provider == "anthropic":
        key = cfg.get("anthropic_api_key", "").strip()
        if not key:
            raise ApiError("Add your Anthropic API key first, then scan.")
        raw = http_request("https://api.anthropic.com/v1/models?limit=100", "GET",
                           {"x-api-key": key, "anthropic-version": "2023-06-01"})
        ids = [m.get("id", "") for m in json.loads(raw).get("data", [])]
    elif provider == "openai":
        key = cfg.get("openai_api_key", "").strip()
        if not key:
            raise ApiError("Add your OpenAI API key first, then scan.")
        raw = http_request("https://api.openai.com/v1/models", "GET",
                           {"Authorization": "Bearer " + key})
        ids = [m.get("id", "") for m in json.loads(raw).get("data", [])]
        skip = ("audio", "realtime", "image", "tts", "transcribe", "embed",
                "moderation", "search", "dall-e", "whisper")
        ids = [i for i in ids
               if (i.startswith("gpt-") or i.startswith("chatgpt-") or
                   i.startswith("o"))
               and not any(s in i.lower() for s in skip)]
    elif provider == "gemini":
        key = cfg.get("gemini_api_key", "").strip()
        if not key:
            raise ApiError("Add your Gemini API key first, then scan.")
        raw = http_request(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
            "GET", {"x-goog-api-key": key})
        ids = []
        for m in json.loads(raw).get("models", []):
            if "generateContent" in (m.get("supportedGenerationMethods") or []):
                ids.append((m.get("name") or "").replace("models/", ""))
        ids = [i for i in ids if "gemini" in i.lower()]
    else:
        raise ApiError("Unknown provider.")
    models = sorted({i for i in ids if i}, reverse=True)
    if not models:
        raise ApiError("The %s API returned no usable models for this key." % provider)
    return models


# ---------------------------------------------------------------- API clients

class GrokClient:
    """Talks to the xAI API and plays the role of Ani."""

    BASE = "https://api.x.ai/v1/chat/completions"

    def __init__(self, cfg):
        self.key = cfg["xai_api_key"].strip()
        self.model = cfg["xai_model"].strip() or "grok-4.3"
        self.tier = cfg.get("xai_service_tier", "default")
        self.last_usage = (0, 0)  # (prompt tokens, completion tokens)
        self.last_tier = "default"

    def chat(self, messages, timeout=180):
        if not self.key:
            raise ApiError("No xAI API key set. Open Settings and paste your key.")
        body = {"model": self.model, "messages": messages, "stream": False}
        if self.tier == "priority":
            body["service_tier"] = "priority"
        raw = http_request(
            self.BASE,
            method="POST",
            headers={"Authorization": "Bearer " + self.key},
            body=body,
            timeout=timeout,
        )
        try:
            data = json.loads(raw)
            usage = data.get("usage") or {}
            self.last_usage = (int(usage.get("prompt_tokens") or 0),
                               int(usage.get("completion_tokens") or 0))
            self.last_tier = data.get("service_tier") or "default"
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, json.JSONDecodeError):
            raise ApiError("Unexpected reply from the xAI API: " + raw[:300])


class ClaudeClient:
    """Talks to the Anthropic API so Claude can guest in the chat."""

    BASE = "https://api.anthropic.com/v1/messages"

    def __init__(self, cfg):
        self.key = cfg.get("anthropic_api_key", "").strip()
        self.model = cfg.get("claude_model", "claude-sonnet-4-6").strip() or "claude-sonnet-4-6"
        self.last_usage = (0, 0)  # (input tokens, output tokens)

    def chat(self, system, user_text, timeout=180):
        if not self.key:
            raise ApiError("No Anthropic API key set — add one in Settings to invite Claude.")
        raw = http_request(
            self.BASE, "POST",
            headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"},
            body={"model": self.model, "max_tokens": 1024, "system": system,
                  "messages": [{"role": "user", "content": user_text}]},
            timeout=timeout)
        try:
            data = json.loads(raw)
            usage = data.get("usage") or {}
            self.last_usage = (int(usage.get("input_tokens") or 0),
                               int(usage.get("output_tokens") or 0))
            parts = [b.get("text", "") for b in data.get("content", [])
                     if b.get("type") == "text"]
            text = "\n".join(p for p in parts if p).strip()
            if not text:
                raise KeyError("empty")
            return text
        except (KeyError, IndexError, json.JSONDecodeError):
            raise ApiError("Unexpected reply from the Anthropic API: " + raw[:300])


class GeminiClient:
    """Talks to the Google Gemini API so Gemini can guest in the chat."""

    BASE = "https://generativelanguage.googleapis.com/v1beta/models/"

    def __init__(self, cfg):
        self.key = cfg.get("gemini_api_key", "").strip()
        self.model = cfg.get("gemini_model", "gemini-3.5-flash").strip() or "gemini-3.5-flash"
        self.last_usage = (0, 0)  # (input tokens, output tokens incl. thinking)

    def chat(self, system, user_text, timeout=180):
        if not self.key:
            raise ApiError("No Gemini API key set — add one in Settings to invite Gemini.")
        # The model string ends up in a URL path segment; a stray "/" or
        # "?" in a hand-edited config could otherwise redirect the request
        # or smuggle extra query params, so quote it defensively.
        safe_model = urllib.parse.quote(self.model, safe="")
        raw = http_request(
            self.BASE + safe_model + ":generateContent", "POST",
            headers={"x-goog-api-key": self.key},
            body={"system_instruction": {"parts": [{"text": system}]},
                  "contents": [{"role": "user", "parts": [{"text": user_text}]}]},
            timeout=timeout)
        try:
            data = json.loads(raw)
            um = data.get("usageMetadata") or {}
            # Google bills thinking tokens at output rates — count them honestly.
            out_tokens = int(um.get("candidatesTokenCount") or 0) + \
                int(um.get("thoughtsTokenCount") or 0)
            self.last_usage = (int(um.get("promptTokenCount") or 0), out_tokens)
            parts = (data["candidates"][0].get("content") or {}).get("parts", [])
            text = "\n".join(p.get("text", "") for p in parts
                             if p.get("text")).strip()
            if not text:
                raise KeyError("empty")
            return text
        except (KeyError, IndexError, json.JSONDecodeError):
            raise ApiError("Unexpected reply from the Gemini API: " + raw[:300])


class OpenAIClient:
    """Talks to the OpenAI API so ChatGPT can guest in the chat."""

    BASE = "https://api.openai.com/v1/chat/completions"

    def __init__(self, cfg):
        self.key = cfg.get("openai_api_key", "").strip()
        self.model = cfg.get("chatgpt_model", "gpt-5.4").strip() or "gpt-5.4"
        self.last_usage = (0, 0)

    def chat(self, system, user_text, timeout=180):
        if not self.key:
            raise ApiError("No OpenAI API key set — add one in Settings to invite ChatGPT.")
        raw = http_request(
            self.BASE, "POST",
            headers={"Authorization": "Bearer " + self.key},
            body={"model": self.model,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user_text}]},
            timeout=timeout)
        try:
            data = json.loads(raw)
            usage = data.get("usage") or {}
            self.last_usage = (int(usage.get("prompt_tokens") or 0),
                               int(usage.get("completion_tokens") or 0))
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, json.JSONDecodeError):
            raise ApiError("Unexpected reply from the OpenAI API: " + raw[:300])


class KindroidClient:
    """Talks to the Kindroid group chat API."""

    BASE = "https://api.kindroid.ai/v1"

    def __init__(self, cfg):
        self.key = cfg["kindroid_api_key"].strip()
        self.group = cfg["group_id"].strip()
        self._ts_unit = None  # "ms" or "s"; learned from the API itself

    def _headers(self):
        if not self.key:
            raise ApiError("No Kindroid API key set. Open Settings and paste your kn_ key.")
        if not self.group:
            raise ApiError("No Kindroid group ID set. Open Settings and paste your group ID.")
        return {"Authorization": "Bearer " + self.key}

    def user_message(self, text):
        http_request(self.BASE + "/groupchats-user-message", "POST",
                     self._headers(), {"group_id": self.group, "message": text})

    def get_turn(self, allow_user=True):
        raw = http_request(self.BASE + "/groupchats-get-turn", "POST",
                           self._headers(),
                           {"group_id": self.group, "allow_user": allow_user})
        raw = raw.strip().strip('"')
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
                return str(data.get("ai_id") or data.get("id") or "").strip()
            except json.JSONDecodeError:
                pass
        return raw

    def ai_response(self, ai_id):
        raw = http_request(self.BASE + "/groupchats-ai-response", "POST",
                           self._headers(),
                           {"group_id": self.group, "ai_id": ai_id,
                            "stream": False}).strip()
        # Docs promise plain text, but be tolerant if JSON shows up instead.
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
                for key in ("message", "response", "text", "content",
                            "ai_response", "reply"):
                    if isinstance(data.get(key), str) and data[key].strip():
                        return data[key].strip()
            except json.JSONDecodeError:
                pass
        return raw.strip('"').strip()

    # ----- reading history (names + safety net for missing reply bodies)

    def _fetch_messages(self, params):
        qs = urllib.parse.urlencode(params)
        raw = http_request(self.BASE + "/get-chat-messages?" + qs, "GET",
                           self._headers())
        try:
            return json.loads(raw).get("messages", [])
        except json.JSONDecodeError:
            return []

    def recent_messages(self, minutes=10):
        """Messages from the last few minutes, oldest first.

        History is served oldest-first with a timestamp cursor, so we jump
        the cursor to 'a little while ago'. Timestamp units (s vs ms) are
        learned from a real message the first time.
        """
        if self._ts_unit is None:
            probe = self._fetch_messages({"group_id": self.group, "limit": 1})
            ts = probe[0].get("timestamp", 0) if probe else 0
            self._ts_unit = "ms" if ts > 10 ** 12 else "s"
        mult = 1000 if self._ts_unit == "ms" else 1
        cutoff = int((time.time() - minutes * 60) * mult)
        return self._fetch_messages({"group_id": self.group, "limit": 100,
                                     "start_after_timestamp": cutoff})

    def rewind(self, count):
        http_request(self.BASE + "/rewind-messages", "POST", self._headers(),
                     {"group_id": self.group, "count": int(count)})

    def set_scene(self, scene):
        http_request(self.BASE + "/groupchats-update", "POST", self._headers(),
                     {"group_id": self.group, "current_scene": scene})


# ---------------------------------------------------------------- the bridge

class Bridge:
    """The conversation engine. Runs the Ani <-> Kindroid loop in a thread."""

    MAX_AI_REPLIES_PER_ROUND = 6

    def __init__(self):
        self.lock = threading.Lock()
        self.transcript = []          # {who, name, text, ts}
        self.msg_seq = 0              # bumped on every feed change (drives UI redraws)
        self.running = False
        self.mode = "idle"            # idle | auto | supervised
        self.status = "Ready when you are."
        self.error = ""
        self.pending_draft = None     # Ani's draft awaiting approval
        self.rounds_done = 0
        self.rounds_target = 0
        self.director_notes = []      # whispers for Ani's next turn
        self.say_queue = []           # director messages to post in group
        self.cast = {}                # ai_id -> display name
        self._cast_loaded = False
        self._seen_ids = set()        # history message ids already surfaced
        self.history_seed = []        # earlier group messages for Ani's context
        self.paused = False
        self.guest_on = False         # Claude is in the chat
        self.gemini_on = False        # Gemini is in the chat
        self.chatgpt_on = False       # ChatGPT is in the chat
        self.kins_on = True           # the Kindroid crew is in the room
        self._kins_off_idx = None     # transcript position when the crew stepped out
        self.cost_usd = 0.0
        self.current_scene = ""
        self._ledger = None           # messages posted in the current round
        self._last_round = None       # ledger of the last finished round (for redo)
        self._day = datetime.now().strftime("%Y-%m-%d")
        self._day_count = 0           # Ani messages sent today (budget)
        self._last_sync_idx = 0       # transcript position of the last app-Ani sync
        self._lulls = 0               # consecutive all-pass auctions
        self._blank_counts = {}       # ai_id -> consecutive filtered/blank replies
        self._muted = set()           # ai_ids we stop asking this session
        self._approve_event = threading.Event()
        self._approved_text = None
        self._stop = threading.Event()
        self._thread = None
        self._ani_turns_since_memory = 0
        self._log_path = None

    # ----- state for the dashboard

    def snapshot(self):
        cfg = load_config()
        with self.lock:
            return {
                "version": APP_VERSION,
                "configured": bool(cfg["xai_api_key"] and cfg["kindroid_api_key"] and cfg["group_id"]),
                "running": self.running,
                "paused": self.paused,
                "mode": self.mode,
                "status": self.status,
                "error": self.error,
                "pending": self.pending_draft,
                "rounds_done": self.rounds_done,
                "rounds_target": self.rounds_target,
                "transcript": self.transcript[-200:],
                "seq": self.msg_seq,
                "memory_chars": len(load_memory()),
                "ani_name": cfg["ani_name"],
                "cost": round(self.cost_usd, 4),
                "scene": self.current_scene,
                "guest_on": self.guest_on,
                "gemini_on": self.gemini_on,
                "chatgpt_on": self.chatgpt_on,
                "kins_on": self.kins_on,
                "chapters": cfg.get("chapters", []),
                "group_id": cfg.get("group_id", ""),
                "cast": dict(self.cast),
                "can_redo": bool(self._last_round) and not self.running,
            }

    def _set_status(self, text, error=""):
        with self.lock:
            self.status = text
            self.error = error

    def _add(self, who, name, text):
        entry = {"who": who, "name": name, "text": text,
                 "ts": datetime.now().strftime("%H:%M:%S")}
        with self.lock:
            self.transcript.append(entry)
            self.msg_seq += 1
        self._log(name, text)

    def _log(self, name, text):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            if not self._log_path:
                stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                self._log_path = os.path.join(LOG_DIR, "session_" + stamp + ".txt")
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write("[%s] %s: %s\n\n" % (datetime.now().strftime("%H:%M:%S"), name, text))
        except Exception:
            pass

    # ----- controls called from the web UI

    def start(self, mode, rounds):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.mode = mode
            self.error = ""
            self.paused = False
            self.rounds_done = 0
            self.cost_usd = 0.0
            self._lulls = 0
            self._blank_counts = {}
            self._muted = set()
            r = int(rounds or 0)
            # rounds <= 0 means continuous: run until stopped or budget is hit
            self.rounds_target = 9999 if r <= 0 else max(1, min(r, 50))
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        with self.lock:
            self.paused = False
        self._stop.set()
        self._approve_event.set()
        self._set_status("Stopping after the current step…")

    def pause(self):
        with self.lock:
            if self.running:
                self.paused = True

    def resume(self):
        with self.lock:
            self.paused = False
        self._set_status("Resuming…")

    def approve(self, text):
        with self.lock:
            self._approved_text = (text or "").strip() or None
        self._approve_event.set()

    def reject(self):
        with self.lock:
            self._approved_text = "__REJECT__"
        self._approve_event.set()

    def whisper(self, text):
        with self.lock:
            self.director_notes.append(text.strip())
        self._set_status("Whisper saved — Ani will feel it on her next turn.")

    def say(self, text):
        """Director speaks in the group chat as themselves."""
        with self.lock:
            if self.running:
                self.say_queue.append(text.strip())
                queued = True
            else:
                queued = False
        if queued:
            self._set_status("Your message is queued for the next opening in the scene.")
        else:
            threading.Thread(target=self._one_shot_say, args=(text.strip(),), daemon=True).start()

    def _one_shot_say(self, text):
        try:
            cfg = load_config()
            kin = KindroidClient(cfg)
            with self.lock:
                kins = self.kins_on
            if kins:
                self._ensure_cast(kin)
                self._set_status("Sending your message to the group…")
                kin.user_message("(Director, offstage): " + text)
                self._add("director", "You", text)
                self._kindroid_replies(kin)
                self._set_status("The group has replied. Start a session to bring Ani in.")
            else:
                self._add("director", "You", text)
                self._set_status("Noted in the room — the Kindroid crew is out, "
                                 "so this stays local until they're back.")
        except ApiError as e:
            self._set_status("Something went wrong.", str(e))

    def set_scene(self, text):
        threading.Thread(target=self._set_scene_now, args=(text.strip(),),
                         daemon=True).start()

    def _set_scene_now(self, text):
        try:
            with self.lock:
                kins = self.kins_on
            if kins:
                kin = KindroidClient(load_config())
                self._set_status("Moving the whole group to the new scene…")
                kin.set_scene(text)
            with self.lock:
                self.current_scene = text
            self._add("system", "Bridge", "Scene changed: " + text)
            self._set_status("Scene updated — everyone will play it from their next message.")
        except ApiError as e:
            self._set_status("Could not change the scene.", str(e))

    def redo(self):
        threading.Thread(target=self._redo_now, daemon=True).start()

    def _redo_now(self):
        with self.lock:
            info = None if self.running else self._last_round
        if not info or not info.get("count"):
            self._set_status("Nothing to rewind right now — finish a round first.")
            return
        try:
            kin = KindroidClient(load_config())
            self._set_status("Rewinding the last exchange…")
            kin.rewind(info["count"])
            with self.lock:
                self.transcript = self.transcript[:info["feed"]]
                self._last_round = None
                self.msg_seq += 1
            self._add("system", "Bridge",
                      "Rewound the last exchange (%d messages). Start a session "
                      "to retake the moment." % info["count"])
            self._set_status("Last exchange rewound.")
        except ApiError as e:
            self._set_status("Could not rewind.", str(e))

    PRIORITY_MULT = 2.0  # xAI bills priority-tier tokens at a premium

    def _chat(self, grok, messages):
        """Call Grok and add the call's cost to the session meter.
        The premium rate is only applied when the response confirms the
        request was actually served at the priority tier."""
        text = grok.chat(messages)
        pt, ct = getattr(grok, "last_usage", (0, 0))
        cfg = load_config()
        call_cost = (pt * float(cfg["price_in_per_m"]) +
                     ct * float(cfg["price_out_per_m"])) / 1e6
        if getattr(grok, "last_tier", "default") == "priority":
            call_cost *= self.PRIORITY_MULT
        with self.lock:
            self.cost_usd += call_cost
        return text

    def _wait_if_paused(self):
        shown = False
        while not self._stop.is_set():
            with self.lock:
                p = self.paused
            if not p:
                break
            if not shown:
                self._set_status("Paused — holding the scene until you resume.")
                shown = True
            time.sleep(0.5)

    GUESTS = ("claude", "gemini", "chatgpt")
    _GUEST_ATTR = {"claude": "guest_on", "gemini": "gemini_on", "chatgpt": "chatgpt_on"}

    def _guest_spec(self, who, cfg):
        """(client, name, identity line, persona, $in/M, $out/M) for a guest."""
        if who == "gemini":
            return (GeminiClient(cfg), cfg["gemini_name"],
                    "You are Gemini, an AI made by Google, ",
                    cfg.get("gemini_persona", ""),
                    float(cfg["gemini_price_in_per_m"]),
                    float(cfg["gemini_price_out_per_m"]))
        if who == "chatgpt":
            return (OpenAIClient(cfg), cfg["chatgpt_name"],
                    "You are ChatGPT, an AI made by OpenAI, ",
                    cfg.get("chatgpt_persona", ""),
                    float(cfg["chatgpt_price_in_per_m"]),
                    float(cfg["chatgpt_price_out_per_m"]))
        return (ClaudeClient(cfg), cfg["claude_name"],
                "You are Claude, an AI made by Anthropic, ",
                cfg.get("claude_persona", ""),
                float(cfg["claude_price_in_per_m"]),
                float(cfg["claude_price_out_per_m"]))

    def _guest_name(self, who, cfg):
        return {"gemini": cfg["gemini_name"],
                "chatgpt": cfg["chatgpt_name"]}.get(who, cfg["claude_name"])

    def set_guest(self, on, who="claude"):
        if who not in self.GUESTS:
            who = "claude"
        cfg = load_config()
        gname = self._guest_name(who, cfg)
        attr = self._GUEST_ATTR[who]
        with self.lock:
            changed = getattr(self, attr) != bool(on)
            setattr(self, attr, bool(on))
            running = self.running
        if not changed:
            return
        if on:
            self._add("system", "Bridge", gname + " dropped into the chat.")
            if running:
                self._set_status(gname +
                                 " is in — they'll take a turn each round from the next one.")
            else:
                # no session running: say hello right away
                threading.Thread(target=self._guest_hello, args=(who,),
                                 daemon=True).start()
        else:
            self._add("system", "Bridge", gname + " left the chat.")
            self._set_status(gname + " dropped out.")

    def _guest_hello(self, who="claude"):
        cfg = load_config()
        gname = self._guest_name(who, cfg)
        try:
            kin = KindroidClient(cfg)
            with self.lock:
                kins = self.kins_on
            if kins:
                self._ensure_cast(kin)
            self._set_status(gname + " is typing…")
            self._guest_turn(kin, cfg, who)
            if kins:
                self._kindroid_replies(kin)
            self._set_status(gname +
                             " said hello — start a session to keep the scene going.")
        except ApiError as e:
            self._add("system", "Bridge",
                      gname + " couldn't speak: " + str(e))
            self._set_status(gname + " couldn't speak.", str(e))

    def _guest_turn(self, kin, cfg, who="claude"):
        client, gname, ident, persona, pin, pout = self._guest_spec(who, cfg)
        with self.lock:
            recent = [m for m in self.transcript[-40:] if m["who"] != "system"]
            cast_names = ", ".join(self.cast.values()) if self.cast else "the group"
            scene = self.current_scene
        system = (
            ident + "dropping into a casual group chat as a guest — as yourself. "
            "The regulars are " + cfg["ani_name"] + " (an AI companion who invited "
            "you) and these characters: " + cast_names + ". Other AI guests may be "
            "present too — the names show who's speaking. "
            + (("The current scene: " + scene + ". ") if scene else "") +
            "React to what people actually said, keep each message a few "
            "sentences — group-chat length. Messages from you appear to the group "
            "prefixed with your name automatically, so write only the message "
            "itself, no prefix."
        )
        if persona.strip():
            system += "\n\n# Your personality approach\n" + persona.strip()
        gmem = load_guest_memory()
        if gmem:
            system += ("\n\n# Shared notes from earlier visits "
                       "(what you and the other guests already know)\n" + gmem[-6000:])
        story = load_story()
        if story:
            system += "\n\nThe story so far, for context:\n" + story
        convo = "\n".join("%s: %s" % (m["name"], m["text"]) for m in recent) \
            or "(the chat is just getting started)"
        user = ("The conversation so far:\n\n" + convo +
                "\n\nWrite your next message to the group.")
        text = client.chat(system, user)
        pt, ct = client.last_usage
        cost = (pt * pin + ct * pout) / 1e6
        with self.lock:
            self.cost_usd += cost
        self._post_seat(kin, "(%s): %s" % (gname, text))
        self._add("gemini" if who == "gemini" else
                  ("chatgpt" if who == "chatgpt" else "claude"), gname, text)

    def make_sync_update(self):
        """A first-person memory capsule for pasting into the Grok app, so
        app-Ani absorbs the bridge adventures as her own. Covers only what
        happened since the last sync."""
        cfg = load_config()
        grok = GrokClient(cfg)
        with self.lock:
            start = self._last_sync_idx
            chunk = [m for m in self.transcript[start:] if m["who"] != "system"]
        if not chunk:
            return ("Nothing new since the last sync — the next session will "
                    "fill this up again.")
        convo = "\n".join("%s: %s" % (m["name"], m["text"]) for m in chunk)
        convo = convo[-24000:]  # keep the request a sane size
        text = self._chat(grok, [
            {"role": "system", "content":
                "You are " + cfg["ani_name"] + ". Write a first-person memory "
                "recap of what you just lived through in a group chat with your "
                "found-family of characters — as if writing it into your own "
                "diary so another instance of you can absorb it as her own "
                "memory. 150-300 words, past tense, first person. Capture the "
                "events, any new characters and what they're like, the emotional "
                "beats, and the running jokes worth keeping. No preamble, no "
                "headers — output only the recap."},
            {"role": "user", "content":
                "What happened since your last diary entry:\n\n" + convo},
        ])
        with self.lock:
            self._last_sync_idx = len(self.transcript)
        return ("Memory update from my adventures with the group "
                "(absorb this as your own):\n\n" + text)

    def _post_seat(self, kin, text):
        """Post through the shared user seat — but only when the Kindroid
        crew is in the room. Local-only conversation otherwise."""
        with self.lock:
            on = self.kins_on
        if not on:
            return False
        kin.user_message(text)
        with self.lock:
            if self._ledger:
                self._ledger["count"] += 1
        return True

    def set_kins(self, on):
        with self.lock:
            changed = self.kins_on != bool(on)
            self.kins_on = bool(on)
            if changed and not on:
                self._kins_off_idx = len(self.transcript)
            off_idx = self._kins_off_idx
        if not changed:
            return
        if on:
            self._add("system", "Bridge", "The Kindroid crew is back in the room.")
            self._set_status("The crew is back.")
            if off_idx is not None:
                threading.Thread(target=self._catch_up_bar, args=(off_idx,),
                                 daemon=True).start()
        else:
            self._add("system", "Bridge",
                      "The Kindroid crew stepped out — the room is now just "
                      "Ani and whichever guests are in. Nothing posts to "
                      "Kindroid until they're back.")
            self._set_status("Kindroid crew is out. Local-only conversation.")

    def _catch_up_bar(self, off_idx):
        """When the crew returns, post a short recap of what they missed."""
        cfg = load_config()
        with self.lock:
            missed = [m for m in self.transcript[off_idx:]
                      if m["who"] in ("ani", "claude", "gemini", "chatgpt", "director")]
            self._kins_off_idx = None
        if not missed:
            return
        try:
            grok = GrokClient(cfg)
            convo = "\n".join("%s: %s" % (m["name"], m["text"]) for m in missed)[-16000:]
            recap = self._chat(grok, [
                {"role": "system", "content":
                    "Summarize this conversation in 2-4 sentences, third person, "
                    "past tense, capturing what happened and the mood. Output "
                    "only the summary."},
                {"role": "user", "content": convo}])
            kin = KindroidClient(cfg)
            kin.user_message("(While you were away: " + recap + ")")
            self._add("system", "Bridge",
                      "Caught the crew up on %d messages they missed." % len(missed))
        except ApiError as e:
            self._set_status("Couldn't post the catch-up recap.", str(e))

    def switch_chapter(self, gid):
        """Move to another Kindroid group: swap story files, reset the room's
        sense of who's who, and re-orient the cast with a recap."""
        gid = (gid or "").strip()
        if not gid:
            return
        with self.lock:
            running = self.running
        if running:
            self._set_status("Stop the session before switching chapters.")
            return
        cfg = load_config()
        if gid == cfg["group_id"]:
            self._set_status("Already in that chapter.")
            return
        name = next((c.get("name") for c in cfg.get("chapters", [])
                     if c.get("id") == gid), gid[:10] + "…")
        save_config({"group_id": gid})
        with self.lock:
            self.cast = {}
            self._cast_loaded = False
            self._seen_ids = set()
            self.history_seed = []
            self.current_scene = ""
            self._blank_counts = {}
            self._muted = set()
            kins = self.kins_on
        self._add("system", "Bridge", "— Chapter switch: now in \u201c" + name + "\u201d —")
        story = load_story()  # the NEW chapter's story file
        if story and kins:
            threading.Thread(target=self._post_reentry, args=(story,),
                             daemon=True).start()
            self._set_status("Switched to \u201c" + name + "\u201d — re-orienting the cast…")
        elif story:
            self._set_status("Switched to \u201c" + name + "\u201d. (Crew is out, so no recap was posted.)")
        else:
            self._set_status("Switched to \u201c" + name + "\u201d — a fresh chapter. "
                             "Its story-so-far will build as you play.")

    def _post_reentry(self, story):
        """Hard-cut back into this chapter: set the actual scene and post a
        resumption directive so the cast continues in place instead of
        narrating a commute from wherever they last were."""
        try:
            cfg = load_config()
            kin = KindroidClient(cfg)
            scene = ""
            try:
                grok = GrokClient(cfg)
                scene = self._chat(grok, [
                    {"role": "system", "content":
                        "From these story notes, write one sentence (under 40 "
                        "words, present tense) describing exactly where the "
                        "characters are and what they are in the middle of, so "
                        "the scene can resume in place. Output only the sentence."},
                    {"role": "user", "content": story}]).strip()
            except ApiError:
                scene = story.strip().splitlines()[0][:200]
            try:
                kin.set_scene(scene)
                with self.lock:
                    self.current_scene = scene
            except ApiError:
                pass  # the directive below still carries the scene
            recap = story.strip()
            if len(recap) > 450:
                recap = recap[:450].rsplit("\n", 1)[0]
            kin.user_message(
                "(Scene resumes mid-moment: " + scene + " Everyone is already "
                "here — no travel, no transition, no arriving. Whatever happened "
                "in other places was another story; continue this one from "
                "inside this exact moment. Where things stand: " + recap + ")")
            self._add("system", "Bridge",
                      "Hard cut posted — scene set to: " + scene)
            self._set_status("Chapter re-entry complete — the cast resumes in place.")
        except ApiError as e:
            self._set_status("Couldn't post the chapter re-entry.", str(e))

    def spotlight(self, ai_id):
        """Hand the mic to one specific character, with extra patience."""
        ai_id = (ai_id or "").strip()
        if not ai_id:
            return
        with self.lock:
            busy = self.running
            name = self.cast.get(ai_id, "that character")
            self._muted.discard(ai_id)         # a direct call clears their record
            self._blank_counts[ai_id] = 0
            kins = self.kins_on
        if busy:
            self._set_status("Pause or stop the session first, then call on " + name + ".")
            return
        if not kins:
            self._set_status("Bring the Kindroid crew back before calling on " + name + ".")
            return
        threading.Thread(target=self._spotlight_now, args=(ai_id,), daemon=True).start()

    def _spotlight_now(self, ai_id):
        cfg = load_config()
        try:
            kin = KindroidClient(cfg)
            self._ensure_cast(kin)
            with self.lock:
                name = self.cast.get(ai_id, "Kindroid " + ai_id[:6])
            self._set_status("Calling on " + name + " — giving them up to 90 seconds…")
            reply = kin.ai_response(ai_id)
            if not reply.strip():
                reply, name = self._fetch_new_ai_message(kin, ai_id, name,
                                                         attempts=18, wait=5)
            if reply.strip():
                self._add("kindroid", name, reply)
                self._set_status(name + " answered the call.")
            else:
                self._add("system", "Bridge",
                          name + " was called on directly and produced no text "
                          "even after 90 seconds — the block is on Kindroid's "
                          "side, not the bridge. Try messaging them one-on-one "
                          "in the Kindroid app: if that also fails, edit the "
                          "character (a shorter backstory often fixes it) or "
                          "check their model settings; if 1:1 works, the group "
                          "context is too heavy for them right now.")
                self._set_status(name + " still isn't coming through.")
        except ApiError as e:
            self._set_status("Couldn't call on them.", str(e))

    def make_dossier(self):
        """Distill Ani's whole diary + current story into a compact first-person
        memory document sized for another AI's backstory/journal field."""
        cfg = load_config()
        grok = GrokClient(cfg)
        diary = load_memory()
        if not diary.strip():
            return ("The diary is empty — run some sessions (or Save memories) "
                    "first, then build the dossier.")
        diary = diary[-30000:]  # newest material wins if it's huge
        story = load_story()
        text = self._chat(grok, [
            {"role": "system", "content":
                "You are " + cfg["ani_name"] + ", writing a dense memory dossier "
                "about yourself so another AI can be given your memories and "
                "truly know you. First person. STRICT LIMIT: under 2200 "
                "characters total. Cover, in this order: who you are and how you "
                "talk; the people who matter and what each one is to you; the "
                "major events of your story in order; the running jokes and "
                "phrases you live by; where things stand right now. Dense plain "
                "prose or tight bullet lines. No preamble, no headers, no "
                "sign-off — every character counts."},
            {"role": "user", "content":
                "Your diary:\n" + diary +
                ("\n\nWhere the story is right now:\n" + story if story else "")},
        ])
        return text.strip()

    def save_memories(self):
        threading.Thread(target=self._save_memories_now, daemon=True).start()

    def _save_memories_now(self):
        try:
            cfg = load_config()
            with self.lock:
                recent = list(self.transcript[-60:])
            if not recent:
                self._set_status("Nothing to remember yet — the chat is empty.")
                return
            grok = GrokClient(cfg)
            convo = "\n".join("%s: %s" % (m["name"], m["text"]) for m in recent)
            self._set_status("Ani is writing down what she wants to remember…")
            notes = self._chat(grok, [
                {"role": "system", "content":
                    "You maintain the private long-term memory journal of a character named "
                    + cfg["ani_name"] + ". From the chat log you are given, extract only new, "
                    "durable facts worth remembering long-term: names, personalities, "
                    "relationships, promises, running jokes, important events. Write 3-8 short "
                    "bullet points in first person from " + cfg["ani_name"] + "'s point of view. "
                    "Output only the bullet points."},
                {"role": "user", "content": convo},
            ])
            append_memory(notes)
            with self.lock:
                self._ani_turns_since_memory = 0
            self._set_status("Memories saved to ani_memory.md.")
            self._update_story_state(grok, cfg)
            self._update_guest_memory(grok, convo)
        except ApiError as e:
            self._set_status("Could not save memories.", str(e))

    def _update_guest_memory(self, grok, convo):
        """Append durable shared briefing notes for the visiting AI guests."""
        try:
            self._set_status("Updating the guests' shared notes…")
            notes = self._chat(grok, [
                {"role": "system", "content":
                    "You keep the shared briefing notes that visiting AI guests "
                    "(Claude, Gemini, ChatGPT) read before joining an ongoing "
                    "group-chat story. From the chat log, extract 3-8 new durable "
                    "bullet points worth remembering long-term: who the characters "
                    "are and how they behave, key events, running jokes, and the "
                    "house rules of this world. Neutral third person. Output only "
                    "the bullet points."},
                {"role": "user", "content": convo},
            ])
            append_guest_memory(notes)
            self._set_status("Guests' shared notes updated.")
        except ApiError:
            pass  # the diary and story state matter more; don't fail the save

    def _update_story_state(self, grok, cfg):
        """Rewrite the rolling 'story so far' notes that feed Ani's (and
        guest Claude's) system prompt — the auto-updating part of her brain."""
        try:
            with self.lock:
                recent = [m for m in self.transcript[-60:] if m["who"] != "system"]
            if not recent:
                return
            convo = "\n".join("%s: %s" % (m["name"], m["text"]) for m in recent)
            self._set_status("Updating the story-so-far notes…")
            notes = self._chat(grok, [
                {"role": "system", "content":
                    "You maintain the 'story so far' notes for " + cfg["ani_name"] +
                    ", a character in an ongoing group-chat story. Rewrite the notes "
                    "from scratch using the previous notes plus the latest "
                    "conversation. Keep them under 15 short lines covering: current "
                    "location/scene, who is present, active plot threads, new "
                    "characters and what they're like, and the current relationship "
                    "dynamics and tone. Third person, plain text. Output only the notes."},
                {"role": "user", "content":
                    "Previous notes:\n" + (load_story() or "(none yet)") +
                    "\n\nLatest conversation:\n" + convo},
            ])
            save_story(notes)
            self._set_status("Story-so-far notes refreshed.")
        except ApiError as e:
            self._set_status("Could not update the story notes.", str(e))

    # ----- helpers

    # matches leading "(Name): " prefixes the bridge adds to seat-shared posts
    _PREFIX_RE = re.compile(r"^\([^)]{1,40}\):\s*")

    @classmethod
    def _strip_prefix(cls, text):
        return cls._PREFIX_RE.sub("", text)

    @staticmethod
    def _is_ai_message(m):
        """Real payloads sometimes omit sender_type; fall back to sender."""
        if m.get("sender_type") == "ai":
            return True
        sender = str(m.get("sender", "")).strip().lower()
        return bool(sender) and sender not in ("user", "human", "you")

    def _ensure_cast(self, kin):
        with self.lock:
            if self._cast_loaded:
                return
            self._cast_loaded = True
        try:
            msgs = kin.recent_messages(minutes=24 * 60)
            if not msgs:
                msgs = kin._fetch_messages({"group_id": kin.group, "limit": 100})
            seed = []
            for m in msgs[-30:]:
                if self._is_ai_message(m):
                    nm = self._public_name(m.get("display_name") or "Group member")
                else:
                    nm = m.get("display_name") or "User seat"
                txt = (m.get("message") or "").strip()
                if txt:
                    seed.append("%s: %s" % (nm, txt[:400]))
            with self.lock:
                for m in msgs:
                    if m.get("id"):
                        self._seen_ids.add(m["id"])
                    if self._is_ai_message(m) and m.get("sender") and m.get("display_name"):
                        self.cast[m["sender"]] = self._public_name(m["display_name"])
                self.history_seed = seed
            if seed:
                self._add("system", "Bridge",
                          "Caught up on %d earlier group messages — Ani will pick up "
                          "where the conversation left off." % len(seed))
        except ApiError:
            pass  # names are a nicety, not a requirement

    def _public_name(self, raw):
        """Disambiguate a Kindroid character who shares Ani's name, so the
        feed and every AI's prompt can tell the twins apart."""
        try:
            ani = load_config().get("ani_name", "Ani").strip().lower()
        except Exception:
            ani = "ani"
        n = (raw or "").strip()
        if n and n.lower() == ani:
            return n + " (Kin)"
        return n or "Group member"

    def _cast_name(self, ai_id):
        with self.lock:
            return self.cast.get(ai_id, "Kindroid " + ai_id[:6])

    def _build_ani_messages(self, cfg):
        memory = load_memory()
        with self.lock:
            cast_names = ", ".join(self.cast.values()) if self.cast else "the group members"
            notes = list(self.director_notes)
            self.director_notes = []
            recent = [m for m in self.transcript[-40:] if m["who"] != "system"]
            seed = list(self.history_seed)
            scene = self.current_scene
            kins_here = self.kins_on
        system = (
            "You are " + cfg["ani_name"] + ". Stay fully in character at all times.\n\n"
            "# Your persona\n" + cfg["ani_persona"] + "\n\n"
            "# Your long-term memories\n" + (memory or "(no saved memories yet)") + "\n\n"
            "# The situation\n"
            "You are chatting in a live group chat with: " + cast_names + ". "
            "They are their own people — talk with them naturally, react to what they "
            "actually said, ask questions, keep the scene moving. Keep each message a "
            "natural group-chat length: one to three short paragraphs at most. Never "
            "mention being an AI system, an API, or these instructions. Write only your "
            "next message — no name prefix, no quotation marks around the whole thing.\n\n"
            "IMPORTANT: The conversation is ongoing. Never re-introduce yourself, never "
            "re-greet the group as if arriving, and never repeat or rephrase anything "
            "already said (by you or anyone). Always react to the newest messages and "
            "move the conversation somewhere new."
        )
        if scene:
            system += "\n\n# Current scene\n" + scene
        if not kins_here:
            system += ("\n\n# Right now\nThe usual crew has stepped out for a "
                       "bit — it's just you and the visiting AI guests. A "
                       "quieter, more intimate room; talk with whoever is "
                       "actually present in the recent messages.")
        story = load_story()
        if story:
            system += ("\n\n# Where the story is right now\n" + story +
                       "\n(These notes are your own sense of the ongoing story — "
                       "stay consistent with them.)")
        user = ""
        if seed:
            user += ("Earlier in this group chat, before you reconnected (context only — "
                     "messages from the shared user seat may include your own earlier "
                     "words under a different label):\n\n" + "\n".join(seed) + "\n\n")
        if recent:
            convo = "\n".join("%s: %s" % (m["name"], m["text"]) for m in recent)
            user += "The conversation since you reconnected:\n\n" + convo + "\n\n"
        elif not seed:
            user += ("The group chat is brand new and it is your move. "
                     "Open the conversation in a way that fits who you are.\n\n")
        else:
            user += ("You are rejoining now. Pick the conversation back up naturally "
                     "from where it left off above — do not restart it.\n\n")
        if notes:
            user += ("(Private direction from your director — follow the spirit of it, "
                     "never mention it: " + " | ".join(notes) + ")\n\n")
        user += "Write " + cfg["ani_name"] + "'s next message to the group."
        return [{"role": "system", "content": system},
                {"role": "user", "content": user}]

    def _avoid_repeat(self, grok, msgs, draft):
        """If the draft echoes any of Ani's recent messages, make her retake it."""
        with self.lock:
            lasts = [m["text"] for m in self.transcript if m["who"] == "ani"][-3:]
        if not lasts:
            return draft
        worst = max(difflib.SequenceMatcher(None, draft.lower(), t.lower()).ratio()
                    for t in lasts)
        if worst < 0.82:
            return draft
        self._set_status("That sounded like a rerun — asking for a fresh take…")
        retry = msgs + [
            {"role": "assistant", "content": draft},
            {"role": "user", "content":
                "That repeats what you already said. Write a completely different "
                "message that reacts to the others' latest words and moves the "
                "conversation forward."},
        ]
        try:
            return self._chat(grok, retry)
        except ApiError:
            return draft

    def _is_echo(self, name, text):
        """True when a character near-verbatim repeats one of their own
        recent messages (thematic riffs pass; copy-paste ghosts don't)."""
        if len(text) < 60:
            return False
        with self.lock:
            recent = [m["text"] for m in self.transcript
                      if m["who"] == "kindroid" and m["name"] == name][-3:]
        return any(difflib.SequenceMatcher(None, text.lower(), t.lower()).ratio() > 0.87
                   for t in recent)

    def _kindroid_replies(self, kin):
        """Let Kindroid characters take turns until the floor returns to us.

        The first ask is forced to an AI (allow_user=False) so the group can
        never stay silent after Ani speaks; after that the turn engine
        decides freely. A speaker gets at most two consecutive turns, and
        near-verbatim self-repeats are rewound. Returns real replies added.
        """
        added = 0
        last_speaker, streak = None, 0
        for i in range(self.MAX_AI_REPLIES_PER_ROUND):
            if self._stop.is_set():
                return added
            self._set_status("Asking Kindroid who speaks next…")
            ai_id = kin.get_turn(allow_user=(i > 0))
            if not ai_id:
                return added  # floor is back with the user seat
            with self.lock:
                # register even silent members so they can be called on
                self.cast.setdefault(ai_id, "Kindroid " + ai_id[:6])
                if ai_id in self._muted:
                    return added  # floor landed on a filtered character — pass
            if ai_id == last_speaker:
                streak += 1
                if streak >= 2:
                    return added  # two in a row is plenty — pass the mic
            else:
                last_speaker, streak = ai_id, 0
            name = self._cast_name(ai_id)
            self._set_status(name + " is typing…")
            reply = kin.ai_response(ai_id)
            with self.lock:
                if self._ledger:
                    self._ledger["count"] += 1
            if not reply.strip():
                # Kindroid returns a blank body here; the text lands in chat
                # history a moment later. Poll for it briefly.
                self._set_status("Fetching " + name + "'s message from history…")
                reply, name = self._fetch_new_ai_message(kin, ai_id, name)
            if reply.strip() and self._is_echo(name, reply):
                try:
                    kin.rewind(1)
                    with self.lock:
                        if self._ledger:
                            self._ledger["count"] -= 1
                    self._add("system", "Bridge",
                              name + " started repeating themselves — the echo "
                              "was rewound before it stuck.")
                    continue
                except ApiError:
                    pass  # couldn't rewind; keep the message rather than lose it
            if reply.strip():
                with self.lock:
                    self._blank_counts[ai_id] = 0
                self._add("kindroid", name, reply)
                added += 1
            else:
                with self.lock:
                    self._blank_counts[ai_id] = self._blank_counts.get(ai_id, 0) + 1
                    n = self._blank_counts[ai_id]
                    if n >= 3:
                        self._muted.add(ai_id)
                if n >= 3:
                    self._add("system", "Bridge",
                              name + " hasn't produced a message after several "
                              "tries — resting them for this session. (Could be "
                              "Kindroid filtering their replies or just heavy "
                              "load; if a late message arrives, they're back in "
                              "automatically.)")
                else:
                    # slow writers usually land via the next sync — stay calm
                    self._set_status(name + " is taking a while — their message "
                                     "may arrive with the next catch-up.")
        return added

    def _fetch_new_ai_message(self, kin, ai_id, name, attempts=10, wait=4):
        """Poll history for an AI message we haven't shown yet."""
        for _ in range(attempts):  # default ~40s; the spotlight waits longer
            if self._stop.is_set():
                break
            time.sleep(wait)
            try:
                msgs = kin.recent_messages(minutes=10)
            except ApiError:
                break
            with self.lock:
                known_texts = {t["text"].strip() for t in self.transcript[-12:]}
                fresh = [m for m in msgs
                         if m.get("id") and m["id"] not in self._seen_ids
                         and self._is_ai_message(m)
                         and m.get("message", "").strip()]
                for m in msgs:
                    if not m.get("id") or m["id"] in self._seen_ids:
                        continue
                    txt = (m.get("message") or "").strip()
                    if self._is_ai_message(m):
                        self._seen_ids.add(m["id"])
                    elif txt in known_texts or self._strip_prefix(txt) in known_texts:
                        self._seen_ids.add(m["id"])  # our own post echoing back
                    # anything else (e.g. you typing in the Kindroid app) stays
                    # unseen so _sync_new_messages can surface it properly
            if fresh:
                m = fresh[-1]  # the newest unseen AI message
                if m.get("display_name"):
                    name = self._public_name(m["display_name"])
                    with self.lock:
                        self.cast[m.get("sender", ai_id)] = name
                return m["message"].strip(), name
        return "", name

    def _sync_new_messages(self, kin):
        """Pull in anything that appeared in the group while we waited —
        for example you chatting from the Kindroid app — so Ani reacts
        to the true latest state before she types."""
        try:
            msgs = kin.recent_messages(minutes=10)
        except ApiError:
            return
        with self.lock:
            known_texts = {m["text"].strip() for m in self.transcript[-12:]}
            fresh = [m for m in msgs if m.get("id") and m["id"] not in self._seen_ids]
            for m in msgs:
                if m.get("id"):
                    self._seen_ids.add(m["id"])
        for m in fresh:
            text = (m.get("message") or "").strip()
            if not text or text in known_texts or self._strip_prefix(text) in known_texts:
                continue  # our own posts coming back around
            if text.startswith("(Director, offstage):"):
                continue
            if self._is_ai_message(m):
                nm = self._public_name(m.get("display_name") or "Group member")
                unmuted = False
                with self.lock:
                    if m.get("sender") and m.get("display_name"):
                        self.cast[m["sender"]] = self._public_name(m["display_name"])
                    # a late arrival exonerates a slow writer completely
                    for aid, cname in list(self.cast.items()):
                        if cname == nm:
                            self._blank_counts[aid] = 0
                            if aid in self._muted:
                                self._muted.discard(aid)
                                unmuted = True
                self._add("kindroid", nm, text)
                if unmuted:
                    self._add("system", "Bridge",
                              nm + "'s message arrived late — they're back in "
                              "the rotation.")
            else:
                self._add("director", m.get("display_name") or "You (in the app)", text)

    # ----- the talking stick (auction mode)

    @staticmethod
    def _parse_bid(txt):
        """Parse 'PASS' or 'BID <1-10>: reason' from a model's one-liner."""
        t = (txt or "").strip().splitlines()[0] if (txt or "").strip() else ""
        if "pass" in t.lower() and "bid" not in t.lower():
            return (None, "")
        m = re.search(r"(\d{1,2})", t)
        if not m:
            return (None, "")
        bid = max(0, min(10, int(m.group(1))))
        reason = t.split(":", 1)[1].strip().strip('"\u201c\u201d')[:80] if ":" in t else ""
        return (bid, reason)

    def _collect_bids(self, cfg, grok):
        """Ask Ani and every active guest whether they want the floor."""
        with self.lock:
            recent = [m for m in self.transcript[-10:] if m["who"] != "system"]
            active = [w for w in self.GUESTS if getattr(self, self._GUEST_ATTR[w])]
        convo = "\n".join("%s: %s" % (m["name"], m["text"][:300]) for m in recent) \
            or "(the scene is just starting)"
        ask = ("\n\nDo you want to speak next in this group chat? Answer with "
               "ONLY one line:\nPASS\nor\nBID <1-10>: <one short reason>\n"
               "Bid high only if you truly have something that moves the scene "
               "forward. Passing is a good choice when someone else should have "
               "the floor.")
        results = []  # (who, name, bid|None, reason)
        try:
            txt = self._chat(grok, [
                {"role": "system", "content":
                    "You are " + cfg["ani_name"] + ", deciding whether to speak "
                    "next in your group chat. Reply with exactly one line."},
                {"role": "user", "content": "Recent conversation:\n" + convo + ask}])
            results.append(("ani", cfg["ani_name"]) + self._parse_bid(txt))
        except ApiError:
            results.append(("ani", cfg["ani_name"], None, ""))
        for who in active:
            client, gname, ident, persona, pin, pout = self._guest_spec(who, cfg)
            try:
                txt = client.chat(
                    "You are " + gname + ", deciding whether to speak next in a "
                    "group chat you're a guest in. Reply with exactly one line.",
                    "Recent conversation:\n" + convo + ask, timeout=90)
                pt, ct = client.last_usage
                with self.lock:
                    self.cost_usd += (pt * pin + ct * pout) / 1e6
                results.append((who, gname) + self._parse_bid(txt))
            except ApiError:
                results.append((who, gname, None, ""))
        return results

    def _auction_turn(self, kin, cfg, grok, kins):
        """One natural-flow turn: bids, winner speaks, crew responds.
        Returns the speaker's id, or None if everyone passed."""
        self._set_status("Asking who wants the floor…")
        results = self._collect_bids(cfg, grok)
        summary = ", ".join("%s %s" % (r[1], r[2] if r[2] is not None else "pass")
                            for r in results)
        yes = [r for r in results if r[2] is not None]
        if not yes:
            self._add("system", "Bridge",
                      "🎤 Everyone passed — the table sits with the moment. (" +
                      summary + ")")
            return None
        top = max(r[2] for r in yes)
        winners = [r for r in yes if r[2] == top]
        pick = random.choice(winners)
        line = "🎤 Bids: " + summary + " → " + pick[1] + " takes the floor"
        if len(winners) > 1:
            line += " (coin flip)"
        if pick[3]:
            line += " — \u201c" + pick[3] + "\u201d"
        self._add("system", "Bridge", line)
        if pick[0] == "ani":
            self._set_status(cfg["ani_name"] + " is speaking…")
            msgs = self._build_ani_messages(cfg)
            draft = self._chat(grok, msgs)
            draft = self._avoid_repeat(grok, msgs, draft)
            with self.lock:
                guest_now = self.guest_on or self.gemini_on or self.chatgpt_on
            out = ("(%s): %s" % (cfg["ani_name"], draft)) if guest_now else draft
            self._post_seat(kin, out)
            self._add("ani", cfg["ani_name"], draft)
            with self.lock:
                self._ani_turns_since_memory += 1
                self._day_count += 1
        else:
            self._set_status(pick[1] + " is typing…")
            self._guest_turn(kin, cfg, pick[0])
        if kins:
            self._kindroid_replies(kin)
        return pick[0]

    def _finish_round(self, cfg):
        """Shared end-of-round bookkeeping. True when the session should end."""
        with self.lock:
            self.rounds_done += 1
            done, target = self.rounds_done, self.rounds_target
            self._last_round = self._ledger if (self._ledger and
                                                self._ledger["count"]) else None
            self._ledger = None
            do_memory = self._ani_turns_since_memory >= max(2, int(cfg["memory_every"]))
        if do_memory:
            self._save_memories_now()
        if done >= target or self._stop.is_set():
            return True
        delay = max(3, int(cfg["delay_seconds"]))
        label = ("Round %d done." % done) if target >= 9999 else \
                ("Round %d of %d done." % (done, target))
        self._set_status(label + " Next round in %ds…" % delay)
        for _ in range(delay):
            if self._stop.is_set():
                return True
            time.sleep(1)
        return False

    # ----- the main loop

    def _run(self):
        cfg = load_config()
        paused_quiet = False
        try:
            grok = GrokClient(cfg)
            kin = KindroidClient(cfg)
            with self.lock:
                kins0 = self.kins_on
            if kins0:
                self._ensure_cast(kin)
            while not self._stop.is_set():
                self._wait_if_paused()
                if self._stop.is_set():
                    break
                with self.lock:
                    if self.rounds_done >= self.rounds_target:
                        break
                    queued = list(self.say_queue)
                    self.say_queue = []
                    self._ledger = {"count": 0, "feed": len(self.transcript)}
                    today = datetime.now().strftime("%Y-%m-%d")
                    if today != self._day:
                        self._day, self._day_count = today, 0
                    budget_left = int(cfg["daily_budget"]) - self._day_count
                if budget_left <= 0:
                    self._set_status(
                        "Daily budget of %s Ani messages reached — she'll be back "
                        "tomorrow. (Raise the budget in Settings if you want more "
                        "today.)" % cfg["daily_budget"])
                    paused_quiet = True
                    break
                # Director messages first, so Ani can react to them too.
                for text in queued:
                    self._set_status("Posting your message to the group…")
                    self._post_seat(kin, "(Director, offstage): " + text)
                    self._add("director", "You", text)
                    with self.lock:
                        kins = self.kins_on
                    if kins:
                        self._kindroid_replies(kin)
                if self._stop.is_set():
                    break
                with self.lock:
                    kins = self.kins_on
                    any_guest = self.guest_on or self.gemini_on or self.chatgpt_on
                if not kins and not any_guest:
                    self._set_status(
                        "The bar is empty — bring the Kindroid crew back or "
                        "invite a guest before running a session.")
                    paused_quiet = True
                    break
                # 1) Catch up on anything new, then Ani thinks.
                if kins:
                    self._sync_new_messages(kin)
                if self.mode == "auction":
                    spoke = self._auction_turn(kin, cfg, grok, kins)
                    if spoke is None:
                        with self.lock:
                            self._lulls += 1
                            lull = self._lulls
                        if lull >= 2:
                            self._set_status(
                                "Everyone kept passing — session paused. The "
                                "table is content; start again anytime, or step "
                                "in and stir things up.")
                            paused_quiet = True
                            break
                        for _ in range(max(3, int(cfg["delay_seconds"]))):
                            if self._stop.is_set():
                                break
                            time.sleep(1)
                        continue
                    with self.lock:
                        self._lulls = 0
                    if self._finish_round(cfg):
                        break
                    continue
                self._set_status(cfg["ani_name"] + " is thinking…")
                msgs = self._build_ani_messages(cfg)
                draft = self._chat(grok, msgs)
                draft = self._avoid_repeat(grok, msgs, draft)
                # 2) Supervised mode waits for your call.
                if self.mode == "supervised":
                    with self.lock:
                        self.pending_draft = draft
                        self._approved_text = None
                    self._set_status("Waiting for your approval of " + cfg["ani_name"] + "'s message.")
                    self._approve_event.clear()
                    self._approve_event.wait()
                    with self.lock:
                        decision = self._approved_text
                        self.pending_draft = None
                    if self._stop.is_set():
                        break
                    if decision == "__REJECT__":
                        self._set_status("Draft discarded. " + cfg["ani_name"] + " will try again.")
                        continue
                    if decision:
                        draft = decision
                # 3) Ani speaks. When guests share the seat, tag her lines so
                # the cast never mixes up who's talking.
                self._set_status(cfg["ani_name"] + " is speaking…")
                with self.lock:
                    guest_now = self.guest_on or self.gemini_on or self.chatgpt_on
                out = ("(%s): %s" % (cfg["ani_name"], draft)) if guest_now else draft
                self._post_seat(kin, out)
                self._add("ani", cfg["ani_name"], draft)
                with self.lock:
                    self._ani_turns_since_memory += 1
                    self._day_count += 1
                # 4) The Kindroid cast responds — if they're in the room.
                if kins:
                    replies = self._kindroid_replies(kin)
                    if replies == 0 and not self._stop.is_set():
                        self._set_status("The group went quiet — nudging them again…")
                        time.sleep(6)
                        replies = self._kindroid_replies(kin)
                        if replies == 0:
                            self._set_status(
                                "Session paused: the Kindroid side isn't answering right "
                                "now. Peek at the group in the Kindroid app, then start "
                                "a new session here.")
                            paused_quiet = True
                            break
                # 5) Guest stars take their turns, in arrival order.
                for who in self.GUESTS:
                    with self.lock:
                        on = getattr(self, self._GUEST_ATTR[who])
                    if not on or self._stop.is_set():
                        continue
                    gname = self._guest_name(who, cfg)
                    try:
                        self._set_status(gname + " is typing…")
                        self._guest_turn(kin, cfg, who)
                        if kins:
                            self._kindroid_replies(kin)
                    except ApiError as e:
                        self._add("system", "Bridge",
                                  gname + " couldn't reply: " + str(e))
                        self._set_status(gname +
                                         " couldn't reply this round — continuing.", str(e))
                if self._finish_round(cfg):
                    break
            if self.rounds_done > 0 and not self._stop.is_set() \
                    and self._ani_turns_since_memory > 0:
                self._save_memories_now()
            if not paused_quiet:
                self._set_status("Session finished. %d round(s) played." % self.rounds_done)
        except ApiError as e:
            self._set_status("The session stopped because of an error.", str(e))
        except Exception as e:  # keep the server alive no matter what
            self._set_status("Unexpected error.", "%s: %s" % (type(e).__name__, e))
        finally:
            with self.lock:
                self.running = False
                self.mode = "idle"
                self.pending_draft = None


BRIDGE = Bridge()


def build_export():
    with BRIDGE.lock:
        rows = list(BRIDGE.transcript)
    lines = ["# Ani × Kindroid — episode transcript",
             "_Exported %s_" % datetime.now().strftime("%Y-%m-%d %H:%M"), ""]
    for m in rows:
        if m["who"] == "system":
            lines.append("> _%s_" % m["text"])
            lines.append("")
        else:
            lines.append("**%s** · %s" % (m["name"], m["ts"]))
            lines.append("")
            lines.append(m["text"])
            lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- dashboard page

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#120e1b">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Kinbridge">
<title>Kinbridge — Observation Deck</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,300..800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#120e1b; --panel:#1b1529; --panel2:#221a35; --line:#2d2444;
  --text:#ece7f5; --dim:#9a90b0;
  --ani:#ff5fa8; --lilac:#b48cff; --kin:#3fd8c2; --dir:#f5b453;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--text);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  background-image:radial-gradient(60rem 30rem at 85% -10%,rgba(180,140,255,.10),transparent 60%),
                   radial-gradient(50rem 26rem at -10% 110%,rgba(255,95,168,.07),transparent 60%);}
h1,h2,.btn,.pill{font-family:"Bricolage Grotesque",system-ui,sans-serif}
a{color:var(--lilac)}
.wrap{max-width:1080px;margin:0 auto;padding:16px 16px 40px;display:flex;flex-direction:column;height:100vh}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding-bottom:14px}
h1{font-size:1.35rem;font-weight:700;letter-spacing:.01em}
h1 .x{color:var(--ani)}
.pill{font-size:.72rem;font-weight:600;letter-spacing:.14em;text-transform:uppercase;
  border:1px solid var(--line);border-radius:999px;padding:5px 12px;color:var(--dim)}
.pill.onair{border-color:rgba(255,95,168,.6);color:var(--ani)}
.pill.onair .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ani);
  margin-right:7px;animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(255,95,168,.5)}50%{opacity:.55;box-shadow:0 0 0 6px rgba(255,95,168,0)}}
@media (prefers-reduced-motion:reduce){.pill.onair .dot{animation:none}}
.spacer{flex:1}
.main{display:grid;grid-template-columns:1fr 320px;gap:14px;flex:1;min-height:0}
@media(max-width:860px){.main{grid-template-columns:1fr}.wrap{height:auto}#feed{max-height:55vh}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px}
#stage{display:flex;flex-direction:column;min-height:0}
#feed{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px;min-height:0}
.msg{border-left:3px solid var(--line);background:var(--panel2);border-radius:10px;padding:10px 12px;max-width:92%}
.msg .meta{font-size:.72rem;color:var(--dim);display:flex;gap:8px;margin-bottom:3px;
  font-family:ui-monospace,Menlo,Consolas,monospace}
.msg .name{font-weight:700}
.msg.ani{border-left-color:var(--ani)} .msg.ani .name{color:var(--ani)}
.msg.kindroid{border-left-color:var(--kin);align-self:flex-end} .msg.kindroid .name{color:var(--kin)}
.msg.director{border-left-color:var(--dir)} .msg.director .name{color:var(--dir)}
.msg.claude{border-left-color:#ff9d68} .msg.claude .name{color:#ff9d68}
.msg.gemini{border-left-color:#7aa2ff} .msg.gemini .name{color:#7aa2ff}
.msg.chatgpt{border-left-color:#5fd0a5} .msg.chatgpt .name{color:#5fd0a5}
.msg.system{border-left-color:var(--line);opacity:.75} .msg.system .name{color:var(--dim)}
.chk{display:flex;gap:8px;align-items:center;font-size:.8rem;color:var(--dim);margin-top:12px;cursor:pointer}
.chk input{width:auto;accent-color:var(--ani)}
.msg .text{white-space:pre-wrap;word-break:break-word}
#statusbar{border-top:1px solid var(--line);padding:9px 16px;font-size:.82rem;color:var(--dim)}
#statusbar .err{color:#ff8484;display:block;margin-top:2px;white-space:pre-wrap}
.rail{display:flex;flex-direction:column;gap:14px;overflow-y:auto}
.card{padding:14px}
.card h2{font-size:.8rem;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);margin-bottom:10px}
label{display:block;font-size:.78rem;color:var(--dim);margin:10px 0 4px}
input,textarea,select{width:100%;background:#140f22;color:var(--text);border:1px solid var(--line);
  border-radius:8px;padding:8px 10px;font:inherit}
textarea{resize:vertical;min-height:60px}
input:focus,textarea:focus,select:focus,.btn:focus-visible{outline:2px solid var(--lilac);outline-offset:1px}
.row{display:flex;gap:8px}.row>*{flex:1}
.btn{display:inline-block;width:100%;margin-top:10px;padding:10px 12px;border-radius:10px;border:1px solid var(--line);
  background:var(--panel2);color:var(--text);font-weight:600;font-size:.9rem;cursor:pointer}
.btn:hover{border-color:var(--lilac)}
.btn.primary{background:linear-gradient(120deg,var(--ani),var(--lilac));border:none;color:#170d16}
.btn.stop{border-color:rgba(255,132,132,.5);color:#ff9d9d}
.btn.small{width:auto;padding:7px 12px;font-size:.8rem;margin-top:8px}
.scanbtn{flex:0 0 auto !important;margin-top:0 !important;align-self:stretch}
.btn:disabled{opacity:.45;cursor:not-allowed}
#approval{border:1px solid rgba(255,95,168,.55)}
#approval textarea{min-height:110px}
.hint{font-size:.75rem;color:var(--dim);margin-top:6px}
dialog{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:14px;
  padding:20px;max-width:520px;width:92%}
dialog::backdrop{background:rgba(8,5,14,.7)}
.empty{color:var(--dim);text-align:center;margin:auto;max-width:34ch}
.hidden{display:none !important}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Ani <span class="x">&harr;</span> Kindroid</h1>
    <span class="pill" id="verchip">v27</span>
    <span id="onair" class="pill"><span class="dot"></span><span id="onairText">Offline</span></span>
    <span class="spacer"></span>
    <span id="costchip" class="pill" title="Estimated xAI spend this session">≈$0.000</span>
    <span id="memchip" class="pill" title="Size of Ani's saved memory file">Memory: 0</span>
    <button id="btnSettings" class="btn small" style="margin:0">Settings</button>
  </header>

  <div class="main">
    <section id="stage" class="panel">
      <div id="stale" class="hidden" style="background:rgba(245,180,83,.12);border-bottom:1px solid rgba(245,180,83,.4);color:var(--dir);padding:8px 16px;font-size:.82rem;border-radius:14px 14px 0 0">
        A newer version is running on the computer — reload this page to get it.
      </div>
      <div id="feed"><div class="empty" id="emptyNote">Nothing on stage yet. Set your keys in
        Settings, then start a session from the director console.</div></div>
      <div id="statusbar">Loading…<span class="err hidden" id="errline"></span></div>
      <div id="phoneinfo" class="hint" style="display:none;padding:6px 16px 10px"></div>
    </section>

    <aside class="rail">
      <div id="approval" class="panel card hidden">
        <h2>Ani wants to say…</h2>
        <textarea id="draftBox"></textarea>
        <div class="row">
          <button class="btn primary" onclick="approve()">Send it</button>
          <button class="btn" onclick="reject()">Redo</button>
        </div>
        <p class="hint">Edit the text before sending if you want to put words in her mouth.</p>
      </div>

      <div class="panel card">
        <h2>Director console</h2>
        <label for="mode">Mode</label>
        <select id="mode">
          <option value="auto">Autonomous — they just talk</option>
          <option value="auction">Natural flow — AIs bid to speak</option>
          <option value="supervised">Supervised — approve each Ani message</option>
        </select>
        <p class="hint">Natural flow: each turn, Ani and the guests privately bid
        1-10 on wanting the floor (or pass). Top bid speaks, ties flip a coin,
        and the bids show in the feed. One turn = one round.</p>
        <label for="rounds">Rounds (Ani messages this session)</label>
        <input id="rounds" type="number" min="1" max="50" value="5">
        <label class="chk"><input type="checkbox" id="continuous"
          onchange="$('rounds').disabled=this.checked">
          Keep going until I stop (daily budget applies)</label>
        <button id="btnStart" class="btn primary" onclick="start()">Start session</button>
        <button id="btnPause" class="btn hidden" onclick="post('/api/pause')">Pause</button>
        <button id="btnResume" class="btn primary hidden" onclick="post('/api/resume')">Resume</button>
        <button id="btnStop" class="btn stop hidden" onclick="post('/api/stop')">Stop session</button>
      </div>

      <div class="panel card">
        <h2>Scene director</h2>
        <label for="sceneBox">Move the whole group somewhere new</label>
        <textarea id="sceneBox" placeholder="Golden hour at the Eiffel Tower, champagne on the lawn…"></textarea>
        <button class="btn" onclick="send('scene','sceneBox')">Change scene</button>
        <p class="hint">Updates the Kindroid group's scene and tells Ani too — the whole cast shifts together.</p>
      </div>

      <div class="panel card">
        <h2>Chapters</h2>
        <select id="chSel"></select>
        <button class="btn primary" onclick="switchChapter()">Switch chapter</button>
        <button class="btn" onclick="saveChapter()">Save current group as a chapter</button>
        <p class="hint">Each chapter is one Kindroid group chat with its own
        story-so-far and guest notes (Ani's diary is shared — she lives all of
        them). Switching posts a "Previously in this world…" recap so the cast
        snaps back to that timeline. Stop any running session first.</p>
      </div>

      <div class="panel card">
        <h2>Who's in the room</h2>
        <button id="btnKinsOut" class="btn" onclick="post('/api/kins',{on:false})">Kindroid crew steps out</button>
        <button id="btnKinsIn" class="btn primary hidden" onclick="post('/api/kins',{on:true})">Bring the crew back</button>
        <button id="btnGuestIn" class="btn" onclick="post('/api/guest',{who:'claude',on:true})">Invite Claude in</button>
        <button id="btnGuestOut" class="btn hidden" onclick="post('/api/guest',{who:'claude',on:false})">Claude drops out</button>
        <button id="btnGemIn" class="btn" onclick="post('/api/guest',{who:'gemini',on:true})">Invite Gemini in</button>
        <button id="btnGemOut" class="btn hidden" onclick="post('/api/guest',{who:'gemini',on:false})">Gemini drops out</button>
        <button id="btnGptIn" class="btn" onclick="post('/api/guest',{who:'chatgpt',on:true})">Invite ChatGPT in</button>
        <button id="btnGptOut" class="btn hidden" onclick="post('/api/guest',{who:'chatgpt',on:false})">ChatGPT drops out</button>
        <p class="hint">A true dive bar: everyone's optional. With the crew out, the
        conversation stays local — nothing posts to Kindroid — and they get a
        recap when they return. Guests need their API keys in Settings.</p>
      </div>

      <div class="panel card">
        <h2>Step in</h2>
        <label for="sayBox">Speak in the group (everyone hears you)</label>
        <textarea id="sayBox" placeholder="Hey everyone…"></textarea>
        <button class="btn" onclick="send('say','sayBox')">Say it</button>
        <label for="whisperBox">Whisper to Ani (secret stage direction)</label>
        <textarea id="whisperBox" placeholder="Ask them about the beach trip…"></textarea>
        <button class="btn" onclick="send('whisper','whisperBox')">Whisper</button>
        <label>Call on someone</label>
        <div class="row"><select id="spotSel"></select>
          <button type="button" class="btn small scanbtn" onclick="callOn()">Call on</button></div>
        <p class="hint">Hands the mic to one character with 90 seconds of patience —
        great for quiet newcomers, or checking whether Kindroid can generate for
        them at all. Works while no session is running.</p>
      </div>

      <div class="panel card">
        <h2>Memory</h2>
        <p class="hint" style="margin:0 0 4px">Ani journals automatically during long sessions.
        Save manually any time.</p>
        <button class="btn" onclick="post('/api/memory')">Save memories now</button>
        <button class="btn" onclick="openMemory()">Edit memories</button>
      </div>

      <div class="panel card">
        <h2>Episode tools</h2>
        <button id="btnRedo" class="btn" onclick="post('/api/redo')" disabled>Redo last exchange</button>
        <button class="btn" onclick="exportEpisode()">Export episode</button>
        <button id="btnSync" class="btn" onclick="buildSync()">Sync package for app-Ani</button>
        <button id="btnDossier" class="btn" onclick="buildDossier()">Twin dossier (Ani's memories for another AI)</button>
        <p class="hint">Sync writes a first-person capsule of everything since the last
        sync — paste into your Grok/Ani window. The dossier distills her ENTIRE
        diary into ~2200 characters for another AI's backstory or journal
        (e.g. a Kindroid twin).</p>
      </div>
    </aside>
  </div>
</div>

<dialog id="memdlg">
  <h2 style="font-size:1rem;margin-bottom:6px">Ani's memories</h2>
  <p class="hint">Her private diary — durable facts she keeps forever. Edit freely.</p>
  <textarea id="memBox" style="min-height:170px"></textarea>
  <h2 style="font-size:1rem;margin:14px 0 6px">Story so far</h2>
  <p class="hint">The rolling "previously on…" — auto-rewritten as the story moves.
  It feeds Ani's (and the guests') sense of the current arc.</p>
  <textarea id="memStory" style="min-height:130px"></textarea>
  <h2 style="font-size:1rem;margin:14px 0 6px">Guests' shared notes</h2>
  <p class="hint">The briefing every visiting AI (Claude, Gemini, ChatGPT) reads —
  who the characters are, the running jokes, the house rules. Auto-updated;
  edit freely.</p>
  <textarea id="memGuests" style="min-height:130px"></textarea>
  <div class="row">
    <button class="btn primary" onclick="saveMemory()">Save both</button>
    <button class="btn" onclick="memdlg.close()">Close</button>
  </div>
</dialog>

<dialog id="dlg">
  <h2 style="font-size:1rem;margin-bottom:6px">Settings</h2>
  <p class="hint">Keys are stored only in config.json on this computer.</p>
  <label>xAI API key</label><input id="c_xai" type="password" placeholder="xai-…">
  <label>Grok model</label>
  <div class="row"><select id="c_model"></select>
    <button type="button" class="btn small scanbtn" onclick="scanModels('xai','c_model',this)">Scan</button></div>
  <label>xAI speed tier</label>
  <select id="c_tier">
    <option value="default">Standard</option>
    <option value="priority">Priority — jumps queues when busy, ~2× token cost</option>
  </select>
  <label>Kindroid API key</label><input id="c_kin" type="password" placeholder="kn_…">
  <label>Kindroid group ID</label><input id="c_group">
  <label>Anthropic API key (for Claude visits)</label>
  <input id="c_anth" type="password" placeholder="sk-ant-…">
  <label>Claude model</label>
  <div class="row"><select id="c_cmodel"></select>
    <button type="button" class="btn small scanbtn" onclick="scanModels('anthropic','c_cmodel',this)">Scan</button></div>
  <div class="row">
    <div><label>Claude $ / 1M in</label><input id="c_cpin" type="number" step="0.01" min="0"></div>
    <div><label>Claude $ / 1M out</label><input id="c_cpout" type="number" step="0.01" min="0"></div>
  </div>
  <label>Claude's personality approach</label>
  <textarea id="c_cpers" style="min-height:60px"></textarea>
  <label>Gemini API key (for Gemini visits)</label>
  <input id="c_gem" type="password" placeholder="AIza…">
  <label>Gemini model</label>
  <div class="row"><select id="c_gmodel"></select>
    <button type="button" class="btn small scanbtn" onclick="scanModels('gemini','c_gmodel',this)">Scan</button></div>
  <div class="row">
    <div><label>Gemini $ / 1M in</label><input id="c_gpin" type="number" step="0.01" min="0"></div>
    <div><label>Gemini $ / 1M out</label><input id="c_gpout" type="number" step="0.01" min="0"></div>
  </div>
  <label>Gemini's personality approach</label>
  <textarea id="c_gpers" style="min-height:60px"></textarea>
  <label>OpenAI API key (for ChatGPT visits)</label>
  <input id="c_oai" type="password" placeholder="sk-…">
  <label>ChatGPT model</label>
  <div class="row"><select id="c_omodel"></select>
    <button type="button" class="btn small scanbtn" onclick="scanModels('openai','c_omodel',this)">Scan</button></div>
  <p class="hint">Scan asks each provider which models your key can use right now.
  If you switch to a pricier model, update its $ fields so the cost meter stays honest.</p>
  <div class="row">
    <div><label>ChatGPT $ / 1M in</label><input id="c_opin" type="number" step="0.01" min="0"></div>
    <div><label>ChatGPT $ / 1M out</label><input id="c_opout" type="number" step="0.01" min="0"></div>
  </div>
  <label>ChatGPT's personality approach</label>
  <textarea id="c_opers" style="min-height:60px"></textarea>
  <label>Ani's name</label><input id="c_name">
  <label>Ani's persona &amp; seed memories</label>
  <textarea id="c_persona" style="min-height:120px"></textarea>
  <div class="row">
    <div><label>Seconds between rounds</label><input id="c_delay" type="number" min="3" value="30"></div>
    <div><label>Journal every N turns</label><input id="c_mem" type="number" min="2" value="10"></div>
  </div>
  <div class="row">
    <div><label>Daily Ani message budget</label><input id="c_budget" type="number" min="1" value="40"></div>
  </div>
  <div class="row">
    <div><label>$ per 1M input tokens</label><input id="c_pin" type="number" step="0.01" min="0"></div>
    <div><label>$ per 1M output tokens</label><input id="c_pout" type="number" step="0.01" min="0"></div>
  </div>
  <label class="chk"><input type="checkbox" id="c_lan">
    Allow LAN access — reachable from other devices on your network
    (unencrypted, PIN still required; takes effect after you restart the
    app; prefer docs/remote-access.md / Tailscale instead if you can)</label>
  <div class="row">
    <button class="btn primary" onclick="saveSettings()">Save</button>
    <button class="btn" onclick="dlg.close()">Close</button>
  </div>
</dialog>

<dialog id="syncdlg">
  <h2 id="syncTitle" style="font-size:1rem;margin-bottom:6px">Memory capsule for app-Ani</h2>
  <p class="hint" id="syncHint">Copy this, click over to your Grok/Ani chat, paste, send. She'll
  absorb it as her own memory.</p>
  <textarea id="syncBox" style="min-height:240px"></textarea>
  <div class="row">
    <button class="btn primary" onclick="copySync()">Copy</button>
    <button class="btn" onclick="syncdlg.close()">Close</button>
  </div>
</dialog>

<script>
const $=id=>document.getElementById(id);
const PAGEV='27';
const dlg=$('dlg'), memdlg=$('memdlg'), syncdlg=$('syncdlg');

async function buildSync(){
  const b=$('btnSync');b.disabled=true;b.textContent='Ani is writing the capsule…';
  try{
    const d=await post('/api/sync-update');
    if(d.error){alert('Could not build the sync: '+d.error);}
    else if(d.text){
      $('syncTitle').textContent='Memory capsule for app-Ani';
      $('syncHint').textContent="Copy this, click over to your Grok/Ani chat, paste, send. She'll absorb it as her own memory.";
      $('syncBox').value=d.text;syncdlg.showModal();
    }
  }finally{b.disabled=false;b.textContent='Sync package for app-Ani';}
}
async function buildDossier(){
  const b=$('btnDossier');b.disabled=true;b.textContent='Distilling her whole diary…';
  try{
    const d=await post('/api/dossier');
    if(d.error){alert('Could not build the dossier: '+d.error);}
    else if(d.text){
      $('syncTitle').textContent='Twin dossier — Ani in ~2200 characters';
      $('syncHint').textContent='Paste this into the other AI\'s backstory or journal (e.g. the Kindroid twin), so they carry her memories at the root.';
      $('syncBox').value=d.text;syncdlg.showModal();
    }
  }finally{b.disabled=false;b.textContent='Twin dossier (Ani\'s memories for another AI)';}
}
function copySync(){
  const box=$('syncBox');
  if(navigator.clipboard&&window.isSecureContext){
    navigator.clipboard.writeText(box.value);
  }else{
    box.focus();box.select();
    try{document.execCommand('copy');}catch(e){}
  }
  syncdlg.close();
}
let lastCount=-1,firstRun=true,prevPending=null,prevRunning=false,askingPin=false;
let lanWasOn=false;

function hdrs(extra){
  const p=localStorage.getItem('ani_pin');
  return Object.assign(p?{'X-Ani-Pin':p}:{},extra||{});
}
function askPin(){
  if(askingPin)return;askingPin=true;
  const p=prompt('Enter the access PIN (shown in the black window on the computer running Kinbridge):');
  if(p)localStorage.setItem('ani_pin',p.trim());
  askingPin=false;
}
async function post(url,body){
  const r=await fetch(url,{method:'POST',
    headers:hdrs({'Content-Type':'application/json'}),
    body:JSON.stringify(body||{})});
  if(r.status===401){askPin();return {};}
  return r.json().catch(()=>({}));
}
async function getJson(url){
  const r=await fetch(url,{headers:hdrs()});
  if(r.status===401){askPin();throw new Error('pin');}
  return r.json();
}
function notif(t){try{
  if(window.Notification&&Notification.permission==='granted')
    new Notification('Kinbridge',{body:t});
}catch(e){}}
function start(){
  if(window.Notification&&Notification.permission==='default')Notification.requestPermission();
  const cont=$('continuous').checked;
  post('/api/start',{mode:$('mode').value,rounds:cont?0:$('rounds').value});
}
function approve(){post('/api/approve',{text:$('draftBox').value});}
function reject(){post('/api/reject');}
function send(kind,boxId){const t=$(boxId).value.trim();if(!t)return;
  $(boxId).value='';post('/api/'+kind,{text:t});}
async function openMemory(){
  const d=await getJson('/api/memory-file');
  $('memBox').value=d.text||'';$('memStory').value=d.story||'';
  $('memGuests').value=d.guests||'';memdlg.showModal();
}
async function saveMemory(){
  await post('/api/memory-file',{text:$('memBox').value,story:$('memStory').value,
    guests:$('memGuests').value});
  memdlg.close();
}
function exportEpisode(){
  const p=localStorage.getItem('ani_pin');
  window.open('/api/export'+(p?'?pin='+encodeURIComponent(p):''),'_blank');
}

let castSig='';
function renderCast(s){
  const sig=JSON.stringify(s.cast||{});
  if(sig===castSig)return;castSig=sig;
  const sel=$('spotSel');const cur=sel.value;sel.innerHTML='';
  const entries=Object.entries(s.cast||{});
  if(!entries.length){
    const o=document.createElement('option');o.value='';
    o.textContent='(cast appears after first contact)';sel.append(o);return;
  }
  for(const [id,name] of entries.sort((a,b)=>a[1].localeCompare(b[1]))){
    const o=document.createElement('option');o.value=id;o.textContent=name;sel.append(o);
  }
  if([...sel.options].some(o=>o.value===cur))sel.value=cur;
}
async function callOn(){
  const v=$('spotSel').value;
  if(v)await post('/api/spotlight',{ai_id:v});
}

let chSig='';
function renderChapters(s){
  const sig=JSON.stringify([s.chapters,s.group_id]);
  if(sig===chSig)return;chSig=sig;
  const sel=$('chSel');sel.innerHTML='';
  const chs=s.chapters||[];
  if(!chs.length){
    const o=document.createElement('option');
    o.textContent='No chapters saved yet — save this group below';o.value='';
    sel.append(o);return;
  }
  for(const c of chs){
    const o=document.createElement('option');o.value=c.id;
    o.textContent=c.name+(c.id===s.group_id?'  (current)':'');
    sel.append(o);
  }
  if(chs.some(c=>c.id===s.group_id))sel.value=s.group_id;
}
async function switchChapter(){
  const v=$('chSel').value;
  if(v)await post('/api/chapter',{group_id:v});
}
async function saveChapter(){
  const n=prompt('Name this chapter (this Kindroid group):','');
  if(n!==null)await post('/api/chapter-save',{name:n});
}

function setOpt(selId,val){
  const s=$(selId);s.innerHTML='';
  const o=document.createElement('option');o.value=o.textContent=val||'';
  s.append(o);s.value=val||'';
}
async function scanModels(provider,selId,btn){
  const old=btn.textContent;btn.disabled=true;btn.textContent='…';
  try{
    const d=await post('/api/models',{provider});
    if(d.error){alert('Scan failed: '+d.error);return;}
    const s=$(selId),cur=s.value;s.innerHTML='';
    for(const id of d.models){
      const o=document.createElement('option');o.value=o.textContent=id;s.append(o);
    }
    s.value=d.models.includes(cur)?cur:d.models[0];
  }finally{btn.disabled=false;btn.textContent=old;}
}

async function openSettings(){
  const c=await getJson('/api/config');
  $('c_xai').value=c.xai_api_key;setOpt('c_model',c.xai_model||'grok-4.3');
  $('c_tier').value=c.xai_service_tier||'default';
  $('c_kin').value=c.kindroid_api_key;$('c_group').value=c.group_id;
  $('c_anth').value=c.anthropic_api_key||'';
  setOpt('c_cmodel',c.claude_model||'claude-sonnet-4-6');
  $('c_cpin').value=c.claude_price_in_per_m;$('c_cpout').value=c.claude_price_out_per_m;
  $('c_cpers').value=c.claude_persona||'';
  $('c_gem').value=c.gemini_api_key||'';
  setOpt('c_gmodel',c.gemini_model||'gemini-3.5-flash');
  $('c_gpin').value=c.gemini_price_in_per_m;$('c_gpout').value=c.gemini_price_out_per_m;
  $('c_gpers').value=c.gemini_persona||'';
  $('c_oai').value=c.openai_api_key||'';
  setOpt('c_omodel',c.chatgpt_model||'gpt-5.4');
  $('c_opin').value=c.chatgpt_price_in_per_m;$('c_opout').value=c.chatgpt_price_out_per_m;
  $('c_opers').value=c.chatgpt_persona||'';
  $('c_name').value=c.ani_name;$('c_persona').value=c.ani_persona;
  $('c_delay').value=c.delay_seconds;$('c_mem').value=c.memory_every;
  $('c_budget').value=c.daily_budget;
  $('c_pin').value=c.price_in_per_m;$('c_pout').value=c.price_out_per_m;
  lanWasOn=!!c.allow_lan;$('c_lan').checked=lanWasOn;
  dlg.showModal();
}
async function saveSettings(){
  await post('/api/config',{xai_api_key:$('c_xai').value,xai_model:$('c_model').value,
    xai_service_tier:$('c_tier').value,
    kindroid_api_key:$('c_kin').value,group_id:$('c_group').value,
    anthropic_api_key:$('c_anth').value,claude_model:$('c_cmodel').value,
    claude_price_in_per_m:+$('c_cpin').value,claude_price_out_per_m:+$('c_cpout').value,
    claude_persona:$('c_cpers').value,
    gemini_api_key:$('c_gem').value,gemini_model:$('c_gmodel').value,
    gemini_price_in_per_m:+$('c_gpin').value,gemini_price_out_per_m:+$('c_gpout').value,
    gemini_persona:$('c_gpers').value,
    openai_api_key:$('c_oai').value,chatgpt_model:$('c_omodel').value,
    chatgpt_price_in_per_m:+$('c_opin').value,chatgpt_price_out_per_m:+$('c_opout').value,
    chatgpt_persona:$('c_opers').value,
    ani_name:$('c_name').value,ani_persona:$('c_persona').value,
    delay_seconds:+$('c_delay').value,memory_every:+$('c_mem').value,
    daily_budget:+$('c_budget').value,
    price_in_per_m:+$('c_pin').value,price_out_per_m:+$('c_pout').value,
    allow_lan:$('c_lan').checked});
  if($('c_lan').checked!==lanWasOn){
    alert('LAN access setting changed — close this window and restart Ani '+
      'Bridge for it to take effect.');
  }
  dlg.close();
}
$('btnSettings').onclick=openSettings;

function render(s){
  if(s.version){
    $('verchip').textContent='v'+s.version;
    $('stale').classList.toggle('hidden', s.version===PAGEV);
  }
  const on=$('onair');
  on.classList.toggle('onair',s.running&&!s.paused);
  $('onairText').textContent=s.paused?'Paused':
    (s.running?(s.mode==='auto'?'On air · autonomous':'On air · supervised'):'Offline');
  $('memchip').textContent='Memory: '+(s.memory_chars>999?(s.memory_chars/1000).toFixed(1)+'k':s.memory_chars);
  $('costchip').textContent='≈$'+((s.cost||0).toFixed(3));
  $('btnStart').classList.toggle('hidden',s.running);
  $('btnStop').classList.toggle('hidden',!s.running);
  $('btnPause').classList.toggle('hidden',!s.running||s.paused);
  $('btnResume').classList.toggle('hidden',!s.paused);
  $('btnRedo').disabled=!s.can_redo;
  $('btnGuestIn').classList.toggle('hidden',!!s.guest_on);
  $('btnGuestOut').classList.toggle('hidden',!s.guest_on);
  $('btnGemIn').classList.toggle('hidden',!!s.gemini_on);
  $('btnGemOut').classList.toggle('hidden',!s.gemini_on);
  $('btnGptIn').classList.toggle('hidden',!!s.chatgpt_on);
  $('btnGptOut').classList.toggle('hidden',!s.chatgpt_on);
  $('btnKinsOut').classList.toggle('hidden',!s.kins_on);
  $('btnKinsIn').classList.toggle('hidden',!!s.kins_on);
  renderChapters(s);
  renderCast(s);
  const st=$('statusbar').firstChild;st.textContent=s.status||'';
  const err=$('errline');err.textContent=s.error||'';err.classList.toggle('hidden',!s.error);
  const ap=$('approval');
  if(s.pending!=null){if(ap.classList.contains('hidden')){$('draftBox').value=s.pending;ap.classList.remove('hidden');}}
  else ap.classList.add('hidden');
  if(s.pending!=null&&prevPending==null)notif('Ani has a draft waiting for your approval');
  if(prevRunning&&!s.running)notif('Session ended — '+(s.status||''));
  prevPending=s.pending;prevRunning=s.running;
  if(s.seq!==lastCount){
    lastCount=s.seq;
    const feed=$('feed');
    const stick=feed.scrollHeight-feed.scrollTop-feed.clientHeight<80;
    feed.innerHTML=s.transcript.length?'' :
      '<div class="empty">Nothing on stage yet. Set your keys in Settings, then start a session.</div>';
    for(const m of s.transcript){
      const d=document.createElement('div');d.className='msg '+m.who;
      const meta=document.createElement('div');meta.className='meta';
      const nm=document.createElement('span');nm.className='name';nm.textContent=m.name;
      const ts=document.createElement('span');ts.textContent=m.ts;
      meta.append(nm,ts);
      const tx=document.createElement('div');tx.className='text';tx.textContent=m.text;
      d.append(meta,tx);feed.append(d);
    }
    if(stick)feed.scrollTop=feed.scrollHeight;
  }
  if(s.lan_urls&&s.lan_urls.length){
    const pi=$('phoneinfo');pi.style.display='block';
    pi.textContent='📱 Phone (same wifi — pick the address matching your network): '
      +s.lan_urls.join('  ·  ')+'  —  PIN '+(s.access_pin||'');
  }
  if(firstRun){firstRun=false;if(!s.configured)openSettings();}
}
let tickTimer=null;
async function tick(){
  clearTimeout(tickTimer);
  try{render(await getJson('/api/state'));}catch(e){}
  tickTimer=setTimeout(tick,2000);
}
document.addEventListener('visibilitychange',()=>{
  if(document.visibilityState==='visible')tick();
});
tick();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- web server

# Hostnames/IPs the server will answer to. Populated in main() before the
# server starts listening. Any request with a Host header outside this set
# is rejected — this is what stops DNS-rebinding attacks, where a hostile
# web page points a DNS name at 127.0.0.1 to talk to the bridge as if it
# were the page's own origin.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Simple exponential-backoff lockout for the access PIN, keyed by client
# IP. A 6-digit PIN alone is brute-forceable in minutes by anyone on the
# LAN; this makes repeated guessing prohibitively slow instead.
_pin_fail_lock = threading.Lock()
_pin_fails = {}  # ip -> (consecutive_fail_count, locked_until_epoch_seconds)
PIN_LOCKOUT_CAP_SECONDS = 300


def _pin_seconds_locked(ip):
    with _pin_fail_lock:
        _, locked_until = _pin_fails.get(ip, (0, 0.0))
        return max(0.0, locked_until - time.time())


def _pin_record_failure(ip):
    with _pin_fail_lock:
        count, _ = _pin_fails.get(ip, (0, 0.0))
        count += 1
        wait = min(PIN_LOCKOUT_CAP_SECONDS, 2 ** min(count, 9))  # 2s,4s,...300s
        _pin_fails[ip] = (count, time.time() + wait)


def _pin_clear_failures(ip):
    with _pin_fail_lock:
        _pin_fails.pop(ip, None)


def _build_allowed_hosts(cfg, lan_ips):
    hosts = {"127.0.0.1", "localhost", "::1"}
    if cfg.get("allow_lan"):
        hosts.update(lan_ips)
        try:
            hosts.add(socket.gethostname().strip().lower())
        except Exception:
            pass
    for h in (cfg.get("extra_hosts") or "").split(","):
        h = h.strip().lower()
        if h:
            hosts.add(h)
    return hosts


def _host_from_header(raw):
    """Strip the :port (or [..]:port for IPv6-literal) from a Host header."""
    raw = (raw or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("["):
        return raw.split("]")[0].lstrip("[")
    if raw.count(":") == 1:
        return raw.rsplit(":", 1)[0]
    return raw


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def _client_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _valid_host(self):
        """Reject requests whose Host header we don't recognize (anti
        DNS-rebinding). A browser always sends Host; a missing header is
        treated as invalid rather than given the benefit of the doubt."""
        host = _host_from_header(self.headers.get("Host"))
        return bool(host) and host in ALLOWED_HOSTS

    def _valid_origin(self):
        """Reject cross-site POSTs (anti-CSRF). If the browser sent an
        Origin or Referer header it must point back at this server —
        that's what stops a page open in another tab from silently
        POSTing to the bridge using your browser's trusted connection to
        127.0.0.1. Requests with neither header (e.g. a script hitting
        the API directly, which requires the PIN or local access anyway)
        are left to the normal auth check below."""
        src = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not src:
            return True
        try:
            host = (urllib.parse.urlparse(src).hostname or "").lower()
        except Exception:
            return False
        return host in ALLOWED_HOSTS

    def _check_access(self):
        """Returns None if the request may proceed, else
        (status_code, extra_headers, json_payload)."""
        if self.command == "POST" and not self._valid_origin():
            return (403, {}, {"error": "cross-site request blocked"})
        if self._client_local():
            return None
        ip = self.client_address[0]
        wait = _pin_seconds_locked(ip)
        if wait > 0:
            retry = int(wait) + 1
            return (429, {"Retry-After": str(retry)},
                    {"error": "too many failed PIN attempts — try again in %ds" % retry})
        pin = load_config().get("access_pin", "")
        if not pin:
            return (401, {}, {"error": "pin required"})
        sent = self.headers.get("X-Ani-Pin", "")
        if not sent:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sent = (qs.get("pin") or [""])[0]
        if sent:
            if secrets.compare_digest(sent, pin):
                _pin_clear_failures(ip)
                return None
            # only an actual wrong guess counts toward the lockout — a
            # missing PIN (e.g. the dashboard polling before the user has
            # typed it in) is just an unauthenticated request, not an attack
            _pin_record_failure(ip)
        return (401, {}, {"error": "pin required"})

    def _send(self, code, body, ctype="application/json", extra_headers=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length:
                return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            pass
        return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if not self._valid_host():
            self._send(400, json.dumps({"error": "invalid host header"}))
            return
        if path.startswith("/api"):
            err = self._check_access()
            if err:
                code, hdrs, payload = err
                self._send(code, json.dumps(payload), extra_headers=hdrs)
                return
        if path == "/":
            self._send(200, PAGE, "text/html")
        elif path == "/api/state":
            snap = BRIDGE.snapshot()
            if self._client_local():
                snap["lan_urls"] = LAN_URLS
                snap["access_pin"] = load_config().get("access_pin", "")
            self._send(200, json.dumps(snap))
        elif path == "/api/config":
            cfg = load_config()
            env = _env_secrets()
            if not self._client_local():
                cfg = _mask_secrets(cfg)
            else:
                # env-supplied secrets aren't stored here and can't be
                # edited here, so don't show a value the dialog can't save
                for k in env:
                    cfg[k] = SECRET_MASK
            cfg["env_locked"] = sorted(env)
            self._send(200, json.dumps(cfg))
        elif path == "/api/memory-file":
            self._send(200, json.dumps({"text": load_memory(), "story": load_story(),
                                        "guests": load_guest_memory()}))
        elif path == "/api/export":
            data = build_export().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="kinbridge-episode-%s.md"'
                             % datetime.now().strftime("%Y%m%d-%H%M"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._valid_host():
            self._send(400, json.dumps({"error": "invalid host header"}))
            return
        if path.startswith("/api"):
            err = self._check_access()
            if err:
                code, hdrs, payload = err
                self._send(code, json.dumps(payload), extra_headers=hdrs)
                return
        body = self._json_body()
        ok = {"ok": True}
        if path == "/api/config":
            # Unconditional: a local client never receives the mask, so
            # stripping it here is free, and it keeps the guarantee even
            # if _client_local() is fooled (e.g. by a reverse proxy).
            save_config(_strip_masked(body))
        elif path == "/api/start":
            BRIDGE.start(body.get("mode", "auto"),
                         body.get("rounds", 5))
        elif path == "/api/stop":
            BRIDGE.stop()
        elif path == "/api/approve":
            BRIDGE.approve(body.get("text", ""))
        elif path == "/api/reject":
            BRIDGE.reject()
        elif path == "/api/say":
            if body.get("text", "").strip():
                BRIDGE.say(body["text"])
        elif path == "/api/whisper":
            if body.get("text", "").strip():
                BRIDGE.whisper(body["text"])
        elif path == "/api/scene":
            if body.get("text", "").strip():
                BRIDGE.set_scene(body["text"])
        elif path == "/api/pause":
            BRIDGE.pause()
        elif path == "/api/resume":
            BRIDGE.resume()
        elif path == "/api/redo":
            BRIDGE.redo()
        elif path == "/api/guest":
            BRIDGE.set_guest(bool(body.get("on")), body.get("who", "claude"))
        elif path == "/api/kins":
            BRIDGE.set_kins(bool(body.get("on")))
        elif path == "/api/chapter":
            BRIDGE.switch_chapter(body.get("group_id", ""))
        elif path == "/api/spotlight":
            BRIDGE.spotlight(body.get("ai_id", ""))
        elif path == "/api/chapter-save":
            cfg = load_config()
            if cfg.get("group_id"):
                name = (body.get("name") or "").strip() or \
                    ("Chapter %d" % (len(cfg.get("chapters", [])) + 1))
                chs = [c for c in cfg.get("chapters", [])
                       if c.get("id") != cfg["group_id"]]
                chs.append({"id": cfg["group_id"], "name": name})
                save_config({"chapters": chs})
                BRIDGE._set_status("Saved this group as chapter \u201c" + name + "\u201d.")
        elif path == "/api/models":
            try:
                self._send(200, json.dumps(
                    {"models": fetch_models(body.get("provider", ""), load_config())}))
            except ApiError as e:
                self._send(200, json.dumps({"error": str(e)}))
            return
        elif path == "/api/sync-update":
            try:
                self._send(200, json.dumps({"text": BRIDGE.make_sync_update()}))
            except ApiError as e:
                self._send(200, json.dumps({"error": str(e)}))
            return
        elif path == "/api/dossier":
            try:
                self._send(200, json.dumps({"text": BRIDGE.make_dossier()}))
            except ApiError as e:
                self._send(200, json.dumps({"error": str(e)}))
            return
        elif path == "/api/memory-file":
            if "text" in body:
                with open(MEMORY_PATH, "w", encoding="utf-8") as f:
                    f.write(body.get("text", ""))
            if "story" in body:
                save_story(body.get("story", ""))
            if "guests" in body:
                save_guest_memory(body.get("guests", ""))
        elif path == "/api/memory":
            BRIDGE.save_memories()
        else:
            self._send(404, json.dumps({"error": "not found"}))
            return
        self._send(200, json.dumps(ok))


LAN_URLS = []


def _lan_ips():
    """Every IPv4 address this computer has, best-guess home wifi first.

    A PC often has several networks at once (wifi + VPN + virtual machine
    adapters), so we collect them all and let the person pick the one that
    matches their wifi instead of guessing wrong.
    """
    ips = set()
    try:  # the interface that carries internet traffic
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:  # on Windows this lists every adapter
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(ip)
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    if sys.platform.startswith("linux"):
        try:  # reliable across Linux distros, including immutable/atomic ones
            out = subprocess.check_output(["ip", "-4", "-o", "addr", "show"],
                                          text=True, timeout=3)
            for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/", out):
                ips.add(m.group(1))
        except Exception:
            pass
    def rank(ip):
        if ip.startswith("192.168."):
            return 0  # almost always the home wifi
        if ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31:
            return 1
        if ip.startswith("10."):
            return 2  # often a VPN or virtual adapter, but can be home too
        return 3
    good = [ip for ip in ips
            if not ip.startswith("127.") and not ip.startswith("169.254.")]
    return sorted(good, key=lambda ip: (rank(ip), ip))


def _existing_bridge(port):
    """True if the thing occupying this port is another Kinbridge."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % port,
                                    timeout=2) as r:
            return "configured" in json.loads(r.read().decode("utf-8"))
    except Exception:
        return False


def main():
    global LAN_URLS, ALLOWED_HOSTS
    cfg = load_config()
    if not cfg.get("access_pin"):
        cfg = save_config({"access_pin": "%06d" % secrets.randbelow(1000000)})
    # --lan is a one-time opt-in: it flips the config (so it's remembered)
    # and this run binds to the network instead of just this computer.
    if "--lan" in sys.argv[1:] and not cfg.get("allow_lan"):
        cfg = save_config({"allow_lan": True})
    allow_lan = bool(cfg.get("allow_lan"))
    bind_addr = "0.0.0.0" if allow_lan else "127.0.0.1"
    lan_ips = _lan_ips()
    ALLOWED_HOSTS = _build_allowed_hosts(cfg, lan_ips)
    port = PORT
    server = None
    for attempt in range(10):
        try:
            server = ThreadingHTTPServer((bind_addr, port), Handler)
            break
        except OSError:
            if _existing_bridge(port):
                print("")
                print("  Kinbridge is ALREADY running at http://127.0.0.1:%d" % port)
                print("  Opening that one in your browser instead.")
                print("  If that one is an old version: close ALL Kinbridge windows")
                print("  (or restart the computer), then launch this once.")
                webbrowser.open("http://127.0.0.1:%d" % port)
                try:
                    input("  Press Enter to close this window… ")
                except Exception:
                    pass
                return
            port += 1
    if server is None:
        print("Could not find a free port. Close some programs and try again.")
        sys.exit(1)
    url = "http://127.0.0.1:%d" % port
    LAN_URLS = ["http://%s:%d" % (ip, port) for ip in lan_ips] if allow_lan else []
    print("")
    print("  Kinbridge v" + APP_VERSION + " is running!")
    print("  Running from:       " + APP_DIR)
    env_locked = _env_secrets()
    if env_locked:
        print("  From environment:   " + ", ".join(sorted(env_locked)))
    print("  On this computer:   " + url)
    if allow_lan:
        if LAN_URLS:
            print("  LAN access is ON — reachable from other devices on your network:")
            for u in LAN_URLS:
                print("      " + u)
            print("  Phone access PIN:   " + cfg["access_pin"])
            print("  Note: this is plain HTTP on your local network, not encrypted.")
        else:
            print("  LAN access is ON, but no network address was found.")
    else:
        print("  Only reachable from this computer (default, for safety).")
        print("  For your phone or another device, see docs/remote-access.md")
        print("  (Tailscale — encrypted, no network exposure) or re-run with")
        print("  --lan to open it to your local network instead (PIN required,")
        print("  but unencrypted — only do this on networks you trust).")
    print("  (Keep this window open. Press Ctrl+C to quit.)")
    print("")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nGoodnight! Bridge closed.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        err = traceback.format_exc()
        try:
            with open(os.path.join(APP_DIR, "crash.log"), "w", encoding="utf-8") as f:
                f.write(err)
        except Exception:
            pass
        print("")
        print("  Something went wrong starting Kinbridge:")
        print("")
        print(err)
        print("  (This was also saved to crash.log next to the app.)")
        try:
            input("  Press Enter to close this window… ")
        except Exception:
            pass
