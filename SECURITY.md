# Security

Report a vulnerability privately through GitHub's "Report a vulnerability"
(Security tab) rather than a public issue. We aim to reply within five business days.

## What this module does with access

- **Instance roles** you list in `instance_role_names` get one inline policy:
  `logs:CreateLogStream`, `logs:PutLogEvents` and `logs:DescribeLogStreams` on this
  module's two log groups only.
- **The SSM association** runs AWS's own `AWS-ConfigureAWSPackage` and
  `AmazonCloudWatch-ManageAgent` documents on the instances you list, as root (the
  CloudWatch agent's normal mode). The agent config is a plain `String` parameter.
- **The notifier Lambda** can write its own logs, run Logs Insights queries on the
  slow-log group, publish to the one SNS topic you pass, and read the one SSM
  parameter holding your Slack webhook. `logs:GetQueryResults` has no
  resource-level permission in AWS, so it is granted on `*`.
- **Log content.** PHP-FPM slow logs contain script paths and function names,
  not request bodies or query strings. They go to your log groups and to Slack.
  Set `log_retention_days` to your policy.
- **The Slack webhook URL** is read at runtime from a SecureString and never
  enters Terraform state.
