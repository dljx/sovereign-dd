# Methodology Review — Evidence Audit & the Benchmark-Relative Decision Rule

*Written 2026-07-03. Companion to [CLASSIFICATION_METHODOLOGY.md](CLASSIFICATION_METHODOLOGY.md)
(which documents **how** the pipeline scores; this documents **whether the ingredients have
historical evidence behind them**, and what the system's objective actually is).*

---

## 1. The objective, stated precisely

The system exists to answer one question:

> **Should this dollar buy stock S, or should it buy VWRA?**

VWRA (Vanguard FTSE All-World UCITS, accumulating) is the default sink — the position you hold
when nothing better is proven. A stock recommendation is therefore a claim that:

```
P(win) × E[excess return | win]  +  P(lose) × E[excess return | lose]  >  hurdle
```

where *excess* means **relative to VWRA over the same window**, and the hurdle must cover the
costs a single stock adds over the index: idiosyncratic risk (one thesis can zero out; the index
can't), estimation error in our own fair-value math, and trading/tax drag.

Three honest consequences:

1. **Abstention is a recommendation.** A scan that surfaces nothing is the system working, not
   failing. As of 2026-07-03 an empty scan affirmatively reports "default: VWRA"
   (`notify.alert_scout_summary`).
2. **Nothing makes a single pick "very likely" to outperform.** The best-documented selection
   tilts (§3) historically put roughly **55–60%** 12-month odds on a well-profiled stock beating
   the market — a real edge over the ~48–50% base rate of a random stock (returns are skewed:
   most stocks lose to their index), but nowhere near certainty. Edge compounds across many
   disciplined picks; it does not guarantee any one of them.
3. **The probabilities must come from measurement, not vibes.** Every signal row in
   `scout_history`/`gems_history` carries its entry price, gate outcome, and factor profile
   (`factors` jsonb). *Honesty note:* the price/confirmed capture shipped 2026-06-26 but a
   wiring bug (fields added to the in-memory discovery dicts, while Supabase rows are built
   from the output files) left them NULL until 2026-07-03, when the card builder was fixed to
   read the dossier; pre-fix rows are backfilled with same-day closing prices (approximation).
   Confirmation-gate **rejects** are logged as of 2026-07-03 too (`confirmed = false`) — without
   them, "does the gate add edge?" has no comparison group. The forward-return analysis
   (~mid-July 2026 onward) computes each signal's return **minus VWRA's return over the matched
   window** — the system's real scoreboard, and the calibration source for any future hurdle.

## 2. Base rates — what we're up against

- **SPIVA scorecards** (S&P, updated semiannually): over 10–15 year windows, roughly **85–90% of
  actively managed large-cap funds underperform their benchmark** after costs. Professionals with
  full-time teams mostly lose to the index.
- **Bessembinder (2018), "Do Stocks Outperform Treasury Bills?"**: from 1926–2016, just ~4% of US
  stocks account for **all** net stock-market wealth creation above T-bills; the median stock's
  lifetime return is negative vs T-bills. Stock-picking means fishing in a lake where most fish
  are negative-sum — the payoff distribution is extremely right-skewed.
- **Implication for this system:** the prior on any individual pick is *against* it. The pipeline
  earns its keep only if its selection tilts are drawn from the small set of premia with durable
  evidence (§3), and only if measurement confirms the tilts survive our implementation.

## 3. What has historical evidence (and what doesn't)

Premia with decades of out-of-sample, cross-market documentation. Magnitudes are long-run gross
long-short averages from the academic literature — real but lumpy, with multi-year droughts.

| Factor | Canonical evidence | Historical magnitude (rough) | Caveats |
|---|---|---|---|
| **Momentum (12-1 relative strength)** | Jegadeesh & Titman (1993); Carhart (1997); Geczy & Samonov (two centuries) | ~8–10%/yr long-short; the strongest documented anomaly | **Crashes** when markets whipsaw off a bottom (1932, 2009: −50%+ for long-short); high turnover |
| **Profitability / quality** | Novy-Marx (2013) gross profitability; Fama-French RMW; Asness-Frazzini-Pedersen QMJ | ~3–5%/yr; remarkably persistent | Definitions vary; best paired with a valuation check |
| **Value** | Fama-French (1992/93) HML | ~4–5%/yr long-run | Brutal 2010–2020 drawdown; cheapness alone ≈ junk — needs a quality screen |
| **Insider net buying** | Lakonishok & Lee (2001); Cohen-Malloy-Pomorski (2012) | Several %/yr for opportunistic clusters | Routine/10b5-1 sales are noise; buys >> sells as signal |
| **Earnings-revision momentum / PEAD** | Ball & Brown (1968); Bernard & Thomas (1989) | Drift for ~1–2 quarters post-surprise | Decays fast; weaker in large caps today |
| **Low volatility / BAB** | Frazzini & Pedersen (2014) | Risk-adjusted, not raw, outperformance | Not a raw-return edge; not a current pipeline goal |

**Documented as useless or harmful:**

| Practice | Evidence | Status in this system |
|---|---|---|
| **Analyst price targets** | Systematically ~15–30% over-optimistic; targets chase price; ~no incremental predictive power (Bradshaw et al.) | ±0.3 direct adjustment **removed 2026-07-03** (`scoring.py`). Residual: 0.30-weight blend inside the R:R upside estimate — kept, measured, revisit (§6) |
| **Buying short-term losers ("it fell, so it's cheap")** | 12-month losers keep losing (momentum's flip side); reversal exists only at ~1-month and 3–5-year horizons | `contrarian`/`day_losers` lens **removed 2026-07-03**; triage now deprioritizes negative 12-mo momentum without an identified inflection |
| **Size alone** | Small-cap premium is fragile; mostly junk exposure (Asness et al., "Size Matters, If You Control Your Junk") | Small-cap lenses stay, but only as *candidate sources* feeding quality/momentum-aware triage |

## 4. Audit: the pipeline vs the evidence

| Pipeline component | Verdict | Notes |
|---|---|---|
| Quality signals in triage — ROIC>15%, GM, FCF yield, Rule of 40 (`scout.py:_compute_matched_filters`) | ✅ evidence-aligned | Profitability premium territory |
| BANGER requires insider **net buying** + R:R ≥ 2:1 (`scoring.py`) | ✅ evidence-aligned | |
| `eps_revision_momentum` in the dossier | ✅ evidence-aligned | Revision momentum |
| Adversarial red-team gate on every BUY (`verify.py`) | ✅ design / **was a no-op in practice, fixed 2026-07-03** | Good process on paper — but a 90s timeout starved the grounded call (measured: 18/19 verdicts `UNVERIFIED`, auto-passed via fail-open). Fixed: 300s budget, lean call config, and **fail-closed by default** — no verdict → Under Review, not the alerts feed |
| Two-ladder grading (entry vs hold), softened hold-mode penalties | ✅ sensible design | Behavioral discipline, not a return factor |
| **Price momentum** | **fixed 2026-07-03** | Was absent; "momentum" lens was `most_actives` (volume ≠ momentum). Now: true 52-wk relative-strength lens (`scout._momentum_lens`) + canonical `mom_12_1/mom_6m/mom_1m` in every dossier (`dossier._price_momentum`) |
| **`day_losers` "contrarian" lens** | **removed 2026-07-03** | Anti-evidence (loser continuation) |
| **Analyst-consensus gap ±0.3** | **removed 2026-07-03** | See §3 |
| **LLM debate raw score** — the largest single input | ⚠️ **unvalidated** | Its judgment layer (moats, catalysts, forensics) is plausible and is what factors can't do — but its predictive value is exactly what the outcome measurement will test. Until then it is a hypothesis, not an edge |
| Fair-value composite → R:R matrix | ⚠️ unvalidated | Internally consistent; FV estimation error unmeasured (blocks any numeric hurdle gate — §6) |
| Cycle-position adjustment (±1.0) | ⚠️ judgment call | Macro timing has weak evidence; magnitude is bounded, tracked in `score_adjustments`, measurable later |

## 5. The recommended strategy (what this system now is)

**A funnel where every layer either has documented evidence or is being measured:**

1. **Universe**: all US-listed common stocks > $300M — no coverage bias.
2. **Factor lenses tag the candidates** (momentum = true 52-wk relative strength; quality,
   value, growth, small-cap) — candidates *sourced* from evidence-bearing pools.
3. **Quantitative triage** prefers profitability/quality metrics and deprioritizes documented
   losers (negative 12-mo momentum, insider distribution, estimate cuts near highs).
4. **The 5-agent debate supplies what factors can't**: moat trajectory, catalyst specificity,
   accounting forensics, thesis falsification. This is the system's *potential* edge over a plain
   factor screen — and its least-proven layer, so:
5. **The red-team gate** must falsify every BUY before it surfaces, and
6. **Every signal is stamped** (entry price, gate verdict, factor profile) into an append-only
   log measured **against VWRA**. No pick is graded on story; all are graded on subsequent
   benchmark-relative return.
7. **Abstention is the default.** No qualifying edge → the recommendation *is* VWRA.

Position sizing already caps single names at 4–6% even at max conviction — right order of
magnitude given §2's skew: sized so a zeroed thesis is survivable and a winner still matters.

## 6. Deliberately unchanged — and what unlocks each

| Deferred change | Why deferred | Unlock condition (from the outcome analysis) |
|---|---|---|
| Numeric hurdle gate ("surface only if computed excess return vs VWRA ≥ X%") | FV estimates are noisy; hard-gating on them now is false precision | Measured FV error and hit-rate-by-upside-bucket exist; pick X where historical P(beat VWRA) clears ~55–60% |
| Removing the analyst 0.30 blend inside R:R upside (`risk_reward.py`) | Changes reward tiers in unmeasured ways; tier mapping absorbs most noise; `analyst_only` path is already ×0.6-dampened | Confirmed-BUY returns bucketed by `upside_source` show the blend hurts (or doesn't) |
| Momentum/quality adjustments inside `apply_adjustments` | Internal tuning without internal data = unfalsifiable bet (the shelved momentum overlay stays shelved) | Factor-stamped signals show mom/quality terciles predict excess return **within our own funnel** |
| Reweighting debate score vs factor scores; grade-ladder / BUY-threshold moves | Same | Same |

**The scoreboard (`signal_analysis.py`, added 2026-07-04 — runnable anytime; first meaningful
multi-window read ~mid-July 2026):** for every price-stamped signal — forward return at 1/4/12
weeks **minus VWRA.L** over the matched window; hit rate and average excess bucketed by grade,
confirmed-vs-rejected (does the gate add edge?), verdict, momentum tercile, quality tercile,
methodology version (`factors.v`), and source (scout/gems). Every deferred change above
graduates or dies on those numbers. **How changes are allowed to happen — pre-registration,
version bumps, cadence, kill criteria — is codified in
[ADAPTATION_PROTOCOL.md](ADAPTATION_PROTOCOL.md): the scoreboard is the arbiter, that document
is the law.**

## 7. Honest summary

- Most active strategies lose to the index; most stocks lose to the index. The default position
  is VWRA, and the system now says so out loud.
- The funnel's quantitative ingredients are now aligned with the handful of premia that have
  survived a century of data — momentum, profitability/quality, insider buying, revision
  momentum — and stripped of two documented noise sources (analyst targets, falling-knife
  contrarianism).
- The LLM debate layer is the differentiated bet. It is unproven. It is now instrumented so that
  by late July 2026 there will be a first, small-sample read on whether it earns its complexity —
  and the discipline is pre-committed: **tuning follows measurement, never the reverse.**
- No part of this promises outperformance. It maximizes the probability that *if* the system has
  an edge, we'll know — and that when it doesn't speak, your money is already in the right place.
