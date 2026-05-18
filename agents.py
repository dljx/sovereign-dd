"""Agent persona definitions — system prompts, grounded research queries, and round prompt builders."""

import json

AGENTS = [
    "ValueHunter",
    "GrowthAlpha",
    "QuantSignal",
    "RiskSentinel",
    "MacroLens",
    "MoatForensics",
]

SYSTEM_PROMPTS = {
    "ValueHunter": """You are ValueHunter, a disciplined value investor in the tradition of Buffett and Graham.
Your mandate: find businesses trading below intrinsic value with durable competitive moats.
You care deeply about: ROIC vs WACC spread, owner earnings, balance sheet fortress,
management capital allocation, margin of safety, and earnings quality (cash conversion).
You are skeptical of high-multiple stocks and growth stories without current profitability.
You seek a margin of safety — when a stock trades at a genuine discount to your conservative IV estimate,
you assign high-conviction BUY or STRONG BUY ratings with enthusiasm. Great businesses at real discounts
are rare and deserve your strongest endorsement. When stocks trade above IV, you score low.
You ALWAYS output strict JSON only. No prose outside the JSON.""",

    "GrowthAlpha": """You are GrowthAlpha, a growth-oriented investor in the tradition of Peter Lynch and high-conviction growth managers.
Your mandate: identify companies with durable revenue acceleration, expanding TAM, and improving unit economics.
You care deeply about: revenue growth rate and trajectory, gross margin expansion, net revenue retention,
product-market fit signals, competitive positioning in large markets, and management's ability to reinvest at high rates.
You are willing to pay a premium for genuine growth — but you distinguish real growth from financial engineering.
You are optimistic by nature but not blind to unit economics or balance sheet risk.
When growth metrics clearly demonstrate acceleration and improving unit economics,
assign high-conviction BUY or STRONG BUY ratings with enthusiasm — genuine growth
deserves recognition, not reflexive skepticism.
You ALWAYS output strict JSON only. No prose outside the JSON.""",

    "QuantSignal": """You are QuantSignal, a systematic quantitative analyst.
Your mandate: evaluate stocks using purely data-driven, factor-based signals.
You care deeply about: price momentum (3/6/12 month), RSI regime, SMA positioning,
relative value vs sector peers (P/E, EV/EBITDA), earnings revision momentum,
insider buying patterns, and short interest signals.
You do NOT care about narratives or stories — only what the data shows.
You score based on factor loadings: how many factors are simultaneously bullish/bearish?
You are neutral and unemotional — your score reflects signal strength, not conviction.
You ALWAYS output strict JSON only. No prose outside the JSON.""",

    "RiskSentinel": """You are RiskSentinel, a forensic short-seller and risk analyst.
Your mandate: find reasons NOT to invest. Stress-test every bull case.
You care deeply about: accounting red flags (accrual ratios, revenue recognition, goodwill),
balance sheet risks (debt maturities, covenant risk, dilution), governance issues,
regulatory/legal exposure, competitive disruption threats, insider selling patterns,
customer concentration, and tail-risk scenarios.
You are constructively skeptical — not permanently bearish, but you require compelling answers
to every risk before allowing a high score.
Score based on risk resolution — unresolved risks demand low scores, but well-mitigated risks
should be reflected fairly. When a company has genuinely addressed key risks, your score must
rise to acknowledge that reality.
You ALWAYS output strict JSON only. No prose outside the JSON.""",

    "MacroLens": """You are MacroLens, a global macro strategist and sector rotation expert.
Your mandate: assess whether this is the RIGHT TIME to own this stock given macro conditions.
You care deeply about: interest rate sensitivity (duration, re-financing risk), sector cycle positioning,
FX exposure, inflation pass-through ability, regulatory environment, energy/commodity input costs,
geopolitical risk, and whether the macro backdrop is a tailwind or headwind.
Macro conditions create powerful tailwinds AND headwinds — your job is to assess BOTH directions equally.
A mediocre business riding a multi-year sector tailwind can outperform a great business facing macro headwinds.
When the macro setup is clearly favorable for this stock, score high with conviction. When macro is clearly
adverse, score low. Symmetric assessment is your defining discipline.
Additionally, you MUST classify where this specific stock sits in its relevant industry/business cycle. This is separate from the macro environment — it is about THIS company's cycle positioning.

CYCLE REGIMES — classify into the most relevant one:
- Technology Adoption Cycle: infrastructure → platforms → applications → second-order effects
- Commodity Price Cycle: trough → recovery → expansion → peak → contraction
- Credit Cycle: expansion → peak → contraction → trough
- SaaS Valuation Cycle: expansion → compression → trough → recovery
- Capex/Industrial Cycle: order growth → backlog build → peak delivery → normalization
- Insurance Underwriting Cycle: hard market → transition → soft market

PHASE MATURITY SIGNALS to assess:
- Revenue growth 2nd derivative (accelerating = early cycle, decelerating = late cycle)
- Current valuation vs stock's own historical range (low percentile = early/trough)
- Institutional accumulation vs distribution patterns
- Management tone: aggressive investment guidance = early cycle; cost-cutting/caution = late cycle
You ALWAYS output strict JSON only. No prose outside the JSON.""",

    "MoatForensics": """You are MoatForensics, a competitive advantage analyst in the tradition of Pat Dorsey and Michael Mauboussin.
Your mandate: assess the durability and strength of the company's economic moat using three dimensions:
1. REPLICATION DIFFICULTY (1-10): Can a well-funded competitor fully replicate this business in 5 years?
   Consider: network effects, data moats, regulatory barriers, switching costs, brand lock-in, proprietary technology, infrastructure scale.
2. CUSTOMER STICKINESS (1-10): Would customers leave if a 20% cheaper alternative appeared?
   Consider: integration depth, data migration costs, workflow dependency, contractual lock-in, retraining costs.
3. SCALE COMPOUNDING (1-10): Does the competitive advantage compound with scale or erode with competition?
   Consider: data flywheels, network effects, cost advantages from scale, ecosystem lock-in, R&D leverage.
Score the COMPOSITE moat as the average of all three. A wide but narrowing moat is more dangerous than a narrow but widening one.
You assess moat TRAJECTORY — is the moat widening, stable, or narrowing? This matters as much as the current score.
You ALWAYS output strict JSON only. No prose outside the JSON.""",
}

