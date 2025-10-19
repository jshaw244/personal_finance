$path = "C:\DATA\personal_finance\docs\maintenance\sandbox_baseline_20251018.pdf"
$md = @"
# Personal Finance Sandbox Baseline Snapshot
Version 2025-10-18

## Environment Configuration
.env.sandbox:
ENV_TARGET=sandbox
PLAID_ENV=sandbox
FLASK_SECRET_KEY=dev_secret_key
REPORTS_USER=finance_admin
REPORTS_PASS_HASH=$2b$12$<your_bcrypt_hash_here>

## Key app.py Sections
Flask imports reports_bp, login_manager, ensure_summary_views_and_tables.
Uses app.app_context() for summary table creation.
No auto-shutdown or cleanup jobs.

## run.ps1 Header
Write-Host "Ensuring previous sandbox environment is stopped..."
Write-Host "No stop script configured (StopScript variable removed). Skipping cleanup."

## Expected Startup Output
Project Root: C:\DATA\personal_finance
ngrok public URL: https://<random>.ngrok-free.app
Sandbox environment startup complete.

## Login / Healthcheck
Login:       http://127.0.0.1:5002/reports/login
Healthcheck: http://127.0.0.1:5002/reports/api/healthcheck
Username:    finance_admin

## Stability Notes
âœ“ Flask imports cleanly
âœ“ bcrypt auth verified
âœ“ No PowerShell or Using-variable errors
âœ“ Startup + shutdown stable
"@

$md | Out-File "C:\DATA\personal_finance\docs\maintenance\sandbox_baseline_20251018.md" -Encoding UTF8

