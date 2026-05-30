"""Single source of truth for the 7-tier grade ladder, BUY threshold, and colors.

Imported by debate.py, scoring.py, report.py, scout.py, gems.py so the thresholds
can never drift apart.
"""

BUY_THRESHOLD = 6.5

# (min_score, label), descending.
_LADDER = [
    (9.0, "CONVICTION BUY"),
    (8.0, "STRONG BUY"),
    (6.5, "BUY"),
    (5.0, "HOLD"),
    (3.5, "SELL"),
    (2.0, "STRONG SELL"),
]


def grade(score: float) -> str:
    """7-tier grading scale."""
    for threshold, label in _LADDER:
        if score >= threshold:
            return label
    return "AVOID"


GRADE_COLORS = {
    "CONVICTION BUY": "bold bright_green",
    "STRONG BUY":     "bold green",
    "BUY":            "green",
    "HOLD":           "yellow",
    "SELL":           "red",
    "STRONG SELL":    "bold red",
    "AVOID":          "bold bright_red",
}
