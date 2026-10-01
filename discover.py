#!/usr/bin/env python3
"""
API-first Stage-1 discovery for the job-pipeline skill.

Pulls job listings from public job-board JSON APIs (Greenhouse, Ashby) plus
the LinkedIn guest search API, applies programmatic filters
(dedupe vs state/seen_roles.json FIRST, blacklist, new-grad signal,
lane keywords, recency, location), and writes a compact candidate file for
the agent to judge. The agent judges ONLY these candidates instead of
browsing ~50 pages one by one.

Non-interactive: safe to run from cron workers. Never prompts.

Usage:
    python3 discover.py [--dry-run] [--limit N] [--max-candidates N] [--li-pages N]

Outputs (real runs only; --dry-run writes nothing):
    state/discovery_candidates.json  (compact candidate list for LLM judging)
    state/discovery_watermark.json   ({"last_successful_run": ISO ts, "runs": n})
    state/job_presence.json          ({url: {"run": n, "run_id": "YYYY-MM-DD-HHMM"}})
    state/seen_roles.json            (updated in place: absent-3-runs jobs -> "closed")
    state/source_health.json         (per-source pulled history + status)
    stdout: one-line summary (boards, jobs pulled, filtered, candidates, seconds,
            watermark run counter, newly-closed count, per-source health)
"""
import argparse
import html as htmlmod
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from datetime import time as dtime

SKILL_DIR = os.environ.get("JOB_PIPELINE_DIR", "/home/hatch/workspace/skills/job-pipeline")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
HTTP_TIMEOUT = 20

# ---------------------------------------------------------------- config ---
def load_config():
    import yaml
    with open(f"{SKILL_DIR}/config.yaml") as f:
        return yaml.safe_load(f)

def load_boards():
    with open(f"{SKILL_DIR}/references/company_boards.json") as f:
        return json.load(f)["boards"]

