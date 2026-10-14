# Job Done— Technical Report

**An autonomous, human-in-the-loop job-application pipeline.**
Repo: https://github.com/xf-mike/job-done · Published 2026-09-29

---

## Abstract

Job Done is an end-to-end automation loop that discovers job postings matching your profile, shortlists them, fills out applications, **submits them**, and tracks recruiter correspondence — **fully automatically by default**. The human is not the bottleneck: a standing submit authorization is confirmed once at onboarding (risks stated plainly), and a review gate remains available as an opt-out. It is distributed as a portable **skill** (a `SKILL.md` playbook plus scripts, templates, and onboarding docs) but what actually runs is a **pipeline**: scheduled agents, incremental state, a tracker spreadsheet, and a dedicated chat channel, operating four times a day without being asked.

The pipeline is domain-agnostic: lanes, title filters, target companies, and locations are all configuration. It was built and battle-tested on a **new-grad software-engineering hunt** (the deployment that produced the numbers cited in this report), which serves as the worked example throughout.

The system's central design bet is **token economics**: every stage is engineered to minimize LLM calls — API-first discovery, programmatic pre-filtering, batched judging, cached form structures, and an answer bank for free-text questions. The LLM is spent only where judgment is genuinely required. A full discovery sweep over ~7,000 postings costs on the order of a thousand tokens — against alternatives that render every posting in a browser and score each with its own LLM call, the difference is measured in orders of magnitude, not percent.

---

## 1. User Guide

### 1.1 What it is — and what it is not

Job Done automates the *legwork* of a job hunt (finding roles, filling forms, watching for replies) but never the *decisions*. Concretely:

- It **does** discover roles on a schedule, dedupe them, shortlist the best fits, pre-fill entire applications, and monitor your inbox for recruiter replies.
- It **does not** click a final Submit without your authorization — by default that's the standing `auto_submit` you confirmed at onboarding (risks explained); turn it off and every application parks at the review screen until you say "submit" / "submit all".
- It **does not** invent facts. Citizenship, dates, test scores, and demographics come from your profile or are left blank; unknown required fields are escalated, not guessed.

### 1.2 Installation & onboarding

Prerequisites: a personal AI agent environment (the pipeline was built on Muse), a Google account (for the tracker sheet + Gmail), and your resumes.

1. Clone the repo into your agent's skills folder.
2. Follow `ONBOARDING.md` in conversation with your agent — ten steps:
   - **Step 1 — Resumes.** Provide 1–3 resumes; the agent extracts your profile.
   - **Step 2 — Confirm + fill gaps.** You confirm the extracted profile and fill what's missing (start date, relocation, EEO, work authorization…).
   - **Step 3 — Target roles.** You discuss the hunt; the agent turns it into lanes (one per resume, with keywords + search queries), `title_include`/`title_exclude` filters, and industry/function tags.
   - **Step 4 — Brand set.** The agent *recommends* 15–25 companies from `references/board_directory.json` (a tagged pool of companies with verified public ATS-board APIs) instead of asking you to list companies; you confirm, it verifies each board API live, and writes your `references/company_boards.json`.
   - **Step 5 — Curated job lists.** The agent finds daily-updated community job lists (e.g. GitHub new-grad repos); you confirm; URLs go under `curated_sources:`.
   - **Step 6 — Connect Gmail (required, not optional).** Two reasons: recruiter-reply tracking, and one-time verification codes that some application sites demand mid-flow. Without it, those applications cannot complete.
   - **Step 7 — LinkedIn login (recommended).** User-takeover login — the assistant never sees the password; the session is shared across fill tasks.
   - **Step 8 — Storage.** The agent creates your private Google Sheet tracker ("Job Applications Tracker": an `Applications` tab and a `Run History` tab).
   - **Step 9 — Config.** Review the generated `config.yaml` (lanes, filters, schedule, locations, `submit_review_mode`, `blacklist`…).
   - **Step 10 — Go live.** The agent enables the scheduled runs and drops reports into a dedicated side chat.

Your `profile.yaml`, `config.yaml`, `references/company_boards.json`, and `state/` are gitignored — they never leave your machine.

### 1.3 Daily operation

Once live, the pipeline runs itself:

- **Discovery** — 4× daily (08:00 / 12:00 / 16:00 / 20:00 local). Each run pulls fresh postings, filters them, and — only if the shortlist is non-empty — messages you with company, title, location, lane, link, and a one-line fit note. Empty runs stay silent.
- **You select** — reply with "all", numbers, or names (skipped if `auto_select: true`).
- **Agent fills** — every selected application is completed end-to-end and parked before Submit.
- **You approve** — one combined review (`batch`, default) or one at a time (`per_application`). Say "submit all" / "submit" and the agent submits and logs.
- **Email watch** — every 6 hours a silent scan checks for recruiter replies, interview invites, and application confirmations; it pings you only on hits and updates the tracker's Status column.

### 1.4 Commands

| Command | Effect |
|---|---|
| `run the pipeline now` | Run Stage 1 (discovery) immediately |
| `pause pipeline` / `resume pipeline` | Disable / enable the discovery crons |
| `reset pipeline config` | Re-run onboarding (confirms before wiping profile/config) |

### 1.5 Configuration reference (`config.yaml`)

| Key | Default | Meaning |
|---|---|---|
| `lanes[].name/priority/keywords/queries` | — | Search lanes in priority order; each lane carries keywords (lane assignment), queries (LinkedIn search), and its resume |
| `discovery.title_include` | new-grad patterns | Regex fragments; a posting must match ≥1 (e.g. an experienced hire uses `senior|staff`) |
| `discovery.title_exclude` | `intern` | Regex fragments; any match on the title drops the posting |
| `discovery.browse_target` | 50 | Candidate ceiling the agent judges per run |
| `discovery.shortlist_size` | 20 | Roles surfaced to you per run |
| `discovery.window_steps_days` / `min_candidates` | [1,3,7,14,30] / 20 | Auto-scaling recency window: starts 24h, expands while too few candidates |
| `curated_sources` | — | Community job lists (name, table URL, sections) fetched every run |
| `submit_review_mode` | `batch` | `batch`: one combined review; `per_application`: review each as filled |
| `auto_select` | `false` | Skip the selection step and fill the whole shortlist |
| `blacklist.companies` | — | Never surface these companies |

---

## 2. Core Architecture

### 2.1 The six stages

```
Discover → Select → Prepare → Approve → Submit & Log → Track
   (agent)   (you)    (agent)    (you)      (agent)       (agent)
              ↺ scheduled 4×/day · email watch every 6h
```

