#!/usr/bin/env python3
"""
Q&A answer-bank matcher for the job-pipeline skill (Stage 3 — Prepare).

Before an LLM drafts an answer to a free-text / essay question on an
application form, the agent runs this script against the personal answer
bank (`state/qa_bank.json`). If a previously user-approved answer matches
the new question closely enough, the banked answer is reused verbatim —
the LLM never sees the question at all.

Matching is deliberately conservative (precision over recall): reusing a
WRONG answer on a job application is far worse than drafting fresh. A
candidate must pass ALL of:
  1. difflib sequence similarity >= threshold (default 0.75), AND
  2. content-word recall >= 0.6 (at least 60% of the banked question's
     content words appear in the new question), AND
  3. not a risky single-word substitution (e.g. earliest/latest,
     travel/relocate — near-identical strings, different meaning).

Content words = alphanumeric tokens minus stopwords (generic question
boilerplate like "what is your", plus US-country phrasing "us"/"usa"/
"united states", which carries no discriminating value in these forms).

Non-interactive, stdlib-only, read-only: it never writes to the bank.
Exit code is always 0; missing/empty/malformed banks yield {"match": null}.

Usage:
    python3 qa_match.py --question "Why do you want to work here?"
    python3 qa_match.py --bank /path/to/qa_bank.json --question "..." --threshold 0.8

Output (stdout, single JSON line):
    {"match": "<banked question>", "score": 0.93, "answer": "<approved answer>"}
    or
    {"match": null}
"""

import argparse
import difflib
import json
import os
import re
import sys

SKILL_DIR = os.environ.get("JOB_PIPELINE_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BANK = os.path.join(SKILL_DIR, "state", "qa_bank.json")

STOPWORDS = set(
    "a an the are you do does did will would can could should shall may might "
    "must be is was were been being have has had having do does did of for in "
    "on to with as at by from or and it its this that these those what which "
    "who whom whose when where why how your our their his her my our i we they "
    "them he she him her me us our usa united states".split()
)


def content_tokens(text):
    return [t for t in re.findall(r"[a-z0-9]+", text.lower())
            if t not in STOPWORDS]


def load_bank(path):
    """Return the list of valid bank entries; [] on any problem. Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return []
    # Allow // comment lines (the shareable template is JSONC).
    lines = [ln for ln in raw.splitlines() if not ln.lstrip().startswith("//")]
    try:
        data = json.loads("\n".join(lines))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [
        e for e in data
        if isinstance(e, dict) and e.get("question") and e.get("answer")
    ]


def normalize(text):
    return " ".join(str(text).lower().split())


def is_risky_substitution(bank_tokens, query_tokens):
    """True when exactly one long content word was swapped (earliest/latest)."""
    only_bank = bank_tokens - query_tokens
    only_query = query_tokens - bank_tokens
    if len(only_bank) == 1 and len(only_query) == 1:
        return len(next(iter(only_bank | only_query))) > 3
    return False


def best_match(question, bank, threshold):
    q = normalize(question)
    if not q:
        return None, 0.0
    qtokens = set(content_tokens(question))
    best, best_score = None, 0.0
    for entry in bank:
        seq = difflib.SequenceMatcher(None, q, normalize(entry["question"])).ratio()
        if seq < threshold or seq <= best_score:
            continue
        etokens = set(content_tokens(entry["question"]))
        if not etokens:
            continue
        recall = len(etokens & qtokens) / len(etokens)
        if recall < 0.6:
            continue
        if is_risky_substitution(etokens, qtokens):
            continue
        best, best_score = entry, seq
    return best, best_score


def main():
    ap = argparse.ArgumentParser(description="Match a form question against the approved Q&A answer bank.")
    ap.add_argument("--bank", default=DEFAULT_BANK, help="Path to qa_bank.json")
    ap.add_argument("--question", required=True, help="The free-text question from the application form")
    ap.add_argument("--threshold", type=float, default=0.75,
                    help="Minimum sequence similarity (0-1) to reuse a banked answer")
    args = ap.parse_args()

    try:
        bank = load_bank(args.bank)
        entry, score = best_match(args.question, bank, args.threshold)
        if entry is not None:
            print(json.dumps({
                "match": entry["question"],
                "score": round(score, 3),
                "answer": entry["answer"],
            }, ensure_ascii=False))
        else:
            print(json.dumps({"match": None}))
    except Exception:
        # Belt and suspenders: this script must never break a form fill.
        print(json.dumps({"match": None}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
