# ---------------------------------------------------------------------------
# Logs: the CloudWatch agent ships each server's PHP-FPM slow log and error log
# into two shared log groups, one stream per instance.
# ---------------------------------------------------------------------------
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  slow_log_group  = "/${var.name_prefix}/slow"
  error_log_group = "/${var.name_prefix}/error"

  servers = {
    for k, s in var.servers : k => merge(s, {
      slowlog_path   = coalesce(s.slowlog_path, "/var/log/php${s.php_version}-fpm.slow.log")
      error_log_path = coalesce(s.error_log_path, "/var/log/php${s.php_version}-fpm.log")
    })
  }

  # One agent config for every server: each ships whichever of its files exist.
  # The slow log is split on the entry header ("[13-Nov-2026 01:30:12]  [pool www] pid 1")
  # so each CloudWatch event is one whole entry with its stack.
  agent_config = jsonencode({
    agent = { run_as_user = "root" }
    logs = {
      logs_collected = {
        files = {
          collect_list = concat(
            [for p in distinct([for s in local.servers : s.slowlog_path]) : {
              file_path                = p
              log_group_name           = local.slow_log_group
              log_stream_name          = "{instance_id}"
              multi_line_start_pattern = "^\\[\\d{2}-[A-Za-z]{3}-\\d{4} \\d{2}:\\d{2}:\\d{2}\\]\\s+\\[pool"
            }],
            [for p in distinct([for s in local.servers : s.error_log_path]) : {
              file_path       = p
              log_group_name  = local.error_log_group
              log_stream_name = "{instance_id}"
            }],
          )
        }
      }
    }
  })
}

resource "aws_cloudwatch_log_group" "slow" {
  name              = local.slow_log_group
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "error" {
  name              = local.error_log_group
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_iam_role_policy" "instance_logs" {
  for_each = toset(var.instance_role_names)

  name = "${var.name_prefix}-cloudwatch-logs"
  role = each.value

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "ShipPhpFpmLogs"
      Effect = "Allow"
      Action = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
      Resource = [
        aws_cloudwatch_log_group.slow.arn, "${aws_cloudwatch_log_group.slow.arn}:*",
        aws_cloudwatch_log_group.error.arn, "${aws_cloudwatch_log_group.error.arn}:*",
      ]
    }]
  })
}

# ---------------------------------------------------------------------------
# Agent: one SSM document that installs the agent, then loads the config from
# Parameter Store. The association runs it on create, whenever the config
# changes (the name carries its hash) and on `agent_schedule`.
# ---------------------------------------------------------------------------
resource "aws_ssm_parameter" "agent_config" {
  count = var.install_agent ? 1 : 0

  # The agent's managed document can read any parameter the instance can; the
  # AmazonCloudWatch- prefix is the one CloudWatchAgentServerPolicy allows.
  name  = "AmazonCloudWatch-${var.name_prefix}"
  type  = "String"
  value = local.agent_config
  tags  = var.tags
}

resource "aws_ssm_document" "agent" {
  count = var.install_agent ? 1 : 0

  name          = "${var.name_prefix}-cloudwatch-agent"
  document_type = "Command"
  tags          = var.tags

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install the CloudWatch agent and load the PHP-FPM log config."
    mainSteps = [
      {
        action = "aws:runDocument"
        name   = "install"
        inputs = {
          documentType       = "SSMDocument"
          documentPath       = "AWS-ConfigureAWSPackage"
          documentParameters = jsonencode({ action = "Install", name = "AmazonCloudWatchAgent" })
        }
      },
      {
        action = "aws:runDocument"
        name   = "configure"
        inputs = {
          documentType = "SSMDocument"
          documentPath = "AmazonCloudWatch-ManageAgent"
          documentParameters = jsonencode({
            action                        = "configure"
            mode                          = "ec2"
            optionalConfigurationSource   = "ssm"
            optionalConfigurationLocation = aws_ssm_parameter.agent_config[0].name
            optionalRestart               = "yes"
          })
        }
      },
    ]
  })
}

resource "aws_ssm_association" "agent" {
  count = var.install_agent ? 1 : 0

  name                = aws_ssm_document.agent[0].name
  association_name    = "${var.name_prefix}-cloudwatch-agent-${substr(sha1(local.agent_config), 0, 8)}"
  schedule_expression = var.agent_schedule

  targets {
    key    = "InstanceIds"
    values = [for s in local.servers : s.instance_id]
  }

  # The instances need the log permissions before the agent starts shipping.
  depends_on = [aws_iam_role_policy.instance_logs]
}
