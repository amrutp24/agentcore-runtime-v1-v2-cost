locals {
  tags = {
    project = var.project_tag
    variant = var.variant_id
  }

  name          = "bill-${replace(var.variant_id, "_", "-")}"
  use_vpc       = var.network_mode != "public"
  use_nat       = var.network_mode == "vpc_nat"
  use_endpoints = var.network_mode == "vpc_endpoints"
  use_s3_gw     = local.use_endpoints || (local.use_nat && var.nat_s3_gateway_endpoint)

  az_ids = local.use_vpc ? slice(
    [for z in data.aws_availability_zones.available.zone_ids : z if contains(var.supported_az_ids, z)],
    0, var.az_count
  ) : []
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "this" {
  count                = local.use_vpc ? 1 : 0
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_subnet" "private" {
  count                = length(local.az_ids)
  vpc_id               = aws_vpc.this[0].id
  availability_zone_id = local.az_ids[count.index]
  cidr_block           = cidrsubnet(var.vpc_cidr, 8, count.index)
  tags                 = { Name = "${local.name}-private-${count.index}" }
}

resource "aws_route_table" "private" {
  count  = local.use_vpc ? 1 : 0
  vpc_id = aws_vpc.this[0].id
  tags   = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[0].id
}

# --- NAT variant: one public subnet, IGW, one NAT gateway, one EIP ---

resource "aws_internet_gateway" "this" {
  count  = local.use_nat ? 1 : 0
  vpc_id = aws_vpc.this[0].id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count                = local.use_nat ? 1 : 0
  vpc_id               = aws_vpc.this[0].id
  availability_zone_id = local.az_ids[0]
  cidr_block           = cidrsubnet(var.vpc_cidr, 8, 100)
  tags                 = { Name = "${local.name}-public" }
}

resource "aws_route_table" "public" {
  count  = local.use_nat ? 1 : 0
  vpc_id = aws_vpc.this[0].id
  tags   = { Name = "${local.name}-public" }
}

resource "aws_route" "public_default" {
  count                  = local.use_nat ? 1 : 0
  route_table_id         = aws_route_table.public[0].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this[0].id
}

resource "aws_route_table_association" "public" {
  count          = local.use_nat ? 1 : 0
  subnet_id      = aws_subnet.public[0].id
  route_table_id = aws_route_table.public[0].id
}

resource "aws_eip" "nat" {
  count  = local.use_nat ? 1 : 0
  domain = "vpc"
  tags   = { Name = "${local.name}-nat" }
}

resource "aws_nat_gateway" "this" {
  count         = local.use_nat ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = local.name }
  depends_on    = [aws_internet_gateway.this]
}

resource "aws_route" "private_nat" {
  count                  = local.use_nat ? 1 : 0
  route_table_id         = aws_route_table.private[0].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[0].id
}

# --- Endpoints ---

data "aws_prefix_list" "s3" {
  count = local.use_s3_gw ? 1 : 0
  name  = "com.amazonaws.${var.region}.s3"
}

resource "aws_vpc_endpoint" "s3" {
  count             = local.use_s3_gw ? 1 : 0
  vpc_id            = aws_vpc.this[0].id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private[0].id]
  tags              = { Name = "${local.name}-s3" }
}

resource "aws_security_group" "endpoints" {
  count       = local.use_endpoints ? 1 : 0
  name_prefix = "${local.name}-vpce-"
  description = "Interface endpoints: HTTPS from inside the VPC"
  vpc_id      = aws_vpc.this[0].id
  tags        = { Name = "${local.name}-vpce" }
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_https" {
  count             = local.use_endpoints ? 1 : 0
  security_group_id = aws_security_group.endpoints[0].id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_endpoint" "interface" {
  for_each            = local.use_endpoints ? toset(var.interface_endpoint_services) : toset([])
  vpc_id              = aws_vpc.this[0].id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints[0].id]
  private_dns_enabled = true
  tags                = { Name = "${local.name}-${each.key}" }
}

# --- Runtime ENI security group ---

resource "aws_security_group" "runtime" {
  count       = local.use_vpc ? 1 : 0
  name_prefix = "${local.name}-rt-"
  description = "AgentCore Runtime ENIs: outbound HTTPS only"
  vpc_id      = aws_vpc.this[0].id
  tags        = { Name = "${local.name}-rt" }
}

# NAT mode: anywhere. Endpoints mode: VPC CIDR (interface endpoints) plus the S3 prefix list.
resource "aws_vpc_security_group_egress_rule" "runtime_https_cidr" {
  count             = local.use_vpc ? 1 : 0
  security_group_id = aws_security_group.runtime[0].id
  cidr_ipv4         = local.use_nat ? "0.0.0.0/0" : var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "runtime_https_s3" {
  count             = local.use_s3_gw && !local.use_nat ? 1 : 0
  security_group_id = aws_security_group.runtime[0].id
  prefix_list_id    = data.aws_prefix_list.s3[0].id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}
