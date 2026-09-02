# VPN Setup (Optional, Recommended for YouTube)

If YouTube keeps blocking downloads with *"Sign in to confirm you're not a
bot"*, the most effective fix is to route fnack through a **VPN** (the
in-container VPN plugin). Residential VPN IPs are flagged far less often than
datacenter IPs. fnack runs the VPN **inside its own container** — no extra
container needed.

## Option A: Upload via the Web UI (easiest)

1. Get an OpenVPN (`.ovpn`) or WireGuard (`.conf`) config from any VPN
   provider (Mullvad, NordVPN, ProtonVPN, Windscribe, commercial seedboxes,
   etc.).
2. Open **Settings → VPN (Optional)**.
3. Click **Upload & Apply** and pick your config file (or paste its content).
4. fnack saves it, starts the tunnel, and shows your **public IP** — if it
   differs from your normal IP, the tunnel is working.

You can Start / Stop the VPN and Delete the config any time from the same
screen.

## Option B: Drop a file in `./config/vpn/` and restart

```bash
mkdir -p ./config/vpn
cp ~/Downloads/myvpn.ovpn ./config/vpn/   # OpenVPN
# or
cp ~/Downloads/wg0.conf ./config/vpn/     # WireGuard
docker compose up -d                       # restarts with the tunnel
```

## Requirements

The container needs `NET_ADMIN` and `/dev/net/tun` — both are already set in
the provided `docker-compose.yml`:

```yaml
cap_add:
  - NET_ADMIN
devices:
  - /dev/net/tun:/dev/net/tun
```

For a manual `docker run`:

```bash
docker run -d --name fnack --cap-add=NET_ADMIN --device=/dev/net/tun:/dev/net/tun \
  -v ./config:/config -v ./downloads:/downloads -v /path/to/music:/music -p 4688:4688 fnack:latest
```

> With no config present, fnack runs without a VPN exactly as before.
