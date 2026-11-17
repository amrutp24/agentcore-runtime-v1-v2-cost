#!/usr/bin/env python3
"""Pull AgentCore Runtime USAGE_LOGS from each variant's CloudWatch log group into CSV.

  python analysis/collect_usage_logs.py [--config config.yaml] [--start 2026-11-15] [--end 2026-11-21]
                                        [--variants pub_v1_bursty ...]

Read-only (logs:FilterLogEvents). Reads results/<variant>/outputs.json
(key usage_log_group_name, written by scripts/deploy.sh). Outputs (results/usage/):
  <variant>_usage.csv  one row per log record, every field kept (nested JSON flattened with dots)
  sessions.csv         one row per session: variant, session id, first/last timestamp,
                       total GB-hours and vCPU-hours, peak and final memory (GB), record count,
                       plus invocation counts when session ids match invocations.jsonl

Records are one per session per second. Delivery lags up to ~20 min (reference post),
resource-usage data up to 60 min (docs), so do not collect right after a run ends.
Sessions that ended before delivery was enabled are not in the logs.
--start/--end are UTC dates or datetimes; end is exclusive. Default: config window.
"""
import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent

# Field-name candidates. The docs list dotted names (session.id,
# agent.runtime.vcpu.hours.used); the reference post shows them nested under
# "attributes" / "metrics". After flattening, match on the dotted suffix.
# TODO(verify): confirm exact flattened names against a real record.
SESSION_KEYS = ("session.id", "session_id")
VCPU_KEYS = ("agent.runtime.vcpu.hours.used",)
MEM_KEYS = ("agent.runtime.memory.gb_hours.used",)
ELAPSED_KEYS = ("elapsed_time_seconds", "time_elapsed_seconds")
TS_KEYS = ("event_timestamp",)


