#!/usr/bin/env python3
"""Load driver: invoke one variant's runtime with a controlled load shape.

Shapes
  bursty  many short sessions: a new session id each, --session-length calls, then stop.
          --rate = new sessions per minute.
  steady  one long session invoked every 60/--rate seconds (rotates after
          --session-length calls if > 0, or before max lifetime).
  idle    one invocation, then nothing (the runtime idles until its timeout).

Output: <results_dir>/<variant>/invocations.jsonl, one row per invocation.
Safe to Ctrl-C and re-run: rows are flushed one by one, the hard cap counts
rows already in the log, and a steady session is reused if still alive.
There are no hidden SDK retries, so every attempt is one logged row.
"""
import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
import yaml
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

ROOT = Path(__file__).resolve().parent.parent
PROMPT = "What is 17 + 25? Use the add tool. Answer in one short sentence."


def parse_duration(text: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", str(text).strip())
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration {text!r} (use 90s, 30m, 6h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sleep_until(t_monotonic: float) -> None:
    while True:
        left = t_monotonic - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(left, 1.0))


class Driver:
    def __init__(self, args, cfg, vcfg, outputs):
        self.args = args
        self.variant = args.variant
        self.arn = outputs["agent_runtime_arn"]
        self.dir = ROOT / cfg.get("results_dir", "results") / self.variant
        self.log_path = self.dir / "invocations.jsonl"
        self.state_path = self.dir / "load_state.json"
        self.idle_timeout = vcfg.get("idle_timeout_seconds", 900)
        self.max_lifetime = vcfg.get("max_lifetime_seconds", 28800)
        # Optional per-variant extra request fields (e.g. alloc_mb/hold_s for the alloc agent).
        self.payload = {"prompt": PROMPT, **(vcfg.get("payload_extra") or {})}
        session = boto3.Session(profile_name=cfg.get("aws_profile") or None, region_name=cfg["region"])
        # Client/operation names checked against the installed boto3 service model.
        self.client = session.client(
            "bedrock-agentcore",
            config=Config(read_timeout=180, connect_timeout=10, retries={"max_attempts": 0}),
        )
        self.seen_sessions: set[str] = set()
        self.total = 0
        if self.log_path.exists():
            for line in self.log_path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.seen_sessions.add(row["session_id"])
                    self.total += 1

    # -- one invocation ---------------------------------------------------
    def invoke(self, session_id: str) -> dict:
        row = {
            "ts": now_iso(),
            "variant": self.variant,
            "shape": self.args.shape,
            "session_id": session_id,
            "first_call": session_id not in self.seen_sessions,
            "status": "ok",
            "http_status": None,
            "input_tokens": None,
            "output_tokens": None,
            "request_id": None,
        }
        t0 = time.perf_counter()
        try:
            resp = self.client.invoke_agent_runtime(
                agentRuntimeArn=self.arn,
                runtimeSessionId=session_id,  # API: min length 33 (uuid4 is 36)
                contentType="application/json",
                accept="application/json",
                payload=json.dumps(self.payload).encode(),
            )
            body = resp["response"].read()
            meta = resp.get("ResponseMetadata", {})
            row["http_status"] = resp.get("statusCode") or meta.get("HTTPStatusCode")
            row["request_id"] = meta.get("RequestId")
            try:
                parsed = json.loads(body)
                usage = parsed.get("usage") or {}
                row["input_tokens"] = usage.get("input_tokens")
                row["output_tokens"] = usage.get("output_tokens")
                for k in ("alloc_mb", "rss_mb_before", "rss_mb_peak", "rss_mb_after"):
                    if k in parsed:  # alloc agent only; other variants log no extra keys
                        row[k] = parsed[k]
            except (ValueError, AttributeError):
                row["status"] = "bad_body"
        except ClientError as e:
            row["status"] = e.response.get("Error", {}).get("Code", "ClientError")
            row["http_status"] = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            row["request_id"] = e.response.get("ResponseMetadata", {}).get("RequestId")
        except (BotoCoreError, OSError) as e:
            row["status"] = type(e).__name__
        row["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        self.dir.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps(row) + "\n")
            f.flush()
        self.seen_sessions.add(session_id)
        self.total += 1
        print(f"{row['ts']} {row['variant']} {row['session_id'][-8:]} "
              f"{'COLD' if row['first_call'] else 'warm'} {row['status']} {row['latency_ms']} ms "
              f"[{self.total}/{self.args.max_invocations}]")
        return row

    def capped(self) -> bool:
        if self.total >= self.args.max_invocations:
            print(f"hard cap reached ({self.args.max_invocations} invocations); stopping")
            return True
        return False

    def new_session(self) -> str:
        return f"{self.variant}-{uuid.uuid4()}"

    def stop_session(self, session_id: str) -> None:
        # Optional: end the microVM early instead of waiting for the idle timeout.
        try:
            self.client.stop_runtime_session(
                agentRuntimeArn=self.arn, runtimeSessionId=session_id, qualifier="DEFAULT")
        except (ClientError, BotoCoreError) as e:
            print(f"stop_runtime_session failed: {e}", file=sys.stderr)

    # -- shapes -------------------------------------------------------------
    def run_bursty(self) -> None:
        a = self.args
        period = 60.0 / a.rate
        deadline = time.monotonic() + a.duration
        next_start = time.monotonic()
        while time.monotonic() < deadline and not self.capped():
            sleep_until(next_start)
            next_start += period
            sid = self.new_session()
            for i in range(a.session_length):
                if self.capped():
                    return
                self.invoke(sid)
                if i < a.session_length - 1:
                    time.sleep(a.intra_gap)
            if a.stop_sessions:
                self.stop_session(sid)

    def load_state(self):
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return None

    def save_state(self, st: dict) -> None:
        self.state_path.write_text(json.dumps(st))

    def run_steady(self) -> None:
        a = self.args
        interval = 60.0 / a.rate
        deadline = time.monotonic() + a.duration
        st = self.load_state()
        now = time.time()
        # Resume the old session only if it should still be alive on the service side.
        if st and now - st["last_ts"] < self.idle_timeout and now - st["started_ts"] < self.max_lifetime - 120:
            print(f"resuming session ...{st['session_id'][-8:]}")
        else:
            st = {"session_id": self.new_session(), "started_ts": now, "last_ts": now, "calls": 0}
        next_call = time.monotonic()
        while time.monotonic() < deadline and not self.capped():
            sleep_until(next_call)
            next_call += interval
            now = time.time()
            rotate = (a.session_length and st["calls"] >= a.session_length) or \
                     now - st["started_ts"] >= self.max_lifetime - 120
            if rotate:
                st = {"session_id": self.new_session(), "started_ts": now, "last_ts": now, "calls": 0}
            self.invoke(st["session_id"])
            st["calls"] += 1
            st["last_ts"] = time.time()
            self.save_state(st)

    def run_idle(self) -> None:
        if self.total > 0:
            print("idle shape: already invoked once; nothing to do")
            return
        self.invoke(self.new_session())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--variant", required=True)
    p.add_argument("--shape", choices=["bursty", "steady", "idle"])
    p.add_argument("--duration", type=parse_duration, help="e.g. 90s, 30m, 6h")
    p.add_argument("--rate", type=float, help="steady: calls/min; bursty: new sessions/min")
    p.add_argument("--session-length", type=int, help="calls per session (0 = unlimited)")
    p.add_argument("--intra-gap", type=float, default=5.0, help="bursty: seconds between calls in a session")
    p.add_argument("--max-invocations", type=int, help="hard cap across resumes (spend guard)")
    p.add_argument("--stop-sessions", action="store_true", help="bursty: StopRuntimeSession after each session")
    p.add_argument("--config", default=os.environ.get("CONFIG", str(ROOT / "config.yaml")))
    args = p.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    vcfg = cfg["variants"].get(args.variant) or sys.exit(f"unknown variant {args.variant}")
    load = vcfg.get("load", {})
    args.shape = args.shape or vcfg["shape"]
    args.duration = args.duration if args.duration is not None else parse_duration(load.get("duration", "1h"))
    args.rate = args.rate if args.rate is not None else float(load.get("rate", 1))
    args.session_length = args.session_length if args.session_length is not None else int(load.get("session_length", 3))
    args.max_invocations = args.max_invocations or int(load.get("max_invocations", 200))
    if args.shape != "idle" and args.rate <= 0:
        sys.exit("--rate must be > 0")

    out = ROOT / cfg.get("results_dir", "results") / args.variant / "outputs.json"
    if not out.exists():
        sys.exit(f"{out} not found; deploy the variant first")
    d = Driver(args, cfg, vcfg, json.loads(out.read_text()))

    print(f"variant={args.variant} shape={args.shape} duration={args.duration:.0f}s rate={args.rate} "
          f"session_length={args.session_length} cap={args.max_invocations} already_logged={d.total}")
    try:
        getattr(d, f"run_{args.shape}")()
    except KeyboardInterrupt:
        print(f"\ninterrupted; {d.total} rows logged. Re-run the same command to resume.")
        sys.exit(130)


if __name__ == "__main__":
    main()
