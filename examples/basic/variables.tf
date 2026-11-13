variable "region" {
  description = "Region of the instance and its log groups."
  type        = string
  default     = "us-east-1"
}

variable "instance_id" {
  description = "EC2 instance running PHP-FPM with the SSM agent."
  type        = string
  default     = "i-0123456789abcdef0"
}

variable "instance_role_name" {
  description = "IAM role in the instance profile."
  type        = string
  default     = "web-servers-ssm-role"
}