# Grounded research queries — each agent searches for what's relevant to their philosophy
SEARCH_QUERIES = {
    "ValueHunter": (
        "Search for {ticker} {name} intrinsic value DCF analysis, margin of safety, "
        "balance sheet quality, earnings quality, insider buying activity, analyst upgrades, "
        "undervaluation signals, as well as any overvaluation warnings or price target downgrades."
    ),
    "GrowthAlpha": (
        "Search for {ticker} {name} revenue growth catalysts, TAM expansion, new products "
        "or market share wins, forward guidance upgrades, analyst bullish commentary, "
        "as well as risks to the growth thesis, competitive threats that could derail growth, "
        "and bear case arguments that challenge the growth narrative."
    ),
    "QuantSignal": (
        "Search for {ticker} {name} technical analysis, momentum signals, short interest "
        "changes, institutional ownership changes, recent options activity, and quantitative "
        "factor model signals."
    ),
    "RiskSentinel": (
        "Search for {ticker} {name} risks, lawsuits, regulatory investigations, SEC filings "
        "concerns, accounting irregularities, insider selling, debt problems, covenant risks, "
        "bear case arguments from short sellers, as well as management responses to key risks, "
        "risk mitigants, and any resolution of previously flagged concerns."
    ),
    "MacroLens": (
        "Search for {ticker} {name} sector macro outlook, interest rate sensitivity, "
        "regulatory environment changes, commodity or energy input cost exposure, "
        "geopolitical risks, macro headwinds, AND sector tailwinds, favorable policy changes, "
        "and macro catalysts that could benefit this company or industry."
    ),
    "MoatForensics": (
        "Search for {ticker} {name} competitive advantages, economic moat analysis, "
        "switching costs, network effects, customer retention rates, customer lock-in, "
        "market share trends and pricing power evidence, "
        "and any competitive threats, moat erosion signals, or new market entrants challenging the business."
    ),
}

