#!/usr/bin/env python3
"""
latency_ccdf.py — CCDF comparison of the 5 streaming-latency conditions.

One CCDF curve per condition (P[latency > x], log-x + log-y to spread the tail),
saved to figures/. Same plot style as 5g-dl-property-pi/latency_ccdf.py.

    HEVC              hevc single-stream, WiFi, no contention
    HEVC +wifi        hevc single-stream, WiFi + WiFi contention
    Pulse             pulse two-stream, base metric, no contention
    Pulse +wifi       pulse two-stream, base metric, + WiFi contention
    Pulse +wifi+5g    pulse two-stream, base metric, + WiFi + 5G contention

Legend shortening: the full names ("pulse+wifi-contention+5g-contention") are
unwieldy, so each is reduced to codec + terse load tags, and the meaning of the
tags is spelled out once in the legend title:
    +wifi = WiFi contention,  +5g = 5G contention.

Pulse curves use the BASE-layer latency (lat_base_ms) — the first-displayable
frame, which is what the 5G-contention condition actually stresses (base rides
5G). Set PULSE_METRIC = "enh" to compare full-quality (lat_enh_ms) instead.

Usage:  python3 latency_ccdf.py [--results DIR] [--out FILE]
Files are matched by name substrings in the results dir (newest wins), so the
timestamped default filenames from run_stream_latency.py are picked up as-is.
"""

import argparse
import csv
import glob
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import ScalarFormatter, NullFormatter

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(SCRIPT_DIR, "results")
FIGURES_DIR = os.path.join(SCRIPT_DIR, "figures")

PULSE_METRIC = "base"          # "base" (lat_base_ms) or "enh" (lat_enh_ms)

# Each series: short legend + how to find its CSV (all `inc` substrings present,
# no `exc` substring) + how to read latency ("hevc" -> latency_ms col, "pulse" ->
# the PULSE_METRIC column). Order also sets color/marker (palette below).
SERIES = [
    dict(label="HEVC",           inc=["hevc_"],                    exc=["wifiB"], kind="hevc"),
    dict(label="HEVC +wifi",     inc=["hevc_", "wifiB"],           exc=[],        kind="hevc"),
    dict(label="Pulse",          inc=["pulse_", "ft0_"],           exc=["wifiB"], kind="pulse"),
    dict(label="Pulse +wifi",    inc=["pulse_", "ft0_", "wifiB"],  exc=[],        kind="pulse"),
    dict(label="Pulse +wifi+5g", inc=["pulse_", "ft10_", "wifiB"], exc=[],        kind="pulse"),
]

# --- style, matched to 5g-dl-property-pi/latency_ccdf.py ---
LINE_COLORS = ["#0077BB", "#33BBEE", "#009988", "#EE7733", "#CC3311", "#AA3377"]
LINE_STYLES = [
    {"linestyle": "-",  "marker": "o"},
    {"linestyle": "--", "marker": "s"},
    {"linestyle": "-",  "marker": "^"},
    {"linestyle": "--", "marker": "D"},
    {"linestyle": "-",  "marker": "v"},
    {"linestyle": "--", "marker": "P"},
]
RC_STYLE = {
    "figure.facecolor": "white",
    "axes.facecolor":   "white",
    "savefig.facecolor": "white",
    "axes.labelsize":  22,
    "xtick.labelsize": 20,
    "ytick.labelsize": 20,
    "legend.fontsize": 17,
    "legend.title_fontsize": 17,
}


def find_csv(results, inc, exc):
    """Newest CSV in `results` whose name contains all `inc` and no `exc`."""
    cands = [p for p in glob.glob(os.path.join(results, "*.csv"))
             if all(s in os.path.basename(p) for s in inc)
             and not any(s in os.path.basename(p) for s in exc)]
    return max(cands, key=os.path.getmtime) if cands else None


def read_latencies(path, kind):
    """Return the latency samples (ms) for a CSV, skipping lost/NA rows."""
    col = 1 if kind == "hevc" else (1 if PULSE_METRIC == "base" else 2)
    out = []
    for row in list(csv.reader(open(path)))[1:]:
        if len(row) <= col:
            continue
        try:
            v = float(row[col])
        except ValueError:
            continue          # "", "NA", "SKIP"
        if v >= 0:
            out.append(v)
    return out


def pct(vals, p):
    v = sorted(vals)
    return v[min(len(v) - 1, int(p * len(v)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=RESULTS_DIR)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    os.makedirs(FIGURES_DIR, exist_ok=True)
    out = args.out or os.path.join(FIGURES_DIR, f"ccdf_compare_{PULSE_METRIC}.png")

    print(f"{'series':<16} {'n':>7} {'p50':>7} {'p95':>7} {'p99':>7} {'max':>8}")
    with plt.rc_context(RC_STYLE):
        fig, ax = plt.subplots(figsize=(9, 6))
        plotted = 0
        for idx, s in enumerate(SERIES):
            path = find_csv(args.results, s["inc"], s["exc"])
            if not path:
                print(f"{s['label']:<16}  (no matching CSV — skipped)")
                continue
            vals = read_latencies(path, s["kind"])
            if not vals:
                print(f"{s['label']:<16}  (empty — skipped)")
                continue
            xs = np.sort(np.asarray(vals))
            n = xs.size
            ccdf = 1.0 - np.arange(n) / n            # (0,1], safe for log-y
            style = LINE_STYLES[idx % len(LINE_STYLES)]
            ax.plot(xs, ccdf, label=s["label"],
                    color=LINE_COLORS[idx % len(LINE_COLORS)],
                    linestyle=style["linestyle"], marker=style["marker"],
                    markevery=max(1, n // 10), markersize=9,
                    markeredgewidth=1.0, markeredgecolor="white",
                    linewidth=2.4, zorder=3)
            print(f"{s['label']:<16} {n:>7} {pct(vals,.5):>7.2f} "
                  f"{pct(vals,.95):>7.2f} {pct(vals,.99):>7.2f} {max(vals):>8.2f}")
            plotted += 1
        if not plotted:
            sys.exit(f"no CSVs matched in {args.results}")

        ax.set_xscale("log")
        ax.set_xticks([3, 5, 10, 20, 50, 100, 200, 300])
        ax.xaxis.set_major_formatter(ScalarFormatter())
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.set_xlabel("One-way Latency (ms)")
        ax.set_yscale("log")
        ax.set_ylim(1e-4, 1.2)
        ax.set_ylabel("CCDF  (P[latency > x])")
        ax.grid(True, which="major", linestyle="--", alpha=0.45)
        ax.grid(True, which="minor", linestyle=":",  alpha=0.25)
        ax.legend(loc="lower left", framealpha=0.25, edgecolor="0.7")
        fig.tight_layout()
        fig.savefig(out, dpi=150)
        plt.close(fig)
    print(f"\nsaved -> {out}")


if __name__ == "__main__":
    main()
