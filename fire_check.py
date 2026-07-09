"""Quarterly FIRE-assumption checkup — grounded review, Telegram suggestions.

Reads Daryl's keyed FIRE settings from the dashboard (GET /api/fire with the
upload bearer), asks one grounded Gemma call to compare the stored assumptions
against current Singapore sources (core inflation, CPF interest rates, CPF
Life payouts, SWR research), and Telegrams a review message when anything
looks stale.

Hard rule: this script NEVER writes settings. Suggestions go to a human; the
human keys changes into the FIRE tab. (The /api/fire endpoint enforces this
too — PUT rejects bearer callers.)

Usage: python fire_check.py   (env: SOVEREIGN_EYE_URL, DD_UPLOAD_SECRET,
GEMINI_API_KEYS, TELEGRAM_*)
"""

from __future__ import annotations

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

_SYSTEM = """\
You are a careful financial research assistant reviewing the assumptions behind a
Singapore-based investor's FIRE (financial independence) plan. Use Google Search to
find CURRENT, authoritative figures (MAS, SingStat, CPF Board, established research).
You are checking whether stored assumptions look stale — you are NOT giving
personalized financial advice and you NEVER invent figures. If you cannot verify a
number from a credible source, say verdict OK with a note that no reliable update
was found. Be conservative: only flag REVIEW when the gap is material (would move
the FIRE number or timeline meaningfully)."""

_USER_TEMPLATE = """\
Stored FIRE-plan assumptions (keyed in by the user, SGD):
  - inflation: {inflation}% per year (compare against: latest Singapore CORE inflation
    trend and MAS's medium-term outlook)
  - cpf_life_monthly: S${cpf_life}/month expected from age 65 (0 means auto-estimated
    by the dashboard's CPF simulation; compare against: current CPF LIFE estimated
    payout ranges for a member born in {birth_year} under the Standard plan, if
    determinable)
  - swr: {swr}% safe withdrawal rate (compare against: the mainstream research range for
    a 40+ year early-retirement horizon)
  - expected_return: {expected_return}% nominal per year on a global equity portfolio
    (compare against: commonly cited long-run global equity return expectations)

The dashboard's CPF simulation also hardcodes these model constants (verify each
against CPF Board announcements):
  - cpf_interest_floors: OA 2.5% / SMRA 4.0% per year (compare against: current CPF
    interest rates and whether the 4% SMRA floor extension still holds)
  - frs_anchor: Full Retirement Sum S$220,400 in 2026, escalating 3.5%/year (compare
    against: announced FRS values for coming cohorts)
  - bhs_anchor: Basic Healthcare Sum S$79,000 in 2026, escalating 4.6%/year (compare
    against: the announced BHS for the current year)
  - cpf_life_anchor: FRS set aside at 55 pays about S$1,780/month from 65 under the
    Standard plan for the 2026 cohort (compare against: CPF LIFE payout estimates)
  - cpf_allocation_2026: contribution split by age per the Jan 2026 allocation table,
    e.g. 62.17% OA / 16.21% SA / 21.62% MA at age 35 and below (compare against: any
    announced allocation changes since)

Return ONLY a JSON array, one object per parameter above:
[{{"parameter": "...", "stored": "...", "current": "<figure you found, with year>",
   "verdict": "OK" or "REVIEW", "note": "<one sentence>", "source": "<site/institution>"}}]
"""


def fetch_settings() -> dict | None:
    """Read FIRE settings from the dashboard KV. None = unconfigured/no settings."""
    base = os.getenv("SOVEREIGN_EYE_URL", "").rstrip("/")
    secret = os.getenv("DD_UPLOAD_SECRET", "")
    if not base or not secret:
        print("  [fire-check] SOVEREIGN_EYE_URL / DD_UPLOAD_SECRET not set — skipping")
        return None
    try:
        r = requests.get(f"{base}/api/fire",
                         headers={"Authorization": f"Bearer {secret}"}, timeout=20)
        if not r.ok:
            print(f"  [fire-check] settings fetch HTTP {r.status_code} — skipping")
            return None
        data = r.json()
        return data if isinstance(data, dict) and data else None
    except Exception as e:
        print(f"  [fire-check] settings fetch failed ({e}) — skipping")
        return None


def build_prompt(s: dict) -> str:
    return _USER_TEMPLATE.format(
        inflation=s.get("inflation", "?"),
        cpf_life=s.get("cpfLifeMonthly", "?"),
        birth_year=s.get("birthYear", "?"),
        swr=s.get("swr", "?"),
        expected_return=s.get("expectedReturn", "?"),
    )


def _extract_rows(text: str) -> list:
    """Array-first JSON extraction. llm.extract_json prefers the first {...}
    even inside an array, which would silently keep only row one of five —
    so try the whole reply, then the first [...] block, then fall back."""
    import json
    import re
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.M)
    try:
        v = json.loads(t)
        return v if isinstance(v, list) else [v]
    except Exception:
        pass
    m = re.search(r"\[[\s\S]*\]", t)
    if m:
        try:
            v = json.loads(m.group(0))
            return v if isinstance(v, list) else [v]
        except Exception:
            pass
    try:
        import llm
        v = llm.extract_json(text)
        return v if isinstance(v, list) else [v]
    except Exception:
        return []


def parse_review(raw) -> list[dict]:
    """Normalize the model's array; drop rows without a parameter name."""
    if not isinstance(raw, list):
        return []
    out = []
    for row in raw:
        if isinstance(row, dict) and row.get("parameter"):
            out.append({
                "parameter": str(row.get("parameter")),
                "stored":    str(row.get("stored", "?")),
                "current":   str(row.get("current", "?")),
                "verdict":   "REVIEW" if str(row.get("verdict", "")).upper() == "REVIEW" else "OK",
                "note":      str(row.get("note", "")),
                "source":    str(row.get("source", "")),
            })
    return out


def run() -> int:
    settings = fetch_settings()
    if settings is None:
        return 0  # nothing keyed in yet — a quiet no-op, not a failure

    import llm
    try:
        text = llm.call_gemini(_SYSTEM, build_prompt(settings), grounding=True,
                               temperature=0.2, max_output_tokens=8192)
        rows = parse_review(_extract_rows(text))
    except Exception as e:
        print(f"  [fire-check] grounded review failed ({e})")
        try:
            from notify import alert_ops
            alert_ops("Quarterly FIRE assumption check FAILED to run "
                      f"({str(e)[:150]}) — assumptions unreviewed this quarter.")
        except Exception:
            pass
        return 1

    if not rows:
        print("  [fire-check] model returned no usable rows")
        return 1

    flagged = [r for r in rows if r["verdict"] == "REVIEW"]
    for r in rows:
        print(f"  [fire-check] {r['parameter']}: {r['verdict']} "
              f"(stored {r['stored']} vs {r['current']}) — {r['note']}")

    from notify import alert_fire_review
    alert_fire_review(rows)
    print(f"  [fire-check] done — {len(flagged)} of {len(rows)} flagged for review")
    return 0


if __name__ == "__main__":
    sys.exit(run())
