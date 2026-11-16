# Changelog

All notable changes to this module are documented here. This project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-11-16

- `ship_fail2ban` (default `false`): the agent also ships fail2ban's log
  (`fail2ban_log_path`) to `/<prefix>/fail2ban`, one stream per instance, and every
  slow-request and pool-full alert names the IPs fail2ban banned on that server in
  the last `ban_window_seconds` (300). A pool-full title gains ": N IPs auto-banned".
  The lookup is best-effort: if it fails, the alert still goes out unchanged.
- Off by default, so existing callers see no change.

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
