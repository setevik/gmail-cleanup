#!/usr/bin/env python3
"""
Gmail Unread Cleanup
Usage:
  python cleanup.py fetch       -- fetch all unread metadata → unread.json
  python cleanup.py classify    -- classify with Claude      → classified.json
  python cleanup.py report      -- print summary report
  python cleanup.py trash       -- interactive approve & trash
  python cleanup.py export      -- export classified.csv
"""

import os, sys, json, time, csv, textwrap
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────

SCOPES          = ["https://www.googleapis.com/auth/gmail.modify"]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE       = "token.json"
UNREAD_FILE      = "unread.json"
CLASSIFIED_FILE  = "classified.json"
EXPORT_FILE      = "classified.csv"

CLASSIFY_BATCH  = 80          # emails per Claude call
METADATA_BATCH  = 100         # emails per Gmail batch-get
LIST_PAGE_SIZE  = 500         # max Gmail list page size
TRASH_BATCH     = 1000        # batchModify supports up to 1000

CATEGORIES = {
    "PROMOTIONS":  "Promotions & Ads       (marketing, sales, coupons, deals, % off)",
    "NEWSLETTERS": "Newsletters            (digests, subscriptions, company updates)",
    "SOCIAL":      "Social Notifications   (LinkedIn, Twitter/X, Reddit, Facebook)",
    "SYSTEM":      "System & Transactional (OTPs, receipts, shipping, bank alerts)",
    "REVIEW":      "Review First           (personal, work, calendar — inspect manually)",
}

CLASSIFY_SYSTEM = """\
Classify each email by sender and subject into exactly one category.
Return ONLY a JSON array — no markdown, no explanation, no preamble.

Categories:
  PROMOTIONS  – marketing, ads, sales, deals, coupons, discount emails, "% off", anything with unsubscribe links
  NEWSLETTERS – blog digests, subscriptions, weekly/monthly updates, announcements from companies/creators
  SOCIAL      – LinkedIn, Twitter/X, Facebook, Instagram, Reddit, WhatsApp, Slack, forum notifications
  SYSTEM      – OTP codes, shipping/delivery, bank/card alerts, invoices, receipts, password resets, booking confirmations
  REVIEW      – personal messages, direct replies, work email, calendar invites, anything potentially important

Output format (JSON array, no other text):
[{"id": "...", "category": "PROMOTIONS|NEWSLETTERS|SOCIAL|SYSTEM|REVIEW"}, ...]
"""

# ── Gmail auth ────────────────────────────────────────────────────────────────

def get_gmail_service():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(CREDENTIALS_FILE).exists():
                print(f"\n❌  {CREDENTIALS_FILE} not found.")
                print("    Download it from Google Cloud Console → APIs & Services → Credentials")
                print("    (OAuth 2.0 Client ID → Desktop app → Download JSON → save as credentials.json)\n")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(TOKEN_FILE).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json(path):
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else None

def save_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2))

def header(val, headers):
    """Extract a header value from Gmail message headers list."""
    for h in headers:
        if h["name"].lower() == val.lower():
            return h["value"]
    return ""

def truncate(s, n=80):
    return s[:n] + "…" if len(s) > n else s

def progress(done, total, label="", width=40):
    pct  = done / total if total else 0
    bar  = "█" * int(pct * width) + "░" * (width - int(pct * width))
    print(f"\r  [{bar}] {done:,}/{total:,} {label}", end="", flush=True)

# ── STAGE 1: Fetch ────────────────────────────────────────────────────────────

def cmd_fetch():
    print("\n📥  Fetching unread email metadata…\n")
    svc = get_gmail_service()

    # 1a. Collect all message IDs
    ids = []
    token = None
    page = 0
    while True:
        page += 1
        kwargs = dict(userId="me", q="is:unread", maxResults=LIST_PAGE_SIZE)
        if token:
            kwargs["pageToken"] = token
        resp = svc.users().messages().list(**kwargs).execute()
        msgs = resp.get("messages", [])
        ids.extend(m["id"] for m in msgs)
        estimated = resp.get("resultSizeEstimate", len(ids))
        print(f"  Page {page}: {len(msgs)} IDs  (total so far: {len(ids):,} of ~{estimated:,})")
        token = resp.get("nextPageToken")
        if not token:
            break

    print(f"\n  ✅  {len(ids):,} unread message IDs collected. Fetching metadata…\n")

    # 1b. Batch-fetch metadata (100 per batch)
    from googleapiclient.http import BatchHttpRequest

    emails = []
    errors = 0

    def handle_batch(req_id, response, exception):
        nonlocal errors
        if exception:
            errors += 1
            return
        hdrs = response.get("payload", {}).get("headers", [])
        emails.append({
            "id":      response["id"],
            "subject": header("Subject", hdrs) or "(no subject)",
            "from":    header("From",    hdrs) or "",
            "date":    header("Date",    hdrs) or "",
            "labels":  response.get("labelIds", []),
        })

    batches = [ids[i:i+METADATA_BATCH] for i in range(0, len(ids), METADATA_BATCH)]
    for bi, batch_ids in enumerate(batches):
        progress(bi * METADATA_BATCH, len(ids), "fetching metadata")
        batch = svc.new_batch_http_request(callback=handle_batch)
        for mid in batch_ids:
            batch.add(svc.users().messages().get(
                userId="me", id=mid, format="metadata",
                metadataHeaders=["Subject", "From", "Date"]
            ))
        batch.execute()
        time.sleep(0.1)   # gentle rate limiting

    progress(len(ids), len(ids), "done")
    print(f"\n\n  📧  {len(emails):,} emails fetched  ({errors} errors)\n")

    save_json(UNREAD_FILE, {"fetched_at": datetime.now().isoformat(), "emails": emails})
    print(f"  💾  Saved → {UNREAD_FILE}\n")

