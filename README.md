# AgentCore Runtime V1 vs V2: a billing and latency measurement rig

This repository deploys the same small agent to Amazon Bedrock AgentCore Runtime in
twelve variants, drives each with a controlled load shape, and compares the measured
bill and latency with what the published rate card predicts. It compares:

- Runtime platform V1 against V2 (same agent, same code).
- Public networking against a VPC with a NAT gateway against a VPC with interface
  endpoints only.
- Three load shapes (bursty short sessions, one steady long session, one call then idle).
- Two control agents that isolate what the agent process itself does with memory.

Everything is Terraform plus Python and bash. One Runtime is deployed per variant so
that Cost Explorer (daily granularity) and CloudWatch (per Runtime id) can attribute
cost to a single configuration. Every resource is tagged `project=agentcore-bill-experiment`
and `variant=<id>`.

The deployed resources bill by the hour while they exist. Read "Cost and duration" and
"Safety notes" before running anything.

## The premise, corrected

The AWS documentation says V2 "reclaims memory as your agent releases it", and the
AgentCore pricing page says "Memory is reclaimed within the session when unused for 120
seconds". Both sentences are about memory the agent process has freed. They are not
about an idle session. An agent that never frees memory (a long-lived Python process
holding its heap) should not be expected to show any reclaim, and a session that
sits idle is still a session until its idle timeout.

An earlier version of this experiment assumed the opposite and looked for memory
billing to fall during idle time. The repository now has controls that test the documented behaviour
directly: a bare echo agent (no framework, no model call) and an allocate-and-free agent
that really gives memory back to the operating system before it responds.

## Variants

Defined in `config.example.yaml`. `idle timeout` is `idleRuntimeSessionTimeout`.

| variant | network | platform | shape | agent | idle timeout |
|---|---|---|---|---|---|
| pub_v1_bursty | public | V1 | bursty (0.5 sessions/min, 3 calls each, 2 h) | Strands | 15 min |
| pub_v2_bursty | public | V2 | bursty | Strands | 15 min |
| pub_v1_steady | public | V1 | steady (0.2 calls/min, one long session, 6 h) | Strands | 15 min |
| pub_v2_steady | public | V2 | steady | Strands | 15 min |
| pub_v1_idle | public | V1 | idle (one call, then nothing) | Strands | 6 h |
| pub_v2_idle | public | V2 | idle | Strands | 6 h |
| pub_v1_idle_echo | public | V1 | idle | bare echo | 1 h |
| pub_v2_idle_echo | public | V2 | idle | bare echo | 1 h |
| pub_v1_alloc | public | V1 | idle (one call that allocates 1 GB, holds 5 s, frees it) | allocate and free | 1 h |
| pub_v2_alloc | public | V2 | idle (same) | allocate and free | 1 h |
| nat_v1_bursty | VPC + NAT gateway | V1 | bursty | Strands | 15 min |
| ep_v1_bursty | VPC + interface endpoints (`logs`, `bedrock-runtime`) + S3 gateway endpoint | V1 | bursty | Strands | 15 min |

Agents:

- `agent/main.py`: Strands agent with one tool, low max tokens, Amazon Nova Micro.
- `agent_echo/main.py`: only `BedrockAgentCoreApp`. No Strands, no boto3 client, no
  model call, no background threads.
- `agent_alloc/main.py`: same bare app, but a payload `{"alloc_mb": N, "hold_s": S}` makes
  it hold N MB of touched memory for S seconds, free it (anonymous `mmap` closed, `gc.collect()`,
  glibc `malloc_trim(0)`), and report its resident set size before, at peak and after.

## Repository layout