def load_seen():
    try:
        with open(f"{SKILL_DIR}/state/seen_roles.json") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def job_identity(job):
    """Semantic identity for dedup: (req_id, company_norm, title_norm).

    Catches what URL-equality misses: the same requisition reposted under a
    different URL (e.g. two LinkedIn postings for one Workday JR- number),
    and roles the user already applied to outside the pipeline.
    """
    url = job.get("url", "")
    req_id = ""
    for pat in (r"gh_jid=(\d+)", r"[?&]jobId=(\d+)", r"(JR-\d+)",
                r"/jobs/(\d{5,})", r"job/([A-Za-z0-9_-]{20,})"):
        m = re.search(pat, url)
        if m:
            req_id = m.group(1)
            break
    comp = re.sub(r"\s+", " ", (job.get("company") or "").strip().lower())
    comp = re.sub(r"[^a-z0-9\s]", "", comp).strip()
    title = re.sub(r"\s+", " ", (job.get("title") or "").strip().lower())
    title = re.sub(r"\s*[\(\[].*?[\)\]]", "", title)
    title = re.sub(r"[^a-z0-9\s]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return req_id, comp, title

def build_seen_index(seen):
    """Index seen_roles by req_id and (company, title) for semantic dedup."""
    by_req, by_pair = {}, {}
    for url, rec in seen.items():
        rid, comp, title = job_identity({"url": url, **rec})
        if rid:
            by_req[rid] = url
        if comp and title:
            by_pair[(comp, title)] = url
    return by_req, by_pair

# Cheap prefilter on title+snippet. The full posting-text scan happens in the
# agent's judging step (SKILL.md Stage 2) — this only catches the obvious.
DISQUALIFIER_PATTERNS = [
    (re.compile(r"no\s+(visa\s+)?sponsorship", re.I), "no-sponsorship"),
    (re.compile(r"will not.*sponsor", re.I), "no-sponsorship"),
    (re.compile(r"not eligible for F1", re.I), "no-sponsorship"),
    (re.compile(r"record a video", re.I), "video-required"),
    (re.compile(r"video (self-)?introduction", re.I), "video-required"),
]

# Domains whose application flow is known to force a human-verification wall.
# Flagged at discovery so the shortlist can mark them manual-likely instead
# of burning a fill attempt. (Observed 2026-09-29: YC hCaptcha x3, iCIMS x1.)
# Substring matched against the whole posting URL (domain or query marker).
MANUAL_LIKELY_MARKERS = {
    "ycombinator.com": "hCaptcha expected on YC application modal",
    "icims": "hCaptcha expected on iCIMS application",
}

# --------------------------------------------------------------- fetchers ---
def http_get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def http_get_text(url, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def fetch_greenhouse(token, limit):
    """Returns list of normalized job dicts."""
    data = http_get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    out = []
    for j in data.get("jobs", [])[:limit]:
        loc = (j.get("location") or {}).get("name", "")
        depts = ", ".join(d.get("name", "") for d in (j.get("departments") or []))
        out.append({
            "company": j.get("company_name", ""),
            "title": j.get("title", "").strip(),
            "location": loc,
            "url": j.get("absolute_url", ""),
            "date": (j.get("first_published") or "")[:10],
            "snippet": depts,
            "source": "greenhouse",
        })
    return out

def fetch_ashby(token, limit):
    data = http_get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    out = []
    for j in data.get("jobs", [])[:limit]:
        if not j.get("isListed", True):
            continue
        loc = j.get("location") or ""
        if j.get("isRemote"):
            loc = "Remote" + (f", {loc}" if loc else "")
        dept = j.get("department") or ""
        team = j.get("team") or ""
        snippet = " / ".join(s for s in (dept, team) if s)
        out.append({
            "company": "",
            "title": (j.get("title") or "").strip(),
            "location": loc,
            "url": j.get("jobUrl", ""),
            "date": (j.get("publishedAt") or "")[:10],
            "snippet": snippet,
            "source": "ashby",
            "board_token": token,
        })
    return out

# LinkedIn guest search: HTML cards, ~10 per page, paginate with start=.
# Parsing splits the page into per-card chunks FIRST (cheap split on the card
# urn), then applies small field regexes per chunk — this avoids catastrophic
# backtracking on large or oddly-structured pages.
_LI_URN = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"')
_LI_TITLE = re.compile(r'<h3 class="base-search-card__title">\s*(.*?)\s*</h3>', re.S)
_LI_COMPANY = re.compile(r'<h4 class="base-search-card__subtitle">.*?<a[^>]*>\s*(.*?)\s*</a>', re.S)
_LI_LOC = re.compile(r'<span class="job-search-card__location">\s*(.*?)\s*</span>', re.S)
_LI_TIME = re.compile(r'<time class="job-search-card__listdate"[^>]*>', re.S)
_LI_DT = re.compile(r'datetime="([^"]*)"')
_LI_REL = re.compile(r"(\d+)\s+(hour|day|week|month)s?\s+ago", re.I)
_LI_TAG = re.compile(r"<[^>]+>")

def _clean(s):
    return htmlmod.unescape(_LI_TAG.sub("", s or "")).strip()

def _li_date(dt_attr, time_inner):
    if dt_attr and re.match(r"\d{4}-\d{2}-\d{2}", dt_attr):
        return dt_attr[:10]
    m = _LI_REL.search(time_inner or "")
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = {"hour": 0, "day": n, "week": 7 * n, "month": 30 * n}[unit]
        return date.fromordinal(date.today().toordinal() - days).isoformat()
    return ""

def _li_age_hours(dt_attr, time_inner):
    """Age of a LinkedIn posting in hours — lenient (minimum possible age).

    Relative times ("X hours/days ago") convert directly to hours. A bare ISO
    date is day-granularity, so assume end-of-day (youngest possible posting
    time). Returns None when nothing is parseable (caller keeps the posting).
    """
    now = datetime.now()
    m = _LI_REL.search(time_inner or "")
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return float({"hour": n, "day": n * 24, "week": n * 7 * 24,
                      "month": n * 30 * 24}[unit])
    if dt_attr and re.match(r"\d{4}-\d{2}-\d{2}", dt_attr):
        d = datetime.fromisoformat(dt_attr[:10]).date()
        return max(0.0, (now - datetime.combine(d, dtime.max)).total_seconds() / 3600)
    return None


def _job_age_hours(job):
    """Age of any normalized job dict in hours (lenient), or None if unknown.

    LinkedIn jobs carry a precise ``age_hours``; board jobs fall back to their
    ISO date assuming end-of-day (minimum possible age). Unknown -> None.
    """
    age_h = job.get("age_hours")
    if age_h is not None:
        return float(age_h)
    d = parse_date(job.get("date", ""))
    if d is None:
        return None
    now = datetime.now()
    return max(0.0, (now - datetime.combine(d, dtime.max)).total_seconds() / 3600)


def _parse_li_cards(page_html):
    """Normalized job dicts from one LinkedIn guest-search page."""
    if len(page_html) > 300_000:  # poisoned/oversize page guard
        return []
    starts = [m.start() for m in _LI_URN.finditer(page_html)]
    out = []
    for i, s in enumerate(starts):
        chunk = page_html[s:starts[i + 1] if i + 1 < len(starts) else s + 20000]
        m_id = _LI_URN.match(page_html, s)
        m_t = _LI_TITLE.search(chunk)
        m_c = _LI_COMPANY.search(chunk)
        m_l = _LI_LOC.search(chunk)
        m_tm = _LI_TIME.search(chunk)
        if not (m_id and m_t and m_c and m_l):
            continue
        dt_m = _LI_DT.search(m_tm.group(0)) if m_tm else None
        dt = dt_m.group(1) if dt_m else ""
        t_end = chunk.find("</time>", m_tm.end()) if m_tm else -1
        inner = chunk[m_tm.end():t_end] if m_tm and t_end > 0 else ""
        out.append({
            "company": _clean(m_c.group(1)),
            "title": _clean(m_t.group(1)),
            "location": _clean(m_l.group(1)),
            "url": f"https://www.linkedin.com/jobs/view/{m_id.group(1)}",
            "date": _li_date(dt, inner),
            "age_hours": _li_age_hours(dt, inner),
            "snippet": "",
            "source": "linkedin",
        })
    return out

def fetch_linkedin(query, pages):
    """Best-effort: skip the whole source on HTTP/parse trouble."""
    out = []
    for p in range(pages):
        q = urllib.parse.urlencode({
            "keywords": query, "location": "United States",
            "start": p * 10,
        })
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?{q}"
        page_html = http_get_text(url, timeout=12)  # short timeout: never let LI stall the run
        out.extend(_parse_li_cards(page_html))
    return out

def _curated_job_dict(company_raw, company, role, loc, apply_url, age_text,
                     section):
    """Shared dict builder for curated-list rows. Returns None to skip."""
    m = re.search(r"(\d+)\s*d", age_text or "")
    age_d = int(m.group(1)) if m else None
    date_s = ((date.today() - timedelta(days=age_d)).isoformat()
              if age_d is not None else "")
    markers = "".join(
        e for e in ("\U0001f6c2", "\U0001f1fa\U0001f1f8",  # 🛂 🇺🇸
                    "\U0001f525", "\U0001f393")           # 🔥 🎓
        if e in company_raw)
    clean_company = re.sub(r"[^\w\s&.,'\-]", "", company).strip() or company
    return {
        "company": clean_company,
        "title": re.sub(r"\*+", "", role).strip(),
        "location": re.sub(r"\*+", "", loc).strip(),
        "url": apply_url,
        "date": date_s,
        "snippet": f"[{section}]{' ' + markers if markers else ''}",
    }


def _parse_curated_html(text, sections):
    """Parse <table> job lists (SimplifyJobs New-Grad-Positions format):
    <tr> rows of Company | Role | Location | Application | Age, "↳" rows
    reuse the previous company, 🔒 in the row means closed."""
    jobs = []
    for chunk in re.split(r"^##\s+", text, flags=re.M)[1:]:
        header, _, body = chunk.partition("\n")
        section = re.sub(r"^[^\w]+", "", header).strip()
        if sections and not any(w.lower() in section.lower()
                                for w in sections):
            continue
        last_company = ""
        for tr in re.findall(r"<tr>(.*?)</tr>", body, flags=re.S):
            if "\U0001f512" in tr:  # 🔒 closed posting
                continue
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S)
            if len(tds) < 5:
                continue
            strip = lambda h: htmlmod.unescape(
                re.sub(r"<[^>]+>", "", h)).strip()
            company_raw = strip(tds[0])
            company = company_raw
            if company == "↳" or not company:
                company = last_company
            else:
                last_company = company
            if not company:
                continue
            hrefs = re.findall(r'href="([^"]+)"', tds[3])
            apply_url = next(
                (u for u in hrefs
                 if "simplify.jobs" not in u and "imgur.com" not in u), "")
            if not apply_url:
                continue
            j = _curated_job_dict(company_raw, company, strip(tds[1]),
                                  strip(tds[2]), apply_url, strip(tds[4]),
                                  section)
            if j:
                jobs.append(j)
    return jobs


def _parse_curated_markdown(text, sections):
    """Parse markdown-table job lists: | Company | Role | Location |
    Application | Age |. A logical row may wrap across multiple physical
    lines: a physical line whose cell count reaches the header's starts a
    new row, shorter lines are continuations of the previous row."""
    jobs = []
    section = ""
    last_company = ""

    def parse_row(b):
        nonlocal last_company
        cells = [c.strip() for c in b.strip().strip("|").split("|")]
        if len(cells) < 5:
            return None
        if re.match(r"^:?-{2,}:?$", cells[0]) or cells[0].lower() == "company":
            return None  # header / separator row
        # Column layout varies by list (5-col: Company|Role|Location|Apply|Age;
        # 6-col: Company|Role|Location|Apply|Tailor|Age). The first four are
        # stable; Age is always the last column.
        company_raw, role, loc, app_cell = cells[:4]
        age_cell = cells[-1]
        company = re.sub(r"\*+", "", company_raw).strip()
        # Company may be a markdown link: [Name](url) — extract the text so
        # the URL doesn't get mashed into the company name by clean_company.
        company = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", company).strip()
        if "\U0001f512" in company_raw:  # 🔒 closed posting
            return None
        if company == "↳" or not company:
            company = last_company
        else:
            last_company = company
        if not company:
            return None
        urls = [u.rstrip(").],\"'") for u in
                re.findall(r"https?://\S+", app_cell)]
        apply_url = next(
            (u for u in urls
             if "simplify.jobs" not in u and "imgur.com" not in u
             and "camo.githubusercontent.com" not in u),
            "")
        if not apply_url:
            return None
        return _curated_job_dict(company_raw, company,
                                 re.sub(r"\*+", "", role).strip(),
                                 re.sub(r"\*+", "", loc).strip(),
                                 apply_url, age_cell, section)

    rows, cur, ncols = [], "", 0

    def flush_table():
        nonlocal rows, cur, ncols
        if cur:
            rows.append(cur)
            cur = ""
        for r in rows:
            j = parse_row(r)
            if j:
                jobs.append(j)
        rows, ncols = [], 0

    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("## "):
            flush_table()
            section = re.sub(r"^[^\w]+", "", s[3:]).strip()
        elif s.startswith("|"):
            ncells = len(s.strip().strip("|").split("|"))
            if ncols == 0:
                ncols = ncells  # header defines the expected width
            if ncells >= ncols and cur:
                rows.append(cur)
                cur = ""
            cur += " " + s
        elif cur:
            flush_table()
    flush_table()

    if sections:
        wanted = [w.lower() for w in sections]
        jobs = [j for j in jobs
                if any(w in j["snippet"].lower() for w in wanted)]
    return jobs


def fetch_curated_list(url, sections):
    """Fetch a community-curated job list and return job dicts.

    Supports HTML <table> lists and markdown-table lists
    (Company | Role | Location | Application | Age). `sections` is a
    substring filter on the section heading (empty = all sections).
    Best-effort: returns [] on any fetch/parse trouble so one bad list
    never breaks the run.
    """
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "muse-job-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:
        return []
    try:
        if "<table" in text:
            return _parse_curated_html(text, sections)
        return _parse_curated_markdown(text, sections)
    except Exception:
        return []

# ------------------------------------------------- incremental state ---
# Watermark + presence tracking let consecutive runs act incrementally and
# detect silently-closed postings (ATS boards drop closed jobs without notice).
#   state/discovery_watermark.json : {"last_successful_run": "<ISO ts>", "runs": n}
#   state/job_presence.json        : {url: {"run": n, "run_id": "YYYY-MM-DD-HHMM"}}
# A job absent for ABSENT_RUNS_TO_CLOSE consecutive runs whose seen_roles
# decision is not applied/shortlisted is marked "closed" ("no longer listed").
ABSENT_RUNS_TO_CLOSE = 3

def load_watermark():
    try:
        with open(f"{SKILL_DIR}/state/discovery_watermark.json") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_successful_run": None, "runs": 0}

def load_presence():
    try:
        with open(f"{SKILL_DIR}/state/job_presence.json") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def last_seen_run(entry):
    """Normalize a presence entry to its run counter (supports int or dict)."""
    if isinstance(entry, dict):
        return entry.get("run", 0)
    return entry or 0

def close_absent_jobs(presence, seen, current_n):
    """Mark jobs absent for ABSENT_RUNS_TO_CLOSE consecutive runs as closed.

    Never touches 'applied' or 'shortlisted' decisions (the user may still act
    on those). Only marks URLs already present in seen_roles.json. Mutates
    `seen` in place. Returns the number of newly-closed entries.
    """
    closed = 0
    for url, entry in presence.items():
        if current_n - last_seen_run(entry) < ABSENT_RUNS_TO_CLOSE:
            continue
        rec = seen.get(url)
        if not rec:
            continue  # pulled but never surfaced to the agent; nothing to mark
        if rec.get("decision") in ("applied", "shortlisted", "closed"):
            continue
        rec["decision"] = "closed"
        rec["reason"] = "no longer listed"
        closed += 1
    return closed

# ------------------------------------------------- source health -----
# Per-source liveness monitoring: every run records, for each of the 40 ATS
# boards plus LinkedIn (aggregated), how many parseable jobs it returned and
# how fresh the newest one was. Anomalies (fetch failure, zero parseable jobs,
# volume collapse vs history, or a newest item older than STALE_WARN_HOURS)
# are reported in the one-line summary so the scheduled run can alert the user
# instead of staying silent on an empty shortlist.
HEALTH_HIST_LEN = 10   # pulled-count history kept per source
HEALTH_MIN_HIST = 3    # runs of history before volume-drop alerts fire
STALE_WARN_HOURS = 168  # newest item older than this (7d) -> stale warning

def load_health():
    try:
        with open(f"{SKILL_DIR}/state/source_health.json") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

def eval_source_health(name, pulled, newest_age_h, failed, hist):
    """(status, note); status in ok/warn/fail. Lenient by design: needs
    repeated evidence (history) before crying volume-drop. The staleness check
    applies to LinkedIn only — small ATS boards legitimately go months without
    a new posting, so "oldest newest-item" is not a breakage signal for them."""
    if failed:
        return "fail", f"fetch failed ({failed})"
    if pulled == 0:
        return "warn", "0 parseable jobs returned"
    if name == "linkedin" and newest_age_h is not None and newest_age_h > STALE_WARN_HOURS:
        return "warn", f"newest item {newest_age_h:.0f}h old — LinkedIn may be serving stale data"
    if len(hist) >= HEALTH_MIN_HIST:
        med = _median(hist)
        if med > 0 and pulled < 0.25 * med:
            return "warn", f"volume drop: {pulled} vs median {med:.0f}"
    return "ok", ""

# ---------------------------------------------------------------- filters ---
# Title include/exclude filters are config-driven
# (discovery.title_include / discovery.title_exclude in config.yaml).
# Defaults below preserve the original new-grad SDE behavior so existing
# configs keep working unchanged.
_DEFAULT_TITLE_INCLUDE = (
    r"new[\s\-]?grad|entry[\s\-]?level|university grad|early career"
    r"|\bjunior\b|\b2026\b|\b2027\b|newgrad|recent grad")
_DEFAULT_TITLE_EXCLUDE = r"\bintern(ship)?s?\b"
# Tier-2 default: role keywords / junior signals without an explicit new-grad
# marker (SDE/MLE/developer live here, NOT in title_include, so senior
# postings can't flood tier-1 and jam the auto-scaling window).
_DEFAULT_TITLE_TIER2 = (
    r"\bsde\b|\bmle\b|\bdeveloper\b|\bsoftware engineer\b|\bengineer i\b"
    r"|0[\s\-–]*2 years|0\+ years|recent graduate|\buniversity\b")

# Tier-2 senior guard: obvious senior titles never belong in the low-priority
# pool — dropped in code so the LLM judge never spends effort on them.
# (Tier-1 is untouched: an explicit new-grad marker always wins.)
_SENIOR_TITLE_RE = re.compile(
    r"\bsenior\b|\bstaff\b|\bprincipal\b|\blead\b|\bsr\.?\b"
    r"|\bdirector\b|\bmanager\b|\barchitect\b|\biii\b|\biv\b", re.I)

def compile_title_filters(cfg):
    d = (cfg.get("discovery") or {})
    inc = d.get("title_include") or [_DEFAULT_TITLE_INCLUDE]
    exc = d.get("title_exclude") or [_DEFAULT_TITLE_EXCLUDE]
    t2 = d.get("title_tier2_include") or [_DEFAULT_TITLE_TIER2]
    if isinstance(inc, str):
        inc = [inc]
    if isinstance(exc, str):
        exc = [exc]
    if isinstance(t2, str):
        t2 = [t2]
    return (re.compile("|".join(f"(?:{p})" for p in inc), re.I),
            re.compile("|".join(f"(?:{p})" for p in exc), re.I),
            re.compile("|".join(f"(?:{p})" for p in t2), re.I))

# Lane keywords live on each lane in config.yaml (lane.keywords).
# _LEGACY_LANE_KEYWORDS keeps configs without a keywords key working.
_LEGACY_LANE_KEYWORDS = {
    "agent_infra": ["agent", "agentic", "inference", "serving", "infra",
                    "kernel", "distributed", "ml system", "machine learning",
                    "llm", "foundation model", "training", "gpu", "cuda",
                    "compiler", "runtime", "orchestration"],
    "ai_cloud": ["backend", "cloud", "platform", "kubernetes", "k8s",
                 "distributed", "microservice", "devops", "sre", "network",
                 "storage", "database"],
    "genai": ["llm", "rag", "chatbot", "prompt", "copilot", "genai",
              "generative ai", "ai engineer", "ai application", "voice ai",
              "conversational"],
}

def lane_of(text, lanes_cfg):
    for lane in lanes_cfg:
        kws = lane.get("keywords") or _LEGACY_LANE_KEYWORDS.get(lane["name"], [])
        for kw in kws:
            if re.search(r"\b" + re.escape(kw) + r"\b", text, re.I):
                return lane["name"]
    return None

_US_STATE = re.compile(r",\s*[A-Z]{2}\b")
_US_METROS = {
    "san francisco bay area", "san francisco", "new york", "new york city",
    "seattle", "austin", "boston", "chicago", "los angeles", "san diego",
    "denver", "atlanta", "washington", "portland", "san jose", "mountain view",
    "palo alto", "sunnyvale", "santa clara", "bellevue", "redmond", "irvine",
    "santa monica", "culver city", "menlo park", "cambridge",
}

def loc_score(loc):
    """2 = remote/CA (preferred), 1 = other US, 0 = non-US (skip)."""
    l = (loc or "").lower()
    if "remote" in l:
        return 2
    if re.search(r",\s*ca\b", l) or "california" in l:
        return 2
    if "united states" in l or _US_STATE.search(loc or ""):
        return 1
    if any(m in l for m in _US_METROS):
        return 1
    return 0

def parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:10]).date()
    except ValueError:
        return None