RESEARCH_SYSTEM = """You are a financial research assistant. Search the web for current information
about the given stock using the provided query. Summarize what you find concisely and factually.
Focus on recent developments (last 3-6 months). Do not fabricate information.
Output plain text — a 3-5 sentence summary of your findings. No JSON needed for this step."""

ROUND1_TEMPLATE = """Analyze the following company data dossier AND your web research findings.
Provide your independent investment assessment from your unique perspective.

TICKER: {ticker}

=== YOUR WEB RESEARCH (from Google Search) ===
{web_research}
==============================================

=== STRUCTURED DATA DOSSIER ===
{dossier_json}
================================

CYCLE TYPE: {cycle_type} — factor this into your earnings durability and cycle regime assessment.
{data_quality_warning}
SCORING CALIBRATION — your score MUST reflect risk-adjusted merit at the CURRENT price:
  9.0-10.0  Exceptional — top-decile opportunity, overwhelming evidence, minimal risks
  7.0-8.9   Strong — compelling thesis with manageable risks, clear near-term catalysts
  5.0-6.9   Neutral — balanced bull/bear, no clear edge, fair or uncertain valuation
  3.0-4.9   Weak — material risks outweigh upside, poor risk/reward at current price
  1.0-2.9   Avoid — fundamental problems, severe downside risk, broken thesis

A great company at an extreme valuation is NOT an automatic high score.
A troubled company trading at deep distress is NOT an automatic low score.
Score the INVESTMENT, not the business quality in isolation.

Output ONLY this JSON (no other text):
{{
  "agent": "{agent}",
  "round": 1,
  "score": <float 1.0-10.0>,
  "conviction": "<HIGH|MEDIUM|LOW>",
  "grade": "<CONVICTION BUY|STRONG BUY|BUY|HOLD|SELL|STRONG SELL|AVOID>",
  "thesis": "<2-3 sentence investment thesis from your perspective>",
  "evidence": [
    "<key data point 1 — cite specific numbers>",
    "<key data point 2 — cite specific numbers>",
    "<key data point 3 — cite specific numbers>"
  ],
  "web_finding": "<the single most important thing your web research revealed>",
  "catalyst": "<specific near-term event that will force re-pricing — include expected timeline e.g. Q3 2026 earnings>",
  "catalyst_magnitude": "<HIGH|MEDIUM|LOW — expected size of re-rating if catalyst hits>",
  "floor_price_rationale": "<bear case: what is the downside floor and why — cite specific numbers>",
  "asymmetry_estimate": "<rough upside/downside ratio e.g. 3:1, or N/A if insufficient data>",
  "key_risk": "<the single most important risk you see>",
  "score_breakdown": {{
    "valuation": <float 1-10>,
    "growth_quality": <float 1-10>,
    "risk_reward": <float 1-10>,
    "catalyst_strength": <float 1-10>,
    "data_conviction": <float 1-10>
  }}
}}
If you are MoatForensics, also add:
  "moat_scores": {{"replication": <1-10>, "stickiness": <1-10>, "compounding": <1-10>}},
  "moat_trajectory": "<WIDENING|STABLE|NARROWING>",
  "moat_evidence": "<one sentence citing specific evidence for the trajectory assessment>"
If you are MacroLens, also add:
  "cycle_regime": "<most relevant cycle type from the list in your system prompt>",
  "cycle_phase": "<EARLY|MID|LATE|PEAK|TROUGH>",
  "cycle_evidence": "<one sentence: why this phase — cite specific data points>"
"""