```
config.example.yaml        region, model id, variants (copy to config.yaml)
requirements.txt           local tooling pins
agent/                     Strands agent and its requirements (packaged into the runtime zip)
agent_echo/                bare echo control agent
agent_alloc/               allocate-and-free control agent (has a standalone --selftest)
infra/*.tf                 VPC (NAT / endpoints-only), IAM, S3 code bucket, Runtime, usage-log delivery
scripts/deploy.sh          per-variant deploy (one Terraform workspace each)
scripts/teardown.sh        idempotent teardown plus a listing of anything left over
scripts/usage_log_policy.sh  account-level CloudWatch Logs resource policy for usage-log delivery
scripts/package_agent.py   builds build/agent.zip, or build/agent_<name>.zip for `echo` / `alloc` (arm64 wheels)
scripts/variant_cfg.py     config helper used by the shell scripts
load/run_load.py           bursty / steady / idle driver, writes a JSONL row per invocation
analysis/rates.yaml        rates with source and retrieval date, usage assumptions, Cost Explorer buckets
analysis/collect_costs.py  Cost Explorer and CloudWatch to CSV
analysis/collect_usage_logs.py  Runtime USAGE_LOGS to CSV (per record and per session)
analysis/predict.py        rate-card model
analysis/report.py         predicted vs actual table, latency, charts
analysis/idle_timeline.py  per-minute GB held and vCPU used for idle variants (CloudWatch)
```

Local, untracked output goes to `build/` (zips, generated tfvars) and `results/` (logs, CSVs,
reports). Terraform state is local, one workspace per variant, under `infra/terraform.tfstate.d/`.

## Prerequisites

- Python 3.11 or newer.
- Terraform 1.5 or newer (developed with 1.11.2 and AWS provider 6.73.0).
- AWS CLI v2, recent enough to include the `bedrock-agentcore` and `bedrock-agentcore-control`
  commands (needed by `teardown.sh`).
- bash (Git Bash on Windows works).
- No Docker. Agents ship as direct-deploy zips with linux/arm64 wheels fetched by `uv`.
- An AWS profile in an account you are happy to run this in, in a Region where Runtime V2 and
  Amazon Nova Micro are available (default `us-east-1`). Cost Explorer enabled, and Billing
  console access to activate cost allocation tags.

High-level IAM permissions:

- Deploy and teardown profile: create and delete VPC networking (subnets, route tables, NAT
  gateways, Elastic IPs, VPC endpoints, security groups), IAM roles and inline policies, S3
  buckets and objects, `bedrock-agentcore-control` Runtimes, CloudWatch Logs log groups, delivery
  sources/destinations/deliveries and resource policies, and tagging read (for the leftover listing).
- Load profile: `bedrock-agentcore:InvokeAgentRuntime` and `StopRuntimeSession`.
- Read-only profile for collection and reports: `ce:GetCostAndUsage`, `cloudwatch:GetMetricData`
  and `ListMetrics`, `logs:FilterLogEvents`. Each Cost Explorer API request costs $0.01.

Select profiles with `AWS_PROFILE`. Nothing is written into the repo; `config.yaml` is
gitignored in case you set `aws_profile` there.

## Cost and duration

The original run kept all variants deployed for about 96 hours (four days of load rounds) and was
budgeted at roughly $10. Estimates at us-east-1 list prices follow. Rates and their sources are in
`analysis/rates.yaml`; interface endpoint hourly and NAT per-GB rates are marked unverified there.

| variant | bills while idle | arithmetic | est. USD |
|---|---|---|---|
| nat_v1_bursty | NAT gateway, EIP | 0.045 x 96 + 0.005 x 96 + ~0.12 compute | 4.9 |
| ep_v1_bursty | 2 interface endpoints x 2 AZ | 0.01 x 2 x 2 x 96 + ~0.12 compute | 4.0 |
| pub_v1_bursty | no (V1 memory runs to idle timeout) | 60 sessions x 915 s x 0.4 GB x 0.00945 + CPU, x2 rounds | 0.12 |
| pub_v2_bursty | no | 60 sessions x 133 s x 0.4 GB x 0.0169 + CPU, x2 rounds | 0.04 |
| pub_v1_steady | session memory while the session lives | 6.25 h x 0.4 x 0.00945 = 0.024 per round, 2 rounds | 0.05 |
| pub_v2_steady | memory only 123 s per call | 72 x 123 s x 0.4 x 0.0169 = 0.017 per round, 2 rounds | 0.04 |
| pub_v1_idle | yes, until the 6 h idle timeout | 6 h x 0.4 x 0.00945 | 0.02 |
| pub_v2_idle | see "The premise, corrected" | 123 s x 0.4 x 0.0169 if reclaimed | ~0.00 |
| echo and alloc controls | session memory until the 1 h idle timeout | pennies each | < 0.10 total |
| Nova Micro tokens | | ~1,500 calls x (400 in, 60 out) at 0.035 / 0.14 USD per M (unverified) | 0.04 |
| CloudWatch logs, Cost Explorer API | | logs pennies; $0.01 per CE request, ~15 requests | 0.25 |
| **Total (the eight Strands-agent variants)** | | | **about 9.5** |

