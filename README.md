# Kinbridge

A single-file, zero-dependency Python app that lets a Grok-powered AI
companion (loaded with its own memories and persona) hold a conversation
in a Kindroid group chat — autonomously, or with you steering every line —
while you watch from a web dashboard on your computer or phone.

**Status:** Provided as-is. PRs are welcome; there is no guaranteed
support — see [Support](#support).

![The Kinbridge dashboard control column: director console with mode and
round settings, scene director, chapter switcher, and buttons to bring
AI guests into the room.](docs/screenshot.png)

*The control column. The live conversation feed sits to its left and is
cropped out of this shot — a real dashboard shows your actual messages,
so sanitize before sharing screenshots of your own.*


## What it does

- Bridges a Grok (xAI) API persona into a live Kindroid group chat, so the
  two systems can talk to each other with no copy-pasting.
- Three modes: fully autonomous, "natural flow" (everyone privately bids
  for the floor each turn), and supervised (you approve every line before
  it posts).
- Optional guest seats for Claude, Gemini, and ChatGPT to drop into the
  same chat as themselves.
- A private long-term memory journal, a rolling "story so far" summary,
  and per-chapter (per-Kindroid-group) story state, so the persona stays
  consistent across sessions.
- A dashboard you can reach from your phone — see
  [Remote access](docs/remote-access.md).

## Requirements

- Python 3.8 or newer. No pip packages — the whole app is the standard
  library.
- API keys: an [xAI](https://x.ai) key (required), a
  [Kindroid](https://kindroid.ai) key + group chat ID (required), and
  optionally Anthropic / Google Gemini / OpenAI keys if you want those
  models to guest in the chat.

## Quick start

1. Download this repo (or just `kinbridge.py` plus the launcher for your
   OS).
2. **Windows:** double-click `Start-Kinbridge.bat`.
   **macOS/Linux:** run `./start-kinbridge.sh` (or `python3 kinbridge.py`).
3. Your browser opens automatically to the dashboard. The first run has no
   keys yet, so Settings opens for you — paste in your xAI key, Kindroid
   key, and Kindroid group ID, then close it.
4. Pick a mode and hit **Start session**.

Nothing here talks to the network except the AI providers you've given
keys for. Your keys and conversation data stay in `config.json` and the
memory files next to the script — see [Privacy](#privacy--your-data).

## Privacy & your data

- `config.json` holds your API keys and access PIN in plain text. It's
  created `0600` (owner read/write only), so other users on a shared
  machine can't read it — but it isn't encrypted. Treat it like any
  other secrets file: don't commit it, don't share it.
- **Prefer environment variables if you'd rather keep keys out of the
  app directory entirely.** Any secret can come from the environment,
  and an env-supplied value wins over `config.json`, is never written
  back to it, and can't be overwritten from the dashboard:

  | Variable | Replaces |
  | --- | --- |
  | `KINBRIDGE_XAI_API_KEY` | `xai_api_key` |
  | `KINBRIDGE_KINDROID_API_KEY` | `kindroid_api_key` |
  | `KINBRIDGE_ANTHROPIC_API_KEY` | `anthropic_api_key` |
  | `KINBRIDGE_GEMINI_API_KEY` | `gemini_api_key` |
  | `KINBRIDGE_OPENAI_API_KEY` | `openai_api_key` |
  | `KINBRIDGE_ACCESS_PIN` | `access_pin` |

  Keys sourced this way show as `••••••••` in Settings and are listed
  on startup, so it's obvious where they came from. This doesn't encrypt
  anything — it just lets your keys live in systemd, a password manager,
  or a shell profile you control instead of next to the script.
- `ani_memory.md`, `guest_memory.md`, `chapters/`, and `session_logs/`
  contain your actual conversations. All of these are already listed in
  `.gitignore`.
- `api_debug.log` records `method → endpoint → HTTP status` for
  troubleshooting. It does **not** log request/response bodies or API
  keys.
- Nothing here phones home. The only outbound requests are the ones you'd
  expect: to `api.x.ai`, `api.kindroid.ai`, and whichever guest provider
  APIs you've configured.

See [SECURITY.md](SECURITY.md) for the server's threat model (PIN
lockout, CSRF/DNS-rebinding protections, default-local binding).

## Remote access (using it from your phone)

See [docs/remote-access.md](docs/remote-access.md). Short version:
Tailscale, not opening a port to your LAN — it's encrypted and doesn't
expose the bridge to anything but your own devices.

## Naming & trademarks

"Ani" is a character owned by xAI; "Kindroid" is a trademark of its own
company. Kinbridge is **not affiliated with, endorsed by, or sponsored
by xAI or Kindroid** — it's an independent, unofficial integration that
talks to their public APIs. The default persona shipped in
`config.example.json` is named "Ani" purely as an example; rename it in
your own `config.json` if you'd rather not use xAI's character name.

## Terms of service

This tool automates both the xAI/Grok API and the Kindroid API. Whether
that kind of automation is within either provider's terms is between you
and them — read their current ToS before running this against a
production account, especially before running it autonomously or at
scale. Using this project is your responsibility, not something this
README can certify for you.

## Support

Kinbridge is provided **as-is**. PRs are welcome, but there is no
guaranteed support — no promised response time on issues, and no support
contract. If that doesn't work for your use case, please fork.

## Contributing

- PRs welcome. This project holds up to four third-party API keys per
  user, so `main` is protected and every contribution gets reviewed
  before merge — please don't take a slow review personally.
- Run the test suite before opening a PR: `python3 -m unittest discover
  tests`.
- If you're changing the Kindroid integration, read the quirks documented
  as comments around `KindroidClient` in `kinbridge.py` first — several
  of that API's behaviors are undocumented upstream and were reverse
  engineered the hard way (see [Kindroid API quirks](#kindroid-api-quirks)
  below).

## Kindroid API quirks

These aren't documented by Kindroid anywhere public, so they're recorded
here (and as inline comments in `kinbridge.py`) for whoever touches this
integration next:

- **`groupchats-ai-response` often returns an empty body.** The reply
  text isn't always in the immediate response — it lands in chat history
  moments later. The bridge polls `get-chat-messages` for a few seconds
  after a blank response before giving up on that character's turn.
- **`sender_type` is sometimes missing from history messages.** The
  bridge falls back to inspecting the `sender` field (treating anything
  that isn't `user`/`human`/`you` as an AI) when `sender_type` isn't
  present.
- **Timestamp units aren't documented** — some accounts return Unix
  seconds, others milliseconds. The bridge probes one real message on
  first use and infers the unit from its magnitude (`> 10^12` ⇒
  milliseconds).
- **A character that produces no text for a while isn't necessarily
  broken** — it's often Kindroid-side content filtering or load. The
  bridge rests a character after 3 consecutive blank replies rather than
  erroring, and un-rests them automatically if a late message arrives.

Model pricing (`price_in_per_m` / `price_out_per_m` and the guest
equivalents) and model-ID lists are both things that go stale over time.
The **Scan** button in Settings re-queries each provider's `/models`
endpoint live, so model IDs stay current; the `$`-per-million fields are
plain user input and won't update themselves — check them against your
provider's pricing page after switching models.

## License

MIT — see [LICENSE](LICENSE).
