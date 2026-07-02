# Classification Methodology — BUY / ADD · HOLD · SELL / TRIM

How Sovereign DD turns a stock into a rating. This documents the full pipeline:
the multi-agent debate that produces a raw score, the deterministic adjustment
layers that refine it, the two grade ladders (entry vs. hold), and the
confirmation gate that decides whether a BUY is actually surfaced.

> **Source of truth:** all thresholds live in [`grading.py`](../grading.py). The
> adjustment math lives in [`scoring.py`](../scoring.py) and
> [`risk_reward.py`](../risk_reward.py); the debate in [`debate.py`](../debate.py)
> / [`agents.py`](../agents.py); the BUY gate in [`verify.py`](../verify.py). If
> code and this doc ever disagree, the code wins — update this file.
>
> **Why these ingredients:** see [METHODOLOGY_REVIEW.md](METHODOLOGY_REVIEW.md) —
> the evidence audit against the historical factor literature, and the system's
> benchmark-relative objective (beat VWRA with margin, or abstain to VWRA).

---

## 1. The two vocabularies: entry mode vs. hold mode

The system answers two different questions with two different ladders. **The
score is computed the same way; only the labels and the cutoffs change.**

| Question | Mode | Trigger | Ladder |
|---|---|---|---|
| "Should I **buy** this today?" | **entry** (`is_holding=False`) | Scout & Gems discovery runs | BUY family |
| "Should I **keep** this?" | **hold** (`is_holding=True`) | Portfolio runs, or any re-run of a ticker you currently hold | ADD/HOLD/TRIM family |

Hold-mode is entered automatically: a portfolio screen runs every holding as
`is_holding=True`, and even a one-off scout re-run of a ticker you own
**switches to hold-mode** so the dashboard never shows "SELL" on a position the
portfolio screen calls "TRIM" (`main.py:_run_single`).

Why two ladders: a late-cycle macro view or a thin analyst target shouldn't push
a multi-year compounder you already own to SELL. Hold-mode uses a **lower bar to
keep** (5.5 vs. the 6.5 needed to initiate) and softens several penalties (see §5).

---

## 2. The grade ladders (the actual cutoffs)

### Entry ladder — `grade(score)` (scout / gems)

| Score (0–10) | Label | Meaning |
|---|---|---|
| ≥ 9.0 | **CONVICTION BUY** | Highest tier; the bot's rarest call |
| ≥ 8.0 | **STRONG BUY** | High-conviction initiate |
| ≥ 6.5 | **BUY** | Actionable initiate |
| ≥ 5.0 | **HOLD** | Watch, don't initiate |
| ≥ 3.5 | **SELL** | Avoid / exit |
| ≥ 2.0 | **STRONG SELL** | Strong avoid |
| < 2.0 | **AVOID** | Worst tier |

### Hold ladder — `grade_hold(score)` (portfolio / owned names)

| Score (0–10) | Label | Meaning |
|---|---|---|
| ≥ 7.0 | **ADD** | Increase the position |
| ≥ 5.5 | **HOLD** | Keep as-is |
| ≥ 3.5 | **TRIM** | Reduce |
| < 3.5 | **EXIT** | Close the position |

### `BUY_THRESHOLD = 7.0` — the surfacing lever

Distinct from the 6.5 "BUY" grade. **`BUY_THRESHOLD` (7.0) is the single gate for
whether a name is surfaced at all** — it controls Telegram alerts, the KV upload,
and what appears on the Scout/Gems dashboards. A 6.5–6.99 result is graded "BUY"
internally but does **not** cross the surfacing threshold, so it won't alert. This
keeps one lever for "what's worth showing the user."

`HOLD_THRESHOLD = 5.5` is the hold-mode floor below which a position drops to
TRIM/EXIT pressure.

---

## 3. Stage 0 — the raw score: 5-agent adversarial debate

Every rating starts from a structured debate among five specialist agents
(`agents.py`), each owning one analytical layer:

| Agent | Mandate |
|---|---|
| **StructuralEdge** | Value-chain chokepoint, moat durability & trajectory (widening/stable/narrowing), 3–5yr obsolescence risk |
| **FundamentalForensics** | Fundamental quality & capital efficiency (returns on capital, margins, balance sheet, accounting integrity) |
| **ValuationEngine** | Valuation disconnection & dynamic fair value (DCF, multiples, fair-value composite) |
| **CatalystHunter** | Forward catalysts, risks, macro, and **cycle positioning** (phase + cyclical/secular type) |
| **MarketStructure** | Market structure & execution mechanics (liquidity, positioning, technicals) |

**Convergence loop** (`debate.py`): agents score in Round 1, then run up to
`MAX_LOOPS = 3` rebuttal rounds. After each loop the **spread** (max − min of the
live agent scores) is measured against `CONVERGENCE_THRESHOLD = 2.5`:

- **spread ≤ 2.5** → converged → a **synthesis** call blends the positions.
- **not converged** after the loops (or narrowing < 0.1/loop, a stall) → a
  **moderator** call adjudicates the disagreement.

The output is a `raw_consensus_score` and a `confidence` (HIGH / MEDIUM / LOW).

**Integrity guards:**
- A failed agent emits a placeholder 5.0; those are **excluded** from
  spread/mean/convergence math so a crash can't fake a neutral consensus
  (`_live_scores`).
- ≥ 2 agents failed → confidence forced to **LOW**; 1 failed → HIGH demoted to
  MEDIUM.
- Agent system prompts are hardened against prompt-injection from fetched web
  content (instructions inside dossier text are ignored).

This raw score is **never** the final rating on its own — it feeds the
adjustment pipeline.

---

## 4. Stage 1 — deterministic adjustment pipeline (`apply_adjustments`)

Python — not the LLM — does the math that turns `raw_consensus_score` into the
graded `consensus_score`. Adjustments apply in this fixed order; each is clamped
to the 1.0–10.0 range and recorded in `score_adjustments` for auditability.

1. **Earnings-durability multiplier** — `adjusted = raw × (0.7 + 0.03 × durability)`,
   where durability (1–10) comes from an industry override, then sector
   (`scoring.py` tables). Contractual/recurring (SaaS, insurance, utilities) ≈ ×1.0;
   commodity/cyclical (oil E&P, metals, steel) as low as ×0.73. *(In hold-mode the
   multiplier is floored at 0.92 — you don't dock a compounder you already own
   just for its sector bucket.)*
2. **Analyst-consensus gap** — **removed 2026-07-03.** Analyst price targets have
   no documented predictive power for forward returns (systematically optimistic;
   targets chase price). The audit key remains (`applied: False`) so dashboards
   degrade gracefully. *Known residual:* the risk/reward layer still blends the
   analyst-target gap at 0.30 weight into its upside estimate (`risk_reward.py`) —
   kept for now because the coarse tier mapping absorbs most of the noise;
   revisit once outcome data measures it. See `docs/METHODOLOGY_REVIEW.md`.
3. **Cycle position** — early-cycle + secular up to **+1.0**; cyclical trough +
   strong moat (≥7) **+0.5** (supercycle entry); late/peak + cyclical up to
   **−1.0**. *(Halved in hold-mode.)*
4. **Quantified risk/reward matrix** — see §4a. Full strength in both modes.
5. **Data-confidence penalty** — `data_confidence == LOW` → **−0.5**.
6. **Portfolio overlap** (when a portfolio is supplied) — redundant sector
   exposure (>70%) **−0.5**; genuine diversification **+0.15**; emits a
   concentration warning when any sector > 40%.

Then: **grade assignment** (entry vs. hold ladder per mode), **BANGER detection**
(§4b), and **position sizing** (§4c).

### 4a. The risk/reward matrix (`risk_reward.py`)

A `risk_index` (0–10 composite of balance-sheet, quality, market, insider, and
data flags — cycle is deliberately excluded since step 3 already handles it) and
an `upside` fraction from the fair-value composite map to tiers:

- **Risk tier:** `≤ 2.0` LOW · `≤ 4.5` MED · else HIGH
- **Reward tier:** upside `≥ 35%` HIGH · `≥ 15%` MED · else LOW

The (risk, reward) pair indexes a score adjustment. The diagonal is **neutral** —
high reward "affords" high risk:

| | Reward LOW | Reward MED | Reward HIGH |
|---|---|---|---|
| **Risk LOW** | 0.0 | +0.40 | **+0.75** |
| **Risk MED** | −0.40 | 0.0 | +0.40 |
| **Risk HIGH** | **−0.90** | −0.40 | 0.0 |

A LOW-risk name with a ≥ 3:1 reward-to-risk ratio earns an extra **+0.25** kicker
(max total +1.0). The adjustment is dampened (×0.6) when fair-value inputs are
shaky or ≥ 2 blind-spot flags are present.

### 4b. BANGER flag (asymmetric-opportunity tag)

Not a grade — an additive tag. **All four** must hold: adjusted score ≥ 7.5;
a specific catalyst (or cycle at EARLY/TROUGH); computed R:R ≥ 2:1 (or the legacy
fair-value-floor fallback); and insider **net buying**. A BANGER earns a 1.5×
position-sizing bonus.

### 4c. Position sizing (`position_size`)

