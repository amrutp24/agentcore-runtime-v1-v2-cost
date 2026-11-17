output "variant_id" {
  value = var.variant_id
}

output "region" {
  value = var.region
}

output "network_mode" {
  value = var.network_mode
}

output "platform_version" {
  value = var.platform_version
}

output "agent_runtime_id" {
  value = aws_bedrockagentcore_agent_runtime.this.agent_runtime_id
}

output "agent_runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn
}

output "code_bucket" {
  value = aws_s3_bucket.code.id
}

output "vpc_id" {
  value = try(aws_vpc.this[0].id, null)
}

output "nat_gateway_id" {
  value = try(aws_nat_gateway.this[0].id, null)
}

output "vpc_endpoint_ids" {
  value = concat(
    [for e in aws_vpc_endpoint.interface : e.id],
    [for e in aws_vpc_endpoint.s3 : e.id],
  )
}

output "interface_endpoint_count" {
  value = length(aws_vpc_endpoint.interface)
}

output "az_count" {
  value = length(local.az_ids)
}

output "usage_log_group_name" {
  value = try(aws_cloudwatch_log_group.usage[0].name, null)
}

output "usage_log_delivery_id" {
  value = try(aws_cloudwatch_log_delivery.usage[0].id, null)
}
