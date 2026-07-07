"""Shared text-formatting helpers with no project dependencies (leaf module)."""

from __future__ import annotations


def clip(s: str | None, n: int) -> str:
    """Truncate to at most n chars, breaking at the last whole word so text never
    ends mid-word. Adds an ellipsis only when text was actually cut."""
    s = s or ""
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = cut.rfind(" ")
    if sp > n * 0.6:  # don't sacrifice most of the budget hunting for a space
        cut = cut[:sp]
    return cut.rstrip() + "…"
