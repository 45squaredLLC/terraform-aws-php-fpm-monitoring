output "slow_log_group_name" {
  description = "Log group holding every server's PHP-FPM slow log (one stream per instance)."
  value       = aws_cloudwatch_log_group.slow.name
}

output "error_log_group_name" {
  description = "Log group holding every server's PHP-FPM error log (one stream per instance)."
  value       = aws_cloudwatch_log_group.error.name
}

output "notifier_function_arn" {
  description = "The notifier Lambda. Invoke with {\"mode\": \"digest\", \"hours\": 24} for an on-demand digest."
  value       = aws_lambda_function.notifier.arn
}

output "agent_config" {
  description = "The CloudWatch agent config this module loads. Merge it into your own if install_agent = false."
  value       = local.agent_config
}

output "ssm_association_id" {
  description = "Association that installs and configures the agent (null when install_agent = false)."
  value       = try(aws_ssm_association.agent[0].association_id, null)
}
