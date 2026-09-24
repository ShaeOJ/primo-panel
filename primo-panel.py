#!/usr/bin/env python3
"""
Primo Panel — a tiny stdlib-only web control/monitor for primo-arm-miner.

Primo's own API (TCP 4068) is READ-ONLY ccminer text, so this panel:
  * reads live stats from that API (summary / pool / threads), and
  * changes settings by rewriting the miner's config.json and restarting its
    systemd service (the miner has no live-reconfigure API).

No third-party dependencies — runs on any board with Python 3.8+.

Config via environment (see primo-panel.service):
  PANEL_HOST     bind address           (default 0.0.0.0)
  PANEL_PORT     listen port            (default 8088)
  PANEL_USER     basic-auth username    (default "admin")
  PANEL_PASS     basic-auth password    (default "changeme" — CHANGE IT)
  PRIMO_API_HOST miner API host         (default 127.0.0.1)
  PRIMO_API_PORT miner API port         (default 4068)
  PRIMO_CONFIG   miner config.json path (default /etc/primo/config.json)
  PRIMO_SERVICE  systemd unit name      (default primo-miner)
"""
import base64
import concurrent.futures
import hmac
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("PANEL_HOST", "0.0.0.0")
PORT = int(os.environ.get("PANEL_PORT", "8088"))
USER = os.environ.get("PANEL_USER", "admin")
PASS = os.environ.get("PANEL_PASS", "changeme")
API_HOST = os.environ.get("PRIMO_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("PRIMO_API_PORT", "4068"))
CONFIG_PATH = os.environ.get("PRIMO_CONFIG", "/etc/primo/config.json")
SERVICE = os.environ.get("PRIMO_SERVICE", "primo-miner")

# Swarm: auto-discover other Primo miners on the LAN and aggregate them.
SWARM_ENABLED = os.environ.get("SWARM_ENABLED", "1").lower() not in ("0", "false", "no", "off")
SWARM_PORT = int(os.environ.get("SWARM_PORT", str(API_PORT)))          # miner API port to probe
SWARM_SUBNET = os.environ.get("SWARM_SUBNET", "")                       # e.g. "10.0.0" (auto if empty)
SWARM_PEERS = [p.strip() for p in os.environ.get("SWARM_PEERS", "").split(",") if p.strip()]
SWARM_INTERVAL = int(os.environ.get("SWARM_INTERVAL", "30"))            # seconds between scans

# CPU miners report no wattage, so efficiency is estimated from the board's
# spec draw under full load. H618 quad-A53 SBC (OPi Zero2W/Zero3) ~= 3 W.
BOARD_WATTS = float(os.environ.get("BOARD_WATTS", "3.0"))

VALID_ALGOS = {"verus", "sha256d", "scrypt", "randomx", "rx", "xmr", "monero"}


# ---------------------------------------------------------------------------
# Miner text-API client (ccminer protocol: send "cmd\n", read to '|')
# ---------------------------------------------------------------------------
def api_command(cmd, host=API_HOST, port=API_PORT, timeout=5.0):
    with socket.create_connection((host, port), timeout) as s:
        s.settimeout(timeout)
        s.sendall((cmd + "\n").encode())
        buf = b""
        while b"|" not in buf and b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    return buf.decode(errors="replace")


def parse_records(raw):
    out = []
    for rec in raw.replace("\0", "").split("|"):
        rec = rec.strip()
        if "=" not in rec:
            continue
        d = {}
        for tok in rec.split(";"):
            if "=" in tok:
                k, v = tok.split("=", 1)
                d[k.strip()] = v.strip()
        out.append(d)
    return out


def collect_stats(host=API_HOST, port=API_PORT):
    """Query a miner's 4068 API and return a normalized dict (or an error field)."""
    try:
        summary = (parse_records(api_command("summary", host, port)) or [{}])[0]
        pools = parse_records(api_command("pool", host, port))
        pool = next((p for p in pools if p.get("USER")), (pools[0] if pools else {}))
        threads = parse_records(api_command("threads", host, port))
    except Exception as e:  # miner down / API unreachable
        return {"online": False, "error": str(e)}

    if not summary.get("NAME"):
        return {"online": False, "error": "no summary from miner API"}

    temps = [float(t["TEMP"]) for t in threads if t.get("TEMP") and 0 < float(t["TEMP"]) < 150]
    khs = float(summary.get("KHS", 0) or 0)
    return {
        "online": True,
        "name": summary.get("NAME", ""),
        "version": summary.get("VER", ""),
        "algo": summary.get("ALGO", ""),
        "khs": khs,
        "mhs": khs / 1000.0,
        "accepted": int(pool.get("ACC", summary.get("ACC", 0)) or 0),
        "rejected": int(pool.get("REJ", summary.get("REJ", 0)) or 0),
        "stale": int(pool.get("STALE", 0) or 0),
        "acc_per_min": float(summary.get("ACCMN", 0) or 0),
        "diff": float(pool.get("DIFF", summary.get("DIFF", 0)) or 0),
        "best": float(pool.get("BEST", 0) or 0),
        "uptime": int(summary.get("UPTIME", 0) or 0),
        "threads": len(threads),
        "max_temp": max(temps) if temps else 0,
        "pool_url": pool.get("URL", pool.get("POOL", "")),
        "pool_user": pool.get("USER", ""),
        "ping": pool.get("PING", ""),
        "disconnects": pool.get("DISCO", ""),
    }


# ---------------------------------------------------------------------------
# Swarm: discover + aggregate every Primo miner on the LAN
# ---------------------------------------------------------------------------
_swarm = {"miners": [], "totals": {"count": 0, "mhs": 0, "accepted": 0, "rejected": 0}, "scanned_at": 0}
_swarm_lock = threading.Lock()


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _subnet_base():
    if SWARM_SUBNET:
        return SWARM_SUBNET.rstrip(".")
    return _local_ip().rsplit(".", 1)[0]


def _port_open(ip, port, timeout=0.5):
    try:
        with socket.create_connection((ip, port), timeout):
            return True
    except Exception:
        return False


def swarm_scan():
    """Probe the local /24 (plus any SWARM_PEERS) for the miner API port, then
    query each responder and aggregate. Always includes this host."""
    base = _subnet_base()
    me = _local_ip()
    candidates = set(SWARM_PEERS)
    candidates.add(me)
    ips = ["%s.%d" % (base, i) for i in range(1, 255)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        for ip, ok in zip(ips, ex.map(lambda x: _port_open(x, SWARM_PORT), ips)):
            if ok:
                candidates.add(ip)

    def q(ip):
        return ip, collect_stats(ip, SWARM_PORT)

    miners = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as ex:
        for ip, st in ex.map(q, candidates):
            if st.get("online"):
                user = st.get("pool_user", "") or ""
                worker = user.rsplit(".", 1)[-1] if "." in user else user
                miners.append({
                    "ip": ip, "self": ip == me, "worker": worker, "user": user,
                    "mhs": st.get("mhs", 0), "accepted": st.get("accepted", 0),
                    "rejected": st.get("rejected", 0), "max_temp": st.get("max_temp", 0),
                    "best": st.get("best", 0), "diff": st.get("diff", 0),
                    "uptime": st.get("uptime", 0), "algo": st.get("algo", ""),
                    "version": st.get("version", ""),
                })
    miners.sort(key=lambda m: (not m["self"], m["ip"]))
    total_mhs = sum(m["mhs"] for m in miners)
    est_watts = round(len(miners) * BOARD_WATTS, 1)
    totals = {
        "count": len(miners),
        "mhs": total_mhs,
        "accepted": sum(m["accepted"] for m in miners),
        "rejected": sum(m["rejected"] for m in miners),
        "est_watts": est_watts,
        "mhs_per_w": round(total_mhs / est_watts, 3) if est_watts > 0 else 0,
    }
    with _swarm_lock:
        _swarm["miners"] = miners
        _swarm["totals"] = totals
        _swarm["scanned_at"] = time.time()


def swarm_loop():
    while True:
        try:
            swarm_scan()
        except Exception as e:
            print("swarm scan error:", e, flush=True)
        time.sleep(max(10, SWARM_INTERVAL))


def swarm_snapshot():
    with _swarm_lock:
        return dict(_swarm)


def axeos_info():
    """The whole swarm rendered in the AxeOS/BitAxe `/api/system/info` shape so
    external monitors (AxeOS-VPN) can pull the fleet from one endpoint. hashRate
    is GH/s (BitAxe convention). Read-only; served without auth."""
    s = swarm_snapshot()
    t = s.get("totals", {})
    miners = s.get("miners", [])
    first_algo = miners[0].get("algo", "sha256d") if miners else "sha256d"
    return {
        "hostname": "asic-primo-swarm",
        "ASICModel": "A.S.I.C. Primo Swarm (%d)" % t.get("count", 0),
        "version": "primo-panel",
        "hashRate": round(t.get("mhs", 0) / 1000.0, 6),          # GH/s
        "sharesAccepted": t.get("accepted", 0),
        "sharesRejected": t.get("rejected", 0),
        "temp": max([m.get("max_temp", 0) for m in miners], default=0),
        "power": t.get("est_watts", 0), "powerEstimated": True,
        "voltage": 0, "current": 0, "frequency": 0, "coreVoltage": 0,
        "bestDiff": max([m.get("best", 0) for m in miners], default=0),
        "uptimeSeconds": max([m.get("uptime", 0) for m in miners], default=0),
        "minerCount": t.get("count", 0),
        "algorithm": "scrypt" if "scrypt" in first_algo.lower() else "sha256",
        "scannedAt": s.get("scanned_at", 0),
        "workers": [{
            "ip": m["ip"], "worker": m["worker"], "hashRate": round(m["mhs"] / 1000.0, 6),
            "sharesAccepted": m["accepted"], "sharesRejected": m["rejected"],
            "bestDiff": m["best"], "temp": m["max_temp"], "algo": m["algo"],
            "poolUser": m["user"], "uptimeSeconds": m["uptime"],
        } for m in miners],
    }


# ---------------------------------------------------------------------------
# Config read / validate / write
# ---------------------------------------------------------------------------
def read_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        return {"_error": str(e)}


def validate_config(cfg):
    if not isinstance(cfg, dict):
        return "config must be a JSON object"
    algo = str(cfg.get("algo", "")).lower()
    if algo not in VALID_ALGOS:
        return "algo must be one of: " + ", ".join(sorted(VALID_ALGOS))
    has_url = bool(cfg.get("url"))
    has_pools = isinstance(cfg.get("pools"), list) and any(p.get("url") for p in cfg["pools"])
    if not (has_url or has_pools):
        return "need a 'url' or a non-empty 'pools' array with a url"
    if not cfg.get("user"):
        return "'user' (wallet/worker) is required"
    if "threads" in cfg:
        try:
            int(cfg["threads"])
        except (TypeError, ValueError):
            return "'threads' must be an integer"
    return None


def write_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CONFIG_PATH) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
        # mkstemp creates 0600 root-owned; the miner runs as a non-root user and
        # must be able to READ its config, so make it world-readable before swap.
        os.chmod(tmp, 0o644)
        os.replace(tmp, CONFIG_PATH)  # atomic
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def systemctl(action):
    if action not in ("start", "stop", "restart"):
        return False, "invalid action"
    try:
        r = subprocess.run(
            ["systemctl", action, SERVICE],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except Exception as e:
        return False, str(e)


def service_active():
    try:
        r = subprocess.run(["systemctl", "is-active", SERVICE],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "PrimoPanel/1.0"

    def _authed(self):
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(hdr[6:]).decode()
            u, _, p = raw.partition(":")
        except Exception:
            return False
        return hmac.compare_digest(u, USER) and hmac.compare_digest(p, PASS)

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Primo Panel"')
        self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        # Public, read-only fleet telemetry in the AxeOS/BitAxe shape (no auth,
        # same sensitivity as the miner's own open 4068 API) — for AxeOS-VPN etc.
        if self.path == "/api/system/info":
            return self._json(axeos_info())
        if not self._authed():
            return self._deny()
        if self.path == "/" or self.path.startswith("/?"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/stats":
            stats = collect_stats()
            stats["service"] = service_active()
            if stats.get("online"):
                stats["est_watts"] = BOARD_WATTS
                stats["mhs_per_w"] = round(stats.get("mhs", 0) / BOARD_WATTS, 3) if BOARD_WATTS > 0 else 0
            self._json(stats)
        elif self.path == "/api/config":
            self._json(read_config())
        elif self.path == "/api/swarm":
            self._json(swarm_snapshot())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authed():
            return self._deny()
        if self.path == "/api/config":
            try:
                cfg = json.loads(self._body() or b"{}")
            except Exception as e:
                return self._json({"ok": False, "error": "bad JSON: %s" % e}, 400)
            err = validate_config(cfg)
            if err:
                return self._json({"ok": False, "error": err}, 400)
            try:
                write_config(cfg)
            except Exception as e:
                return self._json({"ok": False, "error": "write failed: %s" % e}, 500)
            ok, msg = systemctl("restart")
            self._json({"ok": ok, "message": "config saved; " + ("restarted" if ok else "restart failed: " + msg)})
        elif self.path == "/api/control":
            try:
                action = json.loads(self._body() or b"{}").get("action", "")
            except Exception:
                action = ""
            ok, msg = systemctl(action)
            self._json({"ok": ok, "message": msg or action})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        pass  # quiet; systemd journal captures stderr if needed


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A.S.I.C. — Primo Miner Monitor</title>
<meta name="theme-color" content="#0a0d0a">
<style>
:root{--bg:#0a0d0a;--card:#121712;--acc:#ffd200;--grn:#7bb661;--txt:#d7e4c9;--dim:#7d8a6f;--red:#e06060}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.5 ui-monospace,Menlo,Consolas,monospace}
.wrap{max-width:860px;margin:0 auto;padding:18px}
h1{color:var(--acc);font-size:18px;letter-spacing:.05em;margin:0 0 2px}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
.card{background:var(--card);border:1px solid #26301f;border-radius:10px;padding:12px}
.card .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.card .v{color:var(--acc);font-size:20px;font-weight:700;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .v.grn{color:var(--grn)}
.pill{display:inline-block;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:700}
.pill.on{background:rgba(123,182,97,.18);color:var(--grn);border:1px solid rgba(123,182,97,.4)}
.pill.off{background:rgba(224,96,96,.18);color:var(--red);border:1px solid rgba(224,96,96,.4)}
h2{color:var(--grn);font-size:14px;border-bottom:1px solid #26301f;padding-bottom:6px;margin:22px 0 12px;letter-spacing:.04em}
h2 .ico,.hero-label .ico{color:var(--acc);margin-right:7px;font-style:normal}
label{display:block;color:var(--dim);font-size:12px;margin:10px 0 3px}
input,select,textarea{width:100%;background:#0e130d;border:1px solid #2c3823;color:var(--txt);border-radius:8px;padding:8px;font:inherit}
textarea{min-height:120px;resize:vertical}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
button{background:var(--acc);color:#0a0d0a;border:0;border-radius:8px;padding:9px 16px;font-weight:700;cursor:pointer;font:inherit}
button.ghost{background:transparent;color:var(--txt);border:1px solid #2c3823}
button.danger{background:transparent;color:var(--red);border:1px solid rgba(224,96,96,.4)}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.msg{margin-top:12px;padding:8px 10px;border-radius:8px;font-size:13px;display:none}
.msg.ok{display:block;background:rgba(123,182,97,.14);color:var(--grn)}
.msg.err{display:block;background:rgba(224,96,96,.14);color:var(--red)}
small.hint{color:var(--dim)}
table.swarm{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
table.swarm th{text-align:left;color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em;padding:6px 8px;border-bottom:1px solid #26301f}
table.swarm td{padding:6px 8px;border-bottom:1px solid #1a2115;white-space:nowrap}
table.swarm tr.me td{color:var(--acc)}
table.swarm tr.me{background:rgba(255,210,0,.05)}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding:14px 18px;border-bottom:1px solid #26301f;background:linear-gradient(180deg,#0e130d,#0a0d0a)}
.brand{display:flex;align-items:center;gap:12px}
.atom{width:40px;height:40px;flex-shrink:0;animation:spin 20s linear infinite}
.atom .orb ellipse{fill:none;stroke:var(--grn);stroke-width:1.1;opacity:.55}
.atom .o2{transform:rotate(60deg);transform-origin:20px 20px}
.atom .o3{transform:rotate(120deg);transform-origin:20px 20px}
.atom .nuc{fill:var(--acc);filter:drop-shadow(0 0 5px var(--acc))}
@keyframes spin{to{transform:rotate(360deg)}}
.wordmark{font-weight:800;font-size:20px;letter-spacing:.16em;color:var(--acc);line-height:1}
.wordmark small{display:block;font-size:9.5px;letter-spacing:.24em;color:var(--grn);font-weight:600;margin-top:4px}
.host{color:var(--dim);font-size:12px;margin-left:8px}
.hero{display:flex;flex-wrap:wrap;align-items:center;gap:18px;margin:18px 0 10px;padding:16px 18px;background:var(--card);border:1px solid #2a3620;border-radius:12px;box-shadow:0 0 24px rgba(255,210,0,.05) inset}
.hero-main{min-width:190px}
.hero-label{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
.hero-val{color:var(--acc);font-size:38px;font-weight:800;line-height:1.1;margin-top:4px;text-shadow:0 0 18px rgba(255,210,0,.28)}
.hero .grid{flex:1;margin:0;min-width:240px}
</style></head><body>
<header class="topbar">
  <div class="brand">
    <svg class="atom" viewBox="0 0 40 40" aria-hidden="true">
      <g class="orb o1"><ellipse cx="20" cy="20" rx="15.5" ry="6"/></g>
      <g class="orb o2"><ellipse cx="20" cy="20" rx="15.5" ry="6"/></g>
      <g class="orb o3"><ellipse cx="20" cy="20" rx="15.5" ry="6"/></g>
      <circle class="nuc" cx="20" cy="20" r="3.4"/>
    </svg>
    <div class="wordmark">A.S.I.C.<small>PRIMO MINER MONITOR</small></div>
  </div>
  <div><span id="svc" class="pill off">…</span> <span class="host" id="host"></span></div>
</header>
<div class="wrap">

<section class="hero">
  <div class="hero-main">
    <div class="hero-label"><span class="ico">⛏</span>Fleet Hashrate <span class="hint" id="swarm-count"></span></div>
    <div class="hero-val" id="hero-hash">—</div>
  </div>
  <div class="grid" id="swarm-totals"></div>
</section>
<div style="overflow-x:auto"><table class="swarm" id="swarm-table"></table></div>

<h2><span class="ico">◈</span>This Device</h2>
<div class="grid" id="stats"></div>

<h2><span class="ico">⚙</span>Settings</h2>
<small class="hint">Edits are written to config.json and the miner is restarted. Advanced keys (pools[], cpu-affinity, etc.) can be edited in Raw JSON below.</small>
<div class="row">
  <div><label>Algorithm</label>
    <select id="algo"><option>sha256d</option><option>scrypt</option><option>verus</option><option>randomx</option></select></div>
  <div><label>Threads</label><input id="threads" type="number" min="1" max="256"></div>
</div>
<label>Pool URL <small class="hint">(stratum+tcp://host:port)</small></label><input id="url" placeholder="stratum+tcp://asicpool.space:5334">
<div class="row">
  <div><label>Wallet.Worker (user)</label><input id="user" placeholder="ADDRESS.worker"></div>
  <div><label>Password</label><input id="pass" placeholder="x"></div>
</div>
<label>API bind <small class="hint">(0.0.0.0:4068 = reachable on LAN)</small></label><input id="apibind" placeholder="0.0.0.0:4068">
<div class="btns"><button onclick="save()">Save &amp; Restart</button>
  <button class="ghost" onclick="toggleRaw()">Raw JSON</button></div>
<div id="rawwrap" style="display:none"><label>Raw config.json</label><textarea id="raw"></textarea>
  <div class="btns"><button onclick="saveRaw()">Save Raw &amp; Restart</button></div></div>

<h2><span class="ico">⟁</span>Service</h2>
<div class="btns">
  <button class="ghost" onclick="ctl('restart')">Restart</button>
  <button class="ghost" onclick="ctl('start')">Start</button>
  <button class="danger" onclick="ctl('stop')">Stop</button>
</div>
<div id="msg" class="msg"></div>
</div>
<script>
var cfg={};
function fmtUptime(s){s=+s||0;var d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return (d?d+'d ':'')+(h?h+'h ':'')+m+'m';}
function el(id){return document.getElementById(id);}
function tile(k,v,grn){return '<div class="card"><div class="k">'+k+'</div><div class="v'+(grn?' grn':'')+'">'+v+'</div></div>';}
function msg(t,ok){var m=el('msg');m.textContent=t;m.className='msg '+(ok?'ok':'err');setTimeout(function(){m.className='msg';},6000);}
async function refresh(){
  try{var r=await fetch('/api/stats');var s=await r.json();
    var svc=el('svc');svc.textContent=(s.service||'?')+(s.online?' · mining':'');svc.className='pill '+(s.online?'on':'off');
    var g=el('stats');
    if(!s.online){g.innerHTML=tile('Status','OFFLINE')+tile('Error',(s.error||'').slice(0,40));return;}
    g.innerHTML=tile('Hashrate',(s.mhs>=1000?(s.mhs/1000).toFixed(2)+' GH/s':s.mhs.toFixed(2)+' MH/s'),1)
      +tile('Accepted',s.accepted,1)+tile('Rejected',s.rejected)+tile('Stale',s.stale)
      +tile('Best Share',(+s.best).toFixed(2))+tile('Pool Diff',(+s.diff).toFixed(4))
      +tile('Max Temp',(+s.max_temp).toFixed(0)+' &deg;C')+tile('Threads',s.threads)
      +tile('Est. Power','~'+(+s.est_watts||0).toFixed(1)+' W')
      +tile('Efficiency','~'+(+s.mhs_per_w||0).toFixed(2)+' MH/s/W',1)
      +tile('Uptime',fmtUptime(s.uptime))+tile('Algo',s.algo);
  }catch(e){el('svc').textContent='panel error';}
}
async function loadCfg(){
  var r=await fetch('/api/config');cfg=await r.json();
  el('raw').value=JSON.stringify(cfg,null,2);
  if(cfg.algo)el('algo').value=cfg.algo;
  if(cfg.threads!=null)el('threads').value=cfg.threads;
  var url=cfg.url||(cfg.pools&&cfg.pools[0]&&cfg.pools[0].url)||'';el('url').value=url;
  el('user').value=cfg.user||'';el('pass').value=cfg.pass||'';
  el('apibind').value=cfg['api-bind']||'';
}
async function save(){
  var c=Object.assign({},cfg);
  c.algo=el('algo').value;c.threads=parseInt(el('threads').value)||undefined;
  c.user=el('user').value;c.pass=el('pass').value;c['api-bind']=el('apibind').value;
  var url=el('url').value.trim();
  if(c.pools&&c.pools.length){c.pools[0].url=url;delete c.url;}else{c.url=url;}
  await post('/api/config',c);
}
async function saveRaw(){
  try{var c=JSON.parse(el('raw').value);}catch(e){return msg('Raw JSON parse error: '+e,false);}
  await post('/api/config',c);
}
async function post(u,b){
  try{var r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
    var j=await r.json();msg(j.message||j.error||(j.ok?'ok':'failed'),j.ok);
    if(j.ok){setTimeout(loadCfg,500);setTimeout(refresh,1500);}
  }catch(e){msg('request failed: '+e,false);}
}
async function ctl(a){await post('/api/control',{action:a});}
function toggleRaw(){var w=el('rawwrap');w.style.display=w.style.display==='none'?'block':'none';}
function fmtHash(mhs){return mhs>=1000?(mhs/1000).toFixed(2)+' GH/s':(+mhs||0).toFixed(2)+' MH/s';}
function fmtDiff(d){d=+d||0;if(d>=1e12)return(d/1e12).toFixed(2)+'T';if(d>=1e9)return(d/1e9).toFixed(2)+'G';if(d>=1e6)return(d/1e6).toFixed(2)+'M';if(d>=1e3)return(d/1e3).toFixed(2)+'K';return d.toFixed(2);}
async function refreshSwarm(){
  try{var r=await fetch('/api/swarm');var s=await r.json();var t=s.totals||{};var n=t.count||0;
    el('hero-hash').textContent=fmtHash(t.mhs||0);
    el('swarm-count').textContent=n?('· '+n+' miner'+(n===1?'':'s')):'';
    el('swarm-totals').innerHTML=tile('Miners',n)
      +tile('Est. Power','~'+(+t.est_watts||0).toFixed(1)+' W')
      +tile('Efficiency','~'+(+t.mhs_per_w||0).toFixed(2)+' MH/s/W',1)
      +tile('Total Accepted',t.accepted||0,1)+tile('Total Rejected',t.rejected||0);
    var rows='<tr><th>Worker</th><th>IP</th><th>Hashrate</th><th>Best Diff</th><th>Acc</th><th>Rej</th><th>Temp</th><th>Uptime</th></tr>';
    (s.miners||[]).forEach(function(m){
      var link='http://'+m.ip+':'+(location.port||'8088')+'/';
      var ipcell=m.self?m.ip:'<a href="'+link+'" target="_blank" rel="noopener" style="color:var(--acc);text-decoration:none">'+m.ip+' ↗</a>';
      rows+='<tr class="'+(m.self?'me':'')+'"><td>'+(m.worker||'?')+(m.self?' (this)':'')+'</td><td>'+ipcell
        +'</td><td>'+fmtHash(m.mhs)+'</td><td>'+fmtDiff(m.best)+'</td><td>'+m.accepted+'</td><td>'+m.rejected
        +'</td><td>'+(+m.max_temp).toFixed(0)+'&deg;C</td><td>'+fmtUptime(m.uptime)+'</td></tr>';
    });
    el('swarm-table').innerHTML=rows;
  }catch(e){}
}
el('host').textContent=location.host;
loadCfg();refresh();setInterval(refresh,3000);
refreshSwarm();setInterval(refreshSwarm,10000);
</script></body></html>"""


def main():
    if PASS == "changeme":
        print("WARNING: PANEL_PASS is the default 'changeme' — set a real password in primo-panel.service", flush=True)
    if SWARM_ENABLED:
        threading.Thread(target=swarm_loop, daemon=True).start()
        print("Swarm scan enabled on %s.0/24 port %d every %ds"
              % (_subnet_base(), SWARM_PORT, SWARM_INTERVAL), flush=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Primo Panel on http://%s:%d (miner API %s:%d, config %s, service %s)"
          % (HOST, PORT, API_HOST, API_PORT, CONFIG_PATH, SERVICE), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
