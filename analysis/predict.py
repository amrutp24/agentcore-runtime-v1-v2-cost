#!/usr/bin/env python3
"""Rate-card model: predicted cost per variant from the load log and rates.yaml.

  python analysis/predict.py [--config config.yaml] [--rates analysis/rates.yaml]
                             [--metrics results/costs/cw_metrics.csv]

Writes results/predicted.csv (one row per variant x bucket). Without --metrics
the AgentCore and network quantities come from the assumptions in rates.yaml;
with it, vCPU-hours, GB-hours and NAT bytes come from CloudWatch metrics
("metered" prediction: what the rate card says given what the meters saw).

Billing model encoded here (from the pricing page and V2 docs):
  V1: memory billed for the whole session, from first request until the idle
      timeout expires after the last one (capped at max lifetime).
  V2: memory billed while active plus the reclaim window (120 s) after each
      call; gaps longer than that are not billed.
  Both: CPU only while the agent is actually running (not during I/O wait).
"""
import argparse
import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- core model
def rate(rates: dict, key: str) -> float:
    return float(rates["rates"][key]["value"])


def merge(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def price_session(intervals, version, rates, idle_timeout, max_lifetime):
    """intervals: [(start_s, end_s)] for one session. Returns vcpu_hours, gb_hours, cost."""
    a = rates["assumptions"]
    first = min(s for s, _ in intervals)
    life_end = first + max_lifetime
    if version == "V1":
        end = min(max(e for _, e in intervals) + idle_timeout, life_end)
        mem_seconds = end - first
        pre = "agentcore_runtime_v1"
    else:
        reclaim = float(rates["behaviour"]["v2_idle_memory_reclaim_seconds"]["value"])
        mem_seconds = sum(max(0.0, min(e, life_end) - s) for s, e in merge((s, e + reclaim) for s, e in intervals))
        pre = "agentcore_runtime_v2"
    vcpu_seconds = len(intervals) * a["cpu_vcpu_seconds_per_warm_invocation"] + a["cpu_vcpu_seconds_extra_cold_start"]
    vcpu_h = vcpu_seconds / 3600
    gb_h = a["agent_memory_gb"] * mem_seconds / 3600
    return {
        "vcpu_hours": vcpu_h,
        "gb_hours": gb_h,
        "cost": vcpu_h * rate(rates, f"{pre}_vcpu_hour") + gb_h * rate(rates, f"{pre}_gb_hour"),
    }


def curve_per_1000(gaps, version, rates, idle_timeout=900, max_lifetime=28800, n=200):
    """Cost per 1000 invocations for one session with fixed gap seconds between calls.
    Gaps above idle_timeout mean every call lands in a new session. Used by report.py."""
    out = []
    busy = 3.0  # seconds a call is in flight; only shapes the curve, not the rates
    for g in gaps:
        if g >= idle_timeout:
            per = price_session([(0, busy)], version, rates, idle_timeout, max_lifetime)["cost"]
            out.append(per * 1000)
        else:
            k = max(1, min(n, int((max_lifetime - idle_timeout) / g)))  # keep the session inside max lifetime
            iv = [(i * g, i * g + busy) for i in range(k)]
            out.append(price_session(iv, version, rates, idle_timeout, max_lifetime)["cost"] / k * 1000)
    return out


# ------------------------------------------------------------------ inputs
def read_rows(path: Path):
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def parse_ts(s):
    return datetime.fromisoformat(s).timestamp()


def hours_deployed(life: dict) -> float:
    if "deployed_at" not in life:
        return 0.0
    end = parse_ts(life["torn_down_at"]) if "torn_down_at" in life else datetime.now(timezone.utc).timestamp()
    return max(0.0, (end - parse_ts(life["deployed_at"])) / 3600)


def read_metrics(path):
    """-> {variant: {metric_key: sum}} for the quantities we can substitute."""
    sums = defaultdict(lambda: defaultdict(float))
    if not path or not Path(path).exists():
        return sums
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r["stat"] != "Sum":
                continue
            dims = json.loads(r["dimensions"])
            name = r["metric"]
            if name in ("CPUUsed-vCPUHours", "MemoryUsed-GBHours") and set(dims) == {"Service", "Resource"}:
                sums[r["variant"]][name] += float(r["value"])
            elif r["namespace"] == "AWS/NATGateway" and name in ("BytesOutToDestination", "BytesInFromSource"):
                sums[r["variant"]]["nat_bytes"] += float(r["value"])
            elif r["namespace"] == "AWS/PrivateLinkEndpoints" and name == "BytesProcessed":
                sums[r["variant"]]["endpoint_bytes"] += float(r["value"])
    return sums


# ------------------------------------------------------------- prediction
def predict_variant(variant, vcfg, outputs, rows, life, rates, metered):
    a = rates["assumptions"]
    version = vcfg["platform_version"]
    idle_t = vcfg.get("idle_timeout_seconds", 900)
    max_l = vcfg.get("max_lifetime_seconds", 28800)
    hours = hours_deployed(life)
    out = []  # (bucket, component, quantity, unit, rate, cost, basis)

    # AgentCore
    sessions = defaultdict(list)
    for r in rows:
        s = parse_ts(r["ts"])
        sessions[r["session_id"]].append((s, s + r.get("latency_ms", 0) / 1000))
    pre = "agentcore_runtime_v1" if version == "V1" else "agentcore_runtime_v2"
    # TODO(verify): that Sum over time of CPUUsed-vCPUHours / MemoryUsed-GBHours equals total usage
    # (docs call them "total ... consumed" per period; confirm against a known run).
    if metered and "CPUUsed-vCPUHours" in metered:
        vcpu_h, gb_h, basis = metered["CPUUsed-vCPUHours"], metered.get("MemoryUsed-GBHours", 0.0), "metric"
    else:
        tot = [price_session(iv, version, rates, idle_t, max_l) for iv in sessions.values()]
        vcpu_h, gb_h, basis = sum(t["vcpu_hours"] for t in tot), sum(t["gb_hours"] for t in tot), "assumption"
    out.append(("agentcore", "vcpu_hours", vcpu_h, "vCPU-h", rate(rates, f"{pre}_vcpu_hour"), vcpu_h * rate(rates, f"{pre}_vcpu_hour"), basis))
    out.append(("agentcore", "gb_hours", gb_h, "GB-h", rate(rates, f"{pre}_gb_hour"), gb_h * rate(rates, f"{pre}_gb_hour"), basis))

    # Network
    net = vcfg["network"]
    n_inv = len(rows)
    if net == "vpc_nat":
        gb = (metered.get("nat_bytes") / 1e9) if metered and "nat_bytes" in metered else \
            n_inv * a["network_kb_per_invocation"] / 1e6 + hours * a["network_gb_background_per_hour"]
        b = "metric" if metered and "nat_bytes" in metered else "assumption"
        out.append(("nat", "nat_hours", hours, "h", rate(rates, "nat_gateway_hour"), hours * rate(rates, "nat_gateway_hour"), "lifecycle"))
        out.append(("nat", "nat_gb", gb, "GB", rate(rates, "nat_gateway_gb"), gb * rate(rates, "nat_gateway_gb"), b))
        out.append(("public_ipv4", "eip_hours", hours, "h", rate(rates, "public_ipv4_hour"), hours * rate(rates, "public_ipv4_hour"), "lifecycle"))
    elif net == "vpc_endpoints":
        n_eni = (outputs.get("interface_endpoint_count") or 0) * (outputs.get("az_count") or 0)
        gb = (metered.get("endpoint_bytes") / 1e9) if metered and "endpoint_bytes" in metered else \
            n_inv * a["network_kb_per_invocation"] / 1e6 + hours * a["network_gb_background_per_hour"]
        b = "metric" if metered and "endpoint_bytes" in metered else "assumption"
        out.append(("vpc_endpoints", "endpoint_eni_hours", n_eni * hours, "ENI-h", rate(rates, "interface_endpoint_hour_per_az"), n_eni * hours * rate(rates, "interface_endpoint_hour_per_az"), "lifecycle"))
        out.append(("vpc_endpoints", "endpoint_gb", gb, "GB", rate(rates, "interface_endpoint_gb"), gb * rate(rates, "interface_endpoint_gb"), b))

    # Model tokens
    tin = sum(r.get("input_tokens") or 0 for r in rows)
    tout = sum(r.get("output_tokens") or 0 for r in rows)
    ri, ro = rate(rates, "model_input_per_1k_tokens"), rate(rates, "model_output_per_1k_tokens")
    out.append(("bedrock_model", "input_tokens", tin, "tokens", ri, tin / 1000 * ri, "log"))
    out.append(("bedrock_model", "output_tokens", tout, "tokens", ro, tout / 1000 * ro, "log"))
    return out


def build_predictions(cfg, rates, results_dir: Path, metrics_path=None):
    metrics = read_metrics(metrics_path)
    result = {}
    for variant, vcfg in cfg["variants"].items():
        vdir = results_dir / variant
        rows = read_rows(vdir / "invocations.jsonl")
        if not rows:
            continue
        life = json.loads((vdir / "lifecycle.json").read_text()) if (vdir / "lifecycle.json").exists() else {}
        outputs = json.loads((vdir / "outputs.json").read_text()) if (vdir / "outputs.json").exists() else {}
        result[variant] = predict_variant(variant, vcfg, outputs, rows, life, rates, metrics.get(variant))
    return result


def write_csv(preds, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["variant", "bucket", "component", "quantity", "unit", "rate", "predicted_usd", "basis"])
        for v, comps in preds.items():
            for c in comps:
                w.writerow([v, c[0], c[1], f"{c[2]:.6f}", c[3], c[4], f"{c[5]:.6f}", c[6]])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=os.environ.get("CONFIG", str(ROOT / "config.yaml")))
    p.add_argument("--rates", default=str(ROOT / "analysis" / "rates.yaml"))
    p.add_argument("--metrics", default=None, help="cw_metrics.csv from collect_costs.py (metered mode)")
    args = p.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())
    rates = yaml.safe_load(Path(args.rates).read_text())
    rd = ROOT / cfg.get("results_dir", "results")
    preds = build_predictions(cfg, rates, rd, args.metrics)
    write_csv(preds, rd / "predicted.csv")
    for v, comps in preds.items():
        print(f"{v:18s} ${sum(c[5] for c in comps):8.4f}")
    unverified = [k for k, r in rates["rates"].items() if not r.get("verified")]
    if unverified:
        print("unverified rates:", ", ".join(unverified))


if __name__ == "__main__":
    main()
