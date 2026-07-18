#!/usr/bin/env python3
"""
run_hevc_wifi.py — one-button HEVC single-stream latency experiment,
edge0 -> amari over WiFi.

Streams a pre-encoded single-layer .hevc clip from edge0 to THIS host (amari)
over WiFi and measures per-frame one-way latency with pulse-codec's client.
Unlike run_stream_latency.py (edge0 -> Pi), the measuring client runs LOCALLY on
amari; only the server is remote (edge0).

    server (edge0)  pulse_server_single  --bind 192.168.1.150 --dst 192.168.1.159:9000
    client (amari, local)  pulse_client --single  --listen-a 0.0.0.0:9000

Both need CLOCK_REALTIME synced (PTP) for a valid one-way number.

Usage:
  python3 run_hevc_wifi.py --video beauty4k_hevc25Mbps_snr44.6
  python3 run_hevc_wifi.py --video beauty4k_hevc64Mbps_snr47.8 --duration 200
"""

import argparse
import atexit
import csv
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- constants ---
EDGE0 = "edge0"                                   # ssh alias (server side)
PULSE_DIR_EDGE0 = "/home/zx/pulse-codec"
VIDEO_DIR = f"{PULSE_DIR_EDGE0}/compare_videos"   # .hevc clips live on edge0
CLIENT_BIN = "/root/pulse-codec/bin/pulse_client" # local (amari) measure client

FPS = 120
EDGE0_WIFI = "192.168.1.150"    # edge0's WiFi-subnet source
AMARI_WIFI = "192.168.1.159"    # this host's WiFi (wlp0s20f0u5) — the receiver
PORT = 9000

S_SRV = "pv_srv_hw"             # server tmux on edge0 (unique; never the PTP ones)
STREAM_WARMUP = 3.0            # stream this long before measuring (path warms)
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

_client_proc = None            # local pulse_client Popen, killed on cleanup


# ------------------------------------------------------------------ helpers ---
def ssh(host, remote_cmd, timeout=30, capture=False):
    argv = ["ssh", *SSH_OPTS, host, remote_cmd]
    return subprocess.run(argv, timeout=timeout,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.STDOUT if capture else None, text=True)


def ssh_out(host, remote_cmd, timeout=30):
    try:
        return (ssh(host, remote_cmd, timeout=timeout, capture=True).stdout or "").strip()
    except Exception as exc:
        return f"<ssh {host} error: {exc}>"


