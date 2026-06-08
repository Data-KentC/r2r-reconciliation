# R2R Intercompany Reconciliation Engine
### Automated APAC Intercompany Reconciliation | Python · GitHub Actions · Supabase · Gemini AI

---

## What This Builds

Month-end intercompany reconciliation across 5 APAC entities is entirely manual at most mid-market companies — compressed into the last 2-3 days of the close window, done in Excel, discovered too late to fix before consolidation.

This system replaces that entire process with an automated engine that:

- **Runs itself** every Monday and Thursday at 08:00 SGT via GitHub Actions — no laptop required
- **Ingests** NetSuite GL exports automatically from a shared Gmail inbox
- **Matches** intercompany transactions across all 5 entities using a 5-tier logic hierarchy
- **Alerts** both counterparty entity accountants the moment a mismatch is detected
- **Escalates** automatically through a formal governance chain if nobody responds
- **Produces** an audit-ready Excel report and a live Google Sheets dashboard
- **Costs** $0/month to run

---

## The Problem It Solves

```
BEFORE (manual)                    AFTER (this system)
────────────────────────────────────────────────────────────────
Export CSV from NetSuite manually  NetSuite emails CSV automatically
Cross-reference 5 entities         Engine matches all entities
in Excel by hand                   in under 60 seconds
Find breaks on day 28              Find breaks from day 1
2 hours to fix them                2-3 weeks to fix them
Email counterparties manually      System emails both sides
No audit trail                     Immutable PostgreSQL audit ledger
CFO asks you for status            CFO checks live Google Sheet
Close takes 3 days                 Close is a confirmation not
                                   a discovery exercise
```

---

## Architecture

```
NetSuite GL Saved Search
  → Scheduled email (Mon/Thu 8am SGT)
    → Shared Gmail inbox
      → GitHub Actions (triggered automatically)
        → Pandera schema validation
          → Ingestor (clean, classify, normalise)
            → Matching Engine (5 tiers)
              ├── Tier 1: tranid exact join (AICJEs)
              ├── Tier 2: SHA-256 hash match
              ├── Tier 3: FX tolerance (≤$100 AND ≤1%)
              ├── Tier 4: Subset sum (1-to-many lines)
              └── Tier 5: LLM suggestion (orphans only)
                → Supabase PostgreSQL (Singapore region)
                  ├── Matched pairs → Tab 1
                  ├── Exceptions → Tab 2 + alerts sent
                  ├── JE Drafts → Tab 3
                  └── Run log → immutable audit trail
                    → Excel report → Google Drive
                    → Live dashboard → Google Sheets
                    → Exception alerts → Entity accountants
                    → Escalation chain → Controllers → CFO
```

---

## Entities Covered

| Entity | Currency | Region |
|--------|----------|--------|
| ANTH-SG | SGD | Singapore |
| ANTH-AU | AUD | Australia |
| ANTH-IN | INR | India |
| ANTH-JP | JPY | Japan |
| ANTH-HK | HKD | Hong Kong |

Designed to scale to 30+ entities with zero code changes — add new entities to `config.yaml` only.

---

## Matching Logic — 5 Tiers

| Tier | Method | Handles |
|------|--------|---------|
| 1 | `tranid` exact join | Advanced IC Journal Entries — shared document number |
| 2 | SHA-256 composite hash | Manual JEs — period + entities + currency + rounded amount |
| 3 | FX tolerance match | Variance ≤$100 USD AND ≤1% — FX rounding noise |
| 4 | Subset sum aggregation | 1-to-many — one consolidated line vs multiple itemised |
| 5 | LLM cascade (AI) | Orphan counterparty suggestion — account description only |

Expected match rate: **85-92% straight-through processing** with no human involvement.

---

## Exception Taxonomy

Every unmatched transaction is classified into one of:

| Type | Meaning |
|------|---------|
| `ORPHAN` | No counterpart found in any entity |
| `TIMING_GAP` | Counterpart found in ±1 period |
| `AMOUNT_MISMATCH` | Match found, amounts differ beyond tolerance |
| `ACCOUNT_MISMATCH` | Match found, different GL accounts used |
| `CURRENCY_MISMATCH` | Entities used different transaction currencies |
| `CSEG_INVALID` | IC tag present but unrecognised entity |
| `PARTIAL_REVERSAL` | Reversal leaves non-zero net amount |
| `OUT_OF_SCOPE` | Entity outside APAC 5 |

