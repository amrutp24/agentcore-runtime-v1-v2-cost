data "aws_caller_identity" "current" {}

# --- Code bucket (direct code deploy: no Docker, no ECR) ---

resource "aws_s3_bucket" "code" {
  bucket_prefix = "acbill-${replace(var.variant_id, "_", "-")}-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "code" {
  bucket                  = aws_s3_bucket.code.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

locals {
  zip_md5 = try(filemd5(var.code_zip_path), "missing")
}

resource "aws_s3_object" "code" {
  bucket = aws_s3_bucket.code.id
  key    = "${var.variant_id}/${local.zip_md5}.zip"
  source = var.code_zip_path
  etag   = local.zip_md5

  # The zip is over 16 MB, so S3 stores it with a multipart ETag ("<hash>-N") that
  # never equals the plain md5 and shows as a perpetual in-place change. The key
  # embeds the md5, so a changed zip still produces a new object.
  lifecycle {
    ignore_changes = [etag]
  }
}

# --- Execution role (adapted from the AgentCore Runtime permissions doc) ---

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

data "aws_iam_policy_document" "runtime" {
  statement {
    actions   = ["logs:DescribeLogStreams", "logs:CreateLogGroup"]
    resources = ["arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"]
  }
  statement {
    actions   = ["logs:DescribeLogGroups"]
    resources = ["arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:*"]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"]
  }
  statement {
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
    resources = ["*"]
  }
  statement {
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["bedrock-agentcore"]
    }
  }
  statement {
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
      "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default",
      "arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/*",
    ]
  }
  statement {
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = ["arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:*"]
  }
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.code.arn}/*"]
  }
}

resource "aws_iam_role" "runtime" {
  name_prefix        = "${local.name}-"
  assume_role_policy = data.aws_iam_policy_document.trust.json
}

resource "aws_iam_role_policy" "runtime" {
  role   = aws_iam_role.runtime.id
  name   = "runtime"
  policy = data.aws_iam_policy_document.runtime.json
}

# --- Runtime ---

resource "aws_bedrockagentcore_agent_runtime" "this" {
  agent_runtime_name = "bill_${var.variant_id}"
  role_arn           = aws_iam_role.runtime.arn
  platform_version   = var.platform_version

  agent_runtime_artifact {
    code_configuration {
      entry_point = ["main.py"]
      runtime     = "PYTHON_3_13"
      code {
        s3 {
          bucket = aws_s3_bucket.code.id
          prefix = aws_s3_object.code.key
        }
      }
    }
  }

  network_configuration {
    network_mode = local.use_vpc ? "VPC" : "PUBLIC"

    dynamic "network_mode_config" {
      for_each = local.use_vpc ? [1] : []
      content {
        subnets         = aws_subnet.private[*].id
        security_groups = [aws_security_group.runtime[0].id]
      }
    }
  }

  lifecycle_configuration {
    idle_runtime_session_timeout = var.idle_timeout_seconds
    max_lifetime                 = var.max_lifetime_seconds
  }

  environment_variables = {
    MODEL_ID       = var.model_id
    MAX_TOKENS     = "128"
    VARIANT_ID     = var.variant_id
    BEDROCK_REGION = var.region
  }

  tags = local.tags

  depends_on = [
    aws_iam_role_policy.runtime,
    aws_route.private_nat,
    aws_vpc_endpoint.interface,
    aws_vpc_endpoint.s3,
    aws_vpc_security_group_egress_rule.runtime_https_cidr,
    aws_vpc_security_group_egress_rule.runtime_https_s3,
  ]
}
