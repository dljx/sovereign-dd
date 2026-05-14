"""Shared live-event emitter — used by both debate.py and dossier.py."""

import asyncio
import os
import time

import requests

_LIVE_URL    = os.getenv("SOVEREIGN_EYE_URL", "https://master.sovereign-eye.pages.dev")
_LIVE_SECRET = os.getenv("DD_UPLOAD_SECRET", "")


async def emit_live(ticker: str, event: dict) -> None:
    """Fire-and-forget: POST a single live event to sovereign-eye KV.
    Non-blocking — failures are silently swallowed so they never stall the pipeline."""
    if not _LIVE_SECRET:
        return
    event["ts"] = time.time()
    payload = {"ticker": ticker, "event": event}
    headers = {"Authorization": f"Bearer {_LIVE_SECRET}", "Content-Type": "application/json"}
    try:
        await asyncio.to_thread(
            requests.post,
            f"{_LIVE_URL}/api/dd/live",
            json=payload,
            headers=headers,
            timeout=8,
        )
    except Exception:
        pass
