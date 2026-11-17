#!/usr/bin/env python3
"""Join predicted vs actual cost and latency into one table, plus charts.

  python analysis/report.py [--config config.yaml] [--rates analysis/rates.yaml]

Needs results/<variant>/invocations.jsonl (load driver) and results/costs/*.csv
(collect_costs.py). Writes results/report.md, results/report.csv,
results/predicted_vs_actual.png, results/breakeven.png.

Actual cost attribution, per variant and bucket:
  tag      Cost Explorer grouped by the "variant" cost allocation tag (preferred)
  share    tag data missing: the account-level bucket total is split across
           variants by their share of the metered (else predicted) usage.
           Treat these as allocations, not measurements.
"""
import argparse
import csv
import json
import os
import re
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import yaml

import predict

ROOT = Path(__file__).resolve().parent.parent


def percentile(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def read_csv(path: Path):
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def make_classifier(rules):
    compiled = [(r["bucket"], re.compile(r["service_regex"]), re.compile(r["usage_type_regex"]),
                 r["usage_type_regex"] == ".*") for r in rules]

    def classify(service, usage_type, service_level):
        """service_level=True: only service-only buckets (agentcore, bedrock_model) match;
        False: only usage-type buckets. Avoids double counting across the two tag queries."""
        for bucket, sre, ure, service_only in compiled:
            if service_only != service_level:
                continue
            if service_level and sre.search(service or ""):
                return bucket
            if not service_level and ure.search(usage_type or ""):
                return bucket
        return None
    return classify


def tag_value(key: str) -> str:
    return key.split("$", 1)[1] if "$" in key else key


# Model-token spend is not taggable per runtime (calls come from the shared exec-role
# path), so it is always allocated by token share.
ALWAYS_SHARE = {"bedrock_model"}


def actual_costs(cfg, rates, cdir, share_preds):
    classify = make_classifier(rates["usage_type_buckets"])
    variants = list(cfg["variants"])
    act = {v: defaultdict(float) for v in variants}
    how = {v: "tag" for v in variants}

    tagged = defaultdict(lambda: defaultdict(float))
    for r in read_csv(cdir / "ce_tag_service.csv"):
        b = classify(r["group2"], "", True)
        if b and b not in ALWAYS_SHARE:
            tagged[tag_value(r["group1"])][b] += float(r["unblended_usd"])
    for r in read_csv(cdir / "ce_tag_usage.csv"):
        b = classify("", r["group2"], False)
        if b:
            tagged[tag_value(r["group1"])][b] += float(r["unblended_usd"])

    account = defaultdict(float)
    for r in read_csv(cdir / "ce_service_usage.csv"):
        b = classify(r["group1"], "", True) or classify("", r["group2"], False)
        if b:
            account[b] += float(r["unblended_usd"])

    for v in variants:
        if sum(tagged.get(v, {}).values()) > 0:
            act[v].update(tagged[v])
        else:
            how[v] = "share"
    # Fallback: split account totals by predicted/metered share, only for untagged variants.
    for bucket, total in account.items():
        members = {v: sum(c[5] for c in share_preds.get(v, []) if c[0] == bucket)
                   for v in variants if how[v] == "share" or bucket in ALWAYS_SHARE}
        denom = sum(members.values())
        if denom > 0:
            for v, w in members.items():
                act[v][bucket] += total * w / denom
    return act, how


def latency_stats(rows):
    ok = [r for r in rows if r["status"] == "ok"]
    cold = [r["latency_ms"] for r in ok if r["first_call"]]
    warm = [r["latency_ms"] for r in ok if not r["first_call"]]
    return {
        "invocations": len(rows), "errors": len(rows) - len(ok),
        "cold_n": len(cold), "cold_p50": percentile(cold, 0.5), "cold_p95": percentile(cold, 0.95),
        "warm_n": len(warm), "warm_p50": percentile(warm, 0.5), "warm_p95": percentile(warm, 0.95),
    }


def fmt(x, nd=1):
    return "" if x is None else f"{x:.{nd}f}"


def chart_pred_actual(table, path):
    names = [r["variant"] for r in table]
    xs = range(len(names))
    fig, ax = plt.subplots(figsize=(max(7, len(names) * 1.2), 4.5))
    ax.bar([x - 0.2 for x in xs], [r["predicted_usd"] for r in table], 0.4, label="predicted (rate card + assumptions)")
    ax.bar([x + 0.2 for x in xs], [r["actual_usd"] or 0 for r in table], 0.4, label="actual (Cost Explorer)")
    ax.set_xticks(list(xs))
    ax.set_xticklabels(names, rotation=35, ha="right")
    ax.set_ylabel("USD over the window")
    ax.set_title("Predicted vs actual cost per variant")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def chart_breakeven(rates, path):
    gaps = [5, 10, 20, 30, 60, 90, 120, 180, 240, 300, 450, 600, 900, 1200, 1800, 3600]
    v1 = predict.curve_per_1000(gaps, "V1", rates)
    v2 = predict.curve_per_1000(gaps, "V2", rates)
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.plot(gaps, v1, marker="o", label="V1 (memory billed whole session)")
    ax.plot(gaps, v2, marker="o", label="V2 (idle memory reclaimed after 120 s, higher rates)")
    cross = next((g for g, a, b in zip(gaps, v1, v2) if b < a), None)
    if cross:
        ax.axvline(cross, color="grey", linestyle=":")
        ax.annotate(f"V2 cheaper from ~{cross}s gap", (cross, max(v1) * 0.5))
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("seconds between invocations in a session (idle timeout 900 s)")
    ax.set_ylabel("USD per 1000 invocations (runtime compute only)")
    ax.set_title("Cost vs session shape (rate card + rates.yaml assumptions)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=os.environ.get("CONFIG", str(ROOT / "config.yaml")))
    p.add_argument("--rates", default=str(ROOT / "analysis" / "rates.yaml"))
    args = p.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())
    rates = yaml.safe_load(Path(args.rates).read_text())
    rd = ROOT / cfg.get("results_dir", "results")
    cdir = rd / "costs"
    metrics_path = cdir / "cw_metrics.csv"

    naive = predict.build_predictions(cfg, rates, rd, None)
    metered = predict.build_predictions(cfg, rates, rd, metrics_path if metrics_path.exists() else None)
    act, how = actual_costs(cfg, rates, cdir, metered)

    buckets = ["agentcore", "nat", "public_ipv4", "vpc_endpoints", "bedrock_model"]
    table = []
    for v, vcfg in cfg["variants"].items():
        if v not in naive:
            continue
        rows = predict.read_rows(rd / v / "invocations.jsonl")
        pred_b = defaultdict(float)
        for c in naive[v]:
            pred_b[c[0]] += c[5]
        row = {
            "variant": v, "network": vcfg["network"], "version": vcfg["platform_version"], "shape": vcfg["shape"],
            **latency_stats(rows),
            "predicted_usd": sum(pred_b.values()),
            "predicted_metered_usd": sum(c[5] for c in metered[v]),
            "actual_usd": sum(act[v].values()),
            "attribution": how[v],
        }
        has_actual = bool(act[v]) and sum(act[v].values()) > 0   # no Cost Explorer data -> n/a
        row["actual_usd"] = row["actual_usd"] if has_actual else None
        row["delta_usd"] = row["actual_usd"] - row["predicted_usd"] if has_actual else None
        row["delta_pct"] = (row["delta_usd"] / row["predicted_usd"] * 100) if has_actual and row["predicted_usd"] else None
        for b in buckets:
            row[f"pred_{b}"] = pred_b.get(b, 0.0)
            row[f"act_{b}"] = act[v].get(b, 0.0)
        table.append(row)

    if not table:
        raise SystemExit("no invocation logs found; run load/run_load.py first")

    cols = list(table[0].keys())
    with (rd / "report.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(table)

    md = ["# Predicted vs actual", "",
          "| variant | net | ver | shape | calls | err | cold p50/p95 ms | warm p50/p95 ms | predicted $ | metered-pred $ | actual $ | delta % | attribution |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in table:
        md.append(f"| {r['variant']} | {r['network']} | {r['version']} | {r['shape']} | {r['invocations']} | {r['errors']} | "
                  f"{fmt(r['cold_p50'], 0)}/{fmt(r['cold_p95'], 0)} | {fmt(r['warm_p50'], 0)}/{fmt(r['warm_p95'], 0)} | "
                  f"{r['predicted_usd']:.3f} | {r['predicted_metered_usd']:.3f} | {fmt(r['actual_usd'], 3) or 'n/a'} | "
                  f"{fmt(r['delta_pct'], 0) or 'n/a'} | {r['attribution']} |")
    md += ["", "Bucket detail is in report.csv (pred_* and act_* columns).",
           "`share` attribution means Cost Explorer had no usable variant tag data and the account total was split by usage share.",
           "Cold = first call of a session id (client-side); warm = the rest. Errors excluded from latency.",
           "Unverified rates in use: " + (", ".join(k for k, x in rates["rates"].items() if not x.get("verified")) or "none")]
    (rd / "report.md").write_text("\n".join(md) + "\n")

    chart_pred_actual(table, rd / "predicted_vs_actual.png")
    chart_breakeven(rates, rd / "breakeven.png")
    print("\n".join(md))


if __name__ == "__main__":
    main()
