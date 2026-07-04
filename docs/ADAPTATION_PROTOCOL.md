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
| v3 | *(reserved)* | Next pre-registered change only. | — |

## 5. Currently pre-registered experiments (from METHODOLOGY_REVIEW §6)

| Change | Metric | Graduate if | Kill if | Earliest read |
|---|---|---|---|---|
| v2 as a whole vs legacy | 4-wk excess vs VWRA, factors_v bucket | v2 hit ≥55% and mean excess > legacy's at n≥30 | v2 mean excess < 0 at n≥50 | ~2026-08-15 |
| Confirmation gate adds edge | confirmed vs rejected buckets, 4-wk excess | confirmed − rejected mean excess > 0 with consistent sign across windows | rejected outperforms at n≥30 each | ~2026-08-15 |
| Remove analyst 0.30 blend in R:R upside | confirmed-BUY excess bucketed by `upside_source` | blend-influenced picks underperform | blend-influenced picks outperform | first quarterly review |
| Momentum/quality adjustments in scoring | mom/quality tercile buckets predict excess within our funnel | monotone tercile ordering at n≥30/bucket | no ordering after 2 quarters | first quarterly review |
| Numeric hurdle gate (excess ≥ X%) | measured FV error + hit-rate-by-upside-bucket | bucket with P(beat VWRA) ≥55–60% identifiable | FV error too wide to bucket | needs ~1 quarter of data |

Each row that graduates ships as its own version bump with its own register entry. Each row
that dies gets its status recorded here — negative results are results.

## 6. What this protocol refuses to do

- Chase anything that "worked last month" (that is the noise floor talking).
- Ship two ideas in one version, ever.
- Let an LLM (or any optimizer) close the loop on its own scoreboard.
- Treat the absence of edge as a failure state — abstention to VWRA **is** the system working.