State is the connective tissue: `state/seen_roles.json` (every judged URL + decision), `state/discovery_watermark.json` (run counter), `state/job_presence.json` (per-URL last-seen run), `state/discovery_candidates.json` (current run's pre-filtered set), `state/runs/` (shortlist history), `state/qa_bank.json` (approved free-text answers), `state/form_cache/` (form structures).

### 2.2 Discovery sources — and how each is handled

**2.2.1 Company ATS boards — the highest-recall source.**
Each board is registered in the user's `references/company_boards.json` with its platform token (Greenhouse board token or Ashby org slug), verified live against the vendor's *public* JSON API — no authentication, no scraping of rendered pages. Boards are fetched concurrently (thread pool, per-board failure isolation: one dead board never stalls the run). In the reference deployment, 40 boards pull ~7,000 raw listings in ~25 seconds.

- *Greenhouse handling:* the public board API for listings; the documented `?questions=true` job endpoint for full application-form structure (labels, types, required flags) — this powers the form cache (§2.6), no HTML parsing involved.
- *Ashby handling:* the public posting API; form introspection is best-effort (Ashby exposes less structure publicly), so unknown fields fall back to individual browser attention.

How a user's board list is built (ONBOARDING.md Step 4): the repo ships `references/board_directory.json`, a tagged pool of companies with verified public ATS-board APIs (industry/function tags). The agent recommends 15–25 companies matching the user's Step-3 tags, the user confirms, and each token is re-verified live before being written to the user's gitignored `references/company_boards.json`. A starter template (`templates/company_boards.template.json`, ~10 generic companies) gives coverage from minute one. Runtime source-health monitoring warns if a board goes quiet (company switched ATS, token died).

**2.2.2 LinkedIn guest API — 8 lane queries × 2 pages × 10 cards.**
The unauthenticated `jobs-guest` search endpoint is queried per lane query. Result cards are parsed with targeted regexes (title, company, location, posting URL, timestamp). Relative timestamps ("5 hours ago", "2 days ago") are converted to **hours** at parse time — this is what enables true hour-precision recency filtering (§2.3). Short per-source timeouts: if LinkedIn stalls, the run continues without it.

**2.2.3 Web supplement — capped, not the default.**
Some targets expose no stable public API (notably YC Jobs). A small supplement — 2–3 targeted searches, at most ~10 page opens per run, every URL checked against the seen-cache first — covers these. This path exists deliberately as a *minority* supplement: the old approach of browsing dozens of pages per run was measured at ~10× the token cost and is never the primary method.

**2.2.4 Curated job lists — community tables as a source.**
Community-maintained job lists (e.g. GitHub repos publishing daily new-grad postings as tables) aggregate companies with no public ATS API — the exact gap board polling can't cover. `fetch_curated_list` parses HTML `<table>` and markdown tables (Company | Role | Location | Application | Age), extracts the direct application URL (skipping tracker/affiliate links), computes posting age from the Age column, skips closed (🔒) rows, and resolves "↳" continuation rows to the previous company. Rows then flow through the identical dedupe/filter/recency/health path as every other source. Lists are registered under `curated_sources:` during onboarding (user-confirmed); a format drift that yields 0 parseable rows trips the health check's WARN.

### 2.3 The filter cascade (programmatic, zero LLM)

Every pulled listing passes through `consider()` in `discover.py`, in this order:

1. **Dedupe first** — URL seen in `state/seen_roles.json` (or earlier in this run) → dropped. **Semantic dedupe** goes further: requisition IDs extracted from URLs (`gh_jid`, `jobId`, `JR-…`) and normalized (company, title) pairs catch the same posting under a different URL and roles the user already applied to outside the pipeline. Nothing is ever re-read.
2. **Blacklist** — user-declared never-apply companies.
3. **Disqualifier prefilter** — obvious hard disqualifiers on title + snippet ("no sponsorship", "not eligible for F1", "record a video") drop the listing before judging; the judge scans full posting text for the rest.
4. **Manual-likely flagging** — URLs matching known human-verification flows (`ycombinator.com`, `icims`) are tagged `manual_likely` so the shortlist warns instead of burning a fill attempt.
5. **Title exclude** — `discovery.title_exclude` regexes against the title (default: internship postings). Any match drops the listing.
6. **Title include** — `discovery.title_include` regexes over title + snippet. Listings matching none are dropped *before* any LLM sees them. (In the reference new-grad deployment this is the new-grad signal: "new grad", "entry level", class-year markers, etc.)
7. **Lane keyword match** — each lane's `keywords` (config) matched against title + snippet; assigned to the highest-priority matching lane; unmatched listings are dropped.
8. **Recency, hour-precision, lenient** — the auto-scaling window (`discovery.window_steps_days`, default starting 24h and expanding while fewer than `discovery.min_candidates` roles are in-window) compared against the posting's age in hours. LinkedIn relative times are exact; day-granularity board dates assume end-of-day (minimum possible age — a posting dated yesterday still passes in the morning run); unknown dates are always kept. Never drop on missing data. (`--max-age-days` overrides to a fixed window for one-time backfills.)
9. **Location scoring** — remote / California / hybrid preferred; `relocation=yes` keeps other US locations eligible but deprioritized; zero-score locations are dropped.

Survivors (typically ~10–15 of ~7,000) are written to `state/discovery_candidates.json` — a ~1.2k-token file. Only then does the LLM get involved.

### 2.4 Batched judging (the LLM's only discovery job)

The agent scores candidates in **batches of ~10 per LLM call** from title + company + location + date + snippet — never one call per role. Clear cases are decided from the snippet alone; genuinely borderline roles may open the posting page, capped at ~5 page opens per run. This is the "cascade": ~7,000 → ~14 programmatic → ~10 judged in 1–2 LLM calls, roughly a **10× reduction** in judging calls versus per-role evaluation.

Every judged role is persisted to `state/seen_roles.json` with `"decision": "shortlisted"` or `"decision": "rejected"` + reason — so future runs never pay to reconsider it.

### 2.4b Token budget (measured, reference deployment)

| Stage | Cost per run |
|---|---|
| Discovery (`discover.py`) | **0 LLM calls.** ~7,000 postings pulled via public JSON APIs + LinkedIn guest endpoints; ~1.2k tokens of candidate JSON emitted; ~25–35s wall time. |
| Judging | **1–2 LLM calls** (10 roles per call, ~10× fewer than per-role scoring). Borderline roles may open ≤5 posting pages. |
| Filling (per application) | **0 re-parses.** Form structure comes from `form_cache.py`; only genuinely new free-text questions cost one LLM draft, and only once — approved answers are banked (`qa_match.py`, ≥0.75 reuse) forever after. |
| Tracking | **0 LLM.** `gmail_scan.py` is fully programmatic (ATS-domain allowlist + subject rules), watermarked and incremental. |

The comparison point is the dominant alternative architecture — render every posting in a real browser, score each with its own LLM call, re-parse every form from scratch. Against that, Job Done is cheaper by roughly two orders of magnitude per discovered posting, which is what makes 4×-daily scheduled operation economically sane.

### 2.5 Incremental state: watermark, presence, and silent closes

- **Watermark** (`discovery_watermark.json`): run counter + last successful timestamp; lets consecutive runs behave incrementally.
- **Presence** (`job_presence.json`): every pulled URL is upserted with its last-seen run — *even URLs filtered out or deduped*. A posting absent for **3 consecutive runs** whose seen-decision is not `applied`/`shortlisted` is marked `"closed"` ("no longer listed"). ATS boards silently drop closed postings, so this is how the pipeline detects dead roles without re-reading them.
- **Dedupe is sacred**: the seen-cache is consulted before any URL is opened, in discovery and in the web supplement alike.

### 2.6 Application filling

**Form structure cache** (`scripts/form_cache.py` → `state/form_cache/`): on a cache miss, the board's public form structure is fetched once (Greenhouse `?questions=true`; Ashby/Lever best-effort) and the label→type→required mapping is reused for every future application at that board. The LLM never re-parses a whole form twice.

**Q&A answer bank** (`scripts/qa_match.py` → `state/qa_bank.json`): each free-text/essay question is matched against previously *user-approved* answers. Matching is deliberately **conservative** — difflib similarity ≥ 0.75 *and* a keyword-overlap gate, with explicit exclusions for temporal questions ("earliest/latest"), relocation, and sponsorship: a missed match costs one extra LLM call, but a wrong auto-answer is never risked. Unapproved drafts never enter the bank; CSRF tokens, tracking IDs, and captcha widgets are never sent to the LLM.

**Standing answers** (`references/standing-answers.md`): one-time user decisions applied silently forever — e.g. education entered manually (never trusted to resume auto-parse), relocation = yes, work-authorization wording, EEO selections.

**Filling execution** is browser-based: fields per the cached structure, resume + transcript uploaded automatically (routine — never permission-gated), education typed by hand, then the agent **parks before Submit**. Direct HTTP POST submission was evaluated and *rejected* (documented in `SKILL.md` §Stage 5): Greenhouse's documented POST needs an employer API key, its hosted form runs invisible reCAPTCHA Enterprise (bot-scored posts get HTTP 428 `captcha-failed` plus a two-phase email code flow), and resumes upload via presigned S3 — pure-HTTP submission cannot pass; Ashby needs reCAPTCHA + CSRF with v3 spam scoring.

### 2.7 Review & submit gating

Two automation levels, confirmed at onboarding with risks explained:

- **Full-auto (default):** `auto_select: true` sends the whole judged shortlist straight to filling; `auto_submit: true` submits everything once filled, with no review screen. The standing authorization replaces per-run approval. A submit report (confirmations, IDs, blocked/manual list) is always delivered — logging is never skipped.
- **Human gate:** turn either switch off. `submit_review_mode: batch` (default when gated) fills *all* selected roles, then compiles one combined review — company, role, every field value, every banked/drafted answer — for a single approval ("submit all" or a named subset). `per_application` hands each review over as its role is filled (smaller turns, stoppable early).

The design bet (2026-09-29): a silent failure the user reads about later beats an interruption mid-day. Attempt caps (3 tries per action), the pre-submit checklist, the posting-text disqualifier scan, and semantic dedup are the guardrails that make full-auto responsible instead of reckless.

Submissions are logged with confirmation text, timestamp, and reference ID; the tracker gets one row per application; the URL is marked `"decision": "applied"`.

### 2.8 Email: reply tracking + verification codes

**Reply tracking** (`scripts/gmail_scan.py`, every 6h, silent unless hits): watermarked and incremental — only mail newer than the last scan is read. Zero-LLM pre-filtering: sender domain must be in a 12-domain ATS allowlist (`ashbyhq.com`, `greenhouse-mail.io`, …), verification-code subjects are excluded (they belong to the code flow below). Hits are classified by subject into `confirmation` / `interview` / `rejection` / `offer` / `other`, matched read-only against the tracker's company list. **Bodies are never printed, messages are never marked/archived/trashed.** Hits update the tracker's Status column and notify the user.

**Verification codes** (on demand only): some sites gate registration or submission behind an emailed code. When a browser fill task parks at such a step, it must stop and report the site URL, the exact step, and the masked recipient shown on the page — never guess. The orchestrator then does *one* targeted lookup for that sender's newest message (~15 min window), reads only that message, and hands the code to the waiting task for that step only. Hard rules: one code per step, never reused, never written to files/memory/state/logs, never speculative inbox sweeps. Filling the code does not replace submit approval.

### 2.9 What the pipeline deliberately does not do

- No credential or session reuse across sites; no CAPTCHA solving as a feature (human-takeover or site-native flows only).
- No silent submission *without authorization*: full-auto mode runs on the standing `auto_submit` the user confirmed at onboarding (risks explained). The approval step can be skipped; the authorization and the logging cannot.
- No private data in the repo: `profile.yaml`, `config.yaml`, `state/` are gitignored; the public repo carries only the playbook.

---

## 3. Survey of related open-source work

*Research date: 2026-09-29. Star counts were read from live GitHub pages or search-indexed READMEs on that date; figures taken from secondary sources are flagged. Technique descriptions are at README/docs fidelity unless noted. "No prior art found" claims are scoped to this survey, not an exhaustive literature review.*

### 3.1 Job discovery and scraping

**JobSpy** ([speedyapply/JobSpy](https://github.com/speedyapply/JobSpy), ~4.4k stars, MIT) is a Python library (`pip install python-jobspy`) that scrapes LinkedIn, Indeed, Glassdoor, Google Jobs, ZipRecruiter and others concurrently into a pandas DataFrame. It is the closest prior art to our LinkedIn leg — we hit the same unauthenticated `jobs-guest` endpoints JobSpy uses — and its `hours_old` recency filter parallels ours. Its README is also unusually honest about anti-bot reality (LinkedIn rate-limits around page 10 per IP; proxies are a must). Difference: JobSpy is a scraping *library*, not a pipeline — no LLM judging, no incremental state, no dedupe cache, no application filling, and no ATS-board (Greenhouse/Ashby) coverage at all.

**ATS board API scrapers** are a crowded, mature cluster: [shunsukefuruyama/ats-jobs](https://github.com/shunsukefuruyama/ats-jobs) (12 ATS platforms, "no browser, no HTML parsing, no API keys, no proxies"), [ethancha0/ats-scraper](https://github.com/ethancha0/ats-scraper), [kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers), [datascry/openroles](https://github.com/datascry/openroles) (54 ATS adapters), and [kirbx01/hardware-atlas](https://github.com/kirbx01/hardware-atlas) (stdlib-only Python + thread-pool fetching of Greenhouse/Lever/Ashby boards — very close to our `discover.py` in implementation style). They all call the same public endpoints we use. An independent 2026 research spec additionally confirms two facts we established ourselves: Greenhouse `?questions=true` is still live, and **no candidate-usable submit API exists on any ATS** (Greenhouse docs warn a direct POST would expose the employer's secret key).

### 3.2 Full application agents

**AIHawk** ([feder-cr/Jobs_Applier_AI_Agent_AIHawk](https://github.com/feder-cr/Jobs_Applier_AI_Agent_AIHawk), ~31k stars, now archived) was the flagship 2024 "AI applies to jobs for you" agent: Selenium/Playwright on LinkedIn, LLM-generated tailored resumes and answers, stealth via a patched-Firefox engine, and **fully autonomous submission**. It died by LinkedIn cease-and-desist (confirmed by the author's own README). Notably, the author's successor project now states "do not submit anything a human has not read" — the ecosystem is converging toward the human-gate position we built in from day one.

**ApplyPilot** ([Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot), AGPL-3.0, active 2026) is our closest architectural relative: a 6-stage pipeline (Discover → Enrich → Score → Tailor → Cover → Apply) using JobSpy + 48 Workday portals + career-site scrapers, **LLM scoring every job 1–10 against the resume**, per-job resume tailoring, and Playwright submission. Differences: autonomous submission (we gate on human approval), scraper-heavy discovery (we are API-first, no HTML parsing, no proxies), and no equivalent of our batched judging or incremental watermark/presence tracking.

**career-ops** ([santifer/career-ops](https://github.com/santifer/career-ops), MIT, reportedly ~43k stars — secondary source, unverified) is a YAML-config-driven agentic job-search system that plugs into Claude Code/Codex: skill modes, role evaluation, tailored CVs, batch processing, tracking. Its "portable skill + config + tracking" packaging is the closest prior art to our SKILL.md/ONBOARDING.md/templates distribution model. It is a general agentic system riding on coding CLIs; ours is a narrower self-contained pipeline with its own scripts and cron-scheduled runs.

### 3.3 LinkedIn Easy-Apply bots

A family of independent bots sharing one pattern (Selenium/Playwright + question answering + logging): [rajarshibose19/linkedin-easyapply-bot](https://github.com/rajarshibose19/linkedin-easyapply-bot) (ships gitignored `questions.py`/`personals.py` templates — **static answer templates are prior art for our Q&A bank concept** — and a `pause_before_submit` human-gate option); [rsk2111999/linkedin-easy-apply-bot](https://github.com/rsk2111999/linkedin-easy-apply-bot) (Chrome extension, batch apply); [sideeffects69/magic-apply-jobs](https://github.com/sideeffects69/magic-apply-jobs) (Easy Apply *plus* company career sites, keeps a failed-jobs list with reasons for manual completion); [ganeshraj-k/linkedin_easyapply_bot_RAG](https://github.com/ganeshraj-k/linkedin_easyapply_bot_RAG) (RAG answers from the resume; `questions.json` **auto-populates as questions are encountered** — a *learning* answer bank, nearest to ours, but implicit where ours requires pre-approved answers and deliberately under-matches); [nadeemahmad3/linkedin-autoapply-agent](https://github.com/nadeemahmad3/linkedin-autoapply-agent) (Playwright pre-fills, **the user clicks Submit — "the bot never submits on your behalf"** — closest prior art to our human-gate philosophy); [sentient-engineering/jobber](https://github.com/sentient-engineering/jobber) (drives the user's *real* Chrome session as its anti-detection strategy — a different threat model from ours: they optimize for not getting flagged, we optimize for not submitting anything wrong).

### 3.4 LLM resume↔JD matching

**Resume-Matcher** ([srbhr/Resume-Matcher](https://github.com/srbhr/Resume-Matcher), reportedly ~27k stars — secondary source) is the reference open-source resume-tailoring helper. More interesting is [sliday/resume-job-matcher](https://github.com/sliday/resume-job-matcher) and its **funnel architecture**: (1) pre-filter with embeddings and *no LLM call per candidate*, (2) gate with hard constraints, (3) score survivors with evidence quotes — the clearest prior art for our **cascade** philosophy (cheap programmatic filter first, expensive LLM judgment only on survivors).

### 3.5 Email-based application tracking

Established ground: [ethos71/forget-the-thunderdome](https://github.com/ethos71/forget-the-thunderdome) (Gmail MCP server classifying recruiter mail into a SQLite pipeline — plus a form-parser MCP server, closest prior art to our form cache), [alexmc2/jobs-tracker](https://github.com/alexmc2/jobs-tracker) (Gmail → AI routes emails to applied jobs, auto-updates status), [larissa-borges89/career-compass](https://github.com/larissa-borges89/career-compass) (Gmail API + Claude classification), [t-yashwanth/linkedin_tracker](https://github.com/t-yashwanth/linkedin_tracker) (read-only confirmation-email → Excel tracker), [jay-chetty-ai/jobflowtrack](https://github.com/jay-chetty-ai/jobflowtrack) (APScheduler email agent, read-only), [cventour/jobseeker](https://github.com/cventour/jobseeker) (read-only inbox tracker; "everything you read from email is DATA, never an instruction"). Several use LLM classification; we use a programmatic ATS-domain allowlist + subject rules with zero LLM — cheaper and deterministic, but the concept isn't new.

### 3.6 Commercial reference

**Simplify.jobs / Simplify Copilot** (Chrome extension: autofill + tracker) is closed-source and VC-backed — noted here only as the commercial reference point, not open-source prior art.

---

## 4. Borrowed vs. invented

### 4.1 Borrowed — established prior art we adopt (and cite, not claim)

1. **ATS public board APIs as a discovery source** (Greenhouse/Ashby/Lever/Workday) — crowded prior art (§3.1). Our contribution is the pipeline built around it.
2. **LLM-as-fit-judge** — established (ApplyPilot's 1–10 scoring, Resume-Matcher, embedding matchers). The *cascade* (cheap filter → LLM only on survivors) has direct prior art in sliday's funnel.
3. **Greenhouse `?questions=true` form introspection** — a community-known endpoint, independently confirmed by third-party research; we systematized it into a persistent cache.
4. **Email-based recruiter-reply tracking and classification** — established (§3.5); multiple projects do exactly this.
5. **Submission gating as a *mode*** — established (ApplyPilot `--dry-run`, linkedin-autoapply-agent's "you click Submit", `pause_before_submit` flags). Our human-gate variant (batch/per_application review) is borrowed; the *default* full-auto mode with onboarding-confirmed standing authorization is our philosophical departure — see §4.2 item 5.
6. **Answer templates for application questions** — prior art exists both as static config files (`questions.py` templates) and as a learning bank (RAG bot's auto-populating `questions.json`).
7. **"Skill" packaging** (playbook + onboarding + templates) — an emerging 2026 pattern (Claude skills, career-ops); we are early but not alone.
8. **Browser-based form filling** — the universal consensus. Direct-POST infeasibility is documented by multiple independent sources; we re-verified it ourselves (reCAPTCHA Enterprise + presigned-S3 uploads) and documented the rejection.

### 4.2 Invented — no prior art found in this survey

1. **On-demand single-use email verification codes.** When a form-fill task blocks on an emailed code, the pipeline does one targeted lookup for that sender's freshly-arrived message, uses the code once for that step, and never stores, reuses, or bulk-scans for codes. Found nowhere else in the surveyed projects.
2. **Batched LLM judging (10 jobs per call) as an explicit cost control, combined with incremental watermark + presence tracking** (absent-3-runs → closed) and a URL dedupe cache. Each element has cousins; the packaged combination driving *scheduled bulk discovery* appears new.
3. **Conservative Q&A answer bank with a deliberate under-match safety policy.** Answer banks exist; the safety framing — prefer one extra LLM call over one wrong auto-answer, with explicit exclusions for temporal/relocation/sponsorship questions — is what we found no prior art for.
4. **Hour-precision lenient recency.** LinkedIn "X hours ago" converted to exact hours; day-granularity board dates treated as end-of-day (minimum possible age) so evening postings survive the morning run. Minor, but undocumented elsewhere as far as this survey found.
5. **Full-auto as the default, with the human gate as the opt-out.** Every surveyed tool treats human approval as the safe default and automation as the scary option. We inverted it: silent full-auto is the default posture, the user confirms it once at onboarding with the risks stated plainly, and the engineering goes into making the default *responsible* (attempt caps, pre-submit checklist, disqualifier scan, semantic dedup, always-on logging) rather than into building a prettier gate. The design bet: a failure the user reads about later beats an interruption mid-day.

### 4.3 Honest limits of these claims

Star counts from search snippets may be weeks stale; two figures (career-ops ~43k, JobSpy-fork "10k") come from secondary sources and conflict with a live page read, and we report the conflict rather than picking a number. We read READMEs, docs, and secondary surveys — not full source trees — for AIHawk, ApplyPilot, and career-ops, so technique descriptions are at README fidelity. "No prior art" is scoped to what this survey surfaced on 2026-09-29.

---

## Appendix A — Repository layout

```
SKILL.md                  # the playbook (this report's subject)
ONBOARDING.md             # 10-step conversational setup
discover.py               # API-first discovery + filter cascade (stdlib only)
scripts/
  qa_match.py             # answer-bank fuzzy matching
  form_cache.py           # ATS form-structure cache
  gmail_scan.py           # watermarked reply tracking
references/
  board_directory.json    # tagged pool of verified ATS boards (recommendation source)
  standing-answers.md     # one-time user decisions, applied silently
templates/
  config.template.yaml    # shareable config (no personal data)
  company_boards.template.json  # starter board list → user's references/company_boards.json
  profile.template.yaml
```

`profile.yaml`, `config.yaml`, `references/company_boards.json`, and `state/` are gitignored and never published.
