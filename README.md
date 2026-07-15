# pulse-automation

One-button driver for the **pulse-codec streaming latency** experiment (edge0 → Pi
real 5G UE). Run it on **amari** (this host).

```
sudo python3 run_stream_latency.py --mode <pulse|hevc> --video <name> [options]
```

## Modes

| Mode | Stream | Links |
|---|---|---|
| `pulse` | two-stream layered codec | **base → 5G** (edge0 10.45.0.1 → pi 10.45.0.23:9000), **base+enh → WiFi** (edge0 192.168.1.150 → pi 192.168.1.149:9001) |
| `hevc` | single-stream HEVC baseline | **WiFi only** (edge0 192.168.1.150 → pi 192.168.1.149:9000) |

## Options

- `--video NAME` — clip in `edge0:~/pulse-codec/compare_videos` (extension optional;
  `.pulse` for pulse, `.hevc` for hevc). e.g. `beauty4k_pulse62Mbps_snr47.8`.
- `--duration SEC` — default **200**.
- `--ft N` — **pulse only**: N file-transfer users on 5G (`file_transfer.py`,
  edge0 → amari sim UEs `ue1..ueN` = `10.45.0.2..`), the same TCP/BBR contention
  as the 5g-dl-property-pi downlink test. Competes with the base layer on 5G.
  Ignored in hevc mode (WiFi-only stream never touches 5G). Needs `sudo` (netns).
- `--enh-wait-ms MS` — pulse only, default **300**: measure enhance's *real*
  arrival latency up to this long past base (not censored at the skip deadline).
- `--skip-ms MS` — pulse only, default 10: display deadline, only used for the
  `used/skip/lost` label in `enh_state`.
- `--out-dir DIR` — where to pull the CSV locally (default `./results`).

## Output

The Pi client auto-names its CSV under `~/pulse-codec/results/`
(`<mode>_snr_<tag>_<ts>.csv`, tag = clip[_ftN]); the driver pulls the newest one
to `--out-dir` and prints a percentile summary. Per-run logs (FT etc.) go to
`logs/<ts>/`.

## Behavior

- FT (if any) starts first and warms 3 s, so the stream is measured under
  established 5G contention.
- 5G is warmed with a ping burst before a pulse run (RRC idle→active).
- **On exit — success, error, or Ctrl-C — every experiment process on all three
  hosts is killed** (video server/client, FT servers/clients). The PTP time-sync
  tmux sessions (`ptp`/`ptps`/`phy`/`tg`) are never touched.

## Examples

```bash
# pulse two-stream, 200 s, no contention
sudo python3 run_stream_latency.py --mode pulse --video beauty4k_pulse62Mbps_snr47.8

# pulse two-stream with 4 file-transfer users hammering 5G
sudo python3 run_stream_latency.py --mode pulse --video beauty4k_pulse62Mbps_snr47.8 --ft 4

# HEVC single-stream baseline over WiFi
sudo python3 run_stream_latency.py --mode hevc --video beauty4k_hevc64Mbps_snr47.8

# quick 15 s check
sudo python3 run_stream_latency.py --mode pulse --video beauty4k_pulse21Mbps_snr44.6 --duration 15
```

## Prerequisites

- pulse-codec built: `bin/pulse_server_file` + `bin/pulse_server_single` on edge0,
  `bin/pulse_client` on pi (`make bin/...`).
- Pi 5G up (`wwan0` 10.45.0.23) and WiFi up (`wlan0` 192.168.1.149).
- edge0 on TCP **BBR** (persisted) for representative FT.
- All hosts PTP time-synced (one-way latency needs synced `CLOCK_REALTIME`).
