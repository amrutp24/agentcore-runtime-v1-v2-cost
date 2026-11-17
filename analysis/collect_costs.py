#!/usr/bin/env python3
"""Pull Cost Explorer and CloudWatch data for the experiment window into CSV.

  python analysis/collect_costs.py [--config config.yaml] [--start 2026-11-15] [--end 2026-11-21]

Read-only. Use a read-only profile. Outputs (results/costs/):
  ce_service_usage.csv  daily cost by SERVICE x USAGE_TYPE (whole account)
  ce_tag_service.csv    daily cost by variant tag x SERVICE
  ce_tag_usage.csv      daily cost by variant tag x USAGE_TYPE
  cw_metrics.csv        hourly CloudWatch metrics per variant (AgentCore, NAT, endpoints)

Cost Explorer: data lags about 24 h; the "variant" tag only appears after it is
activated as a cost allocation tag, and only for usage after activation. Each
CE API request costs $0.01 (a few requests per run). CE is served from us-east-1.
"""
import argparse
import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
AGENTCORE_NS = "AWS/Bedrock-AgentCore"
PERCENTILE_METRICS = {"Latency", "OverheadLatency"}


def ce_query(ce, start, end, group_by):
    rows, token = [], None
    while True:
        kw = dict(
            TimePeriod={"Start": start, "End": end},
            Granularity="DAILY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=group_by,
        )
        if token:
            kw["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kw)
        for day in resp["ResultsByTime"]:
            for g in day["Groups"]:
                rows.append({
                    "date": day["TimePeriod"]["Start"],
                    "group1": g["Keys"][0],
                    "group2": g["Keys"][1] if len(g["Keys"]) > 1 else "",
                    "unblended_usd": g["Metrics"]["UnblendedCost"]["Amount"],
                    "usage_quantity": g["Metrics"]["UsageQuantity"]["Amount"],
                    "usage_unit": g["Metrics"]["UsageQuantity"]["Unit"],
                    "estimated": day.get("Estimated", ""),
                })
        token = resp.get("NextPageToken")
        if not token:
            return rows


def write(path: Path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path} ({len(rows)} rows)")


def list_metrics(cw, namespace, needles):
    """Metrics in a namespace with any dimension value containing one of the needles."""
    found, token = [], None
    while True:
        kw = {"Namespace": namespace}
        if token:
            kw["NextToken"] = token
        resp = cw.list_metrics(**kw)
        for m in resp["Metrics"]:
            if any(n and n in d["Value"] for d in m["Dimensions"] for n in needles):
                found.append(m)
        token = resp.get("NextToken")
        if not token:
            return found


def fetch(cw, metrics, start, end, variant, period=3600):
    """metrics: list of CloudWatch Metric dicts. Returns CSV rows."""
    queries, meta = [], {}
    for m in metrics:
        stats = ["Sum", "Average", "Maximum"]
        if m["MetricName"] in PERCENTILE_METRICS:
            stats += ["p50", "p95"]
        for st in stats:
            qid = f"q{len(queries)}"
            queries.append({
                "Id": qid,
                "MetricStat": {"Metric": {"Namespace": m["Namespace"], "MetricName": m["MetricName"],
                                          "Dimensions": m["Dimensions"]},
                               "Period": period, "Stat": st},
                "ReturnData": True,
            })
            meta[qid] = (m, st)
    rows = []
    for i in range(0, len(queries), 400):
        batch, token = queries[i:i + 400], None
        while True:
            kw = dict(MetricDataQueries=batch, StartTime=start, EndTime=end)
            if token:
                kw["NextToken"] = token
            resp = cw.get_metric_data(**kw)
            for r in resp["MetricDataResults"]:
                m, st = meta[r["Id"]]
                dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
                for t, v in zip(r["Timestamps"], r["Values"]):
                    rows.append({"variant": variant, "namespace": m["Namespace"], "metric": m["MetricName"],
                                 "dimensions": json.dumps(dims, sort_keys=True), "stat": st,
                                 "timestamp": t.isoformat(), "value": v})
            token = resp.get("NextToken")
            if not token:
                break
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=os.environ.get("CONFIG", str(ROOT / "config.yaml")))
    p.add_argument("--start")
    p.add_argument("--end", help="exclusive, CE convention")
    args = p.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    start = args.start or cfg["window"]["start"]
    end = args.end or cfg["window"]["end"]
    rd = ROOT / cfg.get("results_dir", "results")
    out = rd / "costs"
    session = boto3.Session(profile_name=cfg.get("aws_profile") or None, region_name=cfg["region"])

    # --- Cost Explorer
    ce = session.client("ce", region_name="us-east-1")
    cols = ["date", "group1", "group2", "unblended_usd", "usage_quantity", "usage_unit", "estimated"]
    dim = lambda k: {"Type": "DIMENSION", "Key": k}
    tag = {"Type": "TAG", "Key": "variant"}
    for name, gb in [("ce_service_usage", [dim("SERVICE"), dim("USAGE_TYPE")]),
                     ("ce_tag_service", [tag, dim("SERVICE")]),
                     ("ce_tag_usage", [tag, dim("USAGE_TYPE")])]:
        try:
            write(out / f"{name}.csv", ce_query(ce, start, end, gb), cols)
        except ClientError as e:
            print(f"WARNING: {name} failed: {e.response['Error']['Message']}")

    # --- CloudWatch per variant
    cw = session.client("cloudwatch")
    t0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    t1 = min(datetime.fromisoformat(end).replace(tzinfo=timezone.utc), datetime.now(timezone.utc) + timedelta(hours=1))
    all_rows = []
    for variant in cfg["variants"]:
        op = rd / variant / "outputs.json"
        if not op.exists():
            continue
        o = json.loads(op.read_text())
        found = []
        # TODO(verify): dimension sets of Invocations/Latency/OverheadLatency are not spelled out in
        # the docs I read; discovering via list_metrics on the runtime id avoids guessing.
        found += list_metrics(cw, AGENTCORE_NS, [o.get("agent_runtime_id")])
        if o.get("nat_gateway_id"):
            found += list_metrics(cw, "AWS/NATGateway", [o["nat_gateway_id"]])
        if o.get("vpc_endpoint_ids"):
            # TODO(verify): namespace AWS/PrivateLinkEndpoints and dimension names.
            found += list_metrics(cw, "AWS/PrivateLinkEndpoints", list(o["vpc_endpoint_ids"]) + [o.get("vpc_id")])
        print(f"{variant}: {len(found)} metric series")
        all_rows += fetch(cw, found, t0, t1, variant)
    write(out / "cw_metrics.csv", all_rows,
          ["variant", "namespace", "metric", "dimensions", "stat", "timestamp", "value"])
    print("\nNext: print the distinct USAGE_TYPE values and update usage_type_buckets in analysis/rates.yaml:")
    print("  python -c \"import csv;print(*sorted({(r['group1'],r['group2']) for r in csv.DictReader(open('results/costs/ce_service_usage.csv'))}),sep='\\n')\"")


if __name__ == "__main__":
    main()
