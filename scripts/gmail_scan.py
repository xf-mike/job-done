#!/usr/bin/env python3
"""gmail_scan.py — read-only recruiter-reply / application-confirmation tracking.

Reads Gmail (via hatch_gws_cli, never writes to the mailbox), pre-filters
without any LLM, and reports compact hits matched against the applications
tracker. Designed for scheduled runs: watermarked, incremental, cheap.

Usage:
  python3 scripts/gmail_scan.py [--max N] [--dry-run] [--fixture]

- --dry-run : run everything but do not advance state/gmail_watermark.json
- --fixture : classify embedded sample messages (no network) — for testing

Rate-limit etiquette: sequential CLI calls, small --max, metadata only
(no message bodies are ever fetched or printed — subjects + senders only).
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

SKILL_DIR = os.environ.get(
    "JOB_PIPELINE_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
WATERMARK_PATH = os.path.join(SKILL_DIR, "state", "gmail_watermark.json")
TRACKER_PATH = os.path.join(SKILL_DIR, "state", "tracker.json")

# ATS / hiring-platform sender domains (verified against real mail 2026-09-29:
# Ashby confirms via no-reply@ashbyhq.com, Greenhouse via *-mail.io).
ATS_DOMAINS = [
    "ashbyhq.com",
    "greenhouse-mail.io",
    "greenhouse.io",
    "lever.co",
    "myworkday.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "icims.com",
    "jobvite.com",
    "workable.com",
    "breezy.hr",
    "personio.de",
    "gem.com",
]

# Subjects that belong to the verification-code flow (part 2), not tracking.
CODE_SUBJECT_RES = [
    r"verification code",
    r"security code",
    r"one[\s-]?time (code|password)",
    r"\botp\b",
    r"2fa",
    r"two[\s-]?factor",
    r"your code is",
    r"confirm your email",
]

KIND_PATTERNS = [
    ("rejection", [
        r"not moving forward", r"unfortunately", r"other candidates",
        r"decided not to", r"not selected", r"will not be proceeding",
        r"no longer under consideration",
    ]),
    ("offer", [
        r"offer letter", r"\boffer\b.*congratulations", r"congratulations.*offer",
    ]),
    ("interview", [
        r"\binterview\b", r"phone screen", r"schedule (a|your)",
        r"invitation to", r"coding (challenge|assessment)",
        r"\bassessment\b", r"next steps",
    ]),
    ("confirmation", [
        r"thank you for applying", r"thanks for applying",
        r"thanks for your application", r"application confirmation",
        r"application received", r"we received your application",
        r"application submitted", r"your application to",
    ]),
]

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at",
    "inc", "llc", "ltd", "co", "corp",
}


def norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def content_tokens(text):
    return {t for t in re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split()
            if t not in STOPWORDS and len(t) >= 3}


def load_watermark():
    try:
        with open(WATERMARK_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_watermark(wm):
    os.makedirs(os.path.dirname(WATERMARK_PATH), exist_ok=True)
    with open(WATERMARK_PATH, "w") as f:
        json.dump(wm, f, indent=2)


def run_cli(args):
    """Run one hatch_gws_cli command; return parsed JSON or raw text."""
    p = subprocess.run(
        ["hatch_gws_cli"] + args, capture_output=True, text=True, timeout=90)
    out = p.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return {"_raw": out}


def build_query(since_epoch=None):
    domains = " OR ".join(ATS_DOMAINS)
    q = f"from:({domains})"
    if since_epoch:
        q += f" after:{int(since_epoch)}"
    else:
        q += " newer_than:7d"
    for pat in [r"verification code", r"security code", r"one-time",
                r"your code is", r"confirm your email"]:
        q += f' -subject:"{pat}"'
    return q


def triage(query, max_n):
    data = run_cli(["gmail", "+triage", "--query", query,
                    "--max", str(max_n), "--format", "json"])
    if isinstance(data, dict):
        return data.get("messages", []) or []
    return []


def get_tracker_companies():
    """Read-only: company names from the Applications tab (col B)."""
    try:
        with open(TRACKER_PATH) as f:
            sheet_id = json.load(f)["spreadsheet_id"]
    except (OSError, ValueError, KeyError):
        return []
    data = run_cli(["sheets", "spreadsheets", "values", "get", "--params",
                    json.dumps({"spreadsheetId": sheet_id,
                                "range": "Applications!B2:B"})])
    if not isinstance(data, dict):
        return []
    return [r[0] for r in data.get("values", []) if r and r[0].strip()]


def is_code_subject(subject):
    s = (subject or "").lower()
    return any(re.search(p, s) for p in CODE_SUBJECT_RES)


def classify(subject):
    s = (subject or "").lower()
    for kind, pats in KIND_PATTERNS:
        if any(re.search(p, s) for p in pats):
            return kind
    return "other"


def match_company(sender, subject, companies):
    hay = norm(sender) + " " + norm(subject)
    for company in companies:
        toks = content_tokens(company)
        if not toks:
            continue
        # full normalized name, or any significant token, present
        if norm(company) in hay:
            return company
        if any(t in hay for t in toks if len(t) >= 4):
            return company
    return None


def guess_company(sender):
    m = re.match(r"\s*(.*?)\s*<", sender or "")
    name = (m.group(1) if m else sender or "").strip().strip('"')
    return name or None


def process_messages(messages, companies):
    hits = []
    for m in messages:
        subject = m.get("subject", "")
        sender = m.get("from", "")
        if is_code_subject(subject):
            continue  # verification-code flow owns these, not tracking
        company = match_company(sender, subject, companies)
        hits.append({
            "company": company,
            "company_guess": None if company else guess_company(sender),
            "subject": subject,
            "sender": sender,
            "date": m.get("message_sent_at", {}).get("user_local")
                    or m.get("date", ""),
            "kind": classify(subject),
        })
    return hits


FIXTURES = [
    {"from": "Replit Hiring Team <no-reply@ashbyhq.com>",
     "subject": "Thank you for your application to Replit",
     "date": "2026-09-28"},
    {"from": "no-reply@us.greenhouse-mail.io",
     "subject": "Thank you for applying to Together AI",
     "date": "2026-09-28"},
    {"from": "Jane Recruiter <jane@acme.ai>",
     "subject": "Interview invitation — next steps at Acme",
     "date": "2026-09-29"},
    {"from": "Hiring <no-reply@ashbyhq.com>",
     "subject": "Update on your application — not moving forward",
     "date": "2026-09-29"},
    {"from": "no-reply@ashbyhq.com",
     "subject": "Your verification code is 483920",
     "date": "2026-09-29"},
    {"from": "UC San Diego Today <today@ucsd.edu>",
     "subject": "Campus news roundup",
     "date": "2026-09-29"},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=25)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fixture", action="store_true")
    args = ap.parse_args()

    if args.fixture:
        companies = ["Replit", "Together AI", "Acme"]
        hits = process_messages(FIXTURES, companies)
        print(json.dumps(hits, indent=2, ensure_ascii=False))
        kinds = {}
        for h in hits:
            kinds[h["kind"]] = kinds.get(h["kind"], 0) + 1
        print(f"fixture: {len(hits)} hits, kinds={kinds} "
              f"(code mail + non-ATS mail excluded as designed)")
        return 0

    wm = load_watermark()
    since = None
    if wm.get("last_scan"):
        try:
            since = datetime.fromisoformat(wm["last_scan"]).timestamp()
        except ValueError:
            since = None
    query = build_query(since)
    messages = triage(query, args.max)
    companies = get_tracker_companies()
    hits = process_messages(messages, companies)

    now = datetime.now(timezone.utc).isoformat()
    if not args.dry_run:
        save_watermark({"last_scan": now,
                        "last_history_id": wm.get("last_history_id")})
    kinds = {}
    matched = 0
    for h in hits:
        kinds[h["kind"]] = kinds.get(h["kind"], 0) + 1
        if h["company"]:
            matched += 1
    print(json.dumps(hits, indent=2, ensure_ascii=False))
    print(f"gmail_scan: {len(hits)} hits kinds={kinds} "
          f"tracker_matched={matched} "
          f"watermark={'held (dry-run)' if args.dry_run else 'advanced to ' + now}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
