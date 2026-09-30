---
name: "job-pipeline"
description: "Job Done: run the automated job-application pipeline — discover roles on a schedule, shortlist them for the user, fill applications, batch the reviews, submit on approval, and log everything to the tracker."
---

# Job Done

## Purpose

An end-to-end, human-in-the-loop job application loop. Scheduled runs discover roles; the user selects; the agent fills; the user approves; the agent submits and logs. Personal data lives in `profile.yaml` and `state/` — never share those. Shareable parts: `SKILL.md`, `discover.py`, `scripts/`, `ONBOARDING.md`, `config.yaml` (as a template), `references/` (including the verified board list `company_boards.json`).

## Workflow

### Stage 1 — Discover (scheduled run, or "run the pipeline now")

1. Run the API-first discovery script (non-interactive — it never prompts):
   `python3 ~/workspace/skills/job-pipeline/discover.py`
   It pulls every board in `references/company_boards.json` through their public JSON APIs (Greenhouse / Ashby — no login needed), the LinkedIn guest search API for each lane query in `config.yaml`, and any curated job lists under `curated_sources:` (community-maintained tables, e.g. GitHub new-grad repos — they catch companies with no public ATS API), then applies programmatic filters in this order: dedupe against `state/seen_roles.json` FIRST (never re-surface a seen URL), `blacklist`, `discovery.title_exclude`/`title_include` (config-driven title filters — **tier-1** = explicit new-grad marker; **tier-2** = `discovery.title_tier2_include` role keywords / junior signals like SDE, MLE, developer, "0-2 years" *without* the marker, sorted after tier-1 and flagged `"tier": 2`), lane `keywords` (each lane in `config.yaml` carries its own), location fit — and recency via the **auto-scaling window** (starts 24h, expands stepwise to 30d until `discovery.min_candidates` **tier-1** roles are in-window, so tier-2 volume can never jam it; `--max-age-days` overrides to a fixed window for one-time backfills). It writes the compact pre-filtered set to `state/discovery_candidates.json` and prints a one-line summary (boards OK, jobs pulled, window used, candidates + tier-2 count, seconds, watermark run counter, newly-closed count). Typical runtime is 1–3 minutes at ~10x lower token cost than browsing page by page.
2. Judge `state/discovery_candidates.json` in BATCHES: one LLM call scores and ranks ~10 roles at a time from title+company+location+date+snippet (never one call per role). Decide keep/reject from the snippet alone for clear cases; open a posting page ONLY for genuinely borderline roles, capped at ~5 page opens per run. Verify new-grad eligibility, lane fit, and sponsorship plausibility per `config.yaml` and `references/standing-answers.md`.
3. Small web supplement (capped): 2–3 targeted web searches for fresh postings from companies with no public board API (YC Jobs has no stable public API; LinkedIn company pages beyond the guest search). Open at most ~10 pages total, and check `state/seen_roles.json` before opening any URL.
4. Keep the top `discovery.shortlist_size`, ordered by lane priority then fit. Per-company application caps: some employers limit applications per candidate — known case: **ByteDance (incl. TikTok, same recruiting system): max 2 new-grad applications total**. Never shortlist more roles than the cap allows for one hiring system; when trimming, keep the best fits and let the user swap.
5. Write EVERY judged role (candidates you evaluated + supplement pages you opened) to `state/seen_roles.json`: keepers → `"decision": "shortlisted"`; the rest → `"decision": "rejected"` with a short `"reason"`.
6. Save the shortlist to `state/runs/<YYYY-MM-DD-HHMM>.json`.
7. Shortlist non-empty → present it to the user for selection (company, role, location, lane, link, one-line fit note). Empty → stay silent.
8. Everything in this stage is non-interactive: never ask the user questions mid-run.
9. Source health (overrides the stay-silent rule): read the `health=` token of the discovery one-line summary. If any source is WARN/FAIL, tell the user which source and the symptom (e.g. "board:Acme — fetch failed (Timeout)", "linkedin — newest item 200h old") even when the shortlist is empty. In normal (non-empty) reports, append one line: `Sources: 41/41 healthy` (or list the degraded ones). The per-source history lives in `state/source_health.json`.

To cover a new company, add its board to `references/company_boards.json` (`greenhouse` token or `ashby` org slug, verified live against the public API) — the next run picks it up automatically.

### Stage 2 — Select (user, unless auto_select)

Present the shortlist: company, role, location, lane, link, one-line fit note. The user picks ("all", numbers, or names).

