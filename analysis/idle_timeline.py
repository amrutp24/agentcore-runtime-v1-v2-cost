#!/usr/bin/env python3
"""Per-minute memory and CPU held by idle sessions, from CloudWatch (read-only).

  python analysis/idle_timeline.py [--variants a b ...] [--minutes 90]

Default variants: every variant in config.yaml with shape idle. For each, queries
AWS/Bedrock-AgentCore MemoryUsed-GBHours and CPUUsed-vCPUHours (Service=AgentCore.Runtime,
Resource=<runtime arn>, Sum, 60 s) starting at the variant's first invocation.
gb_held / vcpu_used = the per-minute value x 60 (hours -> average rate in that minute).
Writes results/idle_timeline.csv. All timestamps are UTC.
"""
import argparse
import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import yaml

ROOT = Path(__file__).resolve().parent.parent
NS = "AWS/Bedrock-AgentCore"


def first_invocation(path: Path) -> datetime:
    for line in path.read_text().splitlines():
        if line.strip():
            return datetime.fromisoformat(json.loads(line)["ts"]).astimezone(timezone.utc)
    raise SystemExit(f"{path} is empty")


def series(cw, arn, metric, start, end):
    """-> {minute_start_utc: sum}"""
    out = {}
    token = None
    while True:
        kw = {"NextToken": token} if token else {}
        r = cw.get_metric_data(
            MetricDataQueries=[{"Id": "m", "MetricStat": {
                "Metric": {"Namespace": NS, "MetricName": metric, "Dimensions": [
                    {"Name": "Service", "Value": "AgentCore.Runtime"},
                    {"Name": "Resource", "Value": arn}]},
                "Period": 60, "Stat": "Sum"}}],
            StartTime=start, EndTime=end, ScanBy="TimestampAscending", **kw)
        res = r["MetricDataResults"][0]
        for t, v in zip(res["Timestamps"], res["Values"]):
            out[t.astimezone(timezone.utc)] = v
        token = r.get("NextToken")
        if not token:
            return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--variants", nargs="+")
    p.add_argument("--minutes", type=int, default=90)
    p.add_argument("--config", default=os.environ.get("CONFIG", str(ROOT / "config.yaml")))
    args = p.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())
    rd = ROOT / cfg.get("results_dir", "results")
    variants = args.variants or [v for v, c in cfg["variants"].items() if c.get("shape") == "idle"]
    session = boto3.Session(profile_name=cfg.get("aws_profile") or None, region_name=cfg["region"])
    cw = session.client("cloudwatch")

    rows = []
    for v in variants:
        inv, outp = rd / v / "invocations.jsonl", rd / v / "outputs.json"
        if not inv.exists() or not outp.exists():
            print(f"{v}: no invocations.jsonl / outputs.json, skipping")
            continue
        arn = json.loads(outp.read_text())["agent_runtime_arn"]
        t0 = first_invocation(inv).replace(second=0, microsecond=0)
        end = min(t0 + timedelta(minutes=args.minutes), datetime.now(timezone.utc))
        mem, cpu = (series(cw, arn, m, t0, end) for m in ("MemoryUsed-GBHours", "CPUUsed-vCPUHours"))
        print(f"== {v} (from {t0:%Y-%m-%d %H:%M} UTC)")
        print(f"{'min':>4} {'timestamp_utc':20} {'gb_held':>9} {'vcpu_used':>10}")
        for i in range(args.minutes):
            t = t0 + timedelta(minutes=i)
            if t >= end:
                break
            gb, vc = mem.get(t, 0.0) * 60, cpu.get(t, 0.0) * 60
            ts = t.strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append([v, i, ts, f"{gb:.6f}", f"{vc:.6f}"])
            print(f"{i:>4} {ts:20} {gb:9.4f} {vc:10.5f}")

    out = rd / "idle_timeline.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["variant", "minute_offset", "timestamp_utc", "gb_held", "vcpu_used"])
        w.writerows(rows)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