# -------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="pull + filter, print summary, write nothing")
    ap.add_argument("--limit", type=int, default=10**9, help="max jobs processed per board (testing)")
    ap.add_argument("--max-candidates", type=int, default=None,
                        help="max candidates emitted (default: discovery.browse_target from config)")
    ap.add_argument("--max-age-days", type=float, default=None,
                        help="override config max_post_age_days (e.g. 30 for a one-time backfill)")
    ap.add_argument("--li-pages", type=int, default=2, help="LinkedIn pages per query (10 cards each)")
    args = ap.parse_args()
    t0 = time.time()

    cfg = load_config()
    boards = load_boards()
    seen = load_seen()
    seen_by_req, seen_by_pair = build_seen_index(seen)

    lanes_cfg = sorted(cfg.get("lanes", []), key=lambda l: l.get("priority", 99))
    lanes_by_priority = [l["name"] for l in lanes_cfg]
    queries = [q for l in lanes_cfg for q in l.get("queries", [])]
    blacklist = [b.lower() for b in (cfg.get("blacklist", {}) or {}).get("companies", [])]
    title_include_re, title_exclude_re, title_tier2_re = compile_title_filters(cfg)

    stats = {"boards_ok": 0, "boards_failed": 0, "pulled": 0,
             "deduped": 0, "disqualified": 0, "candidates": 0, "li_ok": 0, "li_failed": 0}
    failed_boards = []
    seen_urls = set()  # in-run dedupe: same posting can surface via multiple queries
    pulled_urls = []   # every job URL fetched this run (for presence tracking)
    pool = []
    src_stats = {}     # source name -> {"pulled", "newest_age_h", "failed"}

    def track_source(name, jobs, failed=None):
        ages = [_job_age_hours(j) for j in jobs]
        ages = [a for a in ages if a is not None]
        src_stats[name] = {"pulled": len(jobs),
                           "newest_age_h": min(ages) if ages else None,
                           "failed": failed}

    def consider(job, board_company=""):
        stats["pulled"] += 1
        url = job.get("url", "")
        if not url:
            return
        pulled_urls.append(url)  # presence: upserted even if filtered/deduped below
        # (a) dedupe FIRST — never re-read a seen URL; semantic identity
        # catches the same requisition under a different URL and roles the
        # user already applied to outside the pipeline.
        if url in seen or url in seen_urls:
            stats["deduped"] += 1
            return
        rid, comp_norm, title_norm = job_identity({**job, "company": job.get("company") or board_company})
        if (rid and rid in seen_by_req) or ((comp_norm, title_norm) in seen_by_pair):
            stats["deduped"] += 1
            return
        seen_urls.add(url)
        company = job.get("company") or board_company
        # (a2) obvious disqualifiers on title+snippet (full posting scan is in
        # the agent's judging step per SKILL.md Stage 2)
        text_pre = f"{job.get('title','')} {job.get('snippet','')}"
        dq = next((name for rx, name in DISQUALIFIER_PATTERNS if rx.search(text_pre)), None)
        if dq:
            stats["disqualified"] += 1
            return
        # (a3) flag flows known to force human verification — shortlist marks
        # these manual-likely instead of burning a fill attempt
        ml = next((reason for marker, reason in MANUAL_LIKELY_MARKERS.items() if marker in url), None)
        if ml:
            job["manual_likely"] = ml
        # (b) blacklist
        if any(b in company.lower() for b in blacklist):
            return
        text = f"{job.get('title','')} {job.get('snippet','')}"
        # (c) title include/exclude (config-driven; defaults = new-grad SDE).
        # Tier-1 = explicit new-grad marker. Tier-2 = role keyword / junior
        # signal without the marker (SDE/MLE/developer/…); sorted after tier-1
        # and flagged for stricter judging. Intern exclusion applies to both.
        if title_exclude_re.search(job.get("title", "")):
            return
        if title_include_re.search(text):
            tier = 1
        elif title_tier2_re.search(text):
            tier = 2
        else:
            return
        if tier == 2 and _SENIOR_TITLE_RE.search(job.get("title", "")):
            stats["senior_dropped"] = stats.get("senior_dropped", 0) + 1
            return
        # (d) lane keyword match (keywords live on each lane in config.yaml)
        lane = lane_of(text, lanes_cfg)
        if not lane:
            return
        # (e) recency is applied AFTER fetching via the auto-scaling window
        # (see below); record age here. Unknown dates are kept (best-effort,
        # never drop on missing data). LinkedIn relative times are exact;
        # day-granularity dates assume end-of-day (minimum possible age).
        age_h = _job_age_hours(job)
        # (f) location
        ls = loc_score(job.get("location", ""))
        if ls == 0:
            return
        pool.append({
            "company": company, "title": job["title"],
            "location": job.get("location", ""), "url": url,
            "date": job.get("date", ""), "lane": lane,
            "snippet": (job.get("snippet") or "")[:160],
            "source": job.get("source", ""), "tier": tier,
            "_loc": ls, "_date": job.get("date", ""), "_age_h": age_h,
        })
        if job.get("manual_likely"):
            pool[-1]["manual_likely"] = job["manual_likely"]

    def fetch_board(b):
        # returns (board, jobs, error) — run in worker threads, no shared mutation
        try:
            if b["platform"] == "greenhouse":
                return b, fetch_greenhouse(b["token"], args.limit), None
            return b, fetch_ashby(b["token"], args.limit), None
        except Exception as e:  # noqa: BLE001 - best-effort per board
            return b, [], e

    with ThreadPoolExecutor(max_workers=10) as ex:
        for b, jobs, err in ex.map(fetch_board, boards):
            src_name = f"board:{b['company']}"
            if err is not None:
                stats["boards_failed"] += 1
                failed_boards.append(f"{b['company']}({type(err).__name__})")
                track_source(src_name, [], failed=type(err).__name__)
                continue
            stats["boards_ok"] += 1
            track_source(src_name, jobs)
            for j in jobs:
                consider(j, board_company=b["company"])

    # LinkedIn guest API — supplemental, best-effort per query (threaded lightly;
    # any single query failing never affects the rest of the run)
    def fetch_li_query(q):
        try:
            return q, fetch_linkedin(q, args.li_pages), None
        except Exception as e:  # noqa: BLE001
            return q, [], e

    with ThreadPoolExecutor(max_workers=4) as ex:
        li_jobs_all = []
        for q, jobs, err in ex.map(fetch_li_query, queries):
            if err is not None:
                stats["li_failed"] += 1
                continue
            stats["li_ok"] += 1
            li_jobs_all.extend(jobs)
            for j in jobs:
                consider(j)
    # LinkedIn tracked as one aggregate source (per-query volumes are too small
    # for stable anomaly detection).
    li_failed = None
    if stats["li_failed"] and not li_jobs_all:
        li_failed = f"{stats['li_failed']}/{stats['li_ok']+stats['li_failed']} queries failed"
    track_source("linkedin", li_jobs_all, failed=li_failed)

    # Curated job-list sources (GitHub repos / pages publishing fresh postings
    # as tables; configured during onboarding under `curated_sources`). Each is
    # tracked as its own health source; rows flow through the same dedupe,
    # newgrad/lane/location filters and recency window as everything else.
    def fetch_curated(src):
        try:
            name = src.get("name") or src["url"]
            jobs = fetch_curated_list(src["url"], src.get("sections", []))
            return name, jobs, None
        except Exception as e:  # noqa: BLE001
            return src.get("name") or src.get("url", "?"), [], e

    curated_cfg = cfg.get("curated_sources", []) or []
    if curated_cfg:
        with ThreadPoolExecutor(max_workers=4) as ex:
            for name, jobs, err in ex.map(fetch_curated, curated_cfg):
                src_name = f"curated:{name}"
                if err is not None:
                    track_source(src_name, [], failed=type(err).__name__)
                    continue
                track_source(src_name, jobs)
                for j in jobs:
                    j["source"] = src_name
                    consider(j)

    # Auto-scaling recency window: start tight (default 24h), expand stepwise
    # until min_candidates pool entries are in-window (or the max step hits).
    # --max-age-days overrides to a fixed window (one-time backfill behavior).
    disc_cfg = cfg.get("discovery", {})
    if args.max_age_days is not None:
        window_days = args.max_age_days
    else:
        steps = disc_cfg.get("window_steps_days", [1, 3, 7, 14, 30]) or [30]
        min_candidates = disc_cfg.get("min_candidates", 20)
        window_days = steps[-1]
        for w in steps:
            # Window scales on tier-1 (explicit new-grad) only, so tier-2
            # volume can never jam the window tight and crowd out tier-1.
            in_w = sum(1 for c in pool
                       if c.get("tier", 1) == 1
                       and (c["_age_h"] is None or c["_age_h"] <= w * 24))
            if in_w >= min_candidates:
                window_days = w
                break
            window_days = w
    stats["window_days"] = window_days
    windowed = [c for c in pool
                if c["_age_h"] is None or c["_age_h"] <= window_days * 24]

    prio = {name: i for i, name in enumerate(lanes_by_priority)}

    def sort_key(c):
        d = parse_date(c["_date"])
        return (c.get("tier", 1), prio.get(c["lane"], 99), -c["_loc"], -(d.toordinal() if d else 0))
    windowed.sort(key=sort_key)

    candidates = [{k: c[k] for k in ("company", "title", "location", "url", "date", "lane", "snippet", "source", "tier")}
                  for c in windowed[:args.max_candidates or disc_cfg.get("browse_target", 60)]]
    stats["tier2"] = sum(1 for c in candidates if c.get("tier") == 2)
    stats["candidates"] = len(candidates)
    elapsed = time.time() - t0

    # Incremental state: watermark, presence upsert, absence->closed marking.
    # Written only on real runs; --dry-run touches nothing on disk.
    run_id = datetime.now().strftime("%Y-%m-%d-%H%M")
    watermark = load_watermark()
    presence = load_presence()
    run_n = watermark.get("runs", 0)
    closed_marked = 0
    if not args.dry_run:
        run_n += 1
        for u in pulled_urls:
            presence[u] = {"run": run_n, "run_id": run_id}
        closed_marked = close_absent_jobs(presence, seen, run_n)
        watermark = {"last_successful_run": datetime.now().isoformat(timespec="seconds"),
                     "runs": run_n}
        with open(f"{SKILL_DIR}/state/discovery_watermark.json", "w") as f:
            json.dump(watermark, f, indent=1)
        with open(f"{SKILL_DIR}/state/job_presence.json", "w") as f:
            json.dump(presence, f, indent=1)
        with open(f"{SKILL_DIR}/state/seen_roles.json", "w") as f:
            json.dump(seen, f, indent=1)
        with open(f"{SKILL_DIR}/state/discovery_candidates.json", "w") as f:
            json.dump({"generated_at": datetime.now().isoformat(timespec="seconds"),
                       "candidates": candidates}, f, indent=1)

    # Source health: evaluate every run; persist only on real runs.
    health = load_health()
    health_alerts = []
    for name, s in src_stats.items():
        entry = health.get(name, {"pulled_hist": []})
        hist = entry.get("pulled_hist", [])
        status, note = eval_source_health(name, s["pulled"], s["newest_age_h"],
                                          s["failed"], hist)
        if not args.dry_run:
            hist = (hist + [s["pulled"]])[-HEALTH_HIST_LEN:]
            health[name] = {"pulled_hist": hist, "last_pulled": s["pulled"],
                            "last_newest_age_h": s["newest_age_h"],
                            "last_run": run_id, "status": status, "note": note}
        if status != "ok":
            health_alerts.append(f"{name}: {note}")
    if not args.dry_run:
        with open(f"{SKILL_DIR}/state/source_health.json", "w") as f:
            json.dump(health, f, indent=1)
    n_src = len(src_stats)
    n_ok = n_src - len(health_alerts)
    health_tok = f"ok({n_ok}/{n_src})" if not health_alerts else f"WARN({len(health_alerts)})"

    print(f"boards_ok={stats['boards_ok']} boards_failed={stats['boards_failed']} "
          f"li_queries_ok={stats['li_ok']}/{stats['li_ok']+stats['li_failed']} "
          f"jobs_pulled={stats['pulled']} deduped_skipped={stats['deduped']} "
          f"disqualified={stats['disqualified']} "
          f"senior_dropped={stats.get('senior_dropped', 0)} "
          f"window_d={stats['window_days']} "
          f"candidates={stats['candidates']} tier2={stats.get('tier2', 0)} elapsed_s={elapsed:.1f} "
          f"watermark={run_n} closed_marked={closed_marked} health={health_tok}")
    if failed_boards:
        print("failed_boards: " + ", ".join(failed_boards[:10]))
    if health_alerts:
        print("health_alerts: " + "; ".join(health_alerts[:10]))
    if args.dry_run:
        for c in candidates[:15]:
            t2 = " [T2]" if c.get("tier") == 2 else ""
            print(f"  - [{c['lane']}]{t2} {c['company']} — {c['title']} ({c['location']}, {c['date']})")

if __name__ == "__main__":
    main()
