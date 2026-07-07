"""debate._r2_pairing — cross-examination targets come from LIVE positions only.

2026-07-07 audit fix: pairing used to sort the raw scores dict, which includes a
failed agent's fabricated 5.0/empty-thesis fallback — a live agent could be
paired to "challenge" an empty position, wasting an R2/R3 round. Pairing now
applies the same live-agents-only rule _live_scores already enforces for the
convergence statistics.
"""

from debate import _r2_pairing


def _results(failed=()):
    agents = ["A", "B", "C", "D", "E"]
    return {a: ({"_failed": True} if a in failed else {"score": 1}) for a in agents}


def test_all_live_pairs_extremes_inward():
    scores = {"A": 3.0, "B": 5.0, "C": 6.0, "D": 7.0, "E": 9.0}
    t = _r2_pairing(scores, _results())
    assert t["A"] == "E" and t["E"] == "A"   # most bearish <-> most bullish
    assert t["B"] == "D" and t["D"] == "B"
    # middle agent challenges the extreme further from the live mean (6.0): tie -> bull
    assert t["C"] == "E"


def test_failed_agent_excluded_from_pairing():
    """The failed agent's fake 5.0 must not occupy a pairing slot — the four live
    agents pair among themselves, and the failed one is pointed at a live extreme."""
    scores = {"A": 3.0, "B": 5.0, "C": 6.0, "D": 7.0, "E": 9.0}
    t = _r2_pairing(scores, _results(failed=("B",)))
    assert t["A"] == "E" and t["E"] == "A"
    assert t["C"] == "D" and t["D"] == "C"   # live agents pair inward, skipping B
    assert t["B"] == "E"                     # failed agent still runs, vs live extreme
    # nobody is assigned to challenge the failed agent's empty thesis
    assert "B" not in {t[a] for a in ("A", "C", "D", "E")}


def test_all_failed_falls_back_to_full_set():
    scores = {"A": 3.0, "B": 9.0}
    t = _r2_pairing(scores, _results(failed=("A", "B", "C", "D", "E")))
    assert t["A"] == "B" and t["B"] == "A"


def test_single_live_agent_degenerate():
    scores = {"A": 3.0, "B": 9.0}
    t = _r2_pairing(scores, {"A": {"score": 1}, "B": {"_failed": True}})
    assert t["A"] == "A"   # sole live agent — nothing real to challenge
    assert t["B"] == "A"