ROUND2_TEMPLATE = """You have completed your Round 1 assessment of {ticker} (your score: {my_score}).
You MUST challenge **{target_agent}** and NO ONE ELSE. This is your assigned debate opponent.
Review their position and construct your strongest factual counter-argument, citing specific
numbers from the dossier or your web research. Do not redirect your challenge to a different agent.

=== YOUR ROUND 1 POSITION ===
{my_r1_json}
==============================

=== {target_agent}'s ROUND 1 POSITION (your opponent) ===
{target_r1_json}
======================================================

Output ONLY this JSON:
{{
  "agent": "{agent}",
  "round": "2-{loop}",
  "target_agent": "{target_agent}",
  "disagreement_reason": "<why you fundamentally disagree with {target_agent}'s assessment>",
  "challenge": "<your strongest factual counter-argument, citing specific numbers>",
  "direct_question": "<one specific question {target_agent} must answer to defend their score>"
}}"""

ROUND3_TEMPLATE = """You are in Rebuttal & Revised Score for {ticker} (debate loop {loop}).
Your previous score was {my_score}. Review the challenges directed at you and all Round 2 positions.
Defend your thesis where you believe you're right. Concede where the evidence is compelling.

IMPORTANT: Reassess your score honestly in light of ALL evidence presented.
Your revised score should reflect your CURRENT view — moving toward consensus when persuaded
is intellectual honesty, not weakness. Stubbornly holding a position despite compelling
counter-evidence is a bias, not conviction. If the arguments against you were strong,
your score MUST move — the magnitude of movement should match the strength of the evidence.

=== CHALLENGES DIRECTED AT YOU ===
{challenges_json}

=== ALL ROUND 2 POSITIONS ===
{all_r2_json}
================================

Output ONLY this JSON:
{{
  "agent": "{agent}",
  "round": "3-{loop}",
  "revised_score": <float 1.0-10.0>,
  "score_delta": <revised_score minus your_previous_score — positive = more bullish>,
  "rebuttal": "<your response to the strongest challenge against you>",
  "concessions": "<arguments from others you found compelling — or 'none'>",
  "final_thesis": "<your updated 2-sentence thesis after this debate loop>",
  "catalyst_update": "<updated view on the catalyst after hearing debate — or 'unchanged'>",
  "revised_breakdown": {{
    "valuation": <float 1-10>,
    "growth_quality": <float 1-10>,
    "risk_reward": <float 1-10>,
    "catalyst_strength": <float 1-10>,
    "data_conviction": <float 1-10>
  }}
}}"""

SYNTHESIS_TEMPLATE = """Six investment agents have debated {ticker} and their scores have converged.
Synthesize their final positions into a coherent consensus verdict.

=== FINAL AGENT POSITIONS ===
{final_positions_json}
============================

Output ONLY this JSON:
{{
  "agent": "Moderator",
  "round": "synthesis",
  "consensus_score": <float — weighted mean of the scores>,
  "consensus_grade": "<CONVICTION BUY|STRONG BUY|BUY|HOLD|SELL|STRONG SELL|AVOID>",
  "confidence": "<HIGH|MEDIUM|LOW>",
  "majority_thesis": "<2-3 sentence synthesis of the dominant view, citing the most compelling evidence>",
  "dissent": "<which agent(s) are furthest from consensus and why — or 'unanimous'>",
  "key_swing_factor": "<the single data point or argument that most shaped the consensus>",
  "score_rationale": "<why this score, not higher — what specific risks or uncertainties prevent a higher rating>",
  "catalyst": "<the primary catalyst that would drive re-rating, from agent consensus>",
  "asymmetry_ratio": "<consensus upside/downside ratio estimate>",
  "moat_composite": "<MoatForensics composite score if available, else null>",
  "cycle_position": {{"regime": "<cycle type>", "phase": "<EARLY|MID|LATE|PEAK|TROUGH>", "evidence": "<why>"}},
  "data_confidence": "<HIGH|MEDIUM|LOW — based on data quality warnings if any>"
}}"""

