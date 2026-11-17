terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # aws_bedrockagentcore_agent_runtime first shipped in 6.17.0; the
      # platform_version argument is newer.
      # TODO(verify): exact first provider version with platform_version.
      version = ">= 6.58.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.tags
  }
}
