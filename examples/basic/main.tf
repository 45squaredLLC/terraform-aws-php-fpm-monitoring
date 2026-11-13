# One shared WordPress server: PHP-FPM slow requests and full pools to Slack
# through Amazon Q Developer, plus the daily digest.

terraform {
  required_version = ">= 1.3.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0.0, < 7.0.0"
    }
  }
}

provider "aws" {
  region = var.region
}

# The topic your Amazon Q Developer Slack channel subscribes to.
resource "aws_sns_topic" "alerts" {
  name = "php-alerts"
}

module "php_fpm_monitoring" {
  source = "../../"

  servers = {
    web1 = {
      instance_id = var.instance_id
      name        = "web1"
      php_version = "8.4"
    }
  }

  instance_role_names = [var.instance_role_name]

  alert_topic_arn = aws_sns_topic.alerts.arn

  tags = {
    Project = "php-fpm-monitoring"
  }
}

output "slow_log_group_name" {
  value = module.php_fpm_monitoring.slow_log_group_name
}
