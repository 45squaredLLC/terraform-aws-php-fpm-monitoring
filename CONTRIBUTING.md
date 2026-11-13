# Contributing

Issues and pull requests are welcome.

## Before you open a PR

```bash
terraform fmt -recursive -check
terraform init -backend=false && terraform validate
terraform -chdir=examples/basic init -backend=false && terraform -chdir=examples/basic validate
python -m pytest lambda
ruff check lambda && ruff format --check lambda
```

CI runs the same checks on Terraform 1.9.8 and the latest release.

- Keep the Lambda dependency-free (standard library + boto3 from the runtime).
- Don't commit real instance IDs, account IDs, webhook URLs or `.tfvars` files.
  Examples use placeholders.
- Note user-facing changes in `CHANGELOG.md` under an Unreleased heading.

## Testing against real servers

Apply `examples/basic` in a sandbox account against an instance with PHP-FPM, set
`request_slowlog_timeout = 1s` in its pool, and load a page that sleeps
(`<?php sleep(3);`). The entry should reach your topic or webhook within a minute.
Invoke the notifier with `{"mode": "digest", "hours": 1}` for the digest.