Resources that bill while idle, by the hour, whether or not you send traffic:

- NAT gateway (about $0.045 per hour plus data) and its Elastic IP (about $0.005 per hour).
- Interface VPC endpoints: about $0.01 per hour per endpoint per Availability Zone (two endpoints x
  two AZs in the default config). Add `ecr.api` and `ecr.dkr` only if you move to container images
  (another $3.84 per pair over 96 h).
- V1 session memory, until the session's idle timeout.

Free: the VPC, subnets, route tables, security groups, ENIs and the S3 gateway endpoint. Runtime
log groups have no expiry unless removed, so teardown deletes them.

The AgentCore compute amounts are small. The two variants that cost real money are the NAT and
endpoint ones, and only because they are billed for every hour they exist. If you only want the
V1/V2 memory controls, deploy just the four `*_idle_echo` / `*_alloc` variants and tear down the same
day (about two hours).

Cost Explorer lags about 24 hours, so the last day of cost data arrives after teardown (pulling it is
read-only). Cost allocation tags only appear in Cost Explorer after they are activated in Billing,
and only for usage after activation. `report.py` labels variants with no usable tag data `share`
(account total split by usage share) instead of `tag`. Model-token spend is always allocated by token share.

## Step by step

All commands from the repository root. Variant ids are the keys in `config.yaml`.

### 1. Configure

```bash
python -m venv venv && source venv/Scripts/activate   # Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
export AWS_PROFILE=<deploy-profile>
aws sts get-caller-identity          # confirm you are in the intended account
```

Edit `config.yaml`: set `window.start` / `window.end` to cover your run (end is exclusive), and
remove variants you do not want. `supported_az_ids` must be AZ ids AgentCore supports in the Region;
re-check it if you change Region. The example config contains no account ids or credentials.

### 2. Plan (creates nothing)

`deploy.sh` runs `terraform apply -auto-approve`. Always plan first:

```bash
PLAN_ONLY=1 bash scripts/deploy.sh pub_v1_bursty
```

The first plan or deploy builds `build/agent.zip` (and `build/agent_echo.zip` / `build/agent_alloc.zip`
when a selected variant needs them).

### 3. Deploy

```bash
bash scripts/deploy.sh pub_v1_bursty
# smoke test: at most 20 calls
python load/run_load.py --variant pub_v1_bursty --duration 5m --max-invocations 20
# the rest (V2 runtimes take several minutes each to reach READY)
bash scripts/deploy.sh pub_v2_bursty pub_v1_steady pub_v2_steady pub_v1_idle pub_v2_idle nat_v1_bursty ep_v1_bursty
```

Outputs per variant are saved to `results/<variant>/outputs.json`.

### 4. Run load

```bash
mkdir -p results
# idle shape = one call, then nothing
for v in pub_v1_idle pub_v2_idle; do python load/run_load.py --variant $v; done
for v in pub_v1_bursty pub_v2_bursty nat_v1_bursty ep_v1_bursty pub_v1_steady pub_v2_steady; do
  nohup python load/run_load.py --variant $v > results/$v.out 2>&1 &
done
```

Every invocation is one row in `results/<variant>/invocations.jsonl` (no hidden SDK retries). Shape,
rate, duration and the hard invocation cap come from the variant's `load` block in the config.
A run can be interrupted with Ctrl-C and resumed by re-running the same command; the cap counts rows
already logged and a steady session is reused if still alive. Add `--stop-sessions` to bursty runs to
end each session early (default off: the idle tail is part of what is measured).

