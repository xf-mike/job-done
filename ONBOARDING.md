# Job Done— Onboarding

Shareable setup script. Run it as a conversation with a new user — step by step, confirming as you go. Do not dump all questions at once. The pipeline itself is domain-agnostic (it works for any job hunt); the examples below use a software-engineering hunt for concreteness.

## Conversation rules (apply to every step)

- **Roadmap first.** Before Step 1, list all 10 steps in one short message (one line each) with a time estimate.
- **What + why at every step.** Begin every step with its header ("Step 2/10 — 补全档案") plus one or two sentences: what we're about to do and why it matters for the pipeline. No question ever arrives without that framing — the user should always know why they're being asked.
- **Premise before follow-up.** Never ask a detail question whose premise isn't established. If a yes/no answer would make the follow-ups moot, ask the yes/no first. (Example: "还要继续投这家公司吗？" comes before "具体投了哪些岗位？把链接给我".)
- **Ground dates against today.** Compare every resume date with the current date and phrase the inference as a check, never an assertion ("按今天算你已毕业约半年，对吗？" — not "你明年三月毕业").
- **Present, don't guess.** Cite the source of extracted values ("简历上写的是 X") — never "我猜是…".
- **Confirm in user terms.** State what changed ("已把申请邮箱改成 inari2021drexel@gmail.com"), never log lines ("更新邮箱Pipeline配置").
- **Stay on task.** No unsolicited advice, no security tips, no links the user didn't ask for — especially nothing touching their other accounts.
- **Text-first choices.** Every binary/multiple choice is written out in plain text in the message. Widgets are optional decoration only; if the user answers in free text ("好了", "A和B都开"), map it from what was recorded — never ask "你选的是哪一个？".

## Step 1 — Resumes

Ask the user to upload 1–3 resumes. Read each one and extract:

- name, email, phone, address, location
- education (school, degree, field, dates, GPA)
- work authorization + sponsorship needs (ask if unclear)
- links (LinkedIn, GitHub, personal site)
- skills and standout projects

Store the files (note their paths). Copy `templates/profile.template.yaml` → `profile.yaml` and fill it in, then copy `templates/standing-answers.template.md` → `references/standing-answers.md` and adjust the rules to the user's situation.

## Step 2 — Confirm + fill gaps

Present the extracted profile back. Ask for anything missing:

- preferred name, earliest start date
- relocation willingness
- EEO answers (gender / race / veteran / disability)
- interview recording consent
- export-control status if relevant
- transcript file, if they have one
- **date of birth (full YYYY-MM-DD; some forms require month/day only) — collect once here, never ask mid-flow.** Stored only in local `profile.yaml` (gitignored, never leaves the machine) — say so when asking.
- **street address** — some forms make Address Line 1 required; without it those roles are blocked (seen 2026-09-29: Varsity Brands). The resume often lacks it, so ask explicitly. If the user declines to provide it, record "roles requiring a street address are skipped" and move on — never invent one.
- **LinkedIn profile URL** — ask if the resume doesn't list one.
- **salary expectation range** — the standing range to fill in when a posting lists no salary range (e.g. $110k–$140k). Stored in `profile.yaml` as `salary_expectation`; a number is never invented mid-flow.
- **work history with start/end dates (YYYY-MM) for every role — forms require these and they are never invented**
- **companies/roles already applied to** — ask outright ("any jobs you've already applied to on your own?"). Seed each into the dedupe cache so discovery never re-suggests them:
  `python3 scripts/seed_applied.py --entry "Company|Title" [--entry "Company|Title|URL"]`
  (This is the Amazon-ADC lesson: URL-only dedup can't catch "I already applied there.")
  Ordering: for each named company, FIRST ask whether they still want to target it at all. If no → it goes on the `blacklist`, no role details needed. Only if yes, ask for the specific titles/links to seed.
- standardized test scores only if a target form is known to require them (note "none" is fine)

The user confirms or corrects everything before moving on.

## Step 3 — Target roles

Discuss what the user is hunting for and turn it into pipeline configuration. Do not ask them to list target companies — that comes from recommendation in Step 4.

1. **Lanes.** Propose 1 lane per resume (e.g. for a SWE: backend/infra, AI platform, frontend). Each lane gets: a name, its resume, a priority, 8–15 **keywords** (plain phrases matched against title + snippet to assign the lane), and 2–3 LinkedIn **search queries**.
2. **Title filters.** Agree on `title_include` (must match at least one — e.g. a new grad's `new grad | entry level | junior`; an experienced hire's `senior | staff`) and `title_exclude` (any match drops the posting — e.g. `intern` for full-time hunts).
3. **Industry / function tags.** Note 1–3 industry tags and function tags describing the hunt (e.g. industries `ai_infra`, `fintech`; functions `swe`, `sre`). These drive the brand recommendation in Step 4. The tag vocabulary lives in `references/board_directory.json` — reuse its tags when they fit.

## Step 4 — Brand set (recommended, not interrogated)

**What + why:** we're building the list of company career pages the pipeline polls directly on every run. These public ATS boards (Greenhouse/Ashby JSON APIs — complete, structured, no login) are the highest-recall discovery source, which is why they get their own list. **Say this to the user explicitly:** confirming this list does NOT fence the hunt in — roles also arrive from LinkedIn keyword search (the Step 3 queries) and curated community job lists (Step 5), which catch companies with no public board at all. Cutting a company here only removes one direct-poll source; it never blocks that company's roles from surfacing via the other sources.

Users rarely have a target-company list ready, so **recommend one** instead of asking. The pipeline polls company ATS boards directly (public Greenhouse/Ashby JSON APIs — complete, structured, no login), which beats keyword search on recall for the companies it covers.

1. Copy `templates/company_boards.template.json` → `references/company_boards.json` (starter set of ~10 generic companies with stable public APIs — the user has coverage from minute one).
2. From `references/board_directory.json` (49+ companies with verified public ATS APIs, tagged by industry/function), pick 15–25 whose tags overlap the Step 3 tags.
3. Propose the list grouped by category (company names inline, one short line per group at most — never a 20+ item pitch list). They confirm, cut, or add names with one question. In that same message, restate the framing in one line: this list only decides which career pages get polled directly — the search itself is not limited to these companies.
4. **Verify live** before saving: hit each confirmed company's board API (`https://boards-api.greenhouse.io/v1/boards/{token}/jobs` or `https://api.ashbyhq.com/posting-api/job-board/{token}`); drop any token that fails and tell the user.
5. Merge the verified picks into `references/company_boards.json` (dedupe by token).

`references/company_boards.json` is the user's own list — gitignored, never committed. The directory is the shared pool; runtime source-health monitoring warns if a board goes quiet (company switched ATS, token died).

## Step 5 — Curated job lists (recommended)

API polling covers companies with public ATS boards; LinkedIn covers the rest best-effort. Community-curated lists (GitHub repos updated daily with postings) fill the remaining gap — they aggregate companies with no public API at all. During this step:

1. Based on the resumes (Step 1) and target roles (Step 3), search GitHub for daily-updated job repos (e.g. "new grad positions 2026").
2. Open the top candidates and check: updated recently, has a parseable table (Company | Role | Location | Application | Age — HTML `<table>` or markdown both work), covers the user's field.
3. Propose 1–3 to the user with a one-line description each; the user confirms.
4. Note the confirmed URLs + sections to include (e.g. "Software Engineering", "Data Science") — they are written under `curated_sources:` in Step 9.

The pipeline fetches these tables every run and treats rows like any other source: dedupe, recency window, health monitoring. If a list's format drifts (0 parseable rows), the health check warns.

## Step 6 — Connect Gmail (required, not optional)

The pipeline needs mailbox access for two things — this step cannot be skipped:

(a) **Reply tracking.** Recruiter replies, interview invitations, and application confirmation emails are detected automatically (the tracker's status updates depend on it). No mailbox access → no tracking.
(b) **Verification codes.** Some application sites require an email verification code during registration or before submission. Without mailbox access those applications cannot be completed.

Which mailbox: the one the user puts on applications — usually the email already extracted in Step 1. Confirm it with them by citing the source ("your resume lists X — is that the one you apply with?"), never as a guess. If they connect a different mailbox, update the application email in `profile.yaml` and confirm the change explicitly in user terms.

Follow your agent platform's Gmail skill/connector standard connect flow:

1. Run `hatch_gws_cli gmail status`.
2. If it reports not connected, it returns a `connect_url` — post it to the user exactly as `[Connect Gmail](<connect_url>)` and wait for them to finish authorizing. Never invent a URL, never send them to Settings.
3. Verify with one read-only check (e.g. reading the mailbox profile) that commands return data before moving on.

Set expectations in one or two sentences, in the user's own words: verification codes are fetched **on demand only**. The pipeline reads Gmail only when a form-fill task parks at a verification step and reports back the site URL and the masked recipient shown on the page; then it does one targeted lookup for that single freshly-arrived message, uses the code once for that step, and never stores it, never reuses it, and never sweeps the inbox for codes.

## Step 7 — LinkedIn login (recommended if LinkedIn is a discovery source)

Roles discovered from LinkedIn need one click on "Apply" — and LinkedIn gates that behind sign-in. The pipeline handles this with a **user-takeover login** (the assistant never sees the password).

**Order matters: disclose first, then ask.** Before anything else, state the tradeoff — LinkedIn's ToS technically prohibits automated access, so there is a small account-risk — and ask whether to proceed or skip LinkedIn-Apply. Only if they say proceed:

1. Spawn a browser task that opens https://www.linkedin.com/login and parks, asking the user to take over.
2. The user opens the task's browser panel, takes over, and signs in with their own credentials (including any 2FA / verification challenge), then ends the takeover and confirms.
3. Verify the login (linkedin.com/feed shows a logged-in homepage, not a login wall), then close the task. The session persists in the shared browser profile, so all later fill tasks reuse it.
4. If a fill task ever reports "linkedin session expired", repeat this step.

All browser tasks share one Chromium profile — the LinkedIn session is shared across tasks. Be upfront: LinkedIn's ToS technically prohibits automated access, so there is a small account-risk tradeoff; let the user decide.

## Step 8 — Storage

Recommend: **Google Sheet** (human-facing tracker — shareable, phone-friendly) + **local `state/seen_roles.json`** (machine dedupe cache so browsed roles are never re-read). Alternative: local-only if they decline Google.

If Sheet: connect Google Sheets, create the tracker with columns:
`Date | Company | Role | Location | Lane | Link | Confirmation | App ID | Status | Notes`
Save the spreadsheet ID in `state/tracker.json`.

## Step 9 — Config

Copy `templates/config.template.yaml` → `config.yaml` and fill it in from everything agreed above (the template's comments explain every field):

- lanes (name, resume, priority, **keywords**, queries) from Step 3
- `discovery.title_include` / `title_exclude` from Step 3
- `curated_sources` from Step 5
- run frequency and times, timezone
- roles per run (`shortlist_size`), browse target (`browse_target`)
- locations: preferred, acceptable, excluded
- companies to never apply to (`blacklist` — keep small; already-applied roles are deduped via `state/seen_roles.json`, not here)
- submit review mode: `batch` (fill all → one combined review → "submit all") or `per_application` (fill → review → approve → submit, one role at a time)
- posting recency: the auto-scaling window (`window_steps_days`, `min_candidates`) is the primary control; `max_post_age_days` is a legacy fallback
- auto-select: skip the role-picking step and go straight to filling? (`auto_select` — default true; confirm at onboarding with risks explained)
- auto-submit: submit all filled applications with no review screen? (`auto_submit` — default true; confirm at onboarding with risks explained; when false, `submit_review_mode` applies)
- grad-date eligibility rules per lane if relevant

### Automation level (confirm both explicitly — explain the risks first)

The pipeline can run end-to-end with zero interruptions. Present both switches
with their risks in plain language and get an explicit yes/no for each. The
template defaults are the aggressive ones (`true`/`true`); the user may turn
either off. Write both options out as plain text in the message (A/B with their
risk summaries) — never widget-only. If the user answers in free text, map it
from what was recorded; never ask "which one did you pick?".

- **A. Auto-fill the shortlist** (`auto_select`, default true): every role that
  passes judging goes straight to form-filling — no picking from a list.
  Risk: a misjudged role gets filled (wasted effort, but nothing is submitted
  yet — the blast radius is small).
- **B. Auto-submit after filling** (`auto_submit`, default true): once all
  forms are filled, they are submitted immediately with no review screen.
  Risks, stated plainly:
  1. A prefill error or wrong field goes out uncaught — a submitted
     application can't be taken back. (Mitigated by the pre-submit checklist
     and standing answers, not eliminated.)
  2. A misjudged role gets applied to without the user ever seeing it.
     (Mitigated by the posting-text disqualifier scan and semantic dedup.)
  3. Duplicate applications if dedup misses — dangerous where a company caps
     applications (e.g. ByteDance's 2-role limit for new grads).
  A post-submit report (confirmations, IDs, blocked/manual list) is always
  delivered either way — only the approval step is skipped, never the logging.

Record the answers in `config.yaml`. If the user turns B off, also confirm
`submit_review_mode` (batch vs per_application).

### Operating parameters (confirm explicitly — this is the pipeline's basic contract)

The automation switches above are not the whole story. Before the first run,
confirm three more things with the user in plain language — these are the
pipeline's basic operating parameters, and they must be the user's choice,
never silently inherited template defaults:

1. **When it runs (schedule).** State the proposed times explicitly (default:
   4 discovery sweeps/day at 08:00, 12:00, 16:00, 20:00 local time). The user
   can change the times, drop to 2/day, or go manual-only. Whatever they agree
   on is what the crons get — never assume the default. Each run ends with its
   own report, so more runs also means more report messages.
2. **How many fill attempts per run (batch size).** `discovery.shortlist_size`
   (default 20): at most this many judged roles get form-fill attempts per
   sweep. Frame it as an *attempt* budget — the user should know the number
   going in, because it bounds both the work done and the size of each report.
3. **Set the yield expectation.** Say plainly, in the same message: *attempted
   fills do not all become submissions.* Roles get blocked by CAPTCHAs, login
   walls, required address lines the user declined to provide, or questions
   with no safe truthful answer — those are skipped and reported, never forced
   through. Every run's report shows three numbers — attempted / submitted /
   blocked-with-reason — so the user always sees the real yield, not just the
   attempt count.

## Step 10 — Go live

**Final recap first.** Before creating anything, show one consolidated profile recap: legal name, email, phone, location, work authorization + sponsorship need (verify the exact wording — e.g. OPT pending vs approved), earliest start, relocation, EEO answers, blacklist, lanes + title filters, sources, both automation switches, schedule + batch size (fill attempts per run), the attempted-vs-submitted yield expectation, report destination. The user confirms once — this is the last chance to catch extraction errors (wrong grad date, wrong email) before the pipeline acts on them.

Then create the discovery cron(s) per the schedule (owner: the user's job-search goal or tracked item).

Reporting destination: ask whether discovery reports should go to a dedicated side chat (recommended — keeps the main chat clean) or stay in the current chat. If side chat: create it, set it as the crons' `delivery` target, and immediately post an intro message in the new chat — required, not optional. A brand-new empty chat with no first message feels broken. Template: "这里是 Job Done 投递报告：…" — one short paragraph saying what will land here (per-run reports: submitted applications with confirmations/IDs, blocked-manual list, source health; plus recruiter-reply alerts from the mailbox scan) and what won't (no chatter, no questions).

Confirm with the user:

- "run the pipeline now" — runs a discovery immediately
- "reset pipeline config" — re-runs this onboarding (confirm before wiping)
- "pause pipeline" / "resume pipeline" — toggles the schedule
