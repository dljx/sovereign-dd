# Adaptation Protocol — how this system is allowed to change itself

*Written 2026-07-04. Companion to [METHODOLOGY_REVIEW.md](METHODOLOGY_REVIEW.md) (§6 defines
what is deferred and why) and [CLASSIFICATION_METHODOLOGY.md](CLASSIFICATION_METHODOLOGY.md)
(how scoring works today). The scoreboard is the arbiter; this document is the law.*

---

## 1. Purpose

An "eternally self-improving" stock picker is not a realistic goal: markets are adversarial
(edges decay as they are exploited), regimes are non-stationary (optimizing on recent history
fits the last regime), and the feedback channel is thin — at ~100–150 signals per quarter with
weekly-to-quarterly horizons, the pipeline receives only a handful of statistically independent
bits of information per year. Any process that "improves" itself faster than information
arrives is, mathematically, fitting noise (the backtest-overfitting result).

What *is* realistic is an eternally **self-correcting** system: one that forever takes bets
only from externally evidenced priors, measures every decision against the benchmark, kills
what measurably hurts, and abstains to VWRA when nothing clears the bar. This protocol fixes
the maximum honest speed of that loop and defends it from its one fatal failure mode —
Goodhart's law (tuning against our own scoreboard until the scoreboard stops meaning anything).

## 2. The loop

```
propose (scoreboard evidence, external literature)
   → pre-register here (§4) BEFORE shipping: change, metric, thresholds, earliest read
   → ship in one commit with a factors.v bump (upload_kv._factor_stamp)
   → measure out-of-sample: signal_analysis.py, excess return vs VWRA, bucketed by factors.v
   → graduate / kill / revert at the pre-registered read date
```

## 3. Rules (binding)

1. **One structural change at a time.** Two changes in one version = a confounded experiment;
   neither can graduate on the data.
2. **Pre-register before shipping.** Every selection-affecting change gets a §4 register row
   *before* it ships: what changes, the metric, the graduation threshold, the kill threshold,
   and the earliest read date. Earliest read = when ≥30 measurable 4-week signals exist under
   the new version (at the current signal rate, roughly 6–8 weeks after shipping).
3. **Cadence: structural changes at most quarterly.** Reliability and bug fixes are exempt —
   unless they change *which signals surface* (e.g. the 2026-07-03 fail-closed gate), in which
   case they ride the next version bump and are noted in the register.
4. **Kill criteria bind exactly like graduation criteria.** Reverting to a measured-better
   prior version is allowed at any time — a revert is not a new experiment.
5. **Version stamping is mandatory and atomic.** Any selection-affecting change bumps `v` in
   `upload_kv._factor_stamp` in the *same commit* as its register row. A change without a bump
   is invisible to measurement and therefore forbidden.
6. **The scoreboard is the sole arbiter.** `signal_analysis.py` forward returns minus VWRA
   over matched windows, on signals logged *after* the change shipped. No backtests we cannot
   run honestly, no narrative post-hoc rationalization, no tuning on vibes.
7. **Human-in-the-loop is permanent.** The system proposes (scoreboard, digest, Under Review
   board); Daryl disposes. The pipeline never modifies its own selection logic. This is not a
   temporary training wheel — full autonomy would add tail risk without adding one bit of
   feedback data.
8. **Small-sample humility.** Bucket reads with n<30 are directional at best; a single window
   is never decisive; 1-week reads never graduate anything alone (they exist to catch
   catastrophe, not to declare victory).

## 4. Version register (append-only)

| `factors.v` | Shipped | What changed (selection-affecting) | Status |
|---|---|---|---|
| *(unstamped)* | ≤ 2026-07-02 | Legacy: most_actives "momentum" lens, day_losers contrarian lens, analyst-gap ±0.3 in scoring, fail-open red-team gate. Entry prices backfilled with same-day closes (approx). | baseline / superseded |
| **v2** | 2026-07-03 | True 52-wk relative-strength momentum lens; day_losers lens removed; analyst-consensus-gap adjustment removed; quality composite + canonical momentum stamped in dossier; red-team gate un-starved (300s) and **fail-closed** (selection-affecting: fewer auto-passed BUYs). | live — first 4-wk read ≈ 2026-08-15 |
| **v3** | 2026-07-07 | The gate grades instead of gates: R:R cross-check divergence (agents' bull/bear targets vs the computed R:R) demoted from a Stage-1 auto-reject to red-team input — a Stage-2 lead, not proof of a problem. A red-team DOWNGRADE ("real concerns, not fatal") now surfaces ⚠️-tagged on the main board + Trade Alerts instead of being suppressed identically to a VETO (VETO / REJECTED_STAGE1 / UNVERIFIED still route to Under Review). Calm-window re-verification (reliability, not selection — noted here per rule 3) gives UNVERIFIED holds one more attempt once a run's debates finish, keys idle. Shakedown correction of a gate that was only operative since 2026-07-03 (before that, ~95% of BUYs auto-passed UNVERIFIED, so this is a first calibration, not a tune on outcome data); cadence exception (rule 3) approved by Daryl. | live — first 4-wk read ≈ 2026-09-15 |
| **v4** | 2026-07-13 | NTM-PEG disambiguation (Daryl-raised, MNDY "Forward PEG 0.84" case): `ratios_ttm.fwd_peg` is a **1-year** PEG (fwd P/E ÷ next-FY analyst EPS growth) — quote services publish longer-horizon PEGs that read materially higher, so an unlabelled figure in a thesis is uncheckable. Agents/screeners now cite it as "NTM PEG", and ValuationEngine PATH A gains a base-effect trap: a sub-1.0 NTM PEG must be durability-checked against `implied_ntm_growth` vs `fwd_revenue_growth` (a towering EPS jump = margin catch-up / SBC-blind non-GAAP basis gap, not compounding) + web research on years-2-5 growth before scoring as underpriced growth. Long-horizon PEG sources verified: Yahoo no longer publishes per-stock LTG, and Alpha Vantage's PEGRatio has an unstated basis that returned **0.28** for MNDY (built off the depressed GAAP base — the exact artifact it should correct), so AV PEGRatio was **dropped from the dossier**. *(Same-day amendment, Daryl-prompted: FMP's analyst-estimates — already integrated — returns up to 5 future FYs of consensus EPS on covered symbols, so `ratios_ttm.peg_lt` = fwd P/E ÷ FY+1→FY+2/3 consensus EPS CAGR ships after all, with a KNOWN basis; None for FMP-uncovered symbols (free tier 402s e.g. MNDY/MRVL), where the trap falls back to `implied_ntm_growth`-vs-revenue reasoning + web research.)* Selection-affecting: low-NTM-PEG rebound names may score lower. Also folds the **GICS-first cycle_type defect fix** (live 2026-07-12: sector read Finnhub industry, so semis classified HYBRID not SECULAR, ~0.3-0.5 pts in `cycle_position_adjust`; Daryl-approved, holdings re-baselined same day). | live — first 4-wk read ≈ 2026-08-10 |
| v5 | *(reserved)* | Next pre-registered change only. | — |

