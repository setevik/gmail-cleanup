# Gmail Unread Cleanup

One-time cleanup of unread Gmail. Fetch → Classify (Claude AI) → Review → Trash.

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

Run stages in order:

```bash
# 1. Fetch all unread metadata → unread.json (~2-3 min for 8K emails)
python cleanup.py fetch

# 2. Classify with Claude → classified.json (~3-4 min for 8K emails)
python cleanup.py classify

# 3. Print summary report
python cleanup.py report

# 4. Interactive approval + trash
python cleanup.py trash

# Optional: export to CSV for deeper inspection
python cleanup.py export
```

Each stage saves its output as a JSON file so you can resume or re-run any step independently.

---

## How it works

| Stage    | What it does                                                                 |
|----------|------------------------------------------------------------------------------|
| `fetch`  | Lists all unread IDs via Gmail API, then batch-fetches subject/from/date     |
| `classify` | Gmail labels pre-classify Promotions/Social/Updates; Claude handles the rest |
| `trash`  | `batchModify` moves up to 1000 emails per API call, very fast               |

## Categories

| Category      | Description                                              |
|---------------|----------------------------------------------------------|
| `PROMOTIONS`  | Marketing, ads, sales, coupons, deals                   |
| `NEWSLETTERS` | Blog digests, subscriptions, company updates             |
| `SOCIAL`      | LinkedIn, Twitter/X, Reddit, Facebook notifications      |
| `SYSTEM`      | OTPs, shipping, bank alerts, invoices, receipts          |
| `REVIEW`      | Personal messages, work email — **never auto-deleted**   |

## Safety

- Only `fetch` and `trash` touch Gmail. `classify` is purely local + Claude API.
- `trash` moves to Gmail Trash, **not permanent deletion**. Recoverable for 30 days.
- `REVIEW` category is hard-locked and never queued for deletion.
- To permanently delete after trashing: Gmail → Trash → Empty Trash.

## File outputs

| File              | Contents                          |
|-------------------|-----------------------------------|
| `token.json`      | OAuth token (auto-created)        |
| `unread.json`     | All fetched email metadata        |
| `classified.json` | Emails grouped by category        |
| `classified.csv`  | Flat export for spreadsheet review|