If `auto_select: true` in config.yaml, skip this step: proceed with all shortlisted roles straight to Stage 3. Stage 4 review is skipped only when `auto_submit: true` (standing authorization confirmed at onboarding).

**Judging rules (apply during the batch LLM review of candidates):**
- **Posting-text disqualifier scan:** when judging a candidate, read the posting text for hard disqualifiers and exclude with reason: explicit no-sponsorship language ("will not provide visa sponsorship", "not eligible for F1/J1"), video-recording requirements ("record a video", "video introduction"), citizenship-only requirements. (discover.py pre-filters the obvious ones on title+snippet; the judge catches the rest.)
- **`tier: 2` candidates (no explicit new-grad marker):** these matched a tier-2 signal (SDE, MLE, developer, "software engineer", "0-2 years", "university", …) but the title/snippet never says new-grad outright — judge them STRICTER than tier-1. Shortlist ONLY with a positive junior signal: leveling ("SDE I", "Engineer I", "Associate"), "0-2 years"/"0+ years" experience, "recent/university graduate" language. Senior signals (Senior, Staff, Principal, Lead, Sr., level III+, "5+ years", "8+ years") → fast reject from title+snippet, no page open. When genuinely unclear, default to reject — never shortlist a tier-2 on hope, because auto-submit is on.
- **`manual_likely` flag:** candidates tagged by discover.py (e.g. YC jobs, iCIMS — hCaptcha expected) are presented with a "likely manual" warning. They stay in the shortlist (the user may still want them), but the review notes the expected manual step so it never surprises mid-fill.

**Shortlist exclusion filters** (drop before presenting; no need to ask):
- Posting explicitly rules out the applicant: "not eligible for F1/J1 students", "will not provide visa sponsorship now or in the future", or equivalent. (Seen 2026-09-29: Atlassian, IBM Agentic AI.)
- Application requires a video recording / video self-introduction. The user will not record videos — drop these silently. (Seen 2026-09-29: Solace.)

### Stage 3 — Prepare (agent)

For each selected role:

1. **Form structure (cached, no LLM re-parse):** before filling, run
   `python3 ~/workspace/skills/job-pipeline/scripts/form_cache.py "<application URL>"`.
   It prints the board's cached field mapping (form labels → types/requirements), fetching the public structure only on a cache miss. Reuse that mapping to fill standard fields (name, email, phone, links, education, EEO, work authorization) from `profile.yaml` and `references/standing-answers.md` — do NOT have the LLM re-read and re-parse the entire form on every application. Only genuinely new/unknown fields get individual attention.
