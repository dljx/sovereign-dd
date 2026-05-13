"""Rich terminal report renderer."""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

console = Console()

GRADE_COLORS = {
    "STRONG BUY":  "bold green",
    "BUY":         "green",
    "HOLD":        "yellow",
    "SELL":        "red",
    "STRONG SELL": "bold red",
}

AGENT_COLORS = {
    "ValueHunter":  "cyan",
    "GrowthAlpha":  "bright_green",
    "QuantSignal":  "bright_blue",
    "RiskSentinel": "red",
    "MacroLens":    "magenta",
    "Moderator":    "white",
}


def _score_bar(score: float, width: int = 30) -> str:
    filled = int(round(score / 10 * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {score:.1f}/10"


def render(result: dict, dossier: dict) -> None:
    ticker = result["ticker"]
    score = result["consensus_score"]
    grade = result["consensus_grade"]
    profile = dossier.get("profile", {})
    quote = dossier.get("quote", {})

    grade_color = GRADE_COLORS.get(grade, "white")

    # ── Header ─────────────────────────────────────────────────────────────
    price = quote.get("price") or 0
    chg = quote.get("change_pct") or 0
    chg_str = f"+{chg:.2f}%" if chg >= 0 else f"{chg:.2f}%"
    chg_color = "green" if chg >= 0 else "red"

    header = Text()
    header.append(f"  {ticker}  ", style="bold white on dark_blue")
    header.append(f"  {profile.get('name', ticker)}\n", style="bold white")
    header.append(f"  {profile.get('sector', '')}  ·  ", style="dim")
    header.append(f"${price:.2f}  ", style="bold white")
    header.append(chg_str, style=chg_color)
    header.append(f"  ·  Market Cap ${profile.get('market_cap_bn', 0):.1f}B", style="dim")

    console.print(Panel(header, title="[bold]SOVEREIGN DD[/bold]", border_style="blue"))

    # ── Consensus Score ────────────────────────────────────────────────────
    score_text = Text()
    score_text.append(f"\n  CONSENSUS SCORE  ", style="bold white")
    score_text.append(f"{score:.2f} / 10\n", style=f"bold {grade_color}")
    score_text.append(f"  {_score_bar(score)}\n", style=grade_color)
    score_text.append(f"\n  GRADE: ", style="bold white")
    score_text.append(f"{grade}", style=f"bold {grade_color}")
    score_text.append(f"   CONFIDENCE: {result.get('confidence', '?')}\n", style="yellow")
    score_text.append(f"\n  {result.get('majority_thesis', '')}\n", style="white")

    console.print(Panel(score_text, title="[bold]Investment Verdict[/bold]",
                        border_style=grade_color))

    # ── Agent Score Table ──────────────────────────────────────────────────
    loops = result.get("loops_run", 1)
    table = Table(title=f"Agent Scores — R1 → Final ({loops} debate loop(s))",
                  box=box.SIMPLE_HEAVY, show_header=True,
                  header_style="bold white")
    table.add_column("Agent", style="bold", width=14)
    table.add_column("R1", justify="right", width=6)
    table.add_column("Final", justify="right", width=6)
    table.add_column("Δ", justify="right", width=6)
    table.add_column("Bar", width=22)
    table.add_column("Grade", width=12)

    r1 = result.get("agent_r1_scores", {})
    rf = result.get("agent_final_scores", result.get("agent_r3_scores", {}))

    for agent in r1:
        s1 = r1[agent]
        sf = rf.get(agent, s1)
        delta = sf - s1
        color = AGENT_COLORS.get(agent, "white")
        delta_str = f"+{delta:.1f}" if delta > 0 else (f"{delta:.1f}" if delta != 0 else "  —")
        delta_color = "green" if delta > 0 else ("red" if delta < 0 else "dim")
        table.add_row(
            Text(agent, style=color),
            f"{s1:.1f}",
            Text(f"{sf:.1f}", style="bold"),
            Text(delta_str, style=delta_color),
            Text(_score_bar(sf, 14), style=GRADE_COLORS.get(_grade(sf), "white")),
            Text(_grade(sf), style=GRADE_COLORS.get(_grade(sf), "white")),
        )

    console.print(table)

    # ── Key Factors ────────────────────────────────────────────────────────
    console.print(Panel(
        f"[bold]Key Swing Factor:[/bold] {result.get('key_swing_factor', '—')}\n\n"
        f"[bold]Rationale:[/bold] {result.get('score_rationale', '—')}\n\n"
        f"[bold]Dissent:[/bold] {result.get('dissent', '—')}",
        title="[bold]Debate Summary[/bold]", border_style="dim white",
    ))

    # ── Macro Snapshot ─────────────────────────────────────────────────────
    macro = dossier.get("macro", {})
    macro_str = (
        f"Fed Funds: {macro.get('fed_funds_rate', '?')}%  ·  "
        f"10Y: {macro.get('treasury_10y', '?')}%  ·  "
        f"2Y: {macro.get('treasury_2y', '?')}%  ·  "
        f"Spread: {macro.get('yield_curve_spread', '?')}  ·  "
        f"CPI: {macro.get('cpi_yoy', '?')}  ·  "
        f"VIX: {macro.get('vix', '?')}"
    )
    console.print(f"\n[dim]Macro:[/dim] {macro_str}")
    converged = result.get("converged", True)
    loops = result.get("loops_run", "?")
    conv_str = f"Converged in {loops} loop(s)" if converged else f"Moderator invoked after {loops} loop(s)"
    console.print(f"[dim]{conv_str}  ·  Score spread: {result.get('score_spread', '?')}[/dim]\n")


def _grade(score: float) -> str:
    if score >= 8.0: return "STRONG BUY"
    if score >= 6.5: return "BUY"
    if score >= 5.0: return "HOLD"
    if score >= 3.5: return "SELL"
    return "STRONG SELL"