def log(msg):
    print(f"[hevc-wifi {datetime.now():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------ cleanup ---
def cleanup_all(reason=""):
    """Kill the edge0 server and the local client. Safe to call repeatedly."""
    if reason:
        log(f"cleanup ({reason}) ...")
    ssh(EDGE0, f"tmux kill-session -t {S_SRV} 2>/dev/null; "
               f"pkill -9 -f 'pulse_server_singl[e]' 2>/dev/null; true", timeout=20)
    global _client_proc
    if _client_proc and _client_proc.poll() is None:
        try:
            _client_proc.terminate()
            time.sleep(0.3)
            if _client_proc.poll() is None:
                _client_proc.kill()
        except Exception:
            pass
    subprocess.run(["pkill", "-9", "-f", "pulse_clien[t] --single"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _sig_handler(signum, _frame):
    log(f"got signal {signum}")
    cleanup_all(reason=f"signal {signum}")
    sys.exit(130)


# --------------------------------------------------------------- preflight ---
def preflight(video_file):
    log("preflight ...")
    if "error" in ssh_out(EDGE0, "echo ok", timeout=10):
        sys.exit(f"cannot ssh {EDGE0}")
    if "MISSING" in ssh_out(EDGE0, f"test -f '{VIDEO_DIR}/{video_file}' && echo ok || echo MISSING"):
        avail = ssh_out(EDGE0, f"ls {VIDEO_DIR}/*.hevc 2>/dev/null")
        sys.exit(f"video not found on edge0: {VIDEO_DIR}/{video_file}\navailable:\n{avail}")
    if "MISSING" in ssh_out(EDGE0, f"test -x '{PULSE_DIR_EDGE0}/bin/pulse_server_single' && echo ok || echo MISSING"):
        sys.exit("missing pulse_server_single on edge0 (run: make bin/pulse_server_single)")
    if not Path(CLIENT_BIN).exists():
        sys.exit(f"missing local client {CLIENT_BIN} (run: cd /root/pulse-codec && make bin/pulse_client)")
    # amari WiFi iface must have the expected address
    if AMARI_WIFI not in subprocess.run(["ip", "-br", "a"], capture_output=True, text=True).stdout:
        sys.exit(f"amari WiFi {AMARI_WIFI} not up (check wlp0s20f0u5)")
    log("preflight ok")


def connectivity_check():
    """Ping amari's WiFi from edge0 (warms the path + confirms reachability)."""
    log("connectivity check / warmup ...")
    ok = "OK" in ssh_out(
        EDGE0, f"ping -c5 -i0.2 -W2 -I {EDGE0_WIFI} {AMARI_WIFI} >/dev/null 2>&1 "
               f"&& echo OK || echo FAIL", timeout=20)
    log(f"  edge0 {EDGE0_WIFI} -> amari {AMARI_WIFI}  {'OK' if ok else 'FAIL'}")
    return ok


# ------------------------------------------------------------------- runner ---
def run(video_file, duration, tag, out_dir, logdir):
    global _client_proc
    frames = int(duration * FPS)

    # 1) server on edge0 FIRST — stream so the path is warm before we measure
    src = f"{VIDEO_DIR}/{video_file}"
    srv = (f"cd {PULSE_DIR_EDGE0} && bin/pulse_server_single --src {src} "
           f"--dst {AMARI_WIFI}:{PORT} --bind {EDGE0_WIFI} --fps {FPS} "
           f"--loop >/tmp/{S_SRV}.log 2>&1")
    ssh(EDGE0, f"tmux kill-session -t {S_SRV} 2>/dev/null; "
               f"tmux new-session -d -s {S_SRV} '{srv}'", timeout=20)
    if "1" not in ssh_out(EDGE0, f"tmux has-session -t {S_SRV} 2>/dev/null && echo 1 || echo 0"):
        raise RuntimeError("server failed to start on edge0 (see /tmp/pv_srv_hw.log)")
    log("server started on edge0; streaming ...")

    # 2) warm the streaming path
    log(f"warming stream {STREAM_WARMUP:g}s before measuring ...")
    time.sleep(STREAM_WARMUP)

    # 3) client LOCALLY on amari (default CSV naming: results/hevc_snr_<tag>_<ts>.csv)
    out_dir.mkdir(parents=True, exist_ok=True)
    clilog = open(logdir / "client.log", "w")
    _client_proc = subprocess.Popen(
        [CLIENT_BIN, "--single", "--listen-a", f"0.0.0.0:{PORT}",
         "--frames", str(frames), "--tag", tag],
        cwd=str(out_dir.parent), stdout=clilog, stderr=subprocess.STDOUT)
    log(f"client started on amari ({frames} frames @ {FPS}fps ~= {duration:g}s); measuring ...")

    # 4) wait for the client to finish (it stops at --frames), with a margin
    try:
        _client_proc.wait(timeout=duration + 60)
        log("client finished")
    except subprocess.TimeoutExpired:
        log("client did not finish within margin; forcing stop")

    # 5) stop server
    ssh(EDGE0, f"tmux kill-session -t {S_SRV} 2>/dev/null; true", timeout=15)


# ----------------------------------------------------------------- results ---
def collect(out_dir, logdir):
    print("\n----- client summary -----")
    try:
        print((logdir / "client.log").read_text().strip())
    except Exception:
        pass
    csvs = sorted(out_dir.glob("hevc_snr_*.csv"), key=lambda p: p.stat().st_mtime)
    if not csvs:
        log("no CSV produced")
        return
    latest = csvs[-1]
    log(f"CSV -> {latest}")
    analyze(latest)


def _pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(p * len(v)))]


def analyze(path):
    rows = list(csv.reader(open(path)))[1:]
    v = []
    for r in rows:
        if len(r) > 1:
            try:
                x = float(r[1])
                if x >= 0:
                    v.append(x)
            except ValueError:
                pass
    if not v:
        print("no samples"); return
    print("\n----- analysis -----")
    print(f"HEVC single-stream (edge0 -> amari WiFi), frames={len(rows)}")
    print(f"  one-way ms: p50 {_pct(v,.5):.2f}  p95 {_pct(v,.95):.2f}  "
          f"p99 {_pct(v,.99):.2f}  p99.9 {_pct(v,.999):.2f}  max {max(v):.2f}  (n={len(v)})")


# -------------------------------------------------------------------- main ---
def main():
    global FPS
    ap = argparse.ArgumentParser(
        description="HEVC single-stream latency, edge0 -> amari over WiFi.")
    ap.add_argument("--video", required=True,
                    help="clip name in edge0:~/pulse-codec/compare_videos "
                         "(.hevc extension optional)")
    ap.add_argument("--duration", type=float, default=200.0, help="seconds (default 200)")
    ap.add_argument("--fps", type=int, default=FPS, help=f"frame rate (default {FPS})")
    ap.add_argument("--out-dir", default="/root/pulse-automation/results",
                    help="local dir for the CSV")
    args = ap.parse_args()
    FPS = args.fps
    video_file = args.video if args.video.endswith(".hevc") else args.video + ".hevc"
    tag = Path(video_file).stem
    out_dir = Path(args.out_dir)
    logdir = Path("/root/pulse-automation/logs") / datetime.now().strftime("hevcwifi_%Y%m%d_%H%M%S")
    logdir.mkdir(parents=True, exist_ok=True)

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    atexit.register(cleanup_all)

    log(f"video={video_file} duration={args.duration:g}s  edge0 {EDGE0_WIFI} -> amari {AMARI_WIFI}")
    preflight(video_file)
    cleanup_all(reason="pre-run stale sweep")
    if not connectivity_check():
        cleanup_all(reason="connectivity check failed")
        sys.exit("connectivity check FAILED — bring the WiFi path up and retry")

    try:
        run(video_file, args.duration, tag, out_dir, logdir)
        collect(out_dir, logdir)
    finally:
        cleanup_all(reason="run complete")
    log("done.")


if __name__ == "__main__":
    main()
