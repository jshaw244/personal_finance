# Personal Finance Dashboard

A from-scratch personal finance dashboard with a **fully local AI assistant** — Plaid handles the bank sync, but your financial data never leaves your computer.

It links your banks, cards, and brokerage through [Plaid](https://plaid.com), stores everything locally in SQLite, and turns it into analytics: cash-flow tracking, budgeting, savings goals, and an investment-holdings breakdown. Its defining feature is a built-in AI (via local [Ollama](https://ollama.com)) that answers questions about your finances entirely on-device — the only outbound connection is Plaid, used solely to sync with your institutions.

> Originally coded from scratch, then substantially expanded with Claude Code.

## Features

- **Plaid integration** — link accounts, incremental transaction sync, balances, credit-card liabilities (statement balance / due date), and investment holdings.
- **Transaction classification** — a priority-ordered rule engine plus Plaid's personal-finance categories, with per-transaction exclude flags.
- **Flow-type model** — every transaction is typed as expense / savings / investment / transfer so internal transfers are netted out and not double-counted; reports net contributions vs. withdrawals.
- **Reports dashboard** — monthly cash flow, a pay-period calendar, spending by category and merchant, and **per-account drill-down** across the board.
- **Budgeting** — tiered Need / Want / Savings targets with budget-vs-actual.
- **Savings goals** — set a target and deadline; progress is tracked from real account-balance growth with on-track projections.
- **Investment holdings** — positions per account, allocation by asset type, cash vs. invested, top concentrations, and a value-over-time trend.
- **Local AI assistant** — chat about your money, powered by a local Ollama model with **function-calling tools** that pull specific months, accounts, categories, budgets, goals, and holdings on demand. No data leaves the machine.

## Tech stack

Python · Flask · Flask-Login + bcrypt · SQLite (WAL) · pandas · the Plaid API · Ollama (local LLM) · PowerShell launcher · ngrok (for Plaid webhooks).

## Architecture

A five-layer pipeline:

1. **Connection** — link accounts via Plaid Link.
2. **Collection** — fetch transactions/accounts/liabilities/holdings from institutions.
3. **Retention** — store in SQLite (one DB per environment).
4. **Presentation** — analyze + serve the Flask dashboard.
5. **Transmission** — share/export reports.

Three isolated environments — `sandbox` (port 5002), `development` (5001), and `production` (5000) — each with its own `.env` file and database.

## Quick start

**Prerequisites:** Python 3.11+, a free [Plaid](https://dashboard.plaid.com/signup) account, and (optional) [Ollama](https://ollama.com) for the local AI.

```bash
git clone https://github.com/jshaw244/personal_finance.git
cd personal_finance

# 1. Virtual environment + dependencies
python -m venv .venv
. .venv/Scripts/activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r src/requirements.txt

# 2. Environment config (start with sandbox)
cp config/env/.env.example config/env/.env.sandbox
#   then edit .env.sandbox: add your Plaid sandbox keys, a FLASK_SECRET_KEY,
#   REPORTS_USER, and a REPORTS_PASS_HASH:
python scripts/generate_password_hash.py     # prints a bcrypt hash to paste in

# 3. (Optional) personal schedule config for the pay-period calendar / bills
cp config/schedules.example.json config/schedules.json

# 4. Run (sandbox on port 5002)
ENV_TARGET=sandbox python -m flask --app src.ingestion.app run --host 127.0.0.1 --port 5002
```

On Windows, `runs/run.ps1 -Target sandbox` automates the venv, env vars, ngrok, and browser launch.

Open <http://127.0.0.1:5002>, log in with your `REPORTS_USER` / password, then link a bank through Plaid Link (in sandbox, use Plaid's test credentials, e.g. `user_good` / `pass_good`).

**Local AI (optional):** install Ollama, `ollama pull llama3.1:8b`, and set `OLLAMA_URL` / `OLLAMA_MODEL` in your env file. The assistant lives at `/reports/ai`.

## Configuration notes

- Real `config/env/.env.*` files and `config/schedules.json` are **gitignored** — they hold personal data. Commit nothing but the `*.example` templates.
- Databases (`data/*.db`) and uploaded statements (`statements/`) are gitignored and never leave your machine.

## Disclaimer

A personal project for managing my own finances — provided as-is, not financial advice, and not affiliated with Plaid or any institution. The bundled local AI is intentionally narrower than commercial assistants; running fully on-device is the tradeoff that keeps your data private.
