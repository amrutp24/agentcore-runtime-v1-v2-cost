# AgentCore Runtime V1 vs V2: cost and latency measurement

Terraform, Python and bash to deploy the same agent to Amazon Bedrock AgentCore Runtime on
platform V1 and V2, send it controlled load, and compare what each version meters and bills.
It also compares public networking with a VPC behind a NAT gateway and a VPC with endpoints only.

Each variant gets its own Runtime. Cost Explorer is daily and CloudWatch reports usage per
Runtime, so two configurations sharing a Runtime can't be told apart afterwards. Every resource
is tagged `project=agentcore-bill-experiment` and `variant=<id>`.

The NAT gateway and interface endpoints bill for every hour they exist. See "Cost" before you
deploy those two variants.

## What V2 reclaims

The AWS docs say V2 "reclaims memory as your agent releases it", and the pricing page says
"Memory is reclaimed within the session when unused for 120 seconds". Both are about memory the
agent process has freed. A session that sits idle with its libraries loaded has freed nothing,
and it is metered until its idle timeout on both versions.

The two control agents are there to separate those cases. The echo agent does nothing, which
shows what the platform meters on its own. The alloc agent takes 1 GB and gives it back to the
operating system, which shows the reclaim.

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

The agents:

- `agent/main.py` is a Strands agent with one tool, a low max-token limit and Amazon Nova Micro.
- `agent_echo/main.py` is only `BedrockAgentCoreApp`. It returns its input.
- `agent_alloc/main.py` is the same bare app. A payload of `{"alloc_mb": N, "hold_s": S}` makes it
  hold N MB for S seconds, free it, and report its resident set size before, at peak and after.

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

Output goes to `build/` (zips, generated tfvars) and `results/` (logs, CSVs, reports). Both are
gitignored. Terraform state is local, one workspace per variant, under `infra/terraform.tfstate.d/`.

## Prerequisites

- Python 3.11 or newer.
- Terraform 1.5 or newer. I used 1.11.2 with AWS provider 6.73.0.
- AWS CLI v2 and bash. Git Bash on Windows works.
- `uv`, which fetches the linux/arm64 wheels for the agent zips. Docker isn't needed.
- An AWS account in a Region that has Runtime V2 and Amazon Nova Micro (default `us-east-1`),
  with Cost Explorer enabled.

Permissions, roughly:

- To deploy and tear down: VPC networking, IAM roles, S3, `bedrock-agentcore-control` Runtimes,
  CloudWatch Logs (log groups, deliveries, resource policies) and tagging read.
- To send load: `bedrock-agentcore:InvokeAgentRuntime` and `StopRuntimeSession`.
- To collect and report: `ce:GetCostAndUsage`, `cloudwatch:GetMetricData`, `cloudwatch:ListMetrics`
  and `logs:FilterLogEvents`.

Pick the profile with `AWS_PROFILE`. `config.yaml` is gitignored in case you set `aws_profile` there.

## Cost

My whole run cost $9.39 in us-east-1. About $7 of that was the NAT gateway and the interface
endpoints, which stayed up for three and a half days. Runtime compute for every variant and
every run came to $2.20.

| item | list rate (USD) | billed in my run |
|---|---|---|
| NAT gateway and its public IPv4 address | 0.045 + 0.005 per hour, plus about 0.045 per GB | 1.14 for 20 h, with 3.0 GB of data |
| Interface endpoints, 2 endpoints x 2 AZs | 0.01 per endpoint per AZ per hour | 0.80 for 20 h |
| Runtime V1 | 0.0895 per vCPU-hour, 0.00945 per GB-hour | 0.0027 per bursty session |
| Runtime V2 | 0.1276 per vCPU-hour, 0.0169 per GB-hour | 0.0076 per bursty session |
| Cost Explorer API | 0.01 per request | a few requests per collection |

A bursty session here is three calls and then 15 minutes waiting for the idle timeout.

The VPC, subnets, route tables, security groups, network interfaces and the S3 gateway endpoint
are free. Runtime sessions meter memory until their idle timeout, so the idle variants with long
timeouts keep costing a little after the single call.

If you only want the V1 and V2 comparison, skip `nat_v1_bursty` and `ep_v1_bursty`. The public
variants cost cents, and the four control variants (`*_idle_echo`, `*_alloc`) can be deployed,
measured and torn down in about two hours.

Cost Explorer lags about 24 hours, so the last day of cost data arrives after teardown. Cost
allocation tags show up only after you activate them in Billing, and only for usage after that.
`report.py` marks a variant `share` when it had to split the account total by usage share, and
`tag` when it had tag data.

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

In `config.yaml`, set `window.start` and `window.end` to cover your run (end is exclusive) and
remove the variants you don't want. `supported_az_ids` must list AZ ids that AgentCore supports
in the Region, so check it if you change Region.

### 2. Plan (creates nothing)

`deploy.sh` runs `terraform apply -auto-approve`, so plan first:

```bash
PLAN_ONLY=1 bash scripts/deploy.sh pub_v1_bursty
```

The first plan or deploy builds `build/agent.zip`, plus `build/agent_echo.zip` and
`build/agent_alloc.zip` when a selected variant needs them.

### 3. Deploy

```bash
bash scripts/deploy.sh pub_v1_bursty
# smoke test: at most 20 calls
python load/run_load.py --variant pub_v1_bursty --duration 5m --max-invocations 20
# the rest (V2 runtimes take several minutes each to reach READY)
bash scripts/deploy.sh pub_v2_bursty pub_v1_steady pub_v2_steady pub_v1_idle pub_v2_idle nat_v1_bursty ep_v1_bursty
```

Outputs for each variant are saved to `results/<variant>/outputs.json`.

### 4. Run load

```bash
mkdir -p results
# idle shape = one call, then nothing
for v in pub_v1_idle pub_v2_idle; do python load/run_load.py --variant $v; done
for v in pub_v1_bursty pub_v2_bursty nat_v1_bursty ep_v1_bursty pub_v1_steady pub_v2_steady; do
  nohup python load/run_load.py --variant $v > results/$v.out 2>&1 &
done
```

Each invocation is one row in `results/<variant>/invocations.jsonl`. SDK retries are off, so a
row is a single attempt. Shape, rate, duration and the invocation cap come from the variant's
`load` block in the config.

A run can be stopped with Ctrl-C and resumed by running the same command again. The cap counts
rows already logged, and a steady session is reused if it is still alive. `--stop-sessions` ends
each bursty session early. It is off by default because the idle tail is part of the measurement.

A variant's `payload_extra` is merged into the request payload.

### 5. Activate cost allocation tags

This changes billing configuration for the account. Tags can take up to 24 hours to appear in
Billing after the resources exist.

```bash
aws ce update-cost-allocation-tags-status --cost-allocation-tags-status \
  TagKey=project,Status=Active TagKey=variant,Status=Active
```

### 6. Collect

These only read. Use a read-only profile if you have one.

```bash
export AWS_PROFILE=<read-only-profile>
python analysis/collect_costs.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]
python analysis/collect_usage_logs.py     # per-session usage logs -> results/usage/ (see Known issues)
```

After the first `collect_costs.py` run, print the Cost Explorer `USAGE_TYPE` strings (the script
prints the command) and fix `usage_type_buckets` in `analysis/rates.yaml` if they differ.

### 7. Report

```bash
python analysis/predict.py --metrics results/costs/cw_metrics.csv
python analysis/report.py                 # results/report.md, report.csv, *.png
```

### 8. Idle timeline

```bash
python analysis/idle_timeline.py --variants pub_v2_idle_echo pub_v1_idle_echo --minutes 90
```

`results/idle_timeline.csv` has GB held and vCPU used per minute since the variant's first call.
The values are CloudWatch `MemoryUsed-GBHours` and `CPUUsed-vCPUHours` multiplied by 60.
Timestamps are UTC, and CloudWatch lags a few minutes.

### 9. Teardown

```bash
export AWS_PROFILE=<deploy-profile>
bash scripts/teardown.sh all             # or: bash scripts/teardown.sh nat_v1_bursty ep_v1_bursty
```

This runs `terraform destroy` for each variant, deletes the Runtime log groups and any ECR repos
tagged `project=agentcore-bill-experiment`, and with `all` also deletes the account-level log
resource policy. It finishes by listing any Runtimes and tagged resources that are left.

Run it again the next day. AgentCore keeps its VPC network interfaces for up to about 8 hours
after a Runtime is deleted, so the subnets, security groups and VPC can fail to delete on the
first pass. The NAT gateway, Elastic IP and endpoints go on the first pass, and nothing that is
left bills. The second pass is done when the final listings are empty.

If you created the usage-log policy by hand with `bash scripts/usage_log_policy.sh`, remove it
with `bash scripts/usage_log_policy.sh delete`.

## Control: bare echo agent

`pub_v2_idle_echo` and `pub_v1_idle_echo` run `agent_echo/main.py` with a 1 h idle timeout. With
no framework, boto3 client or model call, whatever is metered is the platform's own baseline.
Set it for a variant with `agent: echo`.

```bash
bash scripts/deploy.sh pub_v2_idle_echo pub_v1_idle_echo
python load/run_load.py --variant pub_v2_idle_echo      # one call each
python load/run_load.py --variant pub_v1_idle_echo
# about 30 and again about 75 minutes later:
python analysis/idle_timeline.py --variants pub_v2_idle_echo pub_v1_idle_echo --minutes 90
```

## Control: allocate and free

`pub_v2_alloc` and `pub_v1_alloc` send `payload_extra: {alloc_mb: 1024, hold_s: 5}`. The call
allocates 1 GB as an anonymous `mmap`, writes to every page so it is resident, holds it for 5
seconds, then closes the mapping, runs `gc.collect()` and glibc `malloc_trim(0)`, and responds.
A `bytearray` isn't used because freed memory can stay inside the Python allocator.

The response and the JSONL row include `alloc_mb`, `rss_mb_before`, `rss_mb_peak` and
`rss_mb_after`, read from `/proc/self/status`, so you can check that the process freed it.

```bash
bash scripts/deploy.sh pub_v2_alloc pub_v1_alloc
python load/run_load.py --variant pub_v2_alloc          # one call each
python load/run_load.py --variant pub_v1_alloc
# about 20 minutes later:
python analysis/idle_timeline.py --variants pub_v2_alloc pub_v1_alloc --minutes 30
```

To try the allocation locally without `bedrock_agentcore` installed, run
`python agent_alloc/main.py --selftest 64`. It reads `/proc` on Linux and the process working
set on Windows. On other systems the RSS fields are `null`.

## Known issues

- USAGE_LOGS delivery is set up in `infra/usage_logs.tf` (toggle with `enable_usage_logs`), but
  no records were ever delivered to me. `collect_usage_logs.py` is still here, but the cost and
  usage figures come from CloudWatch metrics and Cost Explorer.
- Each agent zip is over 16 MB, so S3 stores it with a multipart ETag that never matches the
  file's MD5. `infra/runtime.tf` ignores `etag` and puts the MD5 in the object key. Rebuilding a
  zip changes the plan for every variant that uses it.
- A laptop going to sleep interrupts load runs. Run the same command again to resume.
- `OverheadLatency` is emitted for V2 Runtimes only.

## Design notes

- Terraform, not the AgentCore CLI. The CLI deploys through a CDK app and I found no way to set
  the platform version with it. The AWS provider's `aws_bedrockagentcore_agent_runtime` takes
  `platform_version`, tags, VPC config and code from S3.
- Zip deploy, not containers, so there is no Docker or ECR. A zip agent in a VPC needs the S3
  gateway endpoint, Logs and Bedrock Runtime. A container agent would also need `ecr.api` and
  `ecr.dkr`.
- Sessions end after `idleRuntimeSessionTimeout`, which defaults to 15 minutes. The idle variants
  set longer timeouts so there is something to measure. The API allows up to 1,209,600 s.

## Before you run it

- `deploy.sh` and `teardown.sh` both use `-auto-approve`. Check which account
  `aws sts get-caller-identity` reports first.
- `teardown.sh` deletes log groups and ECR repos that carry the project tag.
- Terraform state under `infra/` contains account ids. It is gitignored, along with
  `config.yaml`, `results/` and `build/`.

## Results

From my run in us-east-1, November 2026:

- Strands agent, bursty sessions: V2 cost 2.8 times as much per session as V1 ($0.0076 against
  $0.0027). V2 metered about 1.5 times the GB-hours at 1.8 times the rate.
- Allocate-and-free agent, one-hour session: V2 metered 45 to 56% of V1's GB-hours, and the two
  cost about the same.
- CloudWatch usage metrics matched Cost Explorer to within 0.01%.
- Cold start p50 was 4.69 s on V1 and 2.57 s on V2, measured from a laptop.
- For 20 hours in a VPC, the NAT gateway variant was billed $1.14 and the endpoints variant $0.80.

These are three small Python agents with short runs, so expect different numbers for your own agent.

## License

MIT, see `LICENSE`.
