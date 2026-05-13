"""Upload sovereign-dd output JSONs to Sovereign Eye via the /api/dd/upload endpoint."""

import json
import os
import sys
from pathlib import Path

import requests

UPLOAD_URL    = os.getenv("SOVEREIGN_EYE_URL", "https://master.sovereign-eye.pages.dev")
UPLOAD_SECRET = os.getenv("DD_UPLOAD_SECRET", "")

HEADERS = {
    "Authorization": f"Bearer {UPLOAD_SECRET}",
    "Content-Type": "application/json",
}


def load_json(path: Path) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"  [upload] Could not read {path.name}: {e}")
        return None


def collect_portfolio_results(output_dir: Path) -> tuple[list, dict]:
    """Collect all output/*.json files. Returns (results_list, index_dict)."""
    results = []
    index = {}

    for path in sorted(output_dir.glob("*.json")):
        data = load_json(path)
        if not data:
            continue
        result = data.get("result", {})
        ticker = result.get("ticker") or path.stem.split("_")[0].upper()
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


def collect_scout_results(scout_dir: Path) -> tuple[list, list]:
    """Collect scout/*.json files. Returns (results_list, discoveries_list)."""
    if not scout_dir.exists():
        return [], []

    results = []
    discoveries = []

    for path in sorted(scout_dir.glob("*.json")):
        data = load_json(path)
        if not data:
            continue
        result = data.get("result", {})
        ticker = result.get("ticker") or path.stem.split("_")[0].upper()
        score  = result.get("consensus_score", 0)
        grade  = result.get("consensus_grade", "?")

        results.append({"key": f"scout:{ticker}", "value": data})

        if score >= 7.0:
            discoveries.append({
                "ticker":      ticker,
                "score":       round(score, 2),
                "grade":       grade,
                "conf":        result.get("confidence", ""),
                "thesis":      result.get("majority_thesis", "")[:200],
                "key_swing":   result.get("key_swing_factor", "")[:150],
                "analyzed_at": result.get("built_at", ""),
            })

    return results, discoveries


def main():
    if not UPLOAD_SECRET:
        print("[upload] Missing DD_UPLOAD_SECRET — skipping upload")
        sys.exit(0)

    output_dir = Path("output")
    scout_dir  = output_dir / "scouts"

    print("\n[upload] Collecting portfolio results...")
    portfolio_results, index = collect_portfolio_results(output_dir)
    print(f"  {len(portfolio_results)} ticker file(s) found")

    print("[upload] Collecting scout results...")
    scout_results, discoveries = collect_scout_results(scout_dir)
    print(f"  {len(scout_results)} scout file(s), {len(discoveries)} BUY signal(s)")

    all_results = portfolio_results + scout_results

    if not all_results and not index:
        print("[upload] Nothing to upload.")
        return

    payload = {
        "results": all_results,
        "index":   index,
        "scouts":  discoveries if discoveries else None,
    }

    url = f"{UPLOAD_URL}/api/dd/upload"
    print(f"\n[upload] POSTing {len(all_results)} keys to {url}...")

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
