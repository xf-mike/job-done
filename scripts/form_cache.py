#!/usr/bin/env python3
"""
Per-ATS application-form structure cache for the job-pipeline skill
(Stage 3 — Prepare).

Before the agent fills an application form, it runs this script with the
role's application URL. The script detects the ATS from the URL, fetches the
PUBLIC form structure (no login, polite User-Agent), and caches a compact
field list under state/form_cache/. On a cache hit it prints from cache
without any network fetch.

The agent then reuses the cached field mapping (form labels -> profile
fields) instead of having the LLM re-read the entire form on every
application. Only genuinely new/unknown fields ever need LLM attention.

Coverage (best-effort, public endpoints only):
  - greenhouse: boards-api.greenhouse.io .../jobs/{id}?questions=true exposes
    the real application questions (labels, types, required, options).
  - ashby / lever: public posting APIs expose the posting's core fields
    (title, location, compensation); their full custom-question lists are not
    public, so the cache records what is available and the agent still reads
    custom questions from the live form on first encounter.
  - anything else -> {"cached": false, "reason": "unsupported_ats"}

Cache files: state/form_cache/{ats}_{token}.json, refreshed when older than
30 days (override with --refresh / --max-age-days).

Non-interactive, stdlib-only. Exit code is always 0.

Usage:
    python3 form_cache.py "https://job-boards.greenhouse.io/acme/jobs/12345"
    python3 form_cache.py "https://jobs.ashbyhq.com/acme/abc-def" --refresh
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone

SKILL_DIR = os.environ.get("JOB_PIPELINE_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIRNAME = os.path.join("state", "form_cache")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
HTTP_TIMEOUT = 20
DEFAULT_MAX_AGE_DAYS = 30


def cache_dir():
    d = os.path.join(SKILL_DIR, CACHE_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def http_get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def detect_ats(url):
    """Return (ats, token, job_id) or (None, None, None)."""
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return None, None, None
    host = (p.hostname or "").lower()
    segs = [s for s in p.path.split("/") if s]

    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
        # /{token}/jobs/{id}
        if len(segs) >= 3 and segs[1] == "jobs":
            return "greenhouse", segs[0], segs[2]
        return "greenhouse", (segs[0] if segs else None), None
    if host == "jobs.ashbyhq.com":
        # /{org}/{postingId}[/application]
        if len(segs) >= 2:
            return "ashby", segs[0], segs[1]
        return "ashby", (segs[0] if segs else None), None
    if host == "jobs.lever.co":
        # /{slug}/{postingId}
        if len(segs) >= 2:
            return "lever", segs[0], segs[1]
        return "lever", (segs[0] if segs else None), None
    return None, None, None


def parse_greenhouse_questions(job):
    fields = []
    for q in job.get("questions") or []:
        label = (q.get("label") or "").strip()
        for f in q.get("fields") or []:
            ftype = f.get("type", "")
            # Skip anti-bot / tracking internals: never bank, never send to LLM.
            if ftype in ("input_hidden",):
                continue
            name = f.get("name", "")
            if "recaptcha" in name.lower() or "captcha" in name.lower():
                continue
            fields.append({
                "label": label or name,
                "type": ftype,
                "required": bool(f.get("required", False)),
                "options": (f.get("values") or [])[:20] if f.get("values") else [],
            })
    return fields


def fetch_greenhouse(token, job_id):
    if not job_id:
        return None, "no_job_id_in_url"
    try:
        job = http_get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?questions=true")
    except Exception:
        return None, "fetch_failed"
    return {
        "ats": "greenhouse", "token": token, "job_id": job_id,
        "title": job.get("title", ""),
        "location": ((job.get("location") or {}).get("name", "")),
        "fields": parse_greenhouse_questions(job),
        "note": "questions from public ?questions=true endpoint",
    }, None


def fetch_ashby(org, posting_id):
    try:
        data = http_get_json(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
    except Exception:
        return None, "fetch_failed"
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return None, "fetch_failed"
    match = None
    if posting_id:
        for j in jobs:
            if str(j.get("id", "")).lower() == posting_id.lower():
                match = j
                break
    if match is None:
        return None, "job_not_found"
    fields = [
        {"label": "Full name", "type": "input_text", "required": True, "options": []},
        {"label": "Email", "type": "input_text", "required": True, "options": []},
    ]
    return {
        "ats": "ashby", "token": org, "job_id": posting_id,
        "title": match.get("title", ""),
        "location": match.get("locationName", ""),
        "fields": fields,
        "note": ("public posting API does not expose custom questions; "
                 "standard identity fields assumed, custom questions must be "
                 "read from the live form on first encounter"),
    }, None


def fetch_lever(slug, posting_id):
    if not posting_id:
        return None, "no_job_id_in_url"
    try:
        post = http_get_json(f"https://api.lever.co/v0/postings/{slug}/{posting_id}?mode=json")
    except Exception:
        return None, "fetch_failed"
    fields = []
    for q in post.get("customQuestions") or []:
        fields.append({
            "label": (q.get("text") or "").strip(),
            "type": q.get("type", ""),
            "required": bool(q.get("required", False)),
            "options": [],
        })
    cats = post.get("categories") or {}
    return {
        "ats": "lever", "token": slug, "job_id": posting_id,
        "title": post.get("text", ""),
        "location": cats.get("location", ""),
        "fields": fields,
        "note": "custom questions best-effort from public posting JSON",
    }, None


FETCHERS = {"greenhouse": fetch_greenhouse, "ashby": fetch_ashby, "lever": fetch_lever}


def main():
    ap = argparse.ArgumentParser(description="Cache a job board's public application-form structure.")
    ap.add_argument("url", help="Application or job posting URL")
    ap.add_argument("--refresh", action="store_true", help="Refetch even on cache hit")
    ap.add_argument("--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS)
    args = ap.parse_args()

    try:
        ats, token, job_id = detect_ats(args.url)
        if not ats or not token:
            print(json.dumps({"cached": False, "reason": "unsupported_ats"}))
            return 0

        key = f"{ats}_{token}.json"
        path = os.path.join(cache_dir(), key)

        if not args.refresh and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    cached = json.load(f)
                age_days = (time.time() - os.path.getmtime(path)) / 86400.0
                if age_days <= args.max_age_days:
                    cached["cached"] = True
                    cached["source"] = "cache"
                    cached["requested_url"] = args.url
                    cached["job_id_match"] = (cached.get("job_id") == job_id)
                    print(json.dumps(cached, ensure_ascii=False))
                    return 0
            except (OSError, ValueError):
                pass  # fall through to refetch

        payload, err = FETCHERS[ats](token, job_id)
        if err:
            print(json.dumps({"cached": False, "reason": err, "ats": ats, "token": token}))
            return 0

        payload["source_url"] = args.url
        payload["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        payload["cached"] = True
        payload["source"] = "network"
        print(json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"cached": False, "reason": "error", "detail": str(e)[:120]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
