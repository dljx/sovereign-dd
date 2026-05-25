"""Upload sovereign-dd output JSONs to Sovereign Eye via the /api/dd/upload endpoint."""

import json
import math
import os
import sys
import time
from pathlib import Path

import requests

from scout import BUY_THRESHOLD

# Only upload scout files written in the last 2 hours — prevents re-uploading
# the entire accumulated history on every run (each run adds 12 files; without
# this filter the puts-per-upload grows unboundedly and blows the KV free tier).
SCOUT_UPLOAD_WINDOW_SECS = 2 * 3600

# Filenames to skip in output/ — not ticker results
_SKIP_FILENAMES = {"scout_history.json", "scout_notified.json"}

UPLOAD_URL    = os.getenv("SOVEREIGN_EYE_URL", "https://master.sovereign-eye.pages.dev")
UPLOAD_SECRET = os.getenv("DD_UPLOAD_SECRET", "")

HEADERS = {
    "Authorization": f"Bearer {UPLOAD_SECRET}",
    "Content-Type": "application/json",
}


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
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"  [upload] Could not read {path.name}: {e}")
        return None


def collect_portfolio_results(output_dir: Path) -> tuple[list, dict]:
    """Collect output/*.json ticker result files. Returns (results_list, index_dict)."""
    results = []
    index = {}

    for path in sorted(output_dir.glob("*.json")):
        if path.name in _SKIP_FILENAMES:
            continue
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
            "updated": result.get("built_at", ""),
            "loops":   result.get("loops_run", 0),
            "spread":  result.get("score_spread", 0),
        }

    return results, index


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

        if score >= BUY_THRESHOLD:
            meta = data.get("meta", {})
            discoveries.append({
                "ticker":           ticker,
                "score":            round(score, 2),
                "grade":            grade,
                "conf":             result.get("confidence", ""),
                "thesis":           result.get("majority_thesis", "")[:200],
                "key_swing":        result.get("key_swing_factor", "")[:150],
                "analyzed_at":      result.get("built_at", ""),
                "catalyst":         result.get("catalyst", ""),
                "asymmetry_ratio":  result.get("asymmetry_ratio", ""),
                "banger":           result.get("banger", {}),
                "position_guidance": result.get("position_guidance", {}),
                "cycle_position":   result.get("cycle_position", {}),
                "matched_filters":  meta.get("matched_filters", []),
                "path":             meta.get("path", "A"),
            })

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

        if score >= BUY_THRESHOLD:
            discoveries.append({
                "ticker":            ticker,
                "score":             round(score, 2),
                "grade":             grade,
                "conf":              result.get("confidence", ""),
                "thesis":            result.get("majority_thesis", "")[:200],
                "key_swing":         result.get("key_swing_factor", "")[:150],
                "analyzed_at":       result.get("built_at", ""),
                "catalyst":          result.get("catalyst", ""),
                "asymmetry_ratio":   result.get("asymmetry_ratio", ""),
                "banger":            result.get("banger", {}),
                "position_guidance": result.get("position_guidance", {}),
                "cycle_position":    result.get("cycle_position", {}),
                "fair_value_composite": result.get("fair_value_composite"),
                "entry_assessment":  result.get("entry_assessment", ""),
            })

    return discoveries


def main():
    if not UPLOAD_SECRET:
        print("[upload] Missing DD_UPLOAD_SECRET — skipping upload")
        sys.exit(0)

    output_dir = Path("output")
    scout_dir  = output_dir / "scouts"

    print("\n[upload] Collecting portfolio results...")
    portfolio_results, index = collect_portfolio_results(output_dir)
    print(f"  {len(portfolio_results)} ticker file(s) found")

    print("[upload] Collecting scout results (last 2h)...")
    discoveries = collect_scout_results(scout_dir)
    print(f"  {len(discoveries)} BUY signal(s)")

    gems_dir = output_dir / "gems"
    print("[upload] Collecting gems results (last 26h)...")
    gems_discoveries = collect_gems_results(gems_dir)
    print(f"  {len(gems_discoveries)} gems BUY signal(s)")

    # Include scout history files for KV backup (cache-miss recovery)
    print("[upload] Collecting scout history for KV backup...")
    scout_history = scout_notified = None
    for fname, varname in [("scout_history.json", "scout_history"), ("scout_notified.json", "scout_notified")]:
        path = output_dir / fname
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if varname == "scout_history":
                    scout_history = data
                else:
                    scout_notified = data
                print(f"  {len(data)} ticker(s) in {fname}")
        except Exception as e:
            print(f"  [upload] Could not read {fname}: {e}")

    if not portfolio_results and not index and not discoveries and not gems_discoveries:
        print("[upload] Nothing to upload.")
        return

    payload = {
        "results":        portfolio_results,
        "index":          index,
        "scouts":         discoveries if discoveries else None,
        "gems":           gems_discoveries if gems_discoveries else None,
        "scout_history":  scout_history,
        "scout_notified": scout_notified,
    }

    payload = _sanitize(payload)

    url = f"{UPLOAD_URL}/api/dd/upload"
    print(f"\n[upload] POSTing {len(portfolio_results)} key(s) + {len(discoveries)} scout + {len(gems_discoveries)} gems BUY(s) + history to {url}...")

    try:
        r = requests.post(url, headers=HEADERS, json=payload, timeout=60)
        data = r.json()
        if data.get("ok"):
            print(f"  Success — {len(data.get('written', []))} key(s) written")
        else:
            print(f"  Partial/failed: {data}")
            if data.get("failed"):
                sys.exit(1)
    except Exception as e:
        print(f"  [upload] Request failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