Maps the final score to a suggested allocation, then applies modifiers
(capped at each tier's ceiling):

| Score | Base allocation |
|---|---|
| ≥ 9.0 | 4–6% |
| ≥ 8.0 | 3–4% |
| ≥ 6.5 | 1–2% |
| ≥ 5.0 | 0.5% |
| < 5.0 | 0% |

Modifiers **halve** for: LOW conviction, cyclical + late-cycle, low earnings
durability (< 5), or LOW data confidence. They **size up** for the
LOW-risk/HIGH-reward quadrant (1.25×) and BANGERs (1.5×); HIGH-risk tier scales
down (0.5×, or 0.75× if reward is also HIGH).

The whole pipeline is wrapped so it **never raises** — on any error it falls back
to grading the raw score directly (`_safe_apply_adjustments`).

---

## 5. Entry vs. hold — what changes

Same score machinery, but hold-mode deliberately softens entry-only signals so a
held compounder isn't trimmed on noise:

| Adjustment | Entry mode | Hold mode |
|---|---|---|
| Earnings durability | full multiplier (down to ×0.73) | multiplier **floored at 0.92** |
| Analyst-consensus gap | *removed 2026-07-03 (both modes)* | *removed* |
| Cycle position | full (−1.0 … +1.0) | **halved** |
| Risk/reward matrix | full | **full** (this *is* the "does remaining upside afford the risk" test for a holding) |
| Grade ladder | BUY family, "keep" bar 5.0 | ADD/HOLD/TRIM/EXIT, "keep" bar **5.5** |

---

## 6. Stage 2 — the BUY confirmation gate (`verify.py`)

A grade of BUY/STRONG BUY/CONVICTION BUY is **necessary but not sufficient** to be
surfaced. Every result that crosses `BUY_THRESHOLD` (7.0) in entry mode passes a
second round of scrutiny before it reaches Telegram or the dashboard. (Hold-mode /
portfolio runs are **not** gated.)

### Stage 2a — quality gate (pure Python, 0 LLM calls)

Rejects BUYs whose *internal* signals are shaky — all fields the debate already
computed, so it's free. A BUY is rejected if **any** hold:

- agents **did not converge**, or converged spread > `VERIFY_MAX_SPREAD` (2.0)
- debate **confidence LOW**
- **any agent failed**
- risk index > `VERIFY_MAX_RISK_INDEX` (6.0)
- agents' bull/bear targets **diverge** from the computed R:R (`llm_cross_check`)
- dossier **data confidence LOW**

*(A missing R:R is not by itself disqualifying — many valid BUYs lack a clean
fair value.)*

### Stage 2b — adversarial red-team (1 grounded LLM call)

Survivors face a forensic short-seller (grounded `gemma-4-31b-it`, temp 0.2) whose
only job is to **falsify** the bull thesis with fresh Google-grounded research —
guidance cuts, downgrades, deteriorating fundamentals, dilution, insider selling,
litigation, accounting flags, a valuation pricing in perfection. It attacks the
single load-bearing assumption hardest and returns:

- **CONFIRM** — thesis survives; no material disconfirming evidence.
- **DOWNGRADE** — real concerns that weaken conviction but aren't fatal.
- **VETO** — concrete evidence that breaks the thesis.

**A BUY is `confirmed` only if it passes Stage 2a AND the prosecutor returns
CONFIRM.** Rejected BUYs are not dropped — they route to the **"Under Review"**
watchlist (a dashboard tab + the 🔎 Under Review Telegram topic) with the verdict
and strongest bear point attached.

### Robustness (hardened 2026-06-24)

The grounded call flakes under load, and a naive fail-open would silently
rubber-stamp a BUY on a transient 5xx/timeout. So:

- **Retries:** up to `VERIFY_RED_TEAM_ATTEMPTS` (3), rotating keys, with backoff,
  on error/timeout/unparseable verdict.
- **Tiered fail-closed:** at/above `VERIFY_FAILCLOSED_SCORE` (**8.0** = STRONG BUY
  and up), a verifier that still can't reach a verdict **fails CLOSED** → held to
  Under Review as `UNVERIFIED` (never auto-confirmed). Below 8.0, `VERIFY_FAIL_OPEN`
  (default on) keeps the feed flowing.
- **Auditable:** confirmed cards carry a slim `verification` (verdict, score,
  strongest bear point) so the dashboard 🛡 chip and Telegram alert show *how* a
  BUY was verified; a fail-open auto-pass is **visibly flagged**, not silent.

---

## 7. End-to-end: from ticker to rating

```
 Screen / portfolio list
        │
        ▼
 Dossier build (fundamentals, valuation, insiders, technicals, cycle)
        │
        ▼
 5-agent debate ──► raw_consensus_score + confidence        (debate.py)
        │
        ▼
 Adjustment pipeline (durability · consensus gap · cycle ·
   risk/reward · data confidence · portfolio overlap)        (scoring.py)
        │
        ▼
 GRADE:  entry → BUY/STRONG/CONVICTION/HOLD/SELL/…           (grading.py)
         hold  → ADD/HOLD/TRIM/EXIT
        │
        ├─ hold-mode, or score < 7.0 → done (shown as-is, not gated)
        │
        ▼  (entry-mode BUY, score ≥ 7.0)
 Confirmation gate                                            (verify.py)
   Stage 2a quality gate ── reject ─► Under Review watchlist
        │ pass
   Stage 2b red-team ── DOWNGRADE/VETO ─► Under Review watchlist
        │ CONFIRM
        ▼
   CONFIRMED BUY → Telegram alert + Scout/Gems dashboard (🛡 chip)
```

---

## 8. Tunable knobs (env vars)

| Var | Default | Effect |
|---|---|---|
| `BUY_THRESHOLD` | 7.0 | Min score to surface a BUY (alerts + dashboard) — set in `grading.py` |
| `VERIFY_BUYS` | on | Master switch for the confirmation gate |
| `VERIFY_MAX_SPREAD` | 2.0 | Stage-1 reject if converged spread exceeds this |
| `VERIFY_MAX_RISK_INDEX` | 6.0 | Stage-1 reject above this risk index |
| `VERIFY_RED_TEAM_ATTEMPTS` | 3 | Red-team retries before declaring an error |
| `VERIFY_FAILCLOSED_SCORE` | 8.0 | At/above this, an unreachable verifier fails **closed** |
| `VERIFY_FAIL_OPEN` | on | Below the fail-closed score, auto-confirm on verifier outage |

> Approved models only: `gemini-3.5-flash` and `gemma-4-31b-it`. The red-team
> prosecutor uses grounded `gemma-4-31b-it`.
