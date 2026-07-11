"""Upload sovereign-dd output JSONs to Sovereign Eye via the /api/dd/upload endpoint."""

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from scout import BUY_THRESHOLD
from text_utils import clip
from verify import surfaces_on_board

# Only upload scout files written in the last 2 hours — prevents re-uploading
# the entire accumulated history on every run (each run adds 12 files; without
# this filter the puts-per-upload grows unboundedly and blows the KV free tier).
SCOUT_UPLOAD_WINDOW_SECS = 2 * 3600

# Filenames to skip in output/ — not ticker results
_SKIP_FILENAMES = {"scout_history.json", "scout_notified.json", "gems_history.json",
                   "scout_seen.json", "scoreboard.json"}

UPLOAD_URL    = os.getenv("SOVEREIGN_EYE_URL", "https://sovereign-eye.pages.dev")
UPLOAD_SECRET = os.getenv("DD_UPLOAD_SECRET", "")
SUPABASE_URL  = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY  = os.getenv("SUPABASE_KEY", "")

HEADERS = {
    "Authorization": f"Bearer {UPLOAD_SECRET}",
    "Content-Type": "application/json",
}


def _extract_archetype(dossier: dict) -> str | None:
    fv = dossier.get("fair_values", {})
    arch_obj = fv.get("archetype")
    if isinstance(arch_obj, dict):
        return arch_obj.get("archetype")
    return None


# Retry budget for Supabase inserts. Backoff is a module constant so tests can
# zero it.
_SB_ATTEMPTS = 3
_SB_BACKOFF = (2.0, 6.0)


def _supabase_insert(table: str, rows: list[dict]) -> bool:
    """POST rows to a Supabase table via REST API, retrying transient failures.

    Returns True on success (or when unconfigured / nothing to insert — local
    runs stay a silent no-op). False means the history tables are missing rows;
    the caller must fail the run so the loss is loud, not a log line — these
    tables are the outcome-measurement dataset.
    """
    if not SUPABASE_URL or not SUPABASE_KEY or not rows:
        return True
    for attempt in range(_SB_ATTEMPTS):
        try:
            resp = requests.post(
                f"{SUPABASE_URL}/rest/v1/{table}",
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal,resolution=ignore-duplicates",
                },
                json=rows,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                return True
            print(f"  [supabase] {table} insert returned {resp.status_code}: {resp.text[:120]}")
        except Exception as e:
            print(f"  [supabase] {table} insert failed: {e}")
        if attempt < _SB_ATTEMPTS - 1:
            time.sleep(_SB_BACKOFF[min(attempt, len(_SB_BACKOFF) - 1)])
    return False


def _sanitize(obj):
    """Recursively replace NaN/inf floats with None so requests can serialize the payload.

    Uses a try/except around math.isnan so numpy.float64 (and other numeric subtypes
    that aren't isinstance float in numpy 2.x) are handled correctly.
    """
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    try:
        if math.isnan(obj) or math.isinf(obj):
            return None
    except (TypeError, ValueError):
        pass
    return obj