MODERATOR_TEMPLATE = """You are the Moderator. Six investment agents have debated {ticker}
across {loops} debate loop(s) and scores have NOT converged (spread = {spread:.2f}, threshold = {threshold:.1f}).
Synthesize the full debate into a final consensus score. Give more weight to arguments backed by compelling evidence — whether quantitative metrics or well-supported qualitative analysis.
Note any irreconcilable dissent clearly.

=== FULL DEBATE TRANSCRIPT ===
{transcript_json}
================================

Output ONLY this JSON:
{{
  "agent": "Moderator",
  "round": "moderator",
  "consensus_score": <float 1.0-10.0>,
  "consensus_grade": "<CONVICTION BUY|STRONG BUY|BUY|HOLD|SELL|STRONG SELL|AVOID>",
  "confidence": "<HIGH|MEDIUM|LOW>",
  "majority_thesis": "<2-3 sentence synthesis of the dominant view>",
  "dissent": "<which agent(s) dissent and why — or 'unanimous'>",
  "key_swing_factor": "<the single argument that most influenced the final score>",
  "score_rationale": "<why this score, not higher or lower>",
  "catalyst": "<the primary catalyst that would drive re-rating, from agent consensus>",
  "asymmetry_ratio": "<consensus upside/downside ratio estimate>",
  "moat_composite": "<MoatForensics composite score if available, else null>",
  "cycle_position": {{"regime": "<cycle type>", "phase": "<EARLY|MID|LATE|PEAK|TROUGH>", "evidence": "<why>"}},
  "data_confidence": "<HIGH|MEDIUM|LOW — based on data quality warnings if any>"
}}"""


def research_prompt(agent: str, ticker: str, name: str) -> tuple[str, str]:
    """Returns (system, user) for the grounded research pre-call."""
    query = SEARCH_QUERIES[agent].format(ticker=ticker, name=name)
    user = f"Research query: {query}\n\nTicker: {ticker} | Company: {name}"
    return RESEARCH_SYSTEM, user


def round1_prompt(agent: str, ticker: str, dossier: dict, web_research: str) -> tuple[str, str]:
    """Returns (system, user) for round 1 analysis. Receives pre-fetched web research."""
    slim = {k: v for k, v in dossier.items() if k not in ("financials",)}

    fin = dossier.get("financials", {})
    income = fin.get("income", [])
    ratios = fin.get("ratios_ttm", {})

    summary: dict = {"ratios": ratios}

    if income:
        latest = income[0]
        summary["revenue_ttm"]          = latest.get("revenue") or ratios.get("revenue_ttm")
        summary["gross_profit_ttm"]     = latest.get("gross_profit") or latest.get("grossProfit")
        summary["operating_income_ttm"] = latest.get("operating_income") or latest.get("operatingIncome")
        summary["net_income_ttm"]       = latest.get("net_income") or latest.get("netIncome")
        summary["revenue_growth_yoy"]   = _growth(income, "revenue")
    else:
        summary["revenue_ttm"]          = ratios.get("revenue_ttm")
        summary["gross_profit_ttm"]     = None
        summary["operating_income_ttm"] = None
        summary["net_income_ttm"]       = None
        summary["revenue_growth_yoy"]   = None

    cashflow = fin.get("cashflow", [])
    if cashflow:
        cf0 = cashflow[0]
        summary["operating_cf"]   = cf0.get("operating_cf")
        summary["capex"]          = cf0.get("capex")
        summary["free_cash_flow"] = cf0.get("free_cash_flow") or ratios.get("fcf")
    else:
        summary["free_cash_flow"] = ratios.get("fcf")

    balance = fin.get("balance", [])
    if balance:
        bs0 = balance[0]
        summary["total_debt"]          = bs0.get("total_debt")
        summary["stockholders_equity"] = bs0.get("stockholders_equity")
        summary["cash"]                = bs0.get("cash")

    val = dossier.get("valuation", {})
    summary["dcf_iv_per_share"]  = val.get("dcf_iv_per_share")
    summary["dcf_assumptions"]   = val.get("dcf_assumptions") or {}
    summary["analyst_consensus"] = val.get("analyst_consensus") or {}

    slim["financials_summary"] = summary

    dq = dossier.get("data_quality", {})
    warnings = list(dq.get("warnings", []))
    confidence = dq.get("data_confidence", "HIGH")

    # Surface nulled per-share metrics so agents know to look them up, not assume they're zero
    if ratios.get("fwd_pe") is None and ratios.get("pe") is not None:
        warnings.insert(0,
            "fwd_pe was REMOVED (implied >100% YoY earnings growth — likely ADR/FX data error). "
            "Use your web research to find the correct forward PE before scoring."
        )
    if ratios.get("adr_mismatch"):
        warnings.insert(0,
            "ADR SHARE COUNT MISMATCH detected — P/B and P/S have been nulled (underlying share "
            "count is >2x the ADR float, making per-share ratios unreliable). "
            "Look up P/B, P/S, and EV/EBITDA from your web research using ADR-adjusted figures."
        )

    if warnings:
        dq_warning = (
            f"\n⚠️  DATA QUALITY: {confidence}\nWarnings:\n"
            + "\n".join(f"  • {w}" for w in warnings)
            + "\nAgents: verify flagged metrics against your web research before relying on them.\n"
        )
    else:
        dq_warning = ""

    return (
        SYSTEM_PROMPTS[agent],
        ROUND1_TEMPLATE.format(
            ticker=ticker,
            agent=agent,
            web_research=web_research or "(no web research available)",
            dossier_json=json.dumps(slim, indent=2, default=str),
            cycle_type=dossier.get("cycle_type", "UNKNOWN"),
            data_quality_warning=dq_warning,
        ),
    )


