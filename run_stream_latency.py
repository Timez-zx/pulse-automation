#!/usr/bin/env python3
"""
run_stream_latency.py — one-button pulse-codec streaming latency experiment.

Runs on the Amarisoft box (amari, this host). Streams a pre-encoded clip from
edge0 to the real Pi 5G UE and measures per-frame one-way latency with
pulse-codec's own client. Two modes:

  * pulse : two-stream layered codec.
              base       -> 5G   (edge0 10.45.0.1     -> pi 10.45.0.23:9000)
              base+enh   -> WiFi (edge0 192.168.1.150 -> pi 192.168.1.149:9001)
            enhance real-arrival latency is measured (--enh-wait-ms, default 300).
            Optional 5G file-transfer contention (--ft N): TCP file_transfer
            edge0 -> amari sim UEs ue1..ueN (10.45.0.<i+1>), same as the
            5g-dl-property-pi downlink test, competing with the base layer on 5G.

  * hevc  : single-stream HEVC baseline, WiFi only
              (edge0 192.168.1.150 -> pi 192.168.1.149:9000).
            (--ft is ignored: the stream never touches 5G.)

Both default to 200 s. The client auto-names its CSV under
edge0/pi ~/pulse-codec/results/ (default naming); this driver pulls the newest
one to --out-dir after the run. On exit (success, error, or Ctrl-C) ALL
processes on all three hosts are cleaned up.

Examples:
  sudo python3 run_stream_latency.py --mode pulse --video beauty4k_pulse62Mbps_snr47.8
  sudo python3 run_stream_latency.py --mode pulse --video beauty4k_pulse62Mbps_snr47.8 --ft 4
  sudo python3 run_stream_latency.py --mode hevc  --video beauty4k_hevc64Mbps_snr47.8
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
EDGE0 = "edge0"                    # ssh alias, srsRAN gNB + Open5GS core + sender
PI = "rpi"                         # ssh alias, real 5G UE (latency endpoint)

PULSE_DIR = "/home/zx/pulse-codec"
VIDEO_DIR = f"{PULSE_DIR}/compare_videos"
PI_RESULTS = f"{PULSE_DIR}/results"
FT_SCRIPT_EDGE0 = "/home/zx/pulse-measurement/5g-dl-property-pi/file_transfer.py"
FT_SCRIPT_LOCAL = "/root/pulse-measurement/5g-dl-property-pi/file_transfer.py"

FPS = 120

# link endpoints
EDGE0_5G, PI_5G = "10.45.0.1", "10.45.0.23"        # base (link A)
EDGE0_WIFI, PI_WIFI = "192.168.1.150", "192.168.1.149"  # base+enh / single (link B)

# ports
PORT_A, PORT_B = 9000, 9001       # pulse two-stream
PORT_SINGLE = 9000                # hevc single-stream (WiFi)
FT_PORT = 5201                    # file_transfer TCP

# tmux session names (MUST NOT collide with the PTP sessions ptp/ptps/phy/tg)
S_SRV = "pv_srv"                  # video server on edge0
S_CLI = "pv_cli"                  # video client on pi
S_FTP = "pv_ft"                   # FT clients on edge0 (pv_ft1..pv_ftN)

FT_WARMUP = 3.0                   # let contention ramp before the stream starts
FT_COOLDOWN = 2.0                 # keep contention alive a bit past stream end
STREAM_WARMUP = 3.0               # stream the video this long (path warms) before measuring
PREFLIGHT_TARGET = "192.168.2.2"  # edge0 direct-link IP, pingable from each sim-UE netns
PREFLIGHT_PI_5G_COUNT = 5         # edge0->Pi 5G warm-up pings (wake RRC + confirm reach)

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

# processes we started locally (netns FT servers), terminated on cleanup
_local_procs: list[subprocess.Popen] = []


# ------------------------------------------------------------------ helpers ---
def ue_ip(i: int) -> str:
    """1-based sim-UE index -> 5G IP (ue1 -> 10.45.0.2)."""
    return f"10.45.0.{i + 1}"


def ssh(host: str, remote_cmd: str, timeout: int = 30, check: bool = False,
        capture: bool = False):
    """Run a command on `host` over SSH. remote_cmd is one string for the remote shell."""
    argv = ["ssh", *SSH_OPTS, host, remote_cmd]
    return subprocess.run(argv, timeout=timeout, check=check,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.STDOUT if capture else None,
                          text=True)


def ssh_out(host: str, remote_cmd: str, timeout: int = 30) -> str:
    try:
        r = ssh(host, remote_cmd, timeout=timeout, capture=True)
        return (r.stdout or "").strip()
    except Exception as exc:
        return f"<ssh {host} error: {exc}>"


def log(msg: str) -> None:
    print(f"[auto {datetime.now():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------ cleanup ---
def cleanup_all(reason: str = "") -> None:
    """Kill every experiment process on all three hosts. Safe to call repeatedly.

    pkill patterns use the bracket trick (e.g. pulse_server_fil[e]) so the pkill
    command's own remote shell cmdline is never matched and self-killed."""
    if reason:
        log(f"cleanup ({reason}) ...")
    # edge0: video server + FT clients + any stragglers
    ssh(EDGE0,
        "tmux kill-session -t %s 2>/dev/null; "
        "for s in $(tmux ls 2>/dev/null | grep '^%s' | cut -d: -f1); do "
        "tmux kill-session -t \"$s\" 2>/dev/null; done; "
        "pkill -9 -f 'pulse_server_fil[e]' 2>/dev/null; "
        "pkill -9 -f 'pulse_server_singl[e]' 2>/dev/null; "
        "pkill -9 -f 'file_transfer[.]py -c' 2>/dev/null; true" % (S_SRV, S_FTP),
        timeout=20)
    # pi: video client
    ssh(PI,
        "tmux kill-session -t %s 2>/dev/null; "
        "pkill -9 -f 'pulse_clien[t]' 2>/dev/null; true" % S_CLI,
        timeout=20)
    # amari (local): netns FT servers
    for p in _local_procs:
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
    time.sleep(0.5)
    for p in _local_procs:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass
    subprocess.run(["pkill", "-9", "-f", "file_transfer[.]py -s"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _sig_handler(signum, _frame):
    log(f"got signal {signum}")
    cleanup_all(reason=f"signal {signum}")
    sys.exit(130)


# --------------------------------------------------------------- preflight ---
def preflight(mode: str, video_file: str, ft: int) -> None:
    log("preflight ...")
    # ssh reachability
    for host in (EDGE0, PI):
        if "error" in ssh_out(host, "echo ok", timeout=10):
            sys.exit(f"cannot ssh {host}")
    # video file on edge0
    if "MISSING" in ssh_out(EDGE0, f"test -f '{VIDEO_DIR}/{video_file}' && echo ok || echo MISSING"):
        avail = ssh_out(EDGE0, f"ls {VIDEO_DIR} 2>/dev/null")
        sys.exit(f"video not found on edge0: {VIDEO_DIR}/{video_file}\navailable:\n{avail}")
    # binaries
    srv_bin = "pulse_server_file" if mode == "pulse" else "pulse_server_single"
    if "MISSING" in ssh_out(EDGE0, f"test -x '{PULSE_DIR}/bin/{srv_bin}' && echo ok || echo MISSING"):
        sys.exit(f"missing {srv_bin} on edge0 (run: make bin/{srv_bin})")
    if "MISSING" in ssh_out(PI, f"test -x '{PULSE_DIR}/bin/pulse_client' && echo ok || echo MISSING"):
        sys.exit("missing pulse_client on pi (run: make bin/pulse_client)")
    # FT prerequisites
    if ft > 0 and mode == "pulse":
        if os.geteuid() != 0:
            sys.exit("--ft needs root on amari (ip netns exec). Re-run with sudo.")
        ns = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True).stdout
        for i in range(1, ft + 1):
            if f"ue{i}" not in ns:
                sys.exit(f"netns ue{i} not present on amari (need ue1..ue{ft})")
        if not Path(FT_SCRIPT_LOCAL).exists():
            sys.exit(f"missing local FT script {FT_SCRIPT_LOCAL}")
    log("preflight ok")


def connectivity_check(mode: str, ft: int) -> bool:
    """Ping every UE / path this run will use, to warm the radio (RRC idle->active)
    and confirm reachability BEFORE the experiment. Returns False if any required
    path is unreachable. Mirrors run_dl_measurements.py's warmup + preflight:
    edge0->Pi over each link, and each FT sim-UE from its own netns."""
    log("connectivity check / warmup (ping all used UEs) ...")
    ok = True
    # Pi over WiFi — used by both modes (pulse base+enh link B / hevc single stream)
    wifi_ok = "OK" in ssh_out(
        EDGE0, f"ping -c3 -i0.2 -W2 -I {EDGE0_WIFI} {PI_WIFI} >/dev/null 2>&1 "
               f"&& echo OK || echo FAIL", timeout=20)
    log(f"  Pi WiFi  {PI_WIFI:<15} {'OK' if wifi_ok else 'FAIL'}")
    ok = ok and wifi_ok
    # Pi over 5G — pulse base link A; the pings also wake the modem from RRC-idle
    if mode == "pulse":
        g5_ok = "OK" in ssh_out(
            EDGE0, f"ping -c{PREFLIGHT_PI_5G_COUNT} -i0.2 -W2 -I {EDGE0_5G} {PI_5G} "
                   f">/dev/null 2>&1 && echo OK || echo FAIL", timeout=20)
        log(f"  Pi 5G    {PI_5G:<15} {'OK' if g5_ok else 'FAIL'}")
        ok = ok and g5_ok
    # each FT sim-UE, pinged from inside its netns (uplink to edge0) — confirms attach
    if ft > 0 and mode == "pulse":
        for i in range(1, ft + 1):
            rc = subprocess.run(
                ["ip", "netns", "exec", f"ue{i}", "ping", "-c", "3",
                 "-i", "0.2", "-W", "2", PREFLIGHT_TARGET],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
            ue_ok = rc == 0
            log(f"  ue{i:<2} ({ue_ip(i)}) -> {PREFLIGHT_TARGET}  {'OK' if ue_ok else 'FAIL'}")
            ok = ok and ue_ok
    return ok


# --------------------------------------------------------------- FT control ---
def start_ft(ft: int, ft_duration: float, logdir: Path) -> None:
    """Start FT servers in local netns ue1..ueN and FT clients on edge0 (-> 5G)."""
    # servers: amari netns
    subprocess.run(["pkill", "-9", "-f", "file_transfer[.]py -s"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.4)
    for i in range(1, ft + 1):
        lf = open(logdir / f"ft_server_ue{i}.log", "w")
        p = subprocess.Popen(
            ["ip", "netns", "exec", f"ue{i}", sys.executable, "-u",
             FT_SCRIPT_LOCAL, "-s", "-p", str(FT_PORT)],
            stdout=lf, stderr=subprocess.STDOUT)
        _local_procs.append(p)
    log(f"started {ft} FT server(s) in netns ue1..ue{ft} (TCP :{FT_PORT})")
    time.sleep(1.0)
    # clients: edge0, one detached tmux session each, sending to the UE 5G IPs
    launch = "; ".join(
        f"tmux new-session -d -s {S_FTP}{i} "
        f"'python3 -u {FT_SCRIPT_EDGE0} -c -i {ue_ip(i)} -p {FT_PORT} "
        f"-t {ft_duration:.0f} >/tmp/{S_FTP}{i}.log 2>&1'"
        for i in range(1, ft + 1))
    ssh(EDGE0, launch, timeout=30)
    log(f"started {ft} FT client(s) on edge0 -> {ue_ip(1)}..{ue_ip(ft)} for {ft_duration:.0f}s")


# ------------------------------------------------------------------- runner ---
def run(mode: str, video_file: str, duration: float, ft: int,
        enh_wait_ms: int, skip_ms: int, tag: str, logdir: Path) -> None:
    frames = int(duration * FPS)

    # 1) FT contention first (pulse only), then let it ramp
    if ft > 0 and mode == "pulse":
        start_ft(ft, FT_WARMUP + STREAM_WARMUP + duration + FT_COOLDOWN, logdir)
        log(f"warming contention {FT_WARMUP:g}s ...")
        time.sleep(FT_WARMUP)

    # 2) server on edge0 FIRST — start streaming so the radio/path is warm and
    #    contention is fully established before the client begins measuring
    src = f"{VIDEO_DIR}/{video_file}"
    if mode == "pulse":
        srv = (f"cd {PULSE_DIR} && bin/pulse_server_file --src {src} "
               f"--dst-a {PI_5G}:{PORT_A} --bind-a {EDGE0_5G} "
               f"--dst-b {PI_WIFI}:{PORT_B} --bind-b {EDGE0_WIFI} "
               f"--loop >/tmp/{S_SRV}.log 2>&1")
    else:
        srv = (f"cd {PULSE_DIR} && bin/pulse_server_single --src {src} "
               f"--dst {PI_WIFI}:{PORT_SINGLE} --bind {EDGE0_WIFI} "
               f"--fps {FPS} --loop >/tmp/{S_SRV}.log 2>&1")
    ssh(EDGE0, f"tmux kill-session -t {S_SRV} 2>/dev/null; "
               f"tmux new-session -d -s {S_SRV} '{srv}'", timeout=20)
    if "1" not in ssh_out(EDGE0, f"tmux has-session -t {S_SRV} 2>/dev/null && echo 1 || echo 0"):
        raise RuntimeError("server failed to start on edge0 (see /tmp/pv_srv.log)")
    log("server started on edge0; streaming ...")

    # 3) warm the streaming path before measuring (server is already sending)
    log(f"warming stream {STREAM_WARMUP:g}s before measuring ...")
    time.sleep(STREAM_WARMUP)

    # 4) client on pi (measures under an established stream + contention).
    #    Default CSV naming: results/<mode>_snr_<tag>_<ts>.csv
    if mode == "pulse":
        cli = (f"cd {PULSE_DIR} && mkdir -p results && "
               f"bin/pulse_client --listen-a 0.0.0.0:{PORT_A} --listen-b 0.0.0.0:{PORT_B} "
               f"--skip-ms {skip_ms} --enh-wait-ms {enh_wait_ms} "
               f"--frames {frames} --tag {tag} >/tmp/{S_CLI}.log 2>&1")
    else:
        cli = (f"cd {PULSE_DIR} && mkdir -p results && "
               f"bin/pulse_client --single --listen-a 0.0.0.0:{PORT_SINGLE} "
               f"--frames {frames} --tag {tag} >/tmp/{S_CLI}.log 2>&1")
    ssh(PI, f"tmux kill-session -t {S_CLI} 2>/dev/null; "
            f"tmux new-session -d -s {S_CLI} '{cli}'", timeout=20)
    if "1" not in ssh_out(PI, f"tmux has-session -t {S_CLI} 2>/dev/null && echo 1 || echo 0"):
        raise RuntimeError("client failed to start on pi (see /tmp/pv_cli.log)")
    log(f"client started on pi ({frames} frames @ {FPS}fps ~= {duration:g}s); measuring ...")

    # 5) wait for the client to finish (it stops at --frames), with a margin
    deadline = time.time() + duration + 60
    while time.time() < deadline:
        time.sleep(5)
        if "1" not in ssh_out(PI, f"tmux has-session -t {S_CLI} 2>/dev/null && echo 1 || echo 0"):
            log("client finished")
            break
    else:
        log("client did not finish within margin; forcing stop")

    # 6) stop server + FT (client already exited)
    ssh(EDGE0, f"tmux kill-session -t {S_SRV} 2>/dev/null; true", timeout=15)


# ----------------------------------------------------------------- results ---
def collect(mode: str, out_dir: Path) -> Path | None:
    """Print the client summary and pull the newest CSV from pi."""
    print("\n----- client summary (pi) -----")
    print(ssh_out(PI, f"cat /tmp/{S_CLI}.log", timeout=15))
    newest = ssh_out(PI, f"ls -t {PI_RESULTS}/*.csv 2>/dev/null | head -1")
    if not newest or "error" in newest or not newest.endswith(".csv"):
        log("no CSV found on pi")
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    local = out_dir / Path(newest).name
    try:
        subprocess.run(["scp", *SSH_OPTS, f"{PI}:{newest}", str(local)],
                       check=True, stdout=subprocess.DEVNULL)
    except Exception as exc:
        log(f"scp failed: {exc}")
        return None
    log(f"CSV pulled -> {local}")
    analyze(mode, local)
    return local


def _pct(vals, p):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(p * len(vals)))] if vals else float("nan")


def _num(x):
    try:
        f = float(x)
        return f if f >= 0 else None
    except Exception:
        return None


def analyze(mode: str, path: Path) -> None:
    rows = list(csv.reader(open(path)))[1:]
    print("\n----- analysis -----")
    if mode == "hevc":
        v = [x for x in (_num(r[1]) for r in rows if len(r) > 1) if x is not None]
        if not v:
            print("no samples"); return
        print(f"HEVC single-stream (WiFi), frames={len(rows)}")
        print(f"  one-way ms: p50 {_pct(v,.5):.2f}  p95 {_pct(v,.95):.2f}  "
              f"p99 {_pct(v,.99):.2f}  max {max(v):.2f}")
        return
    base = [x for x in (_num(r[1]) for r in rows if len(r) > 1) if x is not None]
    enh = [x for x in (_num(r[2]) for r in rows if len(r) > 2) if x is not None]
    st = {}
    for r in rows:
        if len(r) > 3:
            st[r[3]] = st.get(r[3], 0) + 1
    print(f"PULSE two-stream (base=5G, base+enh=WiFi), frames={len(rows)}")
    if base:
        print(f"  base one-way ms: p50 {_pct(base,.5):.2f}  p95 {_pct(base,.95):.2f}  "
              f"p99 {_pct(base,.99):.2f}  max {max(base):.2f}")
    if enh:
        print(f"  enh  one-way ms: p50 {_pct(enh,.5):.2f}  p95 {_pct(enh,.95):.2f}  "
              f"p99 {_pct(enh,.99):.2f}  max {max(enh):.2f}  (n={len(enh)})")
    if st:
        print(f"  enh_state: {st}")


# -------------------------------------------------------------------- main ---
def main() -> None:
    global FPS
    ap = argparse.ArgumentParser(
        description="One-button pulse-codec streaming latency experiment (edge0->pi).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", required=True, choices=["pulse", "hevc"])
    ap.add_argument("--video", required=True,
                    help="clip name in edge0:~/pulse-codec/compare_videos "
                         "(extension optional; .pulse for pulse, .hevc for hevc)")
    ap.add_argument("--duration", type=float, default=200.0, help="seconds (default 200)")
    ap.add_argument("--ft", type=int, default=0,
                    help="number of 5G file-transfer users ue1..ueN (pulse only)")
    ap.add_argument("--enh-wait-ms", type=int, default=300,
                    help="pulse: measure enhance arrival up to this long past base (default 300)")
    ap.add_argument("--skip-ms", type=int, default=10,
                    help="pulse: display deadline used only for the used/skip label (default 10)")
    ap.add_argument("--fps", type=int, default=FPS, help=f"frame rate (default {FPS})")
    ap.add_argument("--out-dir", default="/root/pulse-automation/results",
                    help="local dir to pull the CSV into")
    args = ap.parse_args()
    FPS = args.fps

    # resolve filename + extension
    ext = ".pulse" if args.mode == "pulse" else ".hevc"
    video_file = args.video if args.video.endswith(ext) else args.video + ext

    ft = args.ft
    if ft > 0 and args.mode == "hevc":
        log("note: --ft ignored in hevc mode (single stream is WiFi-only, never touches 5G)")
        ft = 0

    tag = (f"{Path(video_file).stem}_ft{ft}" if args.mode == "pulse"
           else Path(video_file).stem)

    logdir = Path("/root/pulse-automation/logs") / datetime.now().strftime("%Y%m%d_%H%M%S")
    logdir.mkdir(parents=True, exist_ok=True)

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    atexit.register(cleanup_all)

    log(f"mode={args.mode} video={video_file} duration={args.duration:g}s "
        f"ft={ft} enh_wait_ms={args.enh_wait_ms}")
    preflight(args.mode, video_file, ft)
    cleanup_all(reason="pre-run stale sweep")

    # warm + confirm every UE/path used, before touching the experiment
    if not connectivity_check(args.mode, ft):
        cleanup_all(reason="connectivity check failed")
        sys.exit("connectivity check FAILED (see above) — bring the UE(s)/path up "
                 "(e.g. `ssh rpi sudo /home/zx/5g-up.sh`, or restart LTE on amari) and retry")

    try:
        run(args.mode, video_file, args.duration, ft,
            args.enh_wait_ms, args.skip_ms, tag, logdir)
        collect(args.mode, Path(args.out_dir))
    finally:
        cleanup_all(reason="run complete")
    log("done.")


if __name__ == "__main__":
    main()