# ── STAGE 2: Classify ─────────────────────────────────────────────────────────

def cmd_classify():
    import anthropic

    data = load_json(UNREAD_FILE)
    if not data:
        print(f"\n❌  {UNREAD_FILE} not found. Run `python cleanup.py fetch` first.\n")
        sys.exit(1)

    emails = data["emails"]
    print(f"\n🧠  Classifying {len(emails):,} emails with Claude…\n")

    # Pre-classify by Gmail system labels (fast, free)
    label_map = {
        "CATEGORY_PROMOTIONS": "PROMOTIONS",
        "CATEGORY_SOCIAL":     "SOCIAL",
        "CATEGORY_UPDATES":    "NEWSLETTERS",
        "CATEGORY_FORUMS":     "SOCIAL",
    }

    pre_classified = {}
    to_classify    = []
    for e in emails:
        matched = None
        for lbl, cat in label_map.items():
            if lbl in e.get("labels", []):
                matched = cat
                break
        if matched:
            pre_classified[e["id"]] = matched
        else:
            to_classify.append(e)

    print(f"  ⚡  {len(pre_classified):,} pre-classified via Gmail labels")
    print(f"  🔍  {len(to_classify):,} remaining → sending to Claude\n")

    client      = anthropic.Anthropic()
    results     = dict(pre_classified)
    batches     = [to_classify[i:i+CLASSIFY_BATCH] for i in range(0, len(to_classify), CLASSIFY_BATCH)]
    errors      = 0

    for bi, batch in enumerate(batches):
        progress(bi * CLASSIFY_BATCH, len(to_classify), "classifying")
        payload = [{"id": e["id"], "subject": e["subject"], "from": e["from"]} for e in batch]

        try:
            resp = client.messages.create(
                model   = "claude-sonnet-4-20250514",
                max_tokens = 4096,
                system  = CLASSIFY_SYSTEM,
                messages = [{"role": "user", "content": json.dumps(payload)}],
            )
            raw   = resp.content[0].text.strip()
            raw   = raw.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(raw)
            for item in parsed:
                cat = item.get("category", "REVIEW")
                if cat not in CATEGORIES:
                    cat = "REVIEW"
                results[item["id"]] = cat
        except Exception as ex:
            errors += len(batch)
            for e in batch:
                results[e["id"]] = "REVIEW"
            print(f"\n  ⚠️  Batch {bi+1} error: {ex}")

        time.sleep(0.3)

    progress(len(to_classify), len(to_classify), "done")
    print(f"\n\n  ✅  Classification complete  ({errors} fallbacks to REVIEW)\n")

    # Build classified structure
    classified = {cat: [] for cat in CATEGORIES}
    for e in emails:
        cat = results.get(e["id"], "REVIEW")
        classified[cat].append(e)

    save_json(CLASSIFIED_FILE, {
        "classified_at": datetime.now().isoformat(),
        "total": len(emails),
        "by_category": classified,
    })
    print(f"  💾  Saved → {CLASSIFIED_FILE}\n")

    # Print quick summary
    cmd_report(show_header=False)

# ── STAGE 3: Report ───────────────────────────────────────────────────────────

