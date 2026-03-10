#!/usr/bin/env python3
"""
Gmail Unread Cleanup
Usage:
  python cleanup.py fetch       -- fetch all unread metadata → unread.json
  python cleanup.py classify    -- classify with Claude      → classified.json
  python cleanup.py report      -- interactive report: review, delete, archive
  python cleanup.py export      -- export classified.csv

Environment:
  CUTOFF_DATE=YYYY-MM-DD   Only report/trash emails older than this date
                           (default: 1 year ago)
"""

import os, sys, json, time, csv, textwrap, re
from pathlib import Path
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

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
PROGRESS_FILE   = "report_progress.json"

# Domains where each sender is a different person (not grouped)
PUBLIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "ymail.com",
    "aol.com",
    "mail.ru", "inbox.ru", "list.ru", "yandex.ru", "ya.ru",
    "protonmail.com", "proton.me",
    "icloud.com", "me.com", "mac.com",
    "013net.net",
}

# Compound TLD suffixes (domain = last 3 parts instead of 2)
COMPOUND_TLDS = {
    "co.il", "org.il", "ac.il", "gov.il", "net.il",
    "co.uk", "org.uk", "ac.uk",
    "com.au", "edu.au", "org.au",
    "co.in", "co.jp", "co.kr",
    "com.br", "com.ru",
}

# Known brand domain families to merge (maps alias → canonical)
BRAND_DOMAIN_ALIASES = {
    "amazon.co.uk": "amazon.com",
    "amazon.de": "amazon.de",      # keep separate? no, merge
    "amazon.es": "amazon.com",
    "amazon.it": "amazon.com",
    "amazon.fr": "amazon.com",
    "amazon.de": "amazon.com",
}

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

def get_cutoff_date():
    """Return cutoff datetime from CUTOFF_DATE env var (YYYY-MM-DD) or default 1 year ago."""
    raw = os.environ.get("CUTOFF_DATE", "")
    if raw:
        return datetime.strptime(raw, "%Y-%m-%d")
    return datetime.now() - timedelta(days=365)

def parse_email_date(date_str):
    """Parse an email Date header into a naive datetime, or None on failure."""
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.replace(tzinfo=None)
    except Exception:
        return None

def filter_by_cutoff(emails, cutoff):
    """Return emails older than (before) the cutoff date."""
    result = []
    for e in emails:
        dt = parse_email_date(e.get("date", ""))
        if dt and dt < cutoff:
            result.append(e)
    return result

def progress(done, total, label="", width=40):
    pct  = done / total if total else 0
    bar  = "█" * int(pct * width) + "░" * (width - int(pct * width))
    print(f"\r  [{bar}] {done:,}/{total:,} {label}", end="", flush=True)

def extract_org_domain(from_header):
    """Extract the organizational domain from a From header.
    e.g. 'Foo <no-reply@t.mail.coursera.org>' → 'coursera.org'
    """
    # Extract email address from "Name <email>" or bare email
    match = re.search(r'<([^>]+)>', from_header)
    if match:
        addr = match.group(1)
    elif '@' in from_header:
        addr = from_header.strip()
    else:
        return from_header.strip()

    parts = addr.split('@')
    if len(parts) != 2:
        return from_header.strip()

    domain = parts[1].lower()

    # Apply brand aliases first
    if domain in BRAND_DOMAIN_ALIASES:
        return BRAND_DOMAIN_ALIASES[domain]

    # Check compound TLDs
    domain_parts = domain.split('.')
    for tld in COMPOUND_TLDS:
        if domain.endswith('.' + tld):
            # Take the part just before the compound TLD
            tld_len = len(tld.split('.'))
            if len(domain_parts) > tld_len:
                return '.'.join(domain_parts[-(tld_len + 1):])
            return domain

    # Also check for brand aliases on the org domain level
    # e.g. sub.amazon.co.uk → amazon.co.uk → amazon.com
    org = '.'.join(domain_parts[-2:]) if len(domain_parts) >= 2 else domain
    if org in BRAND_DOMAIN_ALIASES:
        return BRAND_DOMAIN_ALIASES[org]

    return org

