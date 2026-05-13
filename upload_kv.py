"""Upload sovereign-dd output JSONs to Cloudflare KV after a GitHub Actions run."""

import json
import os
import sys
from pathlib import Path

import requests

CF_ACCOUNT_ID    = os.getenv("CF_ACCOUNT_ID", "")
CF_API_TOKEN     = os.getenv("CF_API_TOKEN", "")
CF_KV_NAMESPACE_ID = os.getenv("CF_KV_NAMESPACE_ID", "")

BASE_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}"
HEADERS  = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type":  "application/json",
}


def _put(key: str, value: dict | list) -> bool:
    """PUT a JSON value into KV."""
    r = requests.put(
        f"{BASE_URL}/values/{key}",
        headers={**HEADERS, "Content-Type": "application/json"},
        data=json.dumps(value, default=str),
        timeout=30,
    )
    if not r.ok:
        print(f"  [kv] FAILED {key}: {r.status_code} {r.text[:200]}")
    return r.ok


def _get(key: str) -> dict | list | None:
    """GET a JSON value from KV (returns None if missing)."""
    r = requests.get(f"{BASE_URL}/values/{key}", headers=HEADERS, timeout=15)
    if r.status_code == 404:
        return None
    if not r.ok:
        return None
    try:
        return r.json()
    except Exception:
        return None


def upload_portfolio_results(output_dir: Path) -> dict:
    """Upload all output/*.json files as dd:{TICKER}, return index entries."""
    index = _get("dd:index") or {}
    uploaded = {}

    for path in sorted(output_dir.glob("*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as e:
            print(f"  [kv] Could not read {path.name}: {e}")
            continue

        result = data.get("result", {})
        ticker = result.get("ticker") or path.stem.split("_")[0].upper()

        # Merge new data with existing: keep if same ticker, replace if newer
        existing = index.get(ticker, {})
        ts = result.get("built_at") or path.stat().st_mtime

        kv_key = f"dd:{ticker}"
        print(f"  [kv] Uploading {kv_key}...", end=" ")
        ok = _put(kv_key, data)
        print("OK" if ok else "FAIL")

        if ok:
            index[ticker] = {
                "score":   result.get("consensus_score", 0),
                "grade":   result.get("consensus_grade", "?"),
                "conf":    result.get("confidence", ""),
                "updated": result.get("built_at", str(ts)),
                "loops":   result.get("loops_run", 0),
                "spread":  result.get("score_spread", 0),
            }
            uploaded[ticker] = index[ticker]

    return index, uploaded


def upload_scout_results(scout_dir: Path) -> list[dict]:
    """Upload scout/*.json files as scout:{TICKER}, update scouts index."""
    if not scout_dir.exists():
        return []

    discoveries = []
    for path in sorted(scout_dir.glob("*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as e:
            print(f"  [kv] Could not read {path.name}: {e}")
            continue

        result = data.get("result", {})
        ticker = result.get("ticker") or path.stem.split("_")[0].upper()
        score  = result.get("consensus_score", 0)
        grade  = result.get("consensus_grade", "?")

        kv_key = f"scout:{ticker}"
        print(f"  [kv] Uploading {kv_key}...", end=" ")
        ok = _put(kv_key, data)
        print("OK" if ok else "FAIL")

        if ok and score >= 7.0:
            discoveries.append({
                "ticker":    ticker,
                "score":     round(score, 2),
                "grade":     grade,
                "conf":      result.get("confidence", ""),
                "thesis":    result.get("majority_thesis", "")[:200],
                "key_swing": result.get("key_swing_factor", "")[:150],
                "analyzed_at": result.get("built_at", ""),
            })

    return discoveries


def main():
    if not CF_ACCOUNT_ID or not CF_API_TOKEN or not CF_KV_NAMESPACE_ID:
        print("[kv] Missing CF_ACCOUNT_ID / CF_API_TOKEN / CF_KV_NAMESPACE_ID — skipping upload")
        sys.exit(0)

    output_dir = Path("output")
    scout_dir  = output_dir / "scouts"

    print("\n[kv] Uploading portfolio results...")
    index, uploaded = upload_portfolio_results(output_dir)

    print("\n[kv] Uploading scout results...")
    scout_discoveries = upload_scout_results(scout_dir)

    # Save scouts list separately for frontend to query
    if scout_discoveries:
        # Merge with any existing scouts, keep last 20
        existing_scouts = _get("dd:scouts") or []
        # Deduplicate by ticker, newest wins
        by_ticker = {s["ticker"]: s for s in existing_scouts}
        for d in scout_discoveries:
            by_ticker[d["ticker"]] = d
        merged = sorted(by_ticker.values(), key=lambda x: x.get("analyzed_at", ""), reverse=True)[:20]
        print(f"\n[kv] Updating dd:scouts ({len(merged)} entries)...", end=" ")
        print("OK" if _put("dd:scouts", merged) else "FAIL")

    # Update master index
    print(f"\n[kv] Updating dd:index ({len(index)} tickers)...", end=" ")
    print("OK" if _put("dd:index", index) else "FAIL")

    print(f"\n[kv] Done. Uploaded {len(uploaded)} portfolio result(s), "
          f"{len(scout_discoveries)} scout BUY signal(s).")


if __name__ == "__main__":
    main()