Optional per-variant `payload_extra` in the config is merged into the request payload; variants
without it send exactly the original payload.

### 5. Activate cost allocation tags (changes billing configuration; run it yourself)

Once the resources exist (tags can take up to 24 hours to appear in Billing):

```bash
aws ce update-cost-allocation-tags-status --cost-allocation-tags-status \
  TagKey=project,Status=Active TagKey=variant,Status=Active
```

### 6. Collect

Read-only. Use a read-only profile if you have one.

```bash
export AWS_PROFILE=<read-only-profile>
python analysis/collect_costs.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]
python analysis/collect_usage_logs.py     # per-session usage logs -> results/usage/ (see Known issues)
```

After the first `collect_costs.py` run, print the real Cost Explorer `USAGE_TYPE` strings (the command
is printed by the script) and fix `usage_type_buckets` in `analysis/rates.yaml` if they differ.

### 7. Report

```bash
python analysis/predict.py --metrics results/costs/cw_metrics.csv
python analysis/report.py                 # results/report.md, report.csv, *.png
```

### 8. Idle timeline (controls)

```bash
python analysis/idle_timeline.py --variants pub_v2_idle_echo pub_v1_idle_echo --minutes 90
```

`results/idle_timeline.csv` has GB held and vCPU used per minute (CloudWatch `MemoryUsed-GBHours`
and `CPUUsed-vCPUHours` times 60) since the variant's first call. Timestamps are UTC. CloudWatch lags a few minutes.

### 9. Teardown

```bash
export AWS_PROFILE=<deploy-profile>
bash scripts/teardown.sh all             # or: bash scripts/teardown.sh nat_v1_bursty ep_v1_bursty
```

It runs `terraform destroy` per variant (NAT gateways, EIPs, endpoints, Runtimes, IAM roles, code
buckets, usage-log delivery), deletes the Runtime log groups, deletes any ECR repos tagged
`project=agentcore-bill-experiment`, deletes the account-level log resource policy when run with `all`,
then lists remaining Runtimes and anything still carrying the project tag. It is idempotent.

Run it a second time later. AgentCore leaves its VPC network interfaces in place for up to about 8
hours after a Runtime is deleted, so subnet, security group and
VPC deletion can fail on the first pass with a dependency error. The billable parts (NAT, EIP, endpoints)
are removed first. Re-run the script the next day and check that the final listings are empty. Nothing left in a VPC bills,
but do not skip the second pass.

If you created the usage-log policy by hand with `bash scripts/usage_log_policy.sh`, remove it with
`bash scripts/usage_log_policy.sh delete` (`teardown.sh all` does this).

## Control: bare echo agent

`pub_v2_idle_echo` and `pub_v1_idle_echo` run `agent_echo/main.py`: only `BedrockAgentCoreApp`, no
Strands, no boto3 client, no model call, no background threads, 1 h idle timeout. They tell apart
"V2 does not reclaim idle memory" from "the Strands/boto stack keeps the session busy". Set per variant
with `agent: echo` in the config.

```bash
bash scripts/deploy.sh pub_v2_idle_echo pub_v1_idle_echo
python load/run_load.py --variant pub_v2_idle_echo      # one call each
python load/run_load.py --variant pub_v1_idle_echo
# about 30 and again about 75 minutes later:
python analysis/idle_timeline.py --variants pub_v2_idle_echo pub_v1_idle_echo --minutes 90
```

## Control: allocate and free

The echo agent never uses much memory, so it cannot show whether V2 returns memory that an
agent releases. `agent_alloc/main.py` tests the documented behaviour directly. With
`payload_extra: {alloc_mb: 1024, hold_s: 5}` (set in the config for `pub_v2_alloc` and `pub_v1_alloc`),
the single call allocates 1 GB as an anonymous `mmap` and writes to every page so it is resident, holds
it for 5 seconds while the request is in flight, then closes the mapping (so the pages are unmapped),
runs `gc.collect()` and glibc `malloc_trim(0)`, and responds. The response and the JSONL row include
`alloc_mb`, `rss_mb_before`, `rss_mb_peak` and `rss_mb_after` (VmRSS from `/proc/self/status`) so you
can confirm the process really freed it. After that the session idles.

