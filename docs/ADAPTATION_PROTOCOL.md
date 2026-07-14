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
| **v4** | 2026-07-13 | **NTM-PEG disambiguation** (Daryl-raised, MNDY "Forward PEG 0.84" case, then extended same-day to a true long-horizon denominator). `ratios_ttm.fwd_peg` is a **1-year** PEG (fwd P/E ÷ next-FY analyst EPS growth) — quote services publish longer-horizon PEGs that read materially higher, so an unlabelled figure in a thesis is uncheckable. Agents/screeners now cite it as "NTM PEG". ValuationEngine PATH A gains a base-effect trap requiring BOTH checks (not either/or, see below): (1) `ratios_ttm.peg_lt` — NTM PEG < 1.0 while peg_lt > 1.5 means short-horizon-only cheapness; (2) `implied_ntm_growth` vs `fwd_revenue_growth` **checked regardless of what peg_lt shows** — a multi-year earnings-recovery bridge (e.g. post-impairment) can depress both the NTM *and* the long-horizon growth rate similarly, so a lack of divergence between them is not proof of durability (found live: MRVL's NTM PEG ~0.90 and peg_lt 0.81 read cheap together, both riding the same recovery from an 80%-collapsed trailing GAAP base — `implied_ntm_growth` 113% vs a much lower `fwd_revenue_growth` is the tell the divergence check alone would have missed). `ratios_ttm.peg_lt` source chain (`_resolve_eps_cagr`), best-horizon-first: **Finviz "EPS next 5Y"** (true 5-year consensus; tried for every ticker, accepted only when Finviz's own Forward P/E is within 25% of our independently-computed fwd_pe — a real basis mismatch runs 2x+, not a few percent) → **FMP** (~2yr reach in practice) → **Alpha Vantage EARNINGS_ESTIMATES** (~1-2yr). `ratios_ttm.eps_cagr_fwd_years`/`eps_cagr_fwd_src` are the audit trail so agents know which horizon they're looking at. Three sources evaluated and rejected: Yahoo LTG (discontinued), AV's own `PEGRatio` field (unstated basis, returned 0.28 for MNDY — built off the depressed GAAP base, dropped from the dossier), Nasdaq's keyless `earnings-forecast` endpoint (fetched cleanly, deeper than AV, but cross-checked against yfinance's `forwardEps` and found GAAP-basis — MNDY 1.59 vs 5.39, ~3.4x gap — would have produced a falsely-cheap peg_lt; not wired in despite fetching cleanly). Finviz's own basis was verified three ways (its published PEG reconciles exactly via its own Forward P/E ÷ EPS-next-5Y; its adjacent "EPS next Y" matched our independently-computed AV growth to 4 sig figs; the runtime per-ticker cross-check above). Selection-affecting: low-NTM-PEG names may score lower on the base-effect check, in either direction depending on peg_lt/recovery evidence. Also folds the **GICS-first cycle_type defect fix** (live 2026-07-12: sector read Finnhub industry, so semis classified HYBRID not SECULAR, ~0.3-0.5 pts in `cycle_position_adjust`; Daryl-approved, holdings re-baselined same day). *(Unrelated to factors.v, found during this work and fixed separately: `finviz_screener._parse_fundament` had been silently reading only 1 of Finviz's 6 sibling snapshot tables since a markup change, dropping every field `pillar_scoring.compute_composite` reads for Gems triage ranking — a discovery-funnel bug fix, not a debate-scoring change, so not version-stamped here.)* | live — first 4-wk read ≈ 2026-08-10 |
| **v5** | 2026-07-13 | **Fair-value composite recalibration** (Daryl: "Recalibrate Fair value composite now. Give me the best possible implementation" — the same-day audit's backstop tightening only flagged miscalibrated static multiples, it didn't fix them). All 6 `fair_value.py` archetype engines now derive their core valuation multiple from **live peer-median comps** (`_peer_median()`, ≥2 qualifying peers) instead of static hardcoded tables — the static tiers (asset-light EV/FCF 12-25x rule-of-40, mature EV/FCF sector table, cyclical mid-cycle P/E and EV/IC 1.5x flat, financial P/TBV ROE-tiers 0.6-1.8x, infrastructure EV/EBITDA-CapEx) are now **fallback-only**, used when peer data is too thin. `dossier._fetch_peer()` extended with `ev_fcf`/`ev_sales`/`ev_ic`/`price_to_book`; new quality gate (`_usable_peer`/`_better_peer_set`) retries against the curated `SECTOR_PEERS` list when Finnhub's suggested peers carry no usable valuation fields (found live on ETN: Finnhub suggested ADSE/HTOO, both 100% empty — composite ratio 0.27x→0.94x once the curated Industrials set kicked in). Bundled 3 net-debt-subtraction bug fixes surfaced while rewriting the EV-based legs (all three formulas were EV-labeled but never bridged EV→equity): asset-light's secondary leg additionally upgraded from raw Price/Sales to EV/Sales (the more rigorous, capital-structure-neutral comps convention) with the net-debt bridge now applied; cyclical's EV/IC and early-stage's EV/Revenue both gained the same missing subtraction. Every rewired archetype's `assumptions` now records a `*_source` (peer_median vs static_fallback) and the raw peer count for auditability. *(Rides this bump per rule 3, not itself a methodology choice — a defect fix, found via this work's live verification sweep, that changes which signals surface:)* `ratios_ttm.fcf` was silently sourced from yfinance's opaque `info['freeCashflow']`, verified live to diverge 20-70%+ from a genuine trailing-twelve-month figure across a broad sample (NVDA −61%, MU −71%, CAT −52%, MRVL +37%, **NEE sign-flipped**: −$18.5B info-dict vs a real +$2.4B) — fed both the DCF and EV/FCF legs of every archetype. Fixed with `_ttm_fcf_from_quarterly()`, summing the last 4 real quarterly OCF/Capex statements; `info[]` kept only as a fallback when fewer than 4 real quarters exist (recent IPOs). Live effect, 20-ticker sweep spanning all 6 archetypes: composite/price ratios for 18/20 now land within 0.35x-3.0x (AAPL 1.18, NVDA 2.26, KO 0.69 — was 0.10 and wide-gap-flagged before the FCF fix, ETN 0.94, JPM 0.52, O 1.20). **WMT (0.22) and TSLA (0.09) remain wide-gap-flagged** even after the FCF fix — WMT's gap is a disclosed DCF-vs-market-premium disagreement (mechanical 5yr+terminal-multiple DCF vs. the market's quality/moat premium, not a further-fixable code issue without guessing at multiples); TSLA's is a **separate, pre-existing, NOT-fixed-today finding**: it matches none of the 5 specific archetype triggers (Consumer Cyclical sector, capex_intensity ~8.8% under the 12% cyclical cutoff) and falls through to the MATURE_COMPOUNDER default — needs its own sign-off before touching archetype classification, noted here per the no-silent-drift rule and left alone. Selection-affecting: `composite_fair_value` feeds `scoring.py`'s Condition-3 BUY-gate (`floor_iv ≥ price×0.7`), `risk_reward.py`'s downside floor, and `watch_triggers.py`'s target-reached alerts — a name that previously passed or failed the gate on a miscalibrated multiple or a wrong-sign FCF figure may land differently now. 39 new tests this work (`test_peer_comps.py`, `test_fcf_source.py`, +21 peer-median cases in `test_fair_value.py`); 441 total passing. *(Addendum 2026-07-14, same rule-3 ride — the FCF defect fix above was incomplete on the PEER side:* `_fetch_peer`'s `ev_fcf` still read `info['freeCashflow']`, so the subject's now-genuine TTM FCF was being multiplied by a broken-basis peer multiple — live-measured +110% median inflation on a semis peer set (MU 136.0x info-basis vs 39.7x real-TTM) and +25% on industrials; live NVDA composite $471/MoS +57% on a $203 price vs ~$401 once fixed. Fixed with the same `_ttm_fcf_from_quarterly()` treatment + a per-peer `fcf_basis` audit field; peer fetches now 12h-cached (`yf:peer:v1:`) to more than repay the extra call. +4 tests (`test_peer_fcf.py`); 445 total. Same-day second ride-along: `round1_prompt`'s financials_summary exposed ANNUAL statement values under `*_ttm`-suffixed keys and preferred the annual FCF over the EDGAR-reconciled TTM — live NVDA prompt said revenue_ttm \$215.9B/FCF \$96.7B vs true TTM \$253.5B/\$119.1B (−15/−19%); now labelled `latest_fy_*` vs true `*_ttm` + `fcf_ttm_source`, and `latest_fy_sbc` supplied for every archetype (two agents' prompts instruct SBC-adjustment but only ASSET_LIGHT prompts carried the value). +5 tests; 450 total. Daryl-approved data-supply enrichment, same day: 5 prompt asks the dossier never carried — 4yr annual income history + diluted-shares trend (FF margin-trend/buyback mandates), 2yr cashflow history, `sma20`, 60d up/down-volume profile, `short_ratio` days-to-cover (MS mandates) — all zero new API calls; supply-side only, no scoring/gate criteria touched. +7 tests; 457 total. Third same-day batch (Daryl: "keep executing per your best recommendations"): validator's computed-PE cross-check period-matched — TTM net income via a new `_ttm_net_income_from_quarterly()` (annual fallback kept), killing spurious divergence warnings on off-cycle growers (live NVDA gap 27%→0.8%; rides per rule 3 since a spurious warning could tip a 3-warning name into score-docking LOW); the hypergrowth static 25x/40x leg is now source-labelled in key_metrics (`hypergrowth_ntm_multiple`/`_multiple_source`/`_composite_weight` — VISIBILITY ONLY, deliberately not a blind_spot flag because risk_reward penalizes >=2 flags; re-anchoring the multiple itself still needs its own row); llm.py retries transient network timeouts like 5xx (reliability-exempt; a read-timeout forfeited the 07-14 09:57 scout window). +12 tests; 469 total.)* | live — first 4-wk read ≈2026-08-10 |
| v6 | *(reserved)* | Next pre-registered change only. | — |

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