def cmd_report(show_header=True):
    data = load_json(CLASSIFIED_FILE)
    if not data:
        print(f"\n❌  {CLASSIFIED_FILE} not found. Run classify first.\n")
        sys.exit(1)

    if show_header:
        print("\n📊  Classification Report\n")

    by_cat = data["by_category"]
    total  = data["total"]

    for cat, desc in CATEGORIES.items():
        emails = by_cat.get(cat, [])
        n      = len(emails)
        bar    = "█" * min(40, int(n / max(total, 1) * 40))
        print(f"  {cat:<14} {n:>5,}  {bar}")
        print(f"               {desc}")

        # Top 5 senders
        from collections import Counter
        senders = Counter(e["from"] for e in emails).most_common(5)
        for sender, count in senders:
            print(f"               · {truncate(sender, 55)}  ×{count}")
        print()

    deletable = sum(len(by_cat.get(c, [])) for c in ["PROMOTIONS","NEWSLETTERS","SOCIAL","SYSTEM"])
    print(f"  {'TOTAL':<14} {total:>5,}")
    print(f"  Deletable (excl. REVIEW): {deletable:,}  ({deletable/total*100:.0f}%)\n")

# ── STAGE 4: Trash ────────────────────────────────────────────────────────────

def cmd_trash():
    data = load_json(CLASSIFIED_FILE)
    if not data:
        print(f"\n❌  {CLASSIFIED_FILE} not found. Run classify first.\n")
        sys.exit(1)

    print("\n🗑️   Interactive Trash\n")
    print("  For each category, confirm whether to move emails to Trash.")
    print("  Trashed emails are recoverable for 30 days.\n")

    by_cat   = data["by_category"]
    approved = []

    for cat, desc in CATEGORIES.items():
        if cat == "REVIEW":
            print(f"  🔒 REVIEW ({len(by_cat.get(cat, [])):,}) — always skipped\n")
            continue

        emails = by_cat.get(cat, [])
        if not emails:
            print(f"  ⬜ {cat} — 0 emails, skipping\n")
            continue

        print(f"  {'─'*60}")
        print(f"  📂 {cat}  ({len(emails):,} emails)")
        print(f"     {desc}")
        print()

        # Show sample
        for e in emails[:5]:
            print(f"     · {truncate(e['from'], 35):<36} {truncate(e['subject'], 50)}")
        if len(emails) > 5:
            print(f"     … and {len(emails)-5:,} more")
        print()

        while True:
            ans = input(f"  Trash {len(emails):,} emails in {cat}? [y/n/s=show more] ").strip().lower()
            if ans == "s":
                for e in emails[:25]:
                    print(f"     {truncate(e['from'],35):<36} {truncate(e['subject'],50)}")
                if len(emails) > 25:
                    print(f"     … and {len(emails)-25:,} more")
                print()
            elif ans in ("y", "yes"):
                approved.extend(e["id"] for e in emails)
                print(f"  ✅ {len(emails):,} queued\n")
                break
            elif ans in ("n", "no"):
                print(f"  ⏭️  Skipped\n")
                break
            else:
                print("     Please enter y, n, or s")

    if not approved:
        print("  Nothing approved for deletion. Exiting.\n")
        return

    print(f"\n  {'─'*60}")
    print(f"  📋 Total queued: {len(approved):,} emails")
    print(f"  These will be moved to Gmail Trash (recoverable for 30 days).")
    final = input(f"\n  Proceed? [yes/no] ").strip().lower()
    if final not in ("yes", "y"):
        print("  Aborted.\n")
        return

    print(f"\n  Moving {len(approved):,} emails to Trash…\n")
    svc    = get_gmail_service()
    done   = 0
    errors = 0
    batches = [approved[i:i+TRASH_BATCH] for i in range(0, len(approved), TRASH_BATCH)]

    for batch_ids in batches:
        try:
            svc.users().messages().batchModify(
                userId="me",
                body={"ids": batch_ids, "addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX", "UNREAD"]},
            ).execute()
            done += len(batch_ids)
            progress(done, len(approved), "trashing")
            time.sleep(0.2)
        except Exception as ex:
            errors += len(batch_ids)
            print(f"\n  ⚠️  Batch error: {ex}")

    print(f"\n\n  🎉  Done!  {done:,} moved to Trash  ({errors} errors)\n")
    if errors:
        print(f"  ⚠️  {errors} emails failed — check Gmail manually.\n")

# ── STAGE 5: Export CSV ───────────────────────────────────────────────────────

def cmd_export():
    data = load_json(CLASSIFIED_FILE)
    if not data:
        print(f"\n❌  {CLASSIFIED_FILE} not found. Run classify first.\n")
        sys.exit(1)

    rows = [["category", "from", "subject", "date", "id"]]
    for cat, emails in data["by_category"].items():
        for e in emails:
            rows.append([cat, e["from"], e["subject"], e["date"], e["id"]])

    with open(EXPORT_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)

    print(f"\n  💾  Exported {len(rows)-1:,} rows → {EXPORT_FILE}\n")

# ── Entry point ───────────────────────────────────────────────────────────────

COMMANDS = {
    "fetch":    cmd_fetch,
    "classify": cmd_classify,
    "report":   cmd_report,
    "trash":    cmd_trash,
    "export":   cmd_export,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("Commands:", ", ".join(COMMANDS))
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