def getchar(prompt=""):
    """Read a single character without requiring Enter.
    Falls back to input() if not a tty."""
    if prompt:
        print(prompt, end="", flush=True)
    if not sys.stdin.isatty():
        return input().strip().lower()
    try:
        import tty, termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        # Handle Ctrl-C / Ctrl-D
        if ch in ('\x03', '\x04'):
            print()
            raise KeyboardInterrupt
        print(ch)  # echo the character
        return ch.lower()
    except ImportError:
        return input().strip().lower()

def save_progress(actions, completed_cats):
    """Save report progress for recovery."""
    save_json(PROGRESS_FILE, {
        "delete": actions["delete"],
        "archive": actions["archive"],
        "completed_categories": completed_cats,
    })

def load_progress():
    """Load saved report progress, or return empty state."""
    data = load_json(PROGRESS_FILE)
    if data:
        return (
            {"delete": data.get("delete", []), "archive": data.get("archive", [])},
            data.get("completed_categories", []),
        )
    return {"delete": [], "archive": []}, []

def clear_progress():
    """Remove progress file after successful completion."""
    p = Path(PROGRESS_FILE)
    if p.exists():
        p.unlink()

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
        kwargs = dict(userId="me", labelIds=["INBOX", "UNREAD"], maxResults=LIST_PAGE_SIZE)
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
    from collections import Counter, OrderedDict

    data = load_json(CLASSIFIED_FILE)
    if not data:
        print(f"\n❌  {CLASSIFIED_FILE} not found. Run classify first.\n")
        sys.exit(1)

    cutoff = get_cutoff_date()
    if show_header:
        print(f"\n📊  Interactive Report  (emails before {cutoff.strftime('%Y-%m-%d')})\n")

    by_cat = data["by_category"]
    filtered_cat = {cat: filter_by_cutoff(emails, cutoff) for cat, emails in by_cat.items()}
    total = sum(len(emails) for emails in filtered_cat.values())

    # Summary overview
    for cat, desc in CATEGORIES.items():
        n = len(filtered_cat.get(cat, []))
        bar = "█" * min(40, int(n / max(total, 1) * 40))
        print(f"  {cat:<14} {n:>5,}  {bar}")
    print(f"  {'TOTAL':<14} {total:>5,}\n")

    # Load saved progress (for recovery)
    actions, completed_cats = load_progress()
    if completed_cats:
        print(f"  ↩️  Resuming — {len(completed_cats)} categories already done: {', '.join(completed_cats)}\n")

    for cat, desc in CATEGORIES.items():
        emails = filtered_cat.get(cat, [])
        if not emails:
            continue

        # Skip already-completed categories
        if cat in completed_cats:
            continue

        print(f"  {'─'*65}")
        print(f"  📂 {cat}  ({len(emails):,} emails)")
        print(f"     {desc}\n")

        # Group by sender, show all
        sender_groups = Counter(e["from"] for e in emails).most_common()
        emails_by_sender = {}
        for e in emails:
            emails_by_sender.setdefault(e["from"], []).append(e)

        for idx, (sender, count) in enumerate(sender_groups, 1):
            print(f"  {idx:>4}. {truncate(sender, 55)}  ×{count}")

        print()

        if cat == "REVIEW":
            print(f"     (REVIEW items default to ignore — press Enter to skip)\n")

        # Ask for action on this category
        while True:
            prompt = (
                f"  Action for {cat}? "
                f"[I]gnore all / [D]elete all / [A]rchive all / [S]elect per sender: "
            )
            ans = input(prompt).strip().lower()
            if ans in ("i", "ignore", ""):
                print(f"  ⏭️  Ignored\n")
                break
            elif ans in ("d", "delete"):
                for e in emails:
                    actions["delete"].append(e["id"])
                print(f"  🗑️  {len(emails):,} marked for deletion\n")
                break
            elif ans in ("a", "archive"):
                for e in emails:
                    actions["archive"].append(e["id"])
                print(f"  📦 {len(emails):,} marked for archive\n")
                break
            elif ans in ("s", "select"):
                # Build domain-grouped view
                domain_groups = OrderedDict()  # domain → {senders: [...], emails: [...], count: N}
                for sender, count in sender_groups:
                    org_domain = extract_org_domain(sender)
                    if org_domain in PUBLIC_EMAIL_DOMAINS:
                        # Public provider: use full sender as key
                        key = sender
                        label = truncate(sender, 50)
                    else:
                        key = org_domain
                        label = None  # will be built from group

                    if key not in domain_groups:
                        domain_groups[key] = {"senders": [], "emails": [], "count": 0, "label": label}

                    domain_groups[key]["senders"].append(sender)
                    domain_groups[key]["emails"].extend(emails_by_sender[sender])
                    domain_groups[key]["count"] += count

                # Sort by count descending
                sorted_groups = sorted(domain_groups.items(), key=lambda x: -x[1]["count"])

                # Build labels for domain groups
                for key, grp in sorted_groups:
                    if grp["label"] is None:
                        if len(grp["senders"]) == 1:
                            grp["label"] = truncate(grp["senders"][0], 50)
                        else:
                            grp["label"] = f"*@{key} ({len(grp['senders'])} senders)"

                n_groups = len(sorted_groups)
                print(f"\n  Per-sender actions ({n_groups} groups, Enter=ignore, d=delete, a=archive):\n")
                for idx, (key, grp) in enumerate(sorted_groups, 1):
                    while True:
                        choice = getchar(
                            f"    [{idx}/{n_groups}] {grp['label']}  ×{grp['count']}  [i/d/a]: "
                        )
                        if choice in ("i", ""):
                            break
                        elif choice == "d":
                            for e in grp["emails"]:
                                actions["delete"].append(e["id"])
                            break
                        elif choice == "a":
                            for e in grp["emails"]:
                                actions["archive"].append(e["id"])
                            break
                        elif choice == "\r" or choice == "\n":
                            # Enter key in raw mode
                            break
                        else:
                            print("       Enter i, d, or a")
                print()
                break
            else:
                print("     Enter I, D, A, or S")

        # Save progress after each category
        completed_cats.append(cat)
        save_progress(actions, completed_cats)

    # Deduplicate IDs (an email could appear in multiple sender groups theoretically)
    actions["delete"] = list(dict.fromkeys(actions["delete"]))
    actions["archive"] = list(dict.fromkeys(actions["archive"]))

    # Summary of planned actions
    print(f"\n  {'═'*65}")
    print(f"  📋 Action Summary:")
    print(f"     Delete:  {len(actions['delete']):,} emails  (moved to Trash, recoverable 30 days)")
    print(f"     Archive: {len(actions['archive']):,} emails  (removed from Inbox, kept in All Mail)")

    if not actions["delete"] and not actions["archive"]:
        print(f"\n  Nothing to do. Exiting.\n")
        clear_progress()
        return

    final = input(f"\n  Execute these actions? [yes/no]: ").strip().lower()
    if final not in ("yes", "y"):
        print("  Aborted. Progress saved — re-run report to resume or modify.\n")
        return

    svc = get_gmail_service()

    # Execute deletions (trash)
    if actions["delete"]:
        print(f"\n  Moving {len(actions['delete']):,} emails to Trash…\n")
        _batch_modify(svc, actions["delete"],
                      add_labels=["TRASH"], remove_labels=["INBOX"],
                      label="trashing")

    # Execute archives
    if actions["archive"]:
        print(f"\n  Archiving {len(actions['archive']):,} emails…\n")
        _batch_modify(svc, actions["archive"],
                      add_labels=[], remove_labels=["INBOX", "UNREAD"],
                      label="archiving")

    clear_progress()
    print(f"\n  🎉  All done!\n")


def _batch_modify(svc, ids, add_labels, remove_labels, label=""):
    """Batch-modify Gmail messages with progress."""
    done = 0
    errors = 0
    batches = [ids[i:i+TRASH_BATCH] for i in range(0, len(ids), TRASH_BATCH)]
    for batch_ids in batches:
        try:
            body = {"ids": batch_ids, "removeLabelIds": remove_labels}
            if add_labels:
                body["addLabelIds"] = add_labels
            svc.users().messages().batchModify(userId="me", body=body).execute()
            done += len(batch_ids)
            progress(done, len(ids), label)
            time.sleep(0.2)
        except Exception as ex:
            errors += len(batch_ids)
            print(f"\n  ⚠️  Batch error: {ex}")
    print(f"\n  ✅  {done:,} processed  ({errors} errors)")

# ── STAGE 4: Export CSV ───────────────────────────────────────────────────────

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
    "export":   cmd_export,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("Commands:", ", ".join(COMMANDS))
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
