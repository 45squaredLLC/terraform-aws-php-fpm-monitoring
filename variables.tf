variable "name_prefix" {
  description = "Prefix for every resource name (log groups, Lambda, SSM document)."
  type        = string
  default     = "php-fpm"
}

variable "servers" {
  description = <<-EOT
    EC2 instances to monitor, keyed by a short name. `name` is what Slack shows.
    `slowlog_path` and `error_log_path` default to Debian/Ubuntu's PHP-FPM paths for
    `php_version`. The pool itself must have `slowlog` and `request_slowlog_timeout`
    set (see the README); this module ships the files, it doesn't edit php-fpm.
  EOT
  type = map(object({
    instance_id    = string
    name           = string
    php_version    = optional(string, "8.4")
    slowlog_path   = optional(string)
    error_log_path = optional(string)
  }))
}

variable "instance_role_names" {
  description = "IAM roles of the instances' profiles. Each gets an inline policy to write to this module's two log groups and nothing else."
  type        = list(string)
}

variable "install_agent" {
  description = "Install and configure the CloudWatch agent on the servers through an SSM association. Set false if you manage the agent yourself (then merge `agent_config` into your own config)."
  type        = bool
  default     = true
}

variable "agent_schedule" {
  description = "How often the SSM association re-applies the agent install and config. It also runs once on create and whenever the config changes."
  type        = string
  default     = "rate(7 days)"
}

variable "slow_seconds" {
  description = "The pool's request_slowlog_timeout, in seconds. Used in message text only."
  type        = number
  default     = 10
}

variable "alert_topic_arn" {
  description = "SNS topic to post to. Null to skip SNS."
  type        = string
  default     = null
}

variable "alert_format" {
  description = "\"amazon_q\": Amazon Q Developer custom-notification JSON (for a topic Amazon Q posts to Slack/Teams). \"text\": subject + plain body (email etc.)."
  type        = string
  default     = "amazon_q"

  validation {
    condition     = contains(["amazon_q", "text"], var.alert_format)
    error_message = "alert_format must be \"amazon_q\" or \"text\"."
  }
}

variable "slack_webhook_ssm_parameter" {
  description = "Name of an SSM SecureString holding a Slack incoming-webhook URL. Null to skip. The value is read at runtime and never enters state."
  type        = string
  default     = null
}

variable "quiet_minutes" {
  description = "After posting about a site + stack frame (or a full pool) on a server, stay quiet about the same thing for this long."
  type        = number
  default     = 30
}

variable "digest_schedule" {
  description = "EventBridge schedule for the Logs Insights digest of slow requests. Null to turn the digest off."
  type        = string
  default     = "cron(0 13 * * ? *)"
}

variable "digest_hours" {
  description = "Hours of slow log the digest covers."
  type        = number
  default     = 24
}

variable "log_retention_days" {
  description = "Retention for the slow-log, error-log and Lambda log groups."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags for every resource."
  type        = map(string)
  default     = {}
}

variable "ship_fail2ban" {
  description = "Also ship fail2ban's log to /<name_prefix>/fail2ban and name the IPs fail2ban banned in the last few minutes in every alert."
  type        = bool
  default     = false
}

variable "fail2ban_log_path" {
  description = "fail2ban's log on the servers (used when ship_fail2ban is true)."
  type        = string
  default     = "/var/log/fail2ban.log"
}

variable "ban_window_seconds" {
  description = "How far back an alert looks for fail2ban bans on the same server."
  type        = number
  default     = 300
}
