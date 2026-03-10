# Gmail Unread Cleanup

Bulk cleanup of unread Gmail. Fetch → Classify (Claude AI) → Review → Trash/Archive. Plus automatic Gmail filter creation from read/unread patterns.

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Google Cloud credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project (or use an existing one)
3. Enable **Gmail API**: APIs & Services → Enable APIs → search "Gmail API" → Enable
4. Create credentials: APIs & Services → Credentials → **+ Create Credentials** → OAuth 2.0 Client ID
   - Application type: **Desktop app**
   - Name: anything (e.g. "gmail-cleanup")
5. Download the JSON → save as **`credentials.json`** in this folder
6. OAuth consent screen → add your Gmail address as a test user

### 3. Anthropic API key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

---

## Usage

### Core workflow (run in order)

```bash
# 1. Fetch all unread metadata → unread.json
python cleanup.py fetch

# 2. Classify with Claude → classified.json
python cleanup.py classify

# 3. Interactive report — review, delete, archive
python cleanup.py report

# 4. (Optional) Export to CSV for spreadsheet review
python cleanup.py export
```

### Gmail filter creation

```bash
# Analyze 3 months of read/unread patterns → propose and create Gmail filters
python cleanup.py filters
```

Each stage saves its output as a JSON file so you can resume or re-run any step independently.

---

## Commands

| Command | What it does |
|---------|-------------|
| `fetch` | Lists all unread message IDs via Gmail API, then batch-fetches subject/from/date/labels → `unread.json` |
| `classify` | Gmail labels pre-classify Promotions/Social/Updates; Claude (Sonnet 4) classifies the rest into 7 categories → `classified.json` |
| `report` | Interactive review: quick-approve defaults, per-category review, or per-sender selection. Executes trash/archive via `batchModify` |
| `export` | Flat CSV export of classified emails → `classified.csv` |
| `filters` | Analyzes 3 months of inbox read/unread patterns, uses Claude for subject analysis, proposes Gmail filters, optionally auto-creates them |

---

## Classification categories

| Category | Default action | Description |
|----------|---------------|-------------|
| `SAFE_DELETE` | delete | Expired promos, old OTPs, delivered shipping notifications, old receipts |
| `NOISE` | delete | Unsolicited marketing, cold outreach, spam-like ads |
| `SOCIAL_NOISE` | delete | LinkedIn endorsements, "X liked your post", forum digests |
| `SAFE_ARCHIVE` | archive | Subscribed newsletters, order confirmations, past travel bookings, statements |
| `SOCIAL_REAL` | ignore | Direct messages, @mentions, personal LinkedIn messages |
| `TRANSACTIONAL` | ignore | Active orders, upcoming travel, pending payments, recent OTPs |
| `REVIEW` | ignore | Personal messages, work email, calendar invites — **never auto-deleted** |

Classification considers email age, sender, subject, and Gmail category labels. When in doubt, emails default to `REVIEW`.

---

## Report interactive modes

The `report` command offers three levels of control:

- **Quick-approve** — apply all default actions (delete/archive/ignore) immediately
- **Per-category review** — approve, change action, or drill into per-sender selection for each category
- **Per-sender selection** — review senders grouped by domain, choose delete/archive/ignore for each group

---

## Filters workflow

The `filters` command creates Gmail filters to prevent future inbox clutter:

1. **Fetch** — retrieves 3 months of inbox emails (cached for 24 hours in `filter_analysis.json`)
2. **Analyze** — groups by sender/domain, computes read ratios (never / rarely / mixed / regular)
3. **Subject analysis** — Claude analyzes "mixed" tier sources to find filterable subject patterns
4. **Propose** — generates filter proposals for rarely-read and pattern-matched sources
5. **Approve** — quick-approve high-confidence filters, review each individually, or export only
6. **Create** — approved filters are created via Gmail API

---

## Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude classification | (required) |
| `CUTOFF_DATE` | Only process emails older than this date (`YYYY-MM-DD`) | 1 year ago |

---

## File outputs

| File | Created by | Contents |
|------|-----------|----------|
| `token.json` | `fetch` | OAuth token (auto-created on first run) |
| `unread.json` | `fetch` | All fetched unread email metadata |
| `classified.json` | `classify` | Emails grouped by category |
| `classified.csv` | `export` | Flat export for spreadsheet review |
| `audit.csv` | `report` | Decisions log (written before execution) |
| `report_progress.json` | `report` | Resume state for interrupted sessions (auto-deleted on success) |
| `filter_analysis.json` | `filters` | Cached email data with read/unread flags |
| `filter_proposals.json` | `filters` | Proposed filter definitions |
| `errors.log` | any | Timestamped errors with stage and exception details |

---

## Other

- `report` moves emails to Gmail Trash, recoverable for 30 days. To permanently delete: Gmail → Trash → Empty Trash.
- exponential backoff on 429 errors, sleeps tuned to Gmail API quota (15,000 units/min).