---

## Output — Excel Report (4 Tabs)

| Tab | Audience | Contents |
|-----|----------|----------|
| **Tab 0: Executive Summary** | CFO | Run metadata, MD5 hash, entity pair match rates, STP%, exception counts |
| **Tab 1: Matched Pairs** | Auditor | All confirmed matches, confidence score, SOX boolean columns, variance decomposition |
| **Tab 2: Exceptions** | Controller | Classified exceptions, priority, aging, LLM suggestions, escalation status |
| **Tab 3: JE Drafts** | Controller | Pre-written correcting entries, approval checkbox, posted_to_netsuite flag |

---

## Governance and SOX Compliance

- **Immutable audit ledger** — PostgreSQL `SECURITY DEFINER` trigger. Append-only. Cannot be modified even by admin.
- **Period locking** — PostgreSQL trigger rejects any modification to a signed-off period at database kernel level.
- **Confidence scoring** — Every match has a 0-100 integer score with SOX boolean decomposition auditors can verify.
- **Escalation chain** — Formal Delegation of Authority matrix. 2 working days → entity accountant → 2 days → controller → 2 days → Head of Accounting.
- **Config governance** — `CODEOWNERS` enforces Pull Request approval for any change to `config.yaml`.
- **GitHub Actions logs** — 90-day immutable retention. Archived to Google Drive for 7-year SOX requirement.
- **GPG-signed commits** — Config verification requires cryptographic proof of human review.

---

## LLM Cascade (All Free)

```
Primary:    Groq — Llama 3.3 70B        (30 RPM)
Secondary:  DeepSeek R1                  (free tier)
Tertiary:   Gemini 2.0 Flash             (15 RPM)
Fallback:   Qwen 2.5 72B via OpenRouter  (60 RPM)
```

**Privacy design:** Only sanitised account description text is sent to any LLM. Zero amounts, zero entity names, zero PII. Pydantic `Literal` whitelist enforces that the LLM can only suggest entities from the APAC 5 — hallucinated entities are caught and routed to `UNCLASSIFIED_ESCROW` before touching the database.

---

## Tech Stack

| Component | Tool | Cost |
|-----------|------|------|
| Orchestration | GitHub Actions | Free |
| Database | Supabase PostgreSQL (Singapore) | Free |
| Schema validation | Pandera | Free |
| Fuzzy matching | rapidfuzz | Free |
| LLM assistance | Groq + DeepSeek + Gemini + Qwen | Free |
| Excel output | openpyxl | Free |
| Live dashboard | Google Sheets API | Free |
| File storage | Google Drive API | Free |
| Email ingestion | Gmail API (3LO OAuth) | Free |
| Notifications | Gmail API + Telegram Bot | Free |
| Auth | GCP Workload Identity Federation | Free |
| **Total** | | **$0/month** |

---

## File Structure

```
r2r-reconciliation/
│
├── .github/
│   ├── workflows/
│   │   ├── recon_pipeline.yml      # Main scheduled pipeline
│   │   ├── period_lock.yml         # Period sign-off workflow
│   │   └── log_archive.yml         # Monthly SOX log archival
│   └── CODEOWNERS                  # Enforces PR approval for config changes
│
├── src/
│   ├── config.py                   # Loads config.yaml + validates
│   ├── ingestion/
│   │   ├── gmail_watcher.py        # Downloads CSV from Gmail
│   │   ├── ingestor.py             # Parse, validate, normalise
│   │   ├── validator.py            # Pandera schema guardrails
│   │   └── classifier.py          # Derives local_entity, CSEG fallback
│   ├── matching/
│   │   ├── engine.py               # Orchestrates Tier 1-5
│   │   ├── keygen.py               # tranid + SHA-256 key generation
│   │   ├── exact.py                # Tier 1-2
│   │   ├── tolerance.py            # Tier 3
│   │   ├── subset_sum.py           # Tier 4
│   │   └── llm_matcher.py          # Tier 5 — LLM cascade
│   ├── providers/
│   │   └── llm_provider.py         # Swap file — change LLM here only
│   ├── persistence/
│   │   ├── database.py             # SQLAlchemy engine + Supabase connection
│   │   ├── models.py               # ORM models = database schema
│   │   └── repository.py           # All DB reads/writes
│   ├── notification/
│   │   └── notifier.py             # Alerts + escalation + Telegram fallback
│   └── reporting/
│       ├── reporter.py             # Orchestrates Excel build
│       └── excel_builder.py        # openpyxl tab construction
│
├── jobs/
│   ├── run_pipeline.py             # Entry point: full reconciliation run
│   ├── run_report.py               # Entry point: generate Excel on demand
│   └── preflight_check.py          # Pre-demo health check script
│
├── tests/
│   ├── synthetic/
│   │   └── generate_synthetic.py   # Generates 13 edge-case test scenarios
│   └── test_*.py                   # Unit tests per module
│
├── data/
│   └── synthetic/                  # Synthetic test CSV (committed)
│       └── synthetic_gl_jun2026.csv
│
├── config.yaml                     # All parameters — edit here, not in code
├── requirements.txt                # Python dependencies
├── .gitignore                      # Protects real data from GitHub
├── .env.example                    # Template — copy to .env, fill in secrets
└── README.md                       # This file
```