Expected reading: on V2, if reclaim works as documented, metered memory (`gb_held`) should fall back
within a few minutes of the free, well before the idle timeout. On V1, compare: V1 meters the whole
session. A result that differs from this is a finding in itself.

```bash
bash scripts/deploy.sh pub_v2_alloc pub_v1_alloc
python load/run_load.py --variant pub_v2_alloc          # one call each
python load/run_load.py --variant pub_v1_alloc
# about 20 minutes later:
python analysis/idle_timeline.py --variants pub_v2_alloc pub_v1_alloc --minutes 30
```

The allocation helper can be tried locally without `bedrock_agentcore` installed:
`python agent_alloc/main.py --selftest 64` prints the RSS before, at peak and after. On Linux this
reads `/proc`; on Windows it falls back to the process working set; elsewhere the RSS fields are `null`.
The runtime's memory limit is not documented here; 1 GB is a request to the platform, and if the
call fails the status is logged in the JSONL row.

## Known issues found during the experiment

- USAGE_LOGS delivery is configured (`infra/usage_logs.tf`: log group, delivery source on the Runtime
  ARN, destination, delivery; toggle with `enable_usage_logs`), but no records were delivered. `collect_usage_logs.py` is kept for completeness; do not depend on it.
  Cost and usage figures come from CloudWatch metrics and Cost Explorer instead.
- Cost Explorer lags about a day, and the `project` / `variant` cost allocation tags must be activated
  in Billing and only apply to usage after activation. Expect the first day or so to be untagged.
- The S3 object for each agent zip is over 16 MB, so S3 stores it with a multipart ETag
  (`<hash>-N`) that never equals the file's MD5, which would show as a perpetual in-place change in
  every plan. `infra/runtime.tf` ignores `etag`; the object key embeds the MD5, so a changed zip still
  produces a new object. Rebuilding a zip therefore changes the plan for every variant using it.
- A laptop going to sleep interrupts load runs. Runs are resumable: re-run the same command.
- `OverheadLatency` is emitted for V2 Runtimes only; V1 variants have no such metric.
- VPC network interfaces can linger for hours after Runtime deletion; see Teardown.

## Design notes

- Terraform rather than the AgentCore CLI: the CLI deploys through a CDK app and the documentation
  consulted exposed no platform-version switch for it. The AWS provider's
  `aws_bedrockagentcore_agent_runtime` supports `platform_version`, `tags`, VPC config and
  code-from-S3.
- Direct code deploy (zip) instead of containers: no Docker, no ECR. Container agents need ECR api/dkr
  and S3 gateway endpoints; zip agents need the S3 gateway endpoint and Logs, plus Bedrock Runtime
  for the model call.
- Idle shape: sessions end after `idleRuntimeSessionTimeout` (default 15 min), so "idle for hours" would
  bill nothing after 15 min. The idle variants set longer timeouts (the API allows up to 1,209,600 s).
- One Runtime per variant, because Cost Explorer is daily and cannot separate shapes that share a Runtime.

## Safety notes

- Everything is tagged `project=agentcore-bill-experiment`, plus `variant=<id>`. The teardown script's
  leftover listing and the cost tags both use that tag.
- `deploy.sh` uses `terraform apply -auto-approve`. Run it with `PLAN_ONLY=1` first and read the plan.
- `teardown.sh` destroys with `-auto-approve` and deletes log groups and tagged ECR repos. Check which
  account `aws sts get-caller-identity` reports before running either script.
- Nothing in this repository contains credentials or account ids. Terraform state under `infra/`
  does contain them; it is gitignored and should stay that way, as should `config.yaml`, `results/`, `build/`.
- Deploying creates billable resources. NAT gateways and interface endpoints bill every hour until torn down.

## Results

Not published yet. The write-up will be linked here when it exists. This repository contains the
method only; no measured results are stated in this README.

## License

MIT, see `LICENSE`.
