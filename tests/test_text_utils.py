"""text_utils.clip — word-boundary truncation (the thesis-cutoff fix).

Moved out of test_signal_capture.py (2026-07-07) when clip() became a shared
helper used by both upload_kv.py and notify.py, not an upload_kv-private one.
"""

from text_utils import clip


def test_clip_passes_short_text_through_unchanged():
    assert clip("short thesis", 1000) == "short thesis"
    assert clip(None, 1000) == ""
    assert clip("", 1000) == ""


def test_clip_breaks_at_word_boundary_not_mid_word():
    text = "EXLS is a high-quality compounder currently mispriced as a legacy BPO in terminal decline"
    out = clip(text, 50)
    assert len(out) <= 51  # + ellipsis
    assert out.endswith("…")
    assert not out[:-1].endswith(" ")  # trailing space stripped before ellipsis
    # must not have chopped a word in half — the char before the cut in the
    # original text must be a word boundary (space) in the source string
    stripped = out[:-1].rstrip()
    assert text.startswith(stripped)
    next_char_idx = len(stripped)
    assert next_char_idx == len(text) or text[next_char_idx] == " "


def test_clip_hard_cuts_when_no_good_word_boundary():
    # one giant "word" with no spaces near the cutoff — falls back to a hard cut
    text = "x" * 2000
    out = clip(text, 100)
    assert out == "x" * 100 + "…"