---

## Running the System

**Automatic (production):** Runs every Monday and Thursday at 08:00 SGT via GitHub Actions. No action needed.

**Manual trigger:**
```
GitHub → Actions tab → APAC Intercompany Reconciliation Engine
→ Run workflow → Select entities → Click Run
```

**Generate period report on demand:**
```
GitHub → Actions tab → Generate Period Report
→ Run workflow → Enter period (e.g. 2026-06) → Click Run
→ Excel file appears in Google Drive within 2 minutes
```

---

## Secrets Required (GitHub Secrets)

| Secret Name | What It Is |
|-------------|------------|
| `SUPABASE_POOLER_URL` | Supabase connection string (port 6543) |
| `GEMINI_API_KEY` | Google AI Studio API key |
| `GROQ_API_KEY` | Groq console API key |
| `DEEPSEEK_API_KEY` | DeepSeek platform API key |
| `OPENROUTER_API_KEY` | OpenRouter API key (Qwen fallback) |
| `GMAIL_REFRESH_TOKEN` | Gmail OAuth 3LO refresh token |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (optional) |
| `GH_PAT` | GitHub Personal Access Token (log archival) |

---

## Setup Checklist

Before first real run, complete `setup_checklist.md` (created separately). Key steps:

- [ ] GCP project created, OAuth consent screen set to **Production**
- [ ] GCP Workload Identity Federation configured
- [ ] Google Drive folder created and shared with service account
- [ ] Google Sheet created and shared with service account
- [ ] Supabase project created in `ap-southeast-1` (Singapore)
- [ ] All GitHub Secrets populated
- [ ] `config.yaml` TODO items completed with real values
- [ ] NetSuite Saved Search scheduled to email CSV Mon/Thu 08:00 SGT
- [ ] GPG key configured on GitHub account for signed commits
- [ ] Synthetic data test run completed successfully

---

## Benchmark Against Commercial Tools

| Capability | This Build | BlackLine | Trintech |
|------------|-----------|-----------|---------|
| Automated matching | ✅ 5-tier logic | ✅ | ✅ |
| Real-time detection | ✅ Twice weekly | ✅ Continuous | ✅ Continuous |
| LLM assistance | ✅ 4-provider cascade | ⚠️ Limited | ❌ |
| SOX audit trail | ✅ DB-level trigger | ✅ | ✅ |
| Period locking | ✅ PostgreSQL trigger | ✅ | ✅ |
| Live dashboard | ✅ Google Sheets | ✅ | ✅ |
| NetSuite integration | ✅ CSV + Gmail API | ✅ Native | ✅ Native |
| Escalation chain | ✅ DoA matrix | ✅ | ✅ |
| Annual cost | **$0** | ~$50,000 | ~$30,000 |

---

## About the Builder

Built by a Singapore Chartered Accountant with Big 4 audit and APAC controllership experience. Every design decision in this system reflects real-world accounting knowledge — the matching tiers handle the actual failure modes seen in production IC reconciliation, the exception taxonomy reflects how controllers think about breaks, and the governance design maps directly to SOX ITGC requirements.

This is not a technology project that happens to touch accounting. It is an accounting solution that happens to be built in code.

---

*Built with Python · Runs on GitHub Actions · Zero cost · Owned forever*