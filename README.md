# Primo Panel

A tiny, dependency-free web **control + monitor** for
[`primo-arm-miner`](https://github.com/PrimoLab/primo-arm-miner), plus a
config-driven install kit so a new SBC comes up mining + manageable in one
command.

## Why it exists

Primo's own API (TCP `4068`) is **read-only** ccminer text — great for stats,
but it can't change settings. This panel reads that API for the live view and
changes settings the only way possible: by rewriting the miner's
`config.json` and restarting its systemd service.

## What the kit installs

| Piece | Path | Role |
|-------|------|------|
| `config.json` | `/etc/primo/config.json` | all miner settings incl. **`api-bind: 0.0.0.0:4068`** (LAN-reachable stats API, no `-b` flag needed) |
| `primo-miner.service` | systemd | runs `primo-arm-miner --config /etc/primo/config.json`, restart-on-fail, boot-start |
| `primo-panel.py` | `/opt/primo-panel/` | stdlib web daemon (no pip deps) |
| `primo-panel.service` | systemd | serves the panel on `:8088`, basic-auth |

## Install (any new Pi)

```bash
# copy this folder to the device, then:
sudo PANEL_PASS='choose-a-password' \
     POOL_USER='YOUR_ADDRESS.worker1' POOL_URL='stratum+tcp://asicpool.space:5334' \
     ./setup.sh
```

The miner binary must be at `/usr/local/bin/primo-arm-miner` (or pass
`PRIMO_BIN=/path/to/binary` to have setup install it). Then open
`http://<device-ip>:8088`.

Seed env (all optional): `MINER_USER`, `PRIMO_BIN`, `PANEL_PASS`, `ALGO`,
`POOL_URL`, `POOL_USER`, `POOL_PASS`, `THREADS`.

## Panel features

- **Monitor:** hashrate, accepted/rejected/stale, best share, pool difficulty,
  max temp, threads, uptime, algo — polled every 3s from the `4068` API.
- **Control:** edit algo / pool URL / wallet.worker / password / threads /
  api-bind (or the whole `config.json` via the Raw JSON box) → saves +
  restarts the miner. Start / Stop / Restart buttons.
- **Swarm:** auto-discovers every other Primo miner on the LAN (scans the local
  /24 for the `4068` API — no peer credentials needed since that API is
  read-only) and shows a combined view: each worker + **total hashrate**, total
  accepted/rejected, and miner count. Every panel shows the whole fleet, tagging
  itself "(this)". Runs on a background thread (default every 30s).
  - Env: `SWARM_ENABLED` (default on), `SWARM_SUBNET` (e.g. `10.0.0`; auto-detected
    if unset), `SWARM_PEERS` (comma IPs to force-include across subnets),
    `SWARM_PORT` (default = miner API port), `SWARM_INTERVAL` (seconds).

## Security notes

- The panel can change the payout wallet and restart the miner, so it is
  **HTTP Basic-auth** protected — **set `PANEL_PASS`** (don't leave `changeme`).
- Auth is over plain HTTP (fine on a trusted LAN; put it behind a reverse
  proxy / WireGuard for anything more exposed).
- It runs as **root** to write `/etc/primo` and call `systemctl`. Keep it on
  the LAN.
- `api-bind: 0.0.0.0` exposes the miner's **read-only** stats API on the LAN.

## Panel API (for scripting / AxeOS-VPN etc.)

- `GET /api/stats` → JSON snapshot of the live miner stats
- `GET /api/config` → current `config.json`
- `POST /api/config` (JSON body) → validate + write + restart
- `POST /api/control` `{"action":"start|stop|restart"}`
- `GET /api/swarm` → `{miners:[...], totals:{count,mhs,accepted,rejected,est_watts,mhs_per_w}, scanned_at}` — the whole LAN fleet
- `GET /api/system/info` → **no auth**; the whole swarm in the AxeOS/BitAxe `/api/system/info` shape (hashRate in GH/s, sharesAccepted/Rejected, temp, estimated `power`, `workers[]`) so external monitors (e.g. AxeOS-VPN) can pull the fleet from one endpoint

## Efficiency estimate

CPU miners have no power sensor, so efficiency is **estimated** from the board's
spec draw under load — `BOARD_WATTS` (default `3.0`, an H618 quad-A53 SBC). The
panel shows `~W` and `~MH/s per watt` (per device and fleet-wide). Tune
`BOARD_WATTS` per board in `primo-panel.service`.
