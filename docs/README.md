# 📘 Personal Finance Automation — System Overview

**Project Root:** `C:\DATA\personal_finance`  
**Purpose:** End-to-end automated snapshot and audit system for Plaid data ingestion, analysis, and environment management.

---

## 🧭 Architecture Overview

```text
C:\DATA\personal_finance
│
├── src\
│   ├── ingestion\webhooks.py     → Handles Plaid webhook ingestion
│   ├── storage\db.py             → SQLite schema and persistence logic
│   ├── analysis\analysis.py      → Data exploration and analysis utilities
│   └── requirements.txt          → Locked dependencies (auto-frozen)
│
├── data\
│   └── plaid.db                  → Primary SQLite data store
│
├── scripts\
│   ├── make_session_snapshot.py      → Captures full environment + docs summary
│   ├── inspect_snapshot_db_state.py  → Logs DB + Git tag summary
│   ├── update_requirements.ps1       → Orchestrates environment + Git tagging
│   ├── seed_transactions.py          → Seeds fake transactions (sandbox)
│   ├── trigger_webhook.py            → Simulates Plaid webhook callbacks
│   └── explore_transactions.py       → Quick Pandas exploration of transactions
│
├── logs\
│   ├── maintenance.log               → Centralized docs + DB + Git tag history
│   ├── app.log, webhook.log          → Flask + webhook runtime logs
│   └── session_snapshot_*.yaml       → Environment snapshots
│
├── backups\                          → Auto-rotating DB and schema backups
└── runs\sandbox\run.ps1              → One-click sandbox startup script


⚙️ Snapshot Lifecycle

Every environment update runs through a single orchestrator:

.\scripts\update_requirements.ps1

🔄 Process Flow
Step	Action	Responsible Script	Output
1️⃣	Freeze dependencies	update_requirements.ps1	src/requirements.txt
2️⃣	Capture environment + docs	make_session_snapshot.py	logs/session_snapshot_YYYYMMDD_HHMM.yaml
3️⃣	Commit and tag repo	PowerShell (Git)	snapshot-YYYYMMDD_HHMM
4️⃣	Inspect DB + log results	inspect_snapshot_db_state.py	Console + maintenance.log
5️⃣	Confirm completion	PowerShell	Printed summary


       ┌───────────────────────────────┐
       │ Plaid Sandbox Webhooks        │
       │ (simulated via trigger_webhook.py) │
       └──────────────┬────────────────┘
                      │
                      ▼
┌────────────────────────────────────────┐
│ src/ingestion/webhooks.py              │
│ Writes webhook + transaction data → DB │
└────────────────────────────────────────┘
                      │
                      ▼
┌────────────────────────────────────────┐
│ data/plaid.db (SQLite)                 │
│ Stores items, accounts, transactions,  │
│ webhook_events, and cursors.           │
└────────────────────────────────────────┘
                      │
                      ▼
┌────────────────────────────────────────┐
│ scripts/make_session_snapshot.py       │
│ Logs docs + environment state          │
│ Writes YAML snapshot + logs entry      │
└────────────────────────────────────────┘
                      │
                      ▼
┌────────────────────────────────────────┐
│ scripts/inspect_snapshot_db_state.py   │
│ Logs DB + Git tag summary              │
│ Appends to maintenance.log             │
└────────────────────────────────────────┘
                      │
                      ▼
┌────────────────────────────────────────┐
│ logs/maintenance.log                   │
│ Central audit trail:                   │
│   • DOCS SUMMARY                       │
│   • DB SUMMARY                         │
│   • Git tag linkage                    │
└────────────────────────────────────────┘


🧾 Example Maintenance Log Entries
[2025-10-09 20:34:55] DOCS SUMMARY — exists: True, files: 2, existing_sections: [automation], missing_sections: [ingestion, analysis, processing, storage], latest_tag: snapshot-20251009_2034
[2025-10-09 20:34:55] DB SUMMARY — snapshot_date: 2025-10-09, webhook_events: 22, tx_before: 234, tx_af