2. **Free-text questions via the answer bank:** for every free-text/essay question on the form:
   a. Run `python3 ~/workspace/skills/job-pipeline/scripts/qa_match.py --question "<exact question text>"` against `state/qa_bank.json`.
   b. Score ≥ 0.75 → reuse the banked answer verbatim (still show it in the user review); bump its `use_count` in `state/qa_bank.json`.
   c. No match → draft the answer with the LLM exactly ONCE from the fact sheet
      (`profile.yaml` + `references/standing-answers.md`) and use it directly —
      never ask the user for wording mid-run. Append it to `state/qa_bank.json`
      (`question`, `answer`, `company`, `role`, `first_used_at`, `use_count: 0`)
      and quote it verbatim in the final report so the user can correct it
      afterwards. Open-text questions ("why us", motivation, "most interesting
      paper", etc.) may be freely drafted from real background facts; hard
      facts (DOB, SSN, test scores, citizenship) are never invented.
   d. **Risky question → skip the role.** If the question asks for a legal
      attestation or agreement to terms, a binding commitment (relocation at
      own expense, a salary number, a start date earlier than `earliest_start`),
      or any hard fact not in the profile — do NOT draft, do NOT fill, do NOT
      submit. Mark the role blocked-manual with reason
      `risky question: <the question>` and list it in the final report. A weak
      application is recoverable; a misrepresentation or an unwanted legal
      commitment is not.
   Rules: drafts are used and banked on first encounter — no pre-approval gate;
      each is quoted verbatim in the final report for after-the-fact correction.
      Sensitive fields (CSRF tokens, tracking IDs, captcha widgets, hidden inputs)
      are never sent to the LLM and never banked.
3. Fill every field per `references/standing-answers.md`, upload the lane-matched resume + transcript automatically (no permission needed). STOP before Submit.
   **Attempt caps:** max 3 tries per single action (a Submit click, a widget workaround, a dropdown selection). After 3 failures on the same action, stop — the role goes to blocked-manual, the browser task closes, and the URL + exact reason are reported. Never burn 10+ attempts on one control (seen 2026-09-29: C3.ai submit clicked ~15x, Nuro location tried ~10 ways). For widget validation bugs that survive the cap, the role goes to blocked-manual — no takeover requests, no more automation attempts.
4. **Email verification codes (on demand only):** some sites require an email verification code during registration or before submission. If a browser fill task parks at such a step, it MUST report back and stop: the site URL, the exact step it is stuck at, and the masked recipient shown on the page (e.g. "code sent to x•••@ucsd.edu"). It must NOT guess the code or proceed.
   The orchestrating agent then performs ONE targeted Gmail lookup: search for the newest message (last ~15 minutes) from that site's sender address, read ONLY that single matching message, take the code, and hand it to the waiting browser task for that step only.
   Hard rules: one code per step, never reuse a code, never write codes to files / memory / state / logs, never scan the inbox for codes speculatively (no background code sweeps). If no fresh matching message exists, skip the role and report it ("verification code not received") — never ask the user to trigger a resend. If the step needs the user to do anything themselves (tap a link on their phone, answer a call), skip the role and report it — never ask. Filling in the code happens during filling; the final Submit still requires the user's explicit approval — see Stage 5.
5. **Site accounts (register; reset the password if the email is taken):** if an
   application site requires a candidate account, create one with the
   application email — the orchestrating agent sets the password and stores it
   in the tracker's `Accounts` tab (never in memory or state files). If the
   email is already registered, run the site's password-reset flow instead
   (reset link via the on-demand Gmail lookup, same one-code rules as step 4)
   and store the new password in `Accounts`. If the reset flow hits a CAPTCHA,
   skip per rule 6.
6. **Blocked → manual handoff: close the browser, never retry.** If a fill task hits any of the following, it MUST stop immediately, close the browser task, and report `blocked: <pattern> — <site URL> — <exact detail>`. Do NOT retry, do NOT schedule an automatic retry, do NOT ask the user mid-flow. The role is marked unfillable and listed in the batch review with its URL and reason, for the user to apply manually later. Observed patterns (2026-09-29 batch):
   - CAPTCHA / image challenge / bot-detection wall (hCaptcha, etc.)
   - Site or backend system errors (e.g. Workday VPS `ErrorPage` errors on save — even repeated)
   - Rate limits / "busy" on verification codes (e.g. "Too Many Attempts. Try Again Later")
   - Submit button unresponsive after multiple attempts with no error shown
   - Form widget validation bugs blocking submission (e.g. location dropdown that won't validate)
   - Any step requiring human verification: ID document check, phone-call verification, manual identity review, proctored/in-person checks, or anything the automated email-code lookup can't complete alone — skip the role, report it, never ask the user to verify
   Missing hard facts (DOB/SSN/test scores/citizenship) are never invented — the role is skipped and reported with URL + reason.
7. **Fill failures are batch-reported:** every role that cannot be completed
   (CAPTCHA, login wall, missing required info, site error) is recorded with
   its posting URL and the exact reason; all of them are listed together in
   the Stage 4 review under "Could not fill".

How the review works depends on `submit_review_mode` in config.yaml:

- **batch** (default): fill ALL selected roles first, then compile everything into one combined review.
- **per_application**: fill ONE role and hand its review to the user immediately (smaller turns; the user can stop early).

### Stage 4 — Approve (user, unless auto_submit)

- **auto_submit: true** (default): skip this stage entirely. After all roles are filled in Stage 3, proceed straight to Stage 5. The standing authorization was confirmed at onboarding with risks explained — it counts as the user's submit approval.
- **batch**: the user reviews the combined batch, edits anything, and says "submit all" (or names a subset).
- **per_application**: the user approves each role's review as it arrives ("submit"), then the agent moves to the next role.

`submit_review_mode` (batch / per_application) only applies when `auto_submit: false`.

### Stage 5 — Submit & log (agent)

**Pre-submit checklist** (before clicking Submit):
- Re-verify prefilled values against `profile.yaml`: city, enrollment/employment status, name spelling — sites prefill these wrong (seen 2026-09-29: Amazon city + enrollment).
- If the first Submit click returns field errors (e.g. hidden required fields like Ashby's Location), fill them from the profile, retry ONCE, then stop. A second failure means blocked-manual.

Submit each approved application. Capture: confirmation text, timestamp, application/reference ID if shown. Append one row per application to the tracker. Mark each URL `"decision": "applied"` in `state/seen_roles.json`.

Submission is always via the browser flow: fill per Stage 3, park at the final review screen, and click Submit only on the user's explicit "submit" / "submit all". Email verification codes encountered during filling are handled per the Stage 3 step 4 on-demand lookup — they never replace the explicit submit approval. Direct-POST submission was evaluated on 2026-09-29 and rejected — do not build or use HTTP submitters: Greenhouse's documented application POST requires an employer API key (Basic Auth); its hosted form is gated by invisible reCAPTCHA Enterprise (bot-scored submissions get HTTP 428 `captcha-failed` and a two-phase email security-code flow) and uploads resumes via presigned S3, so pure-HTTP submission cannot pass; Ashby's hosted submit needs reCAPTCHA + CSRF with v3 spam scoring; Lever/Workday expose no candidate POST path. The public `?questions=true` job endpoint remains the supported way to read a Greenhouse form's structure (used by `scripts/form_cache.py`).

### Stage 6 — Track (ongoing)

The tracker is the source of truth. Run
`python3 ~/workspace/skills/job-pipeline/scripts/gmail_scan.py`
on a schedule (every 4–6h; silent unless hits) to detect recruiter replies,
interview invitations, and application confirmations: it is watermarked and
incremental, pre-filters without any LLM, and matches senders against the
tracker's company list. Report hits to the user and update the tracker's
Status column.

## Lane matching

- **agent_infra**: agent(s), agentic, infrastructure, infra, platform, inference, serving, kernels, distributed, ML systems
- **ai_cloud**: cloud, backend, platform, kubernetes, distributed systems, microservices
- **genai**: LLM, RAG, chatbot, prompt, copilot, GenAI app (lowest priority)

A role may match multiple lanes; assign the highest-priority matching lane and upload that lane's resume.

## Commands

- "run the pipeline now" → execute Stage 1 immediately in chat.
- "reset pipeline config" → re-run `ONBOARDING.md` (confirm before wiping `profile.yaml` / `config.yaml`).
- "pause pipeline" / "resume pipeline" → disable / enable the discovery crons.

## Operating Rules

1. **Never ask the user about form-filling matters — this is the first principle.** The user is never interrupted mid-run with form questions. Draft everything yourself from the fact sheet (`profile.yaml` + `references/standing-answers.md`): free-text / "why us" / motivation answers are drafted from real background facts (never invented), salary expectations use the range printed on the job posting (if the posting lists none, use the lane's standing range from the fact sheet — never invent a number out of thin air). If a required answer is truly uncertain or risky (a legal/factual claim you cannot verify), skip that role, record URL + exact reason, and note it in the final report. The user reviews your drafts in the delivered report and can correct them afterwards.
2. Never click Submit without the user's submit authorization — which is either an explicit per-run "submit" / "submit all", or the standing `auto_submit: true` confirmed at onboarding with risks explained.
3. Resume and transcript uploads are routine — never ask permission.
4. Never invent: citizenship, DOB, SSN, test scores, demographic facts. For a required field with no true answer, use "N/A" only where `references/standing-answers.md` pre-authorizes it — otherwise skip the role and report it (never ask mid-flow).
5. Education is always entered manually; never trust a site's resume auto-parse.
6. `profile.yaml` and `state/` are personal — never include them when sharing the skill.
7. Dedupe is sacred: check `state/seen_roles.json` before reading any role URL.
8. Blocked means manual: CAPTCHA, site/backend errors, rate limits, unresponsive submit, widget bugs → close the browser immediately, no retries (not even scheduled ones), record URL + exact reason, report for the user to apply manually.
9. **Human verification = automatic skip.** Any role whose submission requires human verification (CAPTCHA/image challenge, manual takeover, in-person checks) is skipped without asking — record URL + reason in `state/seen_roles.json` (`"decision": "blocked"`) and list it in the final report. Set by user 2026-09-29; this overrides the old "offer takeover" behavior.

## Sharing

Public repo: https://github.com/xf-mike/job-done

To give this to a friend, just send them the link. Their agent clones it into `~/workspace/skills/job-pipeline/` and follows `ONBOARDING.md` with them in conversation. The repo holds only the shareable playbook + templates — `profile.yaml`, `config.yaml`, and `state/` are gitignored, so personal data can never leak into it.

The repo is the source of truth for the playbook: edit `SKILL.md` / `ONBOARDING.md` / `templates/` in `~/workspace/skills/job-pipeline/`, then `git add -A && git commit -m "..." && git push`.