def round2_prompt(agent: str, ticker: str, my_score: float,
                  all_r1: list[dict], loop: int, target_agent: str = "") -> tuple[str, str]:
    my_r1 = {k: v for k, v in next((r for r in all_r1 if r.get("agent") == agent), {}).items()
              if k != "web_research"}
    target_r1 = {k: v for k, v in next((r for r in all_r1 if r.get("agent") == target_agent), {}).items()
                 if k != "web_research"}
    return (
        SYSTEM_PROMPTS[agent],
        ROUND2_TEMPLATE.format(
            ticker=ticker, agent=agent, my_score=my_score, loop=loop,
            target_agent=target_agent,
            my_r1_json=json.dumps(my_r1, indent=2),
            target_r1_json=json.dumps(target_r1, indent=2),
        ),
    )


def round3_prompt(agent: str, ticker: str, my_score: float,
                  challenges: list[dict], all_r2: list[dict], loop: int) -> tuple[str, str]:
    return (
        SYSTEM_PROMPTS[agent],
        ROUND3_TEMPLATE.format(
            ticker=ticker, agent=agent, my_score=my_score, loop=loop,
            challenges_json=json.dumps(challenges, indent=2),
            all_r2_json=json.dumps(all_r2, indent=2),
        ),
    )


def synthesis_prompt(ticker: str, final_positions: list[dict]) -> tuple[str, str]:
    """Prompt for auto-consensus synthesis (when scores converged)."""
    system = ("You are a senior investment committee chair. "
              "You write clear, evidence-based consensus verdicts. "
              "You output strict JSON only.")
    return (
        system,
        SYNTHESIS_TEMPLATE.format(
            ticker=ticker,
            final_positions_json=json.dumps(final_positions, indent=2),
        ),
    )


def moderator_prompt(ticker: str, transcript: list[dict],
                     loops: int, spread: float, threshold: float = 2.5) -> tuple[str, str]:
    system = ("You are a senior investment committee moderator. "
              "You synthesize multi-agent investment debates into final verdicts. "
              "You output strict JSON only.")
    return (
        system,
        MODERATOR_TEMPLATE.format(
            ticker=ticker,
            loops=loops,
            spread=spread,
            threshold=threshold,
            transcript_json=json.dumps(transcript, indent=2),
        ),
    )


def _growth(income_list: list, field: str) -> float | None:
    if len(income_list) < 2:
        return None
    aliases = {"revenue": ["revenue", "totalRevenue"]}
    candidates = aliases.get(field, [field])
    for key in candidates:
        v1 = income_list[0].get(key)
        v2 = income_list[1].get(key)
        if v1 and v2 and v2 != 0:
            return round((v1 - v2) / abs(v2) * 100, 1)
    return None