**Defect fixes folded into v3, same-day (2026-07-07, before any v3 signal aged into a read
— no comparison is contaminated):** R2 cross-examination pairing now excludes failed agents
(`debate._r2_pairing`) — pairing used to sort on a failed agent's fabricated 5.0/empty
thesis, so a live agent could burn its challenge round on an empty position; the fix
restores the behavior `_live_scores` already enforced for the convergence statistics.
These are documented here per the no-silent-drift rule, not as new versions: they correct
unintended behavior rather than change any intended criterion.

## 5. Currently pre-registered experiments (from METHODOLOGY_REVIEW §6)

| Change | Metric | Graduate if | Kill if | Earliest read |
|---|---|---|---|---|
| v2 as a whole vs legacy | 4-wk excess vs VWRA, factors_v bucket | v2 hit ≥55% and mean excess > legacy's at n≥30 | v2 mean excess < 0 at n≥50 | ~2026-08-15 |
| Confirmation gate adds edge | confirmed vs rejected buckets, 4-wk excess | confirmed − rejected mean excess > 0 with consistent sign across windows | rejected outperforms at n≥30 each | ~2026-08-15 |
| Remove analyst 0.30 blend in R:R upside | confirmed-BUY excess bucketed by `upside_source` | blend-influenced picks underperform | blend-influenced picks outperform | first quarterly review |
| Momentum/quality adjustments in scoring | mom/quality tercile buckets predict excess within our funnel | monotone tercile ordering at n≥30/bucket | no ordering after 2 quarters | first quarterly review |
| Numeric hurdle gate (excess ≥ X%) | measured FV error + hit-rate-by-upside-bucket | bucket with P(beat VWRA) ≥55–60% identifiable | FV error too wide to bucket | needs ~1 quarter of data |
| v3: divergence → red-team input; DOWNGRADE surfaces flagged | verdict bucket, 4-wk excess: CONFIRM vs DOWNGRADE | DOWNGRADE mean excess ≥ 0 and not materially below CONFIRM's | DOWNGRADE mean excess < 0 AND materially below CONFIRM's, each at n≥30 | ~2026-09-15 |

Each row that graduates ships as its own version bump with its own register entry. Each row
that dies gets its status recorded here — negative results are results.

**Candidates raised by the 2026-07-07 picking-logic/agentic-architecture review — NOT
pre-registered, NOT shipped.** They need their own graduate/kill thresholds and Daryl's
sign-off before they earn a §4 row:
- **Benchmark-anchored scoring rubric.** Today's consensus score is mostly LLM debate
  judgment shaped by durability/cycle multipliers; the deterministic R:R layer contributes
  at most ±1.0 of 10. A candidate fix: one calibration line in the agent prompts stating that
  a 7.0 means "expected to beat a global index fund, risk-adjusted, over 6–12 months" — makes
  the score closer to a literal graded-R:R-vs-VWRA ranking instead of a quality composite that
  merely correlates with one.
- **Horizon unification.** Agents currently reason across mixed 3–12 month framings
  (`ROUND1_TEMPLATE`, `CatalystHunter`'s cycle/catalyst timelines); Daryl's stated objective is
  annual ("in any given year"). Unifying the horizon language across `agents.py` is deferred
  until the new `_DEFAULT_WINDOWS` 26/52-week buckets (added alongside v3) have enough signal
  to show whether today's shorter-horizon framing already tracks annual outcomes well enough
  to leave alone.

## 6. What this protocol refuses to do

- Chase anything that "worked last month" (that is the noise floor talking).
- Ship two ideas in one version, ever.
- Let an LLM (or any optimizer) close the loop on its own scoreboard.
- Treat the absence of edge as a failure state — abstention to VWRA **is** the system working.
