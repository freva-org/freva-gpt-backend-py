#!/usr/bin/env python3
import csv
import math
import os
import statistics
import sys

import matplotlib.pyplot as plt


def percentile(sorted_vals, q):
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def read_run_info(path):
    info = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            info[k] = v
    return info


def read_metrics_csv(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 6:
                continue
            rows.append(
                {
                    "id": row[0],
                    "http_code": row[1],
                    "time_connect": float(row[2]),
                    "ttfb": float(row[3]),
                    "total": float(row[4]),
                    "size_download": float(row[5]),
                }
            )
    return rows


def summarize_run(run_dir):
    info = read_run_info(os.path.join(run_dir, "run_info.env"))
    rows = read_metrics_csv(os.path.join(run_dir, "all_metrics.csv"))

    ttfb = sorted(r["ttfb"] for r in rows)
    total = sorted(r["total"] for r in rows)
    ok = sum(1 for r in rows if r["http_code"].startswith("2"))
    err = len(rows) - ok

    return {
        "label": info["label"],
        "parallel": int(info["parallel"]),
        "requests": int(info["requests"]),
        "batch_seconds": float(info["batch_seconds"]),
        "ok": ok,
        "err": err,
        "ttfb_avg": statistics.mean(ttfb),
        "ttfb_p50": percentile(ttfb, 0.50),
        "ttfb_p95": percentile(ttfb, 0.95),
        "total_avg": statistics.mean(total),
        "total_p50": percentile(total, 0.50),
        "total_p95": percentile(total, 0.95),
    }


def collect_runs(base_dir):
    runs = []
    for name in os.listdir(base_dir):
        run_dir = os.path.join(base_dir, name)
        if not os.path.isdir(run_dir):
            continue
        info_file = os.path.join(run_dir, "run_info.env")
        metrics_file = os.path.join(run_dir, "all_metrics.csv")
        if os.path.exists(info_file) and os.path.exists(metrics_file):
            runs.append(summarize_run(run_dir))
    runs.sort(key=lambda x: x["parallel"])
    return runs


def write_summary_csv(base_dir, runs):
    out_path = os.path.join(base_dir, "summary.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "label",
                "parallel",
                "requests",
                "batch_seconds",
                "ok",
                "err",
                "ttfb_avg",
                "ttfb_p50",
                "ttfb_p95",
                "total_avg",
                "total_p50",
                "total_p95",
            ],
        )
        writer.writeheader()
        writer.writerows(runs)
    return out_path


def plot_metric(base_dir, runs, y_keys, title, ylabel, filename):
    x = [r["parallel"] for r in runs]

    plt.figure(figsize=(8, 5))
    for key in y_keys:
        plt.plot(x, [r[key] for r in runs], marker="o", label=key)
    plt.xlabel("Parallel calls")
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(True, alpha=0.3)
    if len(y_keys) > 1:
        plt.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(base_dir, filename), dpi=150)
    plt.close()


def print_table(runs):
    print()
    print(
        f"{'parallel':>8}  {'batch_s':>8}  {'ttfb_p50':>9}  {'ttfb_p95':>9}  {'total_p50':>10}  {'total_p95':>10}  {'err':>5}"
    )
    for r in runs:
        print(
            f"{r['parallel']:>8}  "
            f"{r['batch_seconds']:>8.3f}  "
            f"{r['ttfb_p50']:>9.3f}  "
            f"{r['ttfb_p95']:>9.3f}  "
            f"{r['total_p50']:>10.3f}  "
            f"{r['total_p95']:>10.3f}  "
            f"{r['err']:>5}"
        )
    print()


def main():
    if len(sys.argv) != 2:
        print("usage: python3 summarize_plot.py <performance/rX>")
        sys.exit(1)

    base_dir = sys.argv[1]
    runs = collect_runs(base_dir)
    if not runs:
        print("no valid runs found")
        sys.exit(1)

    csv_path = write_summary_csv(base_dir, runs)
    print_table(runs)

    plot_metric(
        base_dir,
        runs,
        ["batch_seconds"],
        "Batch duration vs parallel calls",
        "Seconds",
        "batch_seconds_vs_parallel.png",
    )
    plot_metric(
        base_dir,
        runs,
        ["ttfb_p50", "ttfb_p95"],
        "TTFB vs parallel calls",
        "Seconds",
        "ttfb_vs_parallel.png",
    )
    plot_metric(
        base_dir,
        runs,
        ["total_p50", "total_p95"],
        "Total request duration vs parallel calls",
        "Seconds",
        "total_duration_vs_parallel.png",
    )
    plot_metric(
        base_dir,
        runs,
        ["err"],
        "Errors vs parallel calls",
        "Errors",
        "errors_vs_parallel.png",
    )

    print(f"wrote: {csv_path}")
    print(f"wrote: {os.path.join(base_dir, 'batch_seconds_vs_parallel.png')}")
    print(f"wrote: {os.path.join(base_dir, 'ttfb_vs_parallel.png')}")
    print(f"wrote: {os.path.join(base_dir, 'total_duration_vs_parallel.png')}")
    print(f"wrote: {os.path.join(base_dir, 'errors_vs_parallel.png')}")


if __name__ == "__main__":
    main()