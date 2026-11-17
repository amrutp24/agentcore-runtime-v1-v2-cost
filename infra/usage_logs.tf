# Per-session usage logs (log type USAGE_LOGS) for the Runtime, delivered to a
# CloudWatch log group via vended log delivery:
#   delivery source (Runtime ARN) -> delivery -> delivery destination (log group)
# Purely additive: nothing here modifies the Runtime, role or bucket.
#
# Docs: "AgentCore generated runtime observability data" (Vended logs) lists the
# USAGE_LOGS log type and its fields at 1-second granularity.
# TODO(verify): the docs do not state whether platform V1 runtimes emit USAGE_LOGS
# (the reference post only tested V2). A V1 variant may deliver nothing.
# TODO(verify): whether the caller needs logs:PutResourcePolicy so CloudWatch can
# add the delivery.logs.amazonaws.com policy on the log group. The /aws/vendedlogs/
# prefix is used because such groups need no per-group resource policy.

locals {
  usage_logs_enabled = var.enable_usage_logs
  usage_log_group    = "/aws/vendedlogs/bedrock-agentcore/runtimes/USAGE_LOGS/${local.name}"
}

resource "aws_cloudwatch_log_group" "usage" {
  count             = local.usage_logs_enabled ? 1 : 0
  name              = local.usage_log_group
  retention_in_days = 14
  tags              = local.tags
}

resource "aws_cloudwatch_log_delivery_source" "usage" {
  count        = local.usage_logs_enabled ? 1 : 0
  name         = "${local.name}-usage-src"
  log_type     = "USAGE_LOGS"
  resource_arn = aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn
  tags         = local.tags
}

resource "aws_cloudwatch_log_delivery_destination" "usage" {
  count = local.usage_logs_enabled ? 1 : 0
  name  = "${local.name}-usage-dst"
  tags  = local.tags

  delivery_destination_configuration {
    destination_resource_arn = aws_cloudwatch_log_group.usage[0].arn
  }
}

resource "aws_cloudwatch_log_delivery" "usage" {
  count                    = local.usage_logs_enabled ? 1 : 0
  delivery_source_name     = aws_cloudwatch_log_delivery_source.usage[0].name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.usage[0].arn
  tags                     = local.tags
}
