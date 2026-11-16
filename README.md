# terraform-aws-php-fpm-monitoring

Know which site is taking your PHP server down before the uptime check does.

On a shared WordPress server, every site draws from one PHP-FPM worker pool. When a
few slow requests (a plugin calling an external API, a WordPress cron job, a backup)
hold all the workers, **every** site on the box stops answering: the uptime
alarm fires, but it can't tell you why. PHP-FPM already knows: its slow log records
the script and the stack each slow request was stuck in, and its error log says when
the pool hit `pm.max_children`. This module gets both into Slack.

## What you get

- **Slow request, in real time.** Each new site + stack frame that held a worker past
  `request_slowlog_timeout`, grouped and counted, with the plugin it was stuck in and
  a link to the entry in CloudWatch.
- **Pool full.** "PHP pool full on ClientServer" the moment every worker is busy,
  usually minutes before the sites time out.
- **Daily digest.** A CloudWatch Logs Insights query over the last 24 h: slow
  requests by server, site and plugin. Nothing is posted on a clean day.
- **Agent setup.** An SSM association installs the CloudWatch agent and loads the
  config. No SSH, no hand-edited JSON on the box.

Repeats of the same site + frame (or a full pool) are held back for `quiet_minutes`.

With `ship_fail2ban = true`, each alert also says which IPs fail2ban banned on that
server in the last few minutes, e.g. *PHP pool full on ClientServer (pool borchers,
6 workers): 1 IP auto-banned*, so a bot burst reads as handled rather than as an outage.

```
PHP-FPM slow log ─┐  CloudWatch agent       ┌▶ subscription "script_filename" ─┐
PHP-FPM error log ┴─(SSM-installed)──▶ logs ┴▶ subscription "reached pm.max_children" ┴▶ Lambda ─▶ SNS (Amazon Q) and/or Slack webhook
EventBridge (daily) ───────────────────────────────────────────▶ Logs Insights query ─┘
```

## Server prerequisite

The module ships the logs; it doesn't edit PHP-FPM. In each pool (e.g.
`/etc/php/8.4/fpm/pool.d/www.conf`):

```ini
request_slowlog_timeout = 10s
slowlog = /var/log/php8.4-fpm.slow.log
; recommended: free a worker from a request that never ends
request_terminate_timeout = 60s
```

Then `php-fpm8.4 -t && systemctl reload php8.4-fpm`. The instances need the SSM agent
and a profile whose role you pass in `instance_role_names`.

## Usage

```hcl
module "php_fpm_monitoring" {
  source = "github.com/45squaredLLC/terraform-aws-php-fpm-monitoring?ref=v1.0.0"

  servers = {
    web1 = { instance_id = "i-0123456789abcdef0", name = "web1", php_version = "8.4" }
  }
  instance_role_names = ["web-servers-ssm-role"]

  # Amazon Q Developer in Slack (custom notifications)...
  alert_topic_arn = aws_sns_topic.alerts.arn
  # ...and/or a Slack incoming webhook kept in Parameter Store (never in state)
  # slack_webhook_ssm_parameter = "/slack/php-alerts-webhook"
}
```

## Inputs

| Name | Default | |
|---|---|---|
| `servers` | (required) | `{ key = { instance_id, name, php_version = "8.4", slowlog_path?, error_log_path? } }` |
| `instance_role_names` | (required) | Roles that get write access to the two log groups only |
| `name_prefix` | `php-fpm` | Log groups `/<prefix>/slow`, `/<prefix>/error`; Lambda `<prefix>-notifier` |
| `alert_topic_arn` | `null` | SNS topic to post to |
| `alert_format` | `amazon_q` | `amazon_q` (custom-notification JSON) or `text` |
| `slack_webhook_ssm_parameter` | `null` | SecureString name holding a Slack webhook URL |
| `install_agent` | `true` | Install + configure the CloudWatch agent via SSM |
| `agent_schedule` | `rate(7 days)` | Re-apply cadence (it also runs on create and on config change) |
| `slow_seconds` | `10` | Your `request_slowlog_timeout`, for message text |
| `quiet_minutes` | `30` | Hold back repeats this long |
| `digest_schedule` | `cron(0 13 * * ? *)` | `null` turns the digest off |
| `digest_hours` | `24` | Window the digest covers |
| `ship_fail2ban` | `false` | Also ship fail2ban's log to `/<prefix>/fail2ban`; alerts then name the IPs fail2ban banned on that server |
| `fail2ban_log_path` | `/var/log/fail2ban.log` | fail2ban's log on the servers |
| `ban_window_seconds` | `300` | How far back an alert looks for bans |
| `log_retention_days` | `30` | All log groups |
| `tags` | `{}` | |

Outputs: `slow_log_group_name`, `error_log_group_name`, `notifier_function_arn`,
`agent_config` (merge it into your own when `install_agent = false`), `ssm_association_id`.

## Notes

- If you apply from a saved plan in CI, carry `<module>/.build/notifier.zip` from the
  plan job to the apply job: `archive_file` doesn't rebuild during a saved-plan apply.
- The quiet window lives in the warm Lambda container, so a cold start can repeat a
  message once. That's deliberate: no table to manage.
- On-demand digest: invoke the notifier with `{"mode": "digest", "hours": 6}`.
- Tests: `python3 -m pytest lambda` (no AWS calls). See CONTRIBUTING.md.


