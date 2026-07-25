# Remote access — using the dashboard from your phone

The bridge runs on one computer, but the dashboard is just a web page —
you can direct sessions from the couch, or from anywhere, if you set up
access properly.

There are two ways to do this. **Tailscale is the recommended one.**

| | Tailscale (recommended) | LAN mode (`--lan`) |
|---|---|---|
| Encrypted | ✅ Yes (WireGuard) | ❌ No — plain HTTP |
| Works away from home | ✅ Yes | ❌ Same wifi only |
| Exposed to other devices on your network | ✅ No | ⚠️ Yes (PIN-protected) |
| Setup | Install an app on 2 devices | One flag |

## Option 1: Tailscale (recommended)

[Tailscale](https://tailscale.com) creates a small private encrypted
network between your own devices. Nothing is opened to your LAN or the
internet; only devices signed into *your* Tailscale account can reach the
bridge. The free personal plan is more than enough.

### Setup

1. **On the computer running the bridge:** install Tailscale
   ([tailscale.com/download](https://tailscale.com/download)) and sign in.
   - Linux: `curl -fsSL https://tailscale.com/install.sh | sh` then
     `sudo tailscale up`.
2. **On your phone:** install the Tailscale app (iOS App Store / Google
   Play) and sign in **with the same account**.
3. **Find your computer's Tailscale name.** Open the [admin console
   machine list](https://login.tailscale.com/admin/machines) — each
   device has a short name (e.g. `my-desktop`) and a MagicDNS name (e.g.
   `my-desktop.tailnet-name.ts.net`). `tailscale status` on the computer
   shows the same info.
4. **Tell the bridge to accept that hostname.** Because the bridge
   rejects unrecognized `Host` headers (an anti-DNS-rebinding
   protection), add your machine's Tailscale names to `extra_hosts` in
   `config.json`, comma-separated, then restart the app:

   ```json
   "extra_hosts": "my-desktop,my-desktop.tailnet-name.ts.net,100.x.y.z"
   ```

   (The `100.x.y.z` Tailscale IP from the machine list works too, and
   doesn't depend on MagicDNS being enabled.)
5. **On your phone**, with Tailscale connected, browse to:

   ```
   http://my-desktop.tailnet-name.ts.net:8770
   ```

   (or `http://100.x.y.z:8770`). Enter the access PIN when prompted —
   it's printed in the console window on the computer running the bridge.

You do **not** need `--lan`/the LAN checkbox for Tailscale — traffic
arrives through the Tailscale interface. If your phone can't connect,
turn on the LAN checkbox as a fallback and restart; some setups route
Tailscale traffic in a way that needs the wider bind.

### Phone gotchas (learned the hard way)

- **iOS allows only ONE active VPN at a time.** If you run a privacy VPN
  (NordVPN, Proton, Mullvad, etc.), enabling Tailscale disconnects it —
  and your privacy VPN reconnecting can silently kick Tailscale off.
  If the dashboard suddenly stops loading on iPhone, check which VPN
  actually holds the slot (Settings → VPN).
- **Android** handles this the same way — one active VPN service. Some
  privacy VPN apps have an "always-on VPN" setting that will keep
  stealing the slot back; turn that off while using Tailscale, or use
  Tailscale's own exit-node feature to get both at once.
- **Add to Home Screen.** Once the dashboard loads, use your browser's
  "Add to Home Screen" (iOS Safari: Share → Add to Home Screen; Android
  Chrome: ⋮ → Add to Home screen). The dashboard ships the right meta
  tags to run as a full-screen app — it feels like a native app and
  keeps you signed in with your PIN.

## Option 2: LAN mode (same-wifi only)

If you just want your phone to reach the bridge over your home wifi and
you trust everyone on that network:

1. Start the bridge with the `--lan` flag once (or tick **Allow LAN
   access** in Settings and restart). The setting is remembered.
2. The console prints your LAN addresses — on your phone, browse to the
   one matching your wifi (usually the first, a `192.168.x.x` address).
3. Enter the access PIN from the console.

**Understand what this means:** LAN mode is plain, unencrypted HTTP.
Anyone on the same network can see the traffic (including your
conversations as they stream to the dashboard), and anything on the
network can attempt the PIN (attempts are rate-limited with exponential
backoff, but a PIN is still just a PIN). Fine on a home network you
control; do not use it on shared, public, dorm, or office wifi — use
Tailscale there instead.

## Troubleshooting

- **"invalid host header" error** — the name/IP you're browsing to isn't
  on the bridge's allowlist. Add it to `extra_hosts` in `config.json`
  and restart. (This is a security feature, not a bug: it blocks DNS
  rebinding attacks.)
- **401 / PIN prompt loops** — the PIN in your browser's storage is
  stale. The current PIN is printed in the console window on the host
  computer at startup.
- **"too many failed PIN attempts"** — the brute-force lockout tripped.
  Wait the number of seconds shown (it doubles per failure, capped at 5
  minutes), or restart the bridge to clear it.
- **Dashboard loads but won't update / buttons do nothing** — usually a
  half-dead VPN connection on the phone. Toggle Tailscale off and on.
