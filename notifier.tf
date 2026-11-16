# ---------------------------------------------------------------------------
# Notifier: subscribed to both log groups (slow entries; "pool full" lines from
# the error log) and run on a schedule for the Logs Insights digest.
# ---------------------------------------------------------------------------
locals {
  notifier_name = "${var.name_prefix}-notifier"
  # Under .build so a repo's CI can carry the planned zip to a saved-plan apply.
  notifier_zip = "${path.module}/.build/notifier.zip"
}

data "archive_file" "notifier" {
  type        = "zip"
  source_file = "${path.module}/lambda/notifier.py"
  output_path = local.notifier_zip
}

resource "aws_cloudwatch_log_group" "notifier" {
  name              = "/aws/lambda/${local.notifier_name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_iam_role" "notifier" {
  name = "${local.notifier_name}-role"
  tags = var.tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "notifier" {
  name = "${local.notifier_name}-policy"
  role = aws_iam_role.notifier.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Sid      = "OwnLogs"
          Effect   = "Allow"
          Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
          Resource = "${aws_cloudwatch_log_group.notifier.arn}:*"
        },
        {
          Sid      = "DigestQuery"
          Effect   = "Allow"
          Action   = ["logs:StartQuery"]
          Resource = [aws_cloudwatch_log_group.slow.arn, "${aws_cloudwatch_log_group.slow.arn}:*"]
        },
        {
          # GetQueryResults has no resource-level permissions.
          Sid      = "DigestResults"
          Effect   = "Allow"
          Action   = ["logs:GetQueryResults"]
          Resource = "*"
        },
      ],
      var.ship_fail2ban ? [{
        Sid      = "BanContext"
        Effect   = "Allow"
        Action   = ["logs:FilterLogEvents"]
        Resource = [aws_cloudwatch_log_group.fail2ban[0].arn, "${aws_cloudwatch_log_group.fail2ban[0].arn}:*"]
      }] : [],
      var.alert_topic_arn == null ? [] : [{
        Sid      = "Publish"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = var.alert_topic_arn
      }],
      var.slack_webhook_ssm_parameter == null ? [] : [{
        Sid      = "SlackWebhook"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter/${trimprefix(var.slack_webhook_ssm_parameter, "/")}"
      }],
    )
  })
}

resource "aws_lambda_function" "notifier" {
  function_name    = local.notifier_name
  description      = "PHP-FPM slow requests and full pools from CloudWatch Logs to Slack"
  filename         = data.archive_file.notifier.output_path
  source_code_hash = data.archive_file.notifier.output_base64sha256
  role             = aws_iam_role.notifier.arn
  handler          = "notifier.lambda_handler"
  runtime          = "python3.12"
  timeout          = 90
  memory_size      = 128

  environment {
    variables = {
      ALERT_TOPIC_ARN     = var.alert_topic_arn == null ? "" : var.alert_topic_arn
      ALERT_FORMAT        = var.alert_format
      SLACK_WEBHOOK_PARAM = var.slack_webhook_ssm_parameter == null ? "" : var.slack_webhook_ssm_parameter
      QUIET_MINUTES       = tostring(var.quiet_minutes)
      SLOW_SECONDS        = tostring(var.slow_seconds)
      SLOW_LOG_GROUP      = aws_cloudwatch_log_group.slow.name
      FPM_LOG_GROUP       = aws_cloudwatch_log_group.error.name
      BAN_LOG_GROUP       = var.ship_fail2ban ? aws_cloudwatch_log_group.fail2ban[0].name : ""
      BAN_WINDOW_SECONDS  = tostring(var.ban_window_seconds)
      DIGEST_HOURS        = tostring(var.digest_hours)
      SERVER_NAMES        = jsonencode({ for s in local.servers : s.instance_id => s.name })
    }
  }

  depends_on = [aws_iam_role_policy.notifier, aws_cloudwatch_log_group.notifier]
  tags       = var.tags
}

resource "aws_lambda_permission" "logs" {
  for_each = {
    slow  = aws_cloudwatch_log_group.slow.arn
    error = aws_cloudwatch_log_group.error.arn
  }

  statement_id  = "AllowLogs-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notifier.function_name
  principal     = "logs.${data.aws_region.current.name}.amazonaws.com"
  source_arn    = "${each.value}:*"
}

resource "aws_cloudwatch_log_subscription_filter" "slow" {
  name            = "${var.name_prefix}-slow-to-slack"
  log_group_name  = aws_cloudwatch_log_group.slow.name
  filter_pattern  = "\"script_filename\""
  destination_arn = aws_lambda_function.notifier.arn
  depends_on      = [aws_lambda_permission.logs]
}

resource "aws_cloudwatch_log_subscription_filter" "pool_full" {
  name            = "${var.name_prefix}-pool-full-to-slack"
  log_group_name  = aws_cloudwatch_log_group.error.name
  filter_pattern  = "\"reached pm.max_children\""
  destination_arn = aws_lambda_function.notifier.arn
  depends_on      = [aws_lambda_permission.logs]
}

# --- daily Logs Insights digest ---------------------------------------------
resource "aws_cloudwatch_event_rule" "digest" {
  count = var.digest_schedule == null ? 0 : 1

  name                = "${var.name_prefix}-digest"
  description         = "Logs Insights digest of PHP-FPM slow requests"
  schedule_expression = var.digest_schedule
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "digest" {
  count = var.digest_schedule == null ? 0 : 1

  rule  = aws_cloudwatch_event_rule.digest[0].name
  arn   = aws_lambda_function.notifier.arn
  input = jsonencode({ mode = "digest", hours = var.digest_hours })
}

resource "aws_lambda_permission" "digest" {
  count = var.digest_schedule == null ? 0 : 1

  statement_id  = "AllowDigestSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notifier.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.digest[0].arn
}
