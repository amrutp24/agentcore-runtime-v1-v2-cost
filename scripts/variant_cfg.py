#!/usr/bin/env python3
"""Small config helper for the shell scripts.

  variant_cfg.py get <key>                  print a top-level config value
  variant_cfg.py variants                   list variant ids
  variant_cfg.py tfvars <variant> <outfile> write Terraform vars JSON
  variant_cfg.py agent <variant>            print the variant's agent (agent | echo | alloc)
  variant_cfg.py mark <variant> <field>     stamp results/<variant>/lifecycle.json
  variant_cfg.py save-outputs <variant>     read `terraform output -json` on stdin
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    path = Path(os.environ.get("CONFIG", ROOT / "config.yaml"))
    if not path.exists():
        sys.exit(f"missing {path}; copy config.example.yaml to config.yaml first")
    return yaml.safe_load(path.read_text())


def results_dir(cfg: dict, variant: str) -> Path:
    d = ROOT / cfg.get("results_dir", "results") / variant
    d.mkdir(parents=True, exist_ok=True)
    return d


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    cfg = load_config()

    if cmd == "get":
        print(cfg[args[0]])
    elif cmd == "variants":
        print("\n".join(cfg["variants"]))
    elif cmd == "agent":
        print((cfg["variants"].get(args[0]) or sys.exit(f"unknown variant {args[0]}")).get("agent", "agent"))
    elif cmd == "tfvars":
        variant, outfile = args
        v = cfg["variants"].get(variant) or sys.exit(f"unknown variant {variant}")
        tfvars = {
            "region": cfg["region"],
            "variant_id": variant,
            "project_tag": cfg["project_tag"],
            "network_mode": v["network"],
            "platform_version": v["platform_version"],
            "idle_timeout_seconds": v.get("idle_timeout_seconds", 900),
            "max_lifetime_seconds": v.get("max_lifetime_seconds", 28800),
            "model_id": cfg["model_id"],
            "az_count": cfg.get("az_count", 2),
            "supported_az_ids": cfg["supported_az_ids"],
            "interface_endpoint_services": cfg["interface_endpoint_services"],
            "nat_s3_gateway_endpoint": cfg.get("nat_s3_gateway_endpoint", False),
            "enable_usage_logs": v.get("enable_usage_logs", cfg.get("enable_usage_logs", True)),
        }
        if v.get("agent", "agent") != "agent":  # default agent keeps the Terraform default zip path
            tfvars["code_zip_path"] = f"../build/agent_{v['agent']}.zip"
        Path(outfile).parent.mkdir(parents=True, exist_ok=True)
        Path(outfile).write_text(json.dumps(tfvars, indent=2))
    elif cmd == "mark":
        variant, field = args
        path = results_dir(cfg, variant) / "lifecycle.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        # deployed_at is the first deploy; re-applying must not move it (it sets
        # the billed-hours window in predict.py).
        if field == "deployed_at" and field in data:
            return
        data[field] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path.write_text(json.dumps(data, indent=2))
    elif cmd == "save-outputs":
        variant = args[0]
        raw = json.load(sys.stdin)
        flat = {k: v["value"] for k, v in raw.items()}
        (results_dir(cfg, variant) / "outputs.json").write_text(json.dumps(flat, indent=2))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
