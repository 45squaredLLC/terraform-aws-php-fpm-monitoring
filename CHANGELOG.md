# Changelog

All notable changes to this module are documented here. This project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-13

First release.

- CloudWatch agent installed and configured on the listed EC2 instances by an SSM
  association (config in Parameter Store; re-applied on change and on a schedule).
- PHP-FPM slow log and error log shipped to `/<prefix>/slow` and `/<prefix>/error`,
  one stream per instance; slow-log entries kept whole with their stack.
- Notifier Lambda: new site + stack-frame groups from the slow log in real time,
  entries older than `MAX_AGE_MINUTES` (15) skipped so the agent's first read of
  an existing log is not posted as news,
  "pool full" when `pm.max_children` is reached, repeats held back for
  `quiet_minutes`.
- Daily CloudWatch Logs Insights digest of slow requests by server, site and plugin
  (`digest_schedule`, off with `null`).
- Delivery to an SNS topic (`amazon_q` custom-notification JSON or `text`) and/or a
  Slack incoming webhook read from an SSM SecureString.
