variable "region" {
  type        = string
  description = "AWS Region. Must support the chosen platform_version."
}

variable "variant_id" {
  type        = string
  description = "Experiment variant id; used in names and the variant tag."
  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{0,29}$", var.variant_id))
    error_message = "variant_id: lowercase letters, digits, underscore; start with a letter; max 30 chars."
  }
}

variable "project_tag" {
  type    = string
  default = "agentcore-bill-experiment"
}

variable "network_mode" {
  type        = string
  description = "public = no VPC; vpc_nat = private subnets + NAT gateway; vpc_endpoints = private subnets + endpoints only."
  validation {
    condition     = contains(["public", "vpc_nat", "vpc_endpoints"], var.network_mode)
    error_message = "network_mode must be public, vpc_nat or vpc_endpoints."
  }
}

variable "platform_version" {
  type    = string
  default = "V1"
  validation {
    condition     = contains(["V1", "V2"], var.platform_version)
    error_message = "platform_version must be V1 or V2."
  }
}

variable "idle_timeout_seconds" {
  type    = number
  default = 900
}

variable "max_lifetime_seconds" {
  type    = number
  default = 28800
}

variable "model_id" {
  type    = string
  default = "amazon.nova-micro-v1:0"
}

variable "code_zip_path" {
  type        = string
  description = "Path to the agent zip built by scripts/package_agent.py (relative to infra/)."
  default     = "../build/agent.zip"
}

variable "vpc_cidr" {
  type    = string
  default = "10.77.0.0/16"
}

variable "az_count" {
  type    = number
  default = 2
}

variable "supported_az_ids" {
  type        = list(string)
  description = "AZ IDs AgentCore supports for VPC mode in this Region."
  default     = ["use1-az1", "use1-az2", "use1-az4"]
}

variable "interface_endpoint_services" {
  type        = list(string)
  description = "Interface endpoint service suffixes for vpc_endpoints mode (e.g. logs, bedrock-runtime, ecr.api, ecr.dkr)."
  default     = ["logs", "bedrock-runtime"]
}

variable "nat_s3_gateway_endpoint" {
  type        = bool
  description = "Also add a free S3 gateway endpoint in vpc_nat mode."
  default     = false
}

variable "enable_usage_logs" {
  type        = bool
  description = "Deliver Runtime USAGE_LOGS (per-session vCPU/memory usage) to a CloudWatch log group."
  default     = true
}
