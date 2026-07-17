"""Thesis-adherence check — the anti-thesis-drift pass (2026-07-11).

Answers a DIFFERENT question from the debate: the debate asks "would we buy at
today's price" (valuation-entangled — a stock that ran 80% can score 5.5 with
its thesis MORE intact than ever); this asks "is the registered reason for
owning this still true". One gemini-3.5-flash call per holding, portfolio runs
only. The registered thesis is a stable anchor held server-side (eye
/api/dd/thesis): seeded once from the system's analysis, changed only by a
manual edit — because quietly rewriting the reason you own something is the
exact behavioral failure this exists to catch.

Display + alert only — never touches scores, boards, or the gate. Any failure
degrades to UNKNOWN and never blocks the portfolio run. Flash quota-dead days
(20 RPD/project since 2026-07-17) fall back to gemma, then flash-lite, rather
than UNKNOWN.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import requests

from llm import call_gemini_async, extract_json
from text_utils import clip

CHECK_MODEL = "gemini-3.5-flash"
# Flash's free tier is 20 requests/day/project (2026-07-17 Google quota change);
# scout-run guidance calls routinely drain it before the portfolio run starts.
# The check mattering more than the model, fall back to the debate workhorse,
# then to flash-lite (500 RPD + 250K TPM/project — alive when both others are
# not; Daryl-approved 2026-07-18 for extraction/check lanes only).
FALLBACK_MODEL = "gemma-4-31b-it"
LITE_MODEL = "gemini-3.1-flash-lite"
STATUSES = {"INTACT", "STRAINED", "BROKEN"}

_SYSTEM = (
    "You are a thesis-adherence auditor for a portfolio holding. Judge ONLY "
    "whether the REGISTERED owning thesis is still true given today's "
    "evidence — NOT whether the stock is attractive at today's price. "
    "Valuation moves alone (price ran up, multiple expanded) do NOT break a "
    "thesis; the thesis breaks when its causal claims stop being true "
    "(the moat cracked, the margin story reversed, the catalyst died, "
    "management pivoted away). Be specific and evidence-based.\n"
    'Respond with ONLY this JSON: {"status": "INTACT" | "STRAINED" | "BROKEN", '
    '"adherence": <0-10, 10 = thesis fully intact>, '
    '"reason": "<ONE sentence citing the specific evidence>"}'
)


def _eye_base_secret() -> tuple[str, str] | None:
    base = os.getenv("SOVEREIGN_EYE_URL", "").rstrip("/")
    secret = os.getenv("DD_UPLOAD_SECRET", "")
    if not base or not secret:
        return None
    return base, secret


def fetch_registry() -> dict | None:
    """Registered theses {TICKER: {thesis, key_swing, status, ...}}.
    {} = endpoint fine but empty (first ever run); None = unreachable/not
    configured — the caller SKIPS checks entirely (never seed blind)."""
    cfg = _eye_base_secret()
    if cfg is None:
        print("  [thesis] SOVEREIGN_EYE_URL / DD_UPLOAD_SECRET not set — skipping checks")
        return None
    base, secret = cfg
    try:
        r = requests.get(f"{base}/api/dd/thesis",
                         headers={"Authorization": f"Bearer {secret}"}, timeout=20)
        if not r.ok:
            print(f"  [thesis] registry fetch HTTP {r.status_code} — skipping checks")
            return None
        doc = r.json()
        return doc if isinstance(doc, dict) else None
    except Exception as e:
        print(f"  [thesis] registry fetch failed ({e}) — skipping checks")
        return None


def _evidence(result: dict, dossier: dict) -> dict:
    """Compact today-facts for the auditor — enough to judge, small enough to
    keep the call cheap."""
    quote = dossier.get("quote") or {}
    tech = dossier.get("technicals") or {}
    ver = result.get("verification") or {}
    return {
        "price": quote.get("price"),
        "change_pct": quote.get("change_pct"),
        "pct_from_52w_high": tech.get("pct_from_52w_high"),
        "above_sma200": tech.get("above_sma200"),
        "rsi_14": tech.get("rsi_14"),
        "todays_debate_score": result.get("consensus_score"),
        "todays_debate_grade": result.get("consensus_grade"),
        "todays_majority_thesis": clip(result.get("majority_thesis"), 600),
        "strongest_bear_point": clip(ver.get("strongest_bear_point"), 400),
        "key_swing_factor_today": clip(result.get("key_swing_factor"), 300),
    }


async def check_thesis(ticker: str, registered: dict, result: dict,
                       dossier: dict) -> dict:
    """{status, adherence, reason} for one holding. UNKNOWN on any failure."""
    try:
        user = json.dumps({
            "ticker": ticker,
            "registered_thesis": registered.get("thesis"),
            "registered_key_swing": registered.get("key_swing"),
            "registered_at": registered.get("set_at"),
            "today": _evidence(result, dossier),
        }, default=str)
        models = (CHECK_MODEL, FALLBACK_MODEL, LITE_MODEL)
        raw = None
        for i, m in enumerate(models):
            try:
                raw = await call_gemini_async(_SYSTEM, user, model=m,
                                              temperature=0.2, max_output_tokens=2048,
                                              thinking_level=None)
                break
            except Exception as model_err:
                if i == len(models) - 1:
                    raise
                print(f"  [thesis] {ticker}: {m} unavailable "
                      f"({model_err}) — retrying on {models[i + 1]}")
        data = extract_json(raw)
        if not isinstance(data, dict):
            raise ValueError("non-dict response")
        status = str(data.get("status", "")).upper().strip()
        if status not in STATUSES:
            raise ValueError(f"bad status {data.get('status')!r}")
        adherence = data.get("adherence")
        adherence = max(0.0, min(10.0, float(adherence))) if adherence is not None else None
        return {"status": status, "adherence": adherence,
                "reason": clip(str(data.get("reason") or ""), 300)}
    except Exception as e:
        print(f"  [thesis] {ticker} check failed: {e}")
        return {"status": "UNKNOWN", "adherence": None,
                "reason": clip(f"check failed: {e}", 200)}


def seed_check(result: dict) -> dict:
    """First sighting of a holding: register today's thesis as the anchor —
    no LLM call (checking today's thesis against today's evidence is a
    tautology)."""
    return {"status": "INTACT", "adherence": None,
            "reason": "thesis registered this run"}


def build_checks(outcomes: dict) -> dict:
    """{ticker: POST-payload check} from {ticker: (result, dossier)} — the
    thesis_check dict must already be attached to each result."""
    now = datetime.now(timezone.utc).isoformat()
    checks = {}
    for ticker, (result, _dossier) in outcomes.items():
        tc = result.get("thesis_check")
        if not tc:
            continue
        checks[ticker] = {
            "status": tc["status"],
            "adherence": tc.get("adherence"),
            "reason": tc.get("reason", ""),
            "checked_at": now,
            "seed_thesis": clip(result.get("majority_thesis"), 1200),
            "seed_key_swing": clip(result.get("key_swing_factor"), 400),
        }
    return checks


def broken_transitions(registry: dict, checks: dict) -> list[dict]:
    """Tickers that just ENTERED BROKEN (alert once per break, not daily)."""
    out = []
    for ticker, c in checks.items():
        if c["status"] != "BROKEN":
            continue
        prev = (registry.get(ticker) or {}).get("status")
        if prev != "BROKEN":
            out.append({"ticker": ticker, "adherence": c.get("adherence"),
                        "reason": c.get("reason", ""),
                        "thesis": (registry.get(ticker) or {}).get("thesis", "")})
    return out


def push_checks(checks: dict, held: list) -> dict | None:
    """POST results + held-list to the eye registry. None on failure."""
    cfg = _eye_base_secret()
    if cfg is None or not checks:
        return None
    base, secret = cfg
    try:
        r = requests.post(f"{base}/api/dd/thesis",
                          headers={"Authorization": f"Bearer {secret}",
                                   "Content-Type": "application/json"},
                          json={"checks": checks, "held": held}, timeout=30)
        if not r.ok:
            print(f"  [thesis] push failed: HTTP {r.status_code} {r.text[:200]}")
            return None
        resp = r.json()
        print(f"  [thesis] pushed: seeded {len(resp.get('seeded', []))} · "
              f"updated {len(resp.get('updated', []))} · removed {len(resp.get('removed', []))}")
        return resp
    except Exception as e:
        print(f"  [thesis] push failed: {e}")
        return None