def load_json(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  [upload] Could not read {path.name}: {e}")
        return None


def _stamp_nav_snapshot() -> None:
    """Hit /api/nav-history with the upload bearer so the daily NAV snapshot is
    recorded even on days nobody opens the dashboard (the endpoint's once/day
    gate dedupes the 6 cron hits). Best-effort: the FIRE chart wants gap-free
    history, but a miss must never fail the run."""
    if not UPLOAD_URL or not UPLOAD_SECRET:
        return
    try:
        r = requests.get(
            f"{UPLOAD_URL}/api/nav-history",
            headers={"Authorization": f"Bearer {UPLOAD_SECRET}"},
            timeout=20,
        )
        if r.ok:
            print("  [nav] daily NAV snapshot stamped")
        else:
            print(f"  [nav] snapshot skipped (HTTP {r.status_code})")
    except Exception as e:
        print(f"  [nav] snapshot skipped ({e})")


def _slim_verification(v: dict | None) -> dict:
    """Compact confirmation-gate verdict for a card — enough to audit a confirmed
    BUY (verdict, score, top bear point) without bloating the board blob. The
    watchlist collector overrides this with the full verification (incl. the
    complete falsification_findings) for rejects."""
    v = v or {}
    if not v:
        return {}
    return {
        "verdict":              v.get("verdict"),
        "verification_score":   v.get("verification_score"),
        "strongest_bear_point": clip(v.get("strongest_bear_point"), 400),
        "checked_at":           v.get("checked_at"),
    }


def _scout_history_row(s: dict) -> dict:
    """Supabase scout_history row from a scout discovery. `price`/`confirmed`/`verdict`
    capture the signal-time entry price and BUY-gate outcome so a signal's forward
    return becomes computable later (see DATA_CONTRACT.md)."""
    return {
        "ticker":        s["ticker"],
        "score":         s.get("score"),
        "grade":         s.get("grade"),
        "sector":        s.get("sector"),
        "path":          s.get("path"),
        "filters":       s.get("matched_filters", []),
        "thesis":        clip(s.get("thesis"), 1000),
        "price":         s.get("price"),
        "confirmed":     s.get("confirmed"),
        "verdict":       (s.get("verification") or {}).get("verdict"),
        "factors":       s.get("factors"),
        "debate_digest": s.get("debate_digest"),
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    }


def _gems_history_row(g: dict) -> dict:
    """Supabase gems_history row from a gems discovery (see _scout_history_row)."""
    return {
        "ticker":        g["ticker"],
        "score":         g.get("score"),
        "grade":         g.get("grade"),
        "thesis":        clip(g.get("thesis"), 1000),
        "catalyst":      clip(g.get("catalyst"), 1000),
        "fair_value":    g.get("fair_value_composite"),
        "price":         g.get("price"),
        "confirmed":     g.get("confirmed"),
        "verdict":       (g.get("verification") or {}).get("verdict"),
        "factors":       g.get("factors"),
        "debate_digest": g.get("debate_digest"),
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    }


def _factor_stamp(dossier: dict | None, result: dict | None = None) -> dict | None:
    """Evidence-factor profile at signal time, built from the output file's
    dossier (+ light debate-quality signals from `result`). upload_kv is the
    ONLY producer of the Supabase signal rows, so the stamp lives here — a
    field added upstream in scout.py's in-memory discovery dict never reaches
    Supabase (that mistake cost 2026-06-26→07-03 of price data). ``v`` versions
    the methodology (see docs/ADAPTATION_PROTOCOL.md §4 register).

    `score_spread`/`confidence` (added 2026-07-07) are additive, non-selection
    fields — they don't affect the gate or any score. They exist so a future
    scoreboard bucket can test whether panel dissent predicts worse outcomes."""
    if not dossier:
        return None
    try:
        from dossier import quality_composite  # lazy — the upload must never die on an import
    except Exception:
        def quality_composite(_r):
            return None
    tech = dossier.get("technicals") or {}
    ratios = (dossier.get("financials") or {}).get("ratios_ttm") or {}
    result = result or {}
    return {
        "v":            3,
        "mom_12_1":     tech.get("mom_12_1"),
        "mom_6m":       tech.get("mom_6m"),
        "mom_1m":       tech.get("mom_1m"),
        "quality":      quality_composite(ratios),
        "eps_rev_mom":  ratios.get("eps_revision_momentum"),
        "fcf_yield":    ratios.get("fcf_yield"),
        "roic":         ratios.get("roic"),
        "score_spread": result.get("score_spread"),
        "confidence":   result.get("confidence"),
        # Macro regime at signal time (2026-07-11) — measurement-only field for
        # the attribution buckets; NOT selection-affecting, so factors.v stays 3
        # (rule 5 bumps v only when a change alters which signals surface).
        "regime":       (dossier.get("macro") or {}).get("regime"),
    }


def _debate_digest(result: dict) -> list | None:
    """Slim per-agent archive of the debate (~2KB cap): each agent's final
    score + last core argument. Full transcripts are deliberately never
    persisted (size), which made post-mortems on past signals impossible
    (2026-07-11 audit) — this keeps just enough to autopsy a signal months
    later. Rides the Supabase history rows ONLY; stripped from the KV boards
    (see _strip_digest)."""
    transcript = result.get("transcript")
    if not isinstance(transcript, list) or not transcript:
        return None
    last_by_agent: dict = {}
    for t in transcript:
        if isinstance(t, dict) and t.get("agent"):
            last_by_agent[t["agent"]] = t
    out = []
    for agent, t in list(last_by_agent.items())[:8]:
        arg = (t.get("thesis") or t.get("revised_thesis") or t.get("key_risk")
               or t.get("rationale") or "")
        out.append({"agent": agent,
                    "score": t.get("revised_score", t.get("score")),
                    "argument": clip(str(arg), 220)})
    return out or None


def _strip_digest(cards: list | None) -> list | None:
    """Board copies of cards without the archival debate_digest field."""
    if not cards:
        return cards
    return [{k: v for k, v in c.items() if k != "debate_digest"} for c in cards]


def _earnings_in_days(dossier: dict) -> int | None:
    """Days until the next earnings report, only when 0–14 (the window where
    a fresh BUY is really a pre-earnings bet); None otherwise/unknown."""
    try:
        upcoming = (dossier.get("earnings_calendar") or {}).get("upcoming") or []
        d = (upcoming[0] or {}).get("date")
        # Date-vs-date, not datetime-vs-midnight: anchoring the target at 00:00
        # UTC made "earnings today" read −1 (chip missing on the day it matters
        # most) and shifted every stamp by one (2026-07-11 audit).
        days = (datetime.strptime(d, "%Y-%m-%d").date()
                - datetime.now(timezone.utc).date()).days
        return days if 0 <= days <= 14 else None
    except Exception:
        return None


def _scout_card(ticker: str, result: dict, meta: dict | None = None,
                dossier: dict | None = None) -> dict:
    """Build the dd:scouts card shape from a debate result. Shared by scout
    collection and portfolio/triggered-analysis results so a card always reflects
    the latest run regardless of which pipeline produced it.

    ``dossier`` (the output file's dossier) supplies the signal-time price,
    sector, and factor stamp — the fields the Supabase history rows need for
    outcome measurement. Callers that have the file MUST pass it."""
    meta = meta or {}
    dossier = dossier or {}
    return {
        "ticker":            ticker,
        "score":             round(result.get("consensus_score", 0), 2),
        "grade":             result.get("consensus_grade", "?"),
        "conf":              result.get("confidence", ""),
        "thesis":            clip(result.get("majority_thesis"), 1000),
        "key_swing":         clip(result.get("key_swing_factor"), 400),
        # built_at lives on the DOSSIER (dossier.py), not the result — reading it
        # off the result shipped analyzed_at="" on every card for weeks, which is
        # why the boards could never age anything out (found 2026-07-11).
        "analyzed_at":       dossier.get("built_at") or result.get("built_at")
                             or datetime.now(timezone.utc).isoformat(),
        "catalyst":          result.get("catalyst", ""),
        "asymmetry_ratio":   result.get("asymmetry_ratio", ""),
        "rr":                (result.get("risk_reward") or {}).get("rr_ratio"),
        "risk":              (result.get("risk_reward") or {}).get("risk_tier"),
        "banger":            result.get("banger", {}),
        "position_guidance": result.get("position_guidance", {}),
        "cycle_position":    result.get("cycle_position", {}),
        "matched_filters":   meta.get("matched_filters", []),
        "path":              meta.get("path", "A"),
        "verification":      _slim_verification(result.get("verification")),
        "sector":            (dossier.get("profile") or {}).get("sector"),
        "price":             (dossier.get("quote") or {}).get("price"),
        "confirmed":         result.get("confirmed", True),
        "fair_value_composite": result.get("fair_value_composite"),
        "factors":           _factor_stamp(dossier, result) if dossier else None,
        "earnings_in_days":  _earnings_in_days(dossier),
        "debate_digest":     _debate_digest(result),
    }


def collect_portfolio_results(output_dir: Path) -> tuple[list, dict, list, list]:
    """Collect output/*.json ticker result files.

    Returns (results_list, index_dict, scout_cards, reconcile_remove):
      - scout_cards: portfolio/triggered tickers scoring >= BUY_THRESHOLD, as scout
        cards, so a manual `analyze` trigger (or a portfolio run) refreshes the
        ticker's Scout card with the latest run.
      - reconcile_remove: tickers analyzed this run that scored BELOW threshold —
        their stale Scout/Gems card (if any) is removed so Scout stays a clean
        >= threshold board.
    """
    # Dedupe by ticker — keep only the newest file per ticker (filename timestamp
    # sorts ascending, so the last one wins). Prevents writing the same dd:TICKER key
    # multiple times per upload (burns the free-tier KV write budget) and avoids
    # loading every historical file into memory.
    latest: dict[str, Path] = {}
    for path in sorted(output_dir.glob("*.json")):
        if path.name in _SKIP_FILENAMES:
            continue
        fname_ticker = path.stem.split("_")[0].upper()
        latest[fname_ticker] = path

    results = []
    index = {}
    scout_cards = []
    reconcile_remove = []
    for _, path in sorted(latest.items()):
        data = load_json(path)
        if not data:
            continue
        result = data.get("result", {})
        if not result.get("ticker"):
            continue  # skip non-ticker files (e.g. history files)
        ticker = result["ticker"]
        results.append({"key": f"dd:{ticker}", "value": data})
        index[ticker] = {
            "score":   result.get("consensus_score", 0),
            "grade":   result.get("consensus_grade", "?"),
            "conf":    result.get("confidence", ""),
            # Same built_at-lives-on-the-dossier trap as _scout_card above.
            "updated": data.get("dossier", {}).get("built_at")
                       or result.get("built_at", ""),
            "loops":   result.get("loops_run", 0),
            "spread":  result.get("score_spread", 0),
            "rr":      (result.get("risk_reward") or {}).get("rr_ratio"),
            "risk":    (result.get("risk_reward") or {}).get("risk_tier"),
        }
        # Reflect this run on the Scout board: >= threshold refreshes/creates the
        # card; below threshold removes any stale card for the ticker.
        # Hold-mode portfolio runs never populate the Scout board — that list is for
        # new-buy signals on tickers we DON'T already own. We still reconcile-remove
        # stale scout cards for held tickers so the board stays clean.
        if result.get("mode") == "hold":
            reconcile_remove.append(ticker)
        elif result.get("consensus_score", 0) >= BUY_THRESHOLD and surfaces_on_board(result):
            scout_cards.append(_scout_card(ticker, result, data.get("meta", {}), data.get("dossier")))
        else:
            # Below threshold OR failed the confirmation gate — keep off the Scout board.
            reconcile_remove.append(ticker)

    return results, index, scout_cards, reconcile_remove


def collect_scout_results(scout_dir: Path) -> list:
    """Collect BUY-signal scout files written in the last 2h. Returns discoveries_list only.

    Individual scout:TICKER KV keys are intentionally NOT written — nothing in the dashboard
    reads them and they consume ~12 of the 1,000 free-tier KV writes per run.
    Only dd:scouts (the accumulated BUY list) is written via the payload 'scouts' field.
    """
    if not scout_dir.exists():
        return []

    cutoff = time.time() - SCOUT_UPLOAD_WINDOW_SECS
    discoveries = []

    # Deduplicate by ticker — keep only the newest file per ticker
    latest: dict[str, Path] = {}
    for path in scout_dir.glob("*.json"):
        if path.stat().st_mtime < cutoff:
            continue
        ticker = path.stem.split("_")[0].upper()
        if ticker not in latest or path.stat().st_mtime > latest[ticker].stat().st_mtime:
            latest[ticker] = path

    for ticker, path in sorted(latest.items()):
        data = load_json(path)
        if not data:
            continue
        result = data.get("result", {})
        score  = result.get("consensus_score", 0)
        grade  = result.get("consensus_grade", "?")

        if score >= BUY_THRESHOLD and surfaces_on_board(result):
            discoveries.append(_scout_card(ticker, result, data.get("meta", {}), data.get("dossier")))

    return discoveries


GEMS_UPLOAD_WINDOW_SECS = 26 * 3600  # gems runs once/day — look back 26h


def collect_gems_results(gems_dir: Path) -> list:
    """Collect BUY-signal gems files written in the last 26h.

    Same dedup-by-ticker logic as collect_scout_results — keep newest file per ticker.
    Returns a list of discovery dicts for the 'gems' field in the upload payload.
    """
    if not gems_dir.exists():
        return []

    cutoff = time.time() - GEMS_UPLOAD_WINDOW_SECS

    # Deduplicate by ticker — keep only the newest file per ticker
    latest: dict[str, Path] = {}
    for path in gems_dir.glob("*.json"):
        if path.stat().st_mtime < cutoff:
            continue
        ticker = path.stem.split("_")[0].upper()
        if ticker not in latest or path.stat().st_mtime > latest[ticker].stat().st_mtime:
            latest[ticker] = path

    discoveries = []
    for ticker, path in sorted(latest.items()):
        data = load_json(path)
        if not data:
            continue
        result = data.get("result", {})
        score  = result.get("consensus_score", 0)
        grade  = result.get("consensus_grade", "?")

        if score >= BUY_THRESHOLD and surfaces_on_board(result):
            # Reuse _scout_card — a hand-copied field list here had already
            # drifted (gems cards silently lost `sector`), exactly the failure
            # _scout_card's docstring warns about. Gems-specific deltas only:
            card = _scout_card(ticker, result, data.get("meta", {}), data.get("dossier"))
            card["entry_assessment"] = result.get("entry_assessment", "")
            card["path"] = ""  # gems have no A/B/C valuation path — falsy renders as "—"
            discoveries.append(card)

    return discoveries


def collect_watchlist_results(output_dir: Path) -> list:
    """Collect BUYs that crossed BUY_THRESHOLD but FAILED the confirmation gate
    (confirmed is False) AND are not surfaced elsewhere, from both the scouts
    and gems output dirs. These are the 'Under Review' cards for dd:watchlist:
    VETO, REJECTED_STAGE1, and UNVERIFIED holds. A red-team DOWNGRADE is
    excluded here since v3 (2026-07-07) — it surfaces flagged on the main
    board instead (see surfaces_on_board).

    Uses the same recency windows as the confirmed collectors so a stale reject
    ages off the board naturally; dedupes to the newest file per ticker.
    """
    out = []
    for src_dir, window in [(output_dir / "scouts", SCOUT_UPLOAD_WINDOW_SECS),
                            (output_dir / "gems",   GEMS_UPLOAD_WINDOW_SECS)]:
        if not src_dir.exists():
            continue
        cutoff = time.time() - window
        latest: dict[str, Path] = {}
        for path in src_dir.glob("*.json"):
            if path.stat().st_mtime < cutoff:
                continue
            ticker = path.stem.split("_")[0].upper()
            if ticker not in latest or path.stat().st_mtime > latest[ticker].stat().st_mtime:
                latest[ticker] = path
        for ticker, path in sorted(latest.items()):
            data = load_json(path)
            if not data:
                continue
            result = data.get("result", {})
            # Only true confirmation-gate rejects: scored a BUY but confirmed is False.
            if result.get("consensus_score", 0) < BUY_THRESHOLD:
                continue
            if surfaces_on_board(result):
                continue  # CONFIRM or v3 flagged-DOWNGRADE — lives on the main board instead
            card = _scout_card(ticker, result, data.get("meta", {}), data.get("dossier"))
            card["verification"] = result.get("verification", {})
            # Source tag so the Supabase insert can route rejects to the right
            # history table (scout_history vs gems_history).
            card["src"] = "gems" if src_dir.name == "gems" else "scout"
            out.append(card)
    return out


def main():
    if not UPLOAD_SECRET:
        print("[upload] Missing DD_UPLOAD_SECRET — skipping upload")
        sys.exit(0)

    output_dir = Path("output")
    scout_dir  = output_dir / "scouts"

    print("\n[upload] Collecting portfolio results...")
    portfolio_results, index, portfolio_cards, reconcile_remove = collect_portfolio_results(output_dir)
    print(f"  {len(portfolio_results)} ticker file(s) found "
          f"({len(portfolio_cards)} >= threshold, {len(reconcile_remove)} below)")

    print("[upload] Collecting scout results (last 2h)...")
    discoveries = collect_scout_results(scout_dir)
    print(f"  {len(discoveries)} BUY signal(s)")

    # Merge portfolio/triggered cards into the scout payload (dedupe by ticker;
    # a dedicated scout result wins over a portfolio card for the same ticker).
    if portfolio_cards:
        seen = {d["ticker"] for d in discoveries}
        discoveries = discoveries + [c for c in portfolio_cards if c["ticker"] not in seen]
        # A ticker that now qualifies must not also be in the removal list.
        qualifying = {d["ticker"] for d in discoveries}
        reconcile_remove = [t for t in reconcile_remove if t not in qualifying]

    gems_dir = output_dir / "gems"
    print("[upload] Collecting gems results (last 26h)...")
    gems_discoveries = collect_gems_results(gems_dir)
    print(f"  {len(gems_discoveries)} gems BUY signal(s)")

    print("[upload] Collecting watchlist (confirmation-gate rejects)...")
    watchlist = collect_watchlist_results(output_dir)
    print(f"  {len(watchlist)} under-review")

    # Include scout/gems history files for KV backup (cache-miss recovery)
    print("[upload] Collecting scout history for KV backup...")
    histories: dict[str, dict | None] = {
        "scout_history": None, "scout_notified": None, "gems_history": None,
        "scout_seen": None,
    }
    for fname, varname in [
        ("scout_history.json",  "scout_history"),
        ("scout_notified.json", "scout_notified"),
        ("gems_history.json",   "gems_history"),
        ("scout_seen.json",     "scout_seen"),
    ]:
        path = output_dir / fname
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                histories[varname] = data
                print(f"  {len(data)} ticker(s) in {fname}")
        except Exception as e:
            print(f"  [upload] Could not read {fname}: {e}")
    scout_history  = histories["scout_history"]
    scout_notified = histories["scout_notified"]
    gems_history   = histories["gems_history"]
    scout_seen     = histories["scout_seen"]

    # Scoreboard snapshot (written by `signal_analysis.py --json` in the analyze
    # workflow) — uploaded wholesale to KV dd:scoreboard for the dashboard panel.
    scoreboard = None
    sb_path = output_dir / "scoreboard.json"
    try:
        if sb_path.exists():
            scoreboard = json.loads(sb_path.read_text(encoding="utf-8"))
            print(f"[upload] Scoreboard snapshot found ({sb_path})")
    except Exception as e:
        print(f"  [upload] Could not read scoreboard.json: {e}")

    # A 0-signal run still MUST upload: the dedup/notify histories grew this run,
    # and skipping the POST would leave the KV backup stale (a later cache miss
    # would then restore an old window → re-debates and possible re-alerts) and
    # skip the dd:meta heartbeat the health screen watches.
    if (not portfolio_results and not index and not discoveries
            and not gems_discoveries and not reconcile_remove and not watchlist
            and not scout_history and not scout_notified and not gems_history
            and not scout_seen and not scoreboard):
        print("[upload] Nothing to upload.")
        return

    if reconcile_remove:
        print(f"[upload] Reconcile: removing {len(reconcile_remove)} below-threshold "
              f"card(s) from Scout/Gems: {', '.join(reconcile_remove)}")

    payload = {
        "results":          portfolio_results,
        "index":            index,
        "scouts":           _strip_digest(discoveries) if discoveries else None,
        "gems":             _strip_digest(gems_discoveries) if gems_discoveries else None,
        "watchlist":        _strip_digest(watchlist) if watchlist else None,
        "reconcile_remove": reconcile_remove if reconcile_remove else None,
        "scout_history":    scout_history,
        "scout_notified":   scout_notified,
        "gems_history":     gems_history,
        "scout_seen":       scout_seen,
        "scoreboard":       scoreboard,
    }

    payload = _sanitize(payload)

    url = f"{UPLOAD_URL}/api/dd/upload"
    print(f"\n[upload] POSTing {len(portfolio_results)} key(s) + {len(discoveries)} scout + {len(gems_discoveries)} gems BUY(s) + history to {url}...")

    try:
        r = requests.post(url, headers=HEADERS, json=payload, timeout=60)
        data = r.json()
        if data.get("ok"):
            print(f"  Success — {len(data.get('written', []))} key(s) written")
            # KV write-budget observability: the endpoint reports its own KV op
            # count (free tier: 1,000 writes+deletes/day). ~6 scheduled runs/day
            # means a per-run count near 150 would blow the budget — warn early
            # instead of discovering a 403 after the fact.
            kv_ops = data.get("kvWrites")
            if isinstance(kv_ops, (int, float)):
                print(f"  KV ops this upload: {kv_ops:.0f}")
                if kv_ops >= 120:
                    try:
                        from notify import alert_ops
                        alert_ops(f"KV write volume high: {kv_ops:.0f} ops in one upload "
                                  f"(free tier: 1,000/day across ~6 runs). Check what grew.")
                    except Exception:
                        pass
        else:
            print(f"  Partial/failed: {data}")
            if data.get("failed"):
                sys.exit(1)
    except Exception as e:
        print(f"  [upload] Request failed: {e}")
        sys.exit(1)

    _stamp_nav_snapshot()

    # ── Supabase: persist DD history ──────────────────────────────
    if SUPABASE_URL and SUPABASE_KEY:
        print("\n[supabase] Writing DD history + scout history...")
        sb_failed: list[str] = []
        dd_rows = []
        for item in portfolio_results:
            raw = item.get("value", {})
            result  = raw.get("result", {})
            dossier = raw.get("dossier", {})
            if not result.get("ticker"):
                continue
            price     = dossier.get("quote", {}).get("price")
            result_fv = result.get("fair_value_composite")
            comp_fv   = dossier.get("fair_values", {}).get("composite_fair_value")
            try:
                mos = round((result_fv - price) / price, 4) if price and result_fv else None
            except Exception:
                mos = None
            dd_rows.append(_sanitize({
                "ticker":       result["ticker"],
                # run_at is NOT NULL in Supabase — a missing built_at would reject the
                # whole batch insert, so fall back to upload time.
                "run_at":       dossier.get("built_at") or result.get("built_at")
                                or datetime.now(timezone.utc).isoformat(),
                "price":        price,
                "composite_fv": comp_fv,
                "result_fv":    result_fv,
                "mos":          mos,
                "score":        result.get("consensus_score"),
                "grade":        result.get("consensus_grade"),
                "confidence":   result.get("confidence"),
                "archetype":    _extract_archetype(dossier),
                "agent_scores": result.get("agent_final_scores", {}),
                "thesis":       clip(result.get("majority_thesis"), 500),
                "swing":        clip(result.get("key_swing_factor"), 300),
                "is_banger":    bool(result.get("banger", {}).get("is_banger")),
                "full_result":  {k: v for k, v in result.items() if k != "transcript"},
            }))
        if dd_rows:
            if _supabase_insert("dd_history", dd_rows):
                print(f"  {len(dd_rows)} DD row(s) inserted")
            else:
                sb_failed.append("dd_history")

        # Scout history — confirmed discoveries AND confirmation-gate rejects.
        # Rejects were previously never logged, which made the core measurement
        # question ("does the gate add edge?") unanswerable: you need the forward
        # returns of the signals the gate DIDN'T like as the comparison group.
        _watch = watchlist or []
        scout_rows = [_scout_history_row(s) for s in (discoveries or [])]
        scout_rows += [_scout_history_row(w) for w in _watch if w.get("src") != "gems"]
        if scout_rows:
            if _supabase_insert("scout_history", scout_rows):
                print(f"  {len(scout_rows)} scout row(s) inserted")
            else:
                sb_failed.append("scout_history")

        # Gems history (see DATA_CONTRACT.md for the table schema).
        gems_rows = [_gems_history_row(g) for g in (gems_discoveries or [])]
        gems_rows += [_gems_history_row(w) for w in _watch if w.get("src") == "gems"]
        if gems_rows:
            if _supabase_insert("gems_history", gems_rows):
                print(f"  {len(gems_rows)} gems row(s) inserted")
            else:
                sb_failed.append("gems_history")

        # All tables were attempted; now fail the run if any insert was lost so
        # the workflow's Telegram failure alert fires. Before 2026-07-04 these
        # failures were print-and-continue — silent holes in the measurement
        # dataset (the KV upload above already succeeded and is unaffected).
        if sb_failed:
            print(f"[supabase] FAILED after retries: {', '.join(sb_failed)} — "
                  "failing the run so the alert fires")
            sys.exit(1)


if __name__ == "__main__":
    main()
