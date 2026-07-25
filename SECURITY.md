# Security

Kinbridge runs a small local web server on your own computer so you can
watch and direct the conversation from a browser (including your phone).
It also holds several third-party API keys. Here's the actual threat
model and what the app does about it.

## What the server protects against

- **Anyone on your LAN reading or controlling the bridge.** Access from
  any device other than the computer running the app requires the access
  PIN shown at startup (and in Settings). PIN comparisons use a
  constant-time check.
- **Brute-forcing the PIN.** Repeated failed PIN attempts from the same
  IP are throttled with exponential backoff (capped at 5 minutes between
  attempts), so guessing a 6-digit PIN is no longer a minutes-long job.
- **DNS rebinding.** The server checks the HTTP `Host` header against an
  allowlist (`127.0.0.1`, `localhost`, and — only if you've explicitly
  turned on LAN access — this machine's LAN IPs and any hostnames you add
  to `extra_hosts` in `config.json`). This stops a malicious web page from
  pointing a DNS name at your loopback address to talk to the bridge as
  if it were a trusted origin.
- **Cross-site request forgery (CSRF).** POST requests are only accepted
  if the browser's `Origin`/`Referer` header (when present) matches this
  server. Without this, any website open in another tab could silently
  drive the bridge — start sessions, change settings, spend your API
  budget — using your browser's already-trusted connection to
  `127.0.0.1`.
- **Exposure by default.** The server binds to `127.0.0.1` only, unless
  you explicitly opt in to LAN access (Settings toggle, or `--lan` on the
  command line). LAN mode is plain HTTP — unencrypted on your local
  network. See [docs/remote-access.md](docs/remote-access.md) for a
  better way to reach the bridge from your phone (Tailscale), which is
  encrypted and doesn't expose anything to your LAN.

- **A stolen PIN turning into stolen API keys.** Your provider keys and
  the PIN itself never leave this machine in the clear. A client on
  loopback sees the real values (that's the Settings dialog on the
  computer running the bridge); any remote client gets a placeholder
  instead. So a compromised PIN costs you control of the bridge, not
  five API keys billable off your machine. You can still rotate or clear
  a key from your phone — sending a new value works normally, and
  sending the placeholder back is understood as "leave this one alone."

## What it does NOT protect against

- **Someone with access to this computer.** `config.json` stores your API
  keys and PIN in plain text next to the script. Anyone who can read
  files on this machine can read your keys. This is the same trust model
  as any local dev tool or `.env` file — it is not designed to resist a
  local attacker or malware already running as your user.
- **A compromised Kindroid character or AI guest.** Text coming back from
  Kindroid, xAI, Anthropic, Google, or OpenAI is rendered into the
  dashboard using `textContent` (not `innerHTML`), so it can't inject
  script into the page. It's still just text from a third-party service —
  treat it the way you'd treat any other untrusted chat content.
- **Your API provider's own security.** If your xAI, Kindroid, Anthropic,
  Gemini, or OpenAI key leaks some other way (e.g. you paste it somewhere
  public), this app can't protect you. Rotate the key with that provider.

## If you find a vulnerability

Please open a private report rather than a public issue if the bug is
exploitable before a fix ships — use GitHub's "Report a vulnerability"
flow on this repo's Security tab. Include: what you found, how to
reproduce it, and what you think the impact is. There's no bug bounty;
this is a hobby project, but reports are genuinely appreciated and will
be credited in the fix unless you'd rather stay anonymous.

## Reporting a non-security bug

Use the regular issue templates. **Redact your logs and screenshots
first** — `api_debug.log`, `session_logs/`, and the dashboard itself all
contain your actual roleplay conversations. Nobody needs to see your
canon to help debug a stack trace.