def parse_dt(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def flatten(obj, prefix="", out=None):
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                flatten(v, f"{prefix}{k}.", out)
            else:
                out[f"{prefix}{k}"] = v
    return out


def pick(rec: dict, names):
    for n in names:
        for k, v in rec.items():
            if k == n or k.endswith("." + n):
                return v
    return None


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_iso(v, fallback_ms: int) -> str:
    """event_timestamp may be epoch ms/s or ISO text; fall back to the CloudWatch event timestamp."""
    try:
        x = float(v)
        return datetime.fromtimestamp(x / 1000 if x > 1e11 else x, timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    if isinstance(v, str):
        try:
            return parse_dt(v.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    return datetime.fromtimestamp(fallback_ms / 1000, timezone.utc).isoformat()


def fetch_events(logs, group, start_ms, end_ms):
    token = None
    while True:
        kw = dict(logGroupName=group, startTime=start_ms, endTime=end_ms, limit=10000)
        if token:
            kw["nextToken"] = token
        resp = logs.filter_log_events(**kw)
        yield from resp["events"]
        token = resp.get("nextToken")
        if not token:
            return


def load_invocations(rd: Path, variant: str):
    p = rd / variant / "invocations.jsonl"
    counts = defaultdict(lambda: {"invocations": 0, "first_invoke_ts": None})
    if not p.exists():
        return counts
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue  # tolerate a partially written last line from a running load
        c = counts[r.get("session_id")]
        c["invocations"] += 1
        ts = r.get("ts")
        if ts and (c["first_invoke_ts"] is None or ts < c["first_invoke_ts"]):
            c["first_invoke_ts"] = ts
    return counts


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=os.environ.get("CONFIG", str(ROOT / "config.yaml")))
    p.add_argument("--start")
    p.add_argument("--end", help="exclusive")
    p.add_argument("--variants", nargs="*")
    args = p.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    t0 = parse_dt(args.start or cfg["window"]["start"])
    t1 = parse_dt(args.end or cfg["window"]["end"])
    start_ms, end_ms = int(t0.timestamp() * 1000), int(t1.timestamp() * 1000)
    rd = ROOT / cfg.get("results_dir", "results")
    out = rd / "usage"
    out.mkdir(parents=True, exist_ok=True)
    session = boto3.Session(profile_name=cfg.get("aws_profile") or None, region_name=cfg["region"])
    logs = session.client("logs")

    sessions = []
    for variant in args.variants or cfg["variants"]:
        op = rd / variant / "outputs.json"
        if not op.exists():
            print(f"{variant}: no outputs.json, skipping")
            continue
        group = json.loads(op.read_text()).get("usage_log_group_name")
        if not group:
            print(f"{variant}: no usage_log_group_name in outputs.json (not applied yet?), skipping")
            continue
        try:
            events = list(fetch_events(logs, group, start_ms, end_ms))
        except ClientError as e:
            print(f"{variant}: {e.response['Error']['Code']} on {group}: {e.response['Error']['Message']}")
            continue

        rows, bysess = [], defaultdict(list)
        for ev in events:
            try:
                rec = json.loads(ev["message"])
            except json.JSONDecodeError:
                print(f"{variant}: non-JSON message skipped: {ev['message'][:80]!r}")
                continue
            flat = flatten(rec)
            flat["cw_ingestion_time"] = datetime.fromtimestamp(ev["ingestionTime"] / 1000, timezone.utc).isoformat()
            ts = to_iso(pick(flat, TS_KEYS), ev["timestamp"])
            flat["_ts"] = ts
            rows.append(flat)
            sid = pick(flat, SESSION_KEYS)
            bysess[sid].append((ts, to_float(pick(flat, MEM_KEYS)), to_float(pick(flat, VCPU_KEYS))))

        rows.sort(key=lambda r: r["_ts"])
        fields = ["variant"] + sorted({k for r in rows for k in r} - {"variant"})
        with (out / f"{variant}_usage.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, restval="")
            w.writeheader()
            for r in rows:
                w.writerow({"variant": variant, **r})
        print(f"{variant}: {len(rows)} records, {len(bysess)} sessions -> {out / (variant + '_usage.csv')}")

        inv = load_invocations(rd, variant)
        matched = sum(1 for s in bysess if s in inv)
        if bysess and not matched:
            # Load driver ids look like <variant>-<uuid>; if the service logs a different id
            # format nothing joins.
            # TODO(verify): confirm session.id in usage logs equals runtimeSessionId.
            print(f"{variant}: WARNING no usage session id matches invocations.jsonl; join skipped")
        for sid, recs in bysess.items():
            recs.sort(key=lambda x: x[0])
            mem = [m for _, m, _ in recs if m is not None]
            cpu = [c for _, _, c in recs if c is not None]
            # Each record covers one second, so GB held = gb_hours_in_that_second * 3600.
            # TODO(verify): per-record value is that second's usage, not a running total.
            gb = [m * 3600 for m in mem]
            row = {
                "variant": variant, "session_id": sid,
                "first_ts": recs[0][0], "last_ts": recs[-1][0], "records": len(recs),
                "gb_hours": sum(mem) if mem else "", "vcpu_hours": sum(cpu) if cpu else "",
                "peak_memory_gb": max(gb) if gb else "", "final_memory_gb": gb[-1] if gb else "",
            }
            if matched:
                c = inv.get(sid)
                row["invocations"] = c["invocations"] if c else ""
                row["first_invoke_ts"] = c["first_invoke_ts"] if c else ""
            sessions.append(row)

    cols = ["variant", "session_id", "first_ts", "last_ts", "records", "gb_hours", "vcpu_hours",
            "peak_memory_gb", "final_memory_gb", "invocations", "first_invoke_ts"]
    with (out / "sessions.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, restval="")
        w.writeheader()
        w.writerows(sorted(sessions, key=lambda r: (r["variant"], r["first_ts"])))
    print(f"wrote {out / 'sessions.csv'} ({len(sessions)} sessions)")


if __name__ == "__main__":
    sys.exit(main())
