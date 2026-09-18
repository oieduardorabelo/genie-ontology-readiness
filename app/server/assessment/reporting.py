"""Readiness report builders shared by the App and unattended assessments."""

from server.pdf import build_pdf_document, markdown_to_safe_html

ASSESSMENT_TITLE = "Genie Ontology Readiness — Assessment"


def _cell(text) -> str:
    """Escape a value for a Markdown table cell (a literal ``|`` would split it)."""
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _fmt_signal(sig: dict) -> str:
    """One signal as ``**Label:** value unit — detail`` (unit/detail optional)."""
    label = sig.get("label") or ""
    value = sig.get("value")
    unit = sig.get("unit") or ""
    detail = sig.get("detail") or ""
    val = "" if value is None else f"{value}{unit}"
    head = f"**{label}:** {val}".rstrip()
    return f"{head} — {detail}" if detail else head


def assessment_markdown(sc: dict) -> str:
    """A deterministic Markdown readout of the scorecard: overall + stage, the
    per-pillar table (score / level / weight), top gaps, then per-pillar detail
    (summary, signals, gaps, recommended practices, and identity attribution).

    Unavailable pillars are labeled rather than shown as a blank/0 (issue #15 AC)."""
    overall = sc.get("overall", {}) or {}
    pillars = sc.get("pillars", []) or []
    top_gaps = sc.get("top_gaps", []) or []

    lines: list[str] = []

    # Overall readiness. Rendered defensively: a legacy/degraded snapshot may carry
    # pillars but a missing or partial `overall`, and the readout must never print a
    # literal "None/100 — LNone" header (issue #15: degrade cleanly).
    score = overall.get("score")
    level = overall.get("level")
    level_label = overall.get("level_label") or ""
    stage = overall.get("readiness_stage") or ""
    detail = overall.get("readiness_detail") or ""
    lines.append("## Overall readiness")
    lines.append("")
    if score is not None:
        header = f"**{score}/100"
        if level is not None:
            header += f" — L{level} {level_label}".rstrip()
        header += "**"
        if stage:
            header += f" · {stage}"
        lines.append(header)
        lines.append("")
    elif stage:
        lines.append(f"**{stage}**")
        lines.append("")
    if detail:
        lines.append(detail)
        lines.append("")

    # Per-pillar scorecard table.
    if pillars:
        lines.append("## Pillar scores")
        lines.append("")
        lines.append("| Pillar | Score | Maturity | Weight |")
        lines.append("| --- | --- | --- | --- |")
        for p in pillars:
            score_cell = str(p.get("score")) if p.get("available", True) else "n/a"
            lines.append(
                f"| {_cell(p.get('name'))} | {_cell(score_cell)} "
                f"| {_cell('L' + str(p.get('level')) + ' ' + (p.get('level_label') or ''))} "
                f"| {_cell(p.get('weight'))} |"
            )
        lines.append("")

    # Top gaps.
    if top_gaps:
        lines.append("## Top gaps to close")
        lines.append("")
        for g in top_gaps:
            lines.append(f"- **{g.get('pillar')}** — {g.get('gap')}")
        lines.append("")

    # Per-pillar detail.
    if pillars:
        lines.append("## Pillar detail")
        lines.append("")
        for p in pillars:
            name = p.get("name") or ""
            available = p.get("available", True)
            if available:
                lines.append(f"### {name} — {p.get('score')}/100 (L{p.get('level')} {p.get('level_label') or ''})")
            else:
                lines.append(f"### {name} — not available")
            lines.append("")

            if not available:
                reason = (p.get("note") or "").strip() or "This pillar could not be assessed for this run."
                lines.append(f"*{reason}*")
                lines.append("")
                continue

            summary = (p.get("summary") or p.get("short") or "").strip()
            if summary:
                lines.append(summary)
                lines.append("")

            identity = p.get("identity") or {}
            if identity.get("label"):
                lines.append(f"*Assessed as: {identity.get('label')}.*")
                lines.append("")

            signals = p.get("signals") or []
            if signals:
                lines.append("**Signals**")
                lines.append("")
                for sig in signals:
                    lines.append(f"- {_fmt_signal(sig)}")
                lines.append("")

            gaps = p.get("gaps") or []
            if gaps:
                lines.append("**Gaps**")
                lines.append("")
                for g in gaps:
                    lines.append(f"- {g}")
                lines.append("")

            practices = p.get("best_practices") or []
            if practices:
                lines.append("**Recommended practices**")
                lines.append("")
                for bp in practices:
                    lines.append(f"- {bp}")
                lines.append("")

    return "\n".join(lines).strip() + "\n"


def build_assessment_pdf_html(sc: dict, title: str) -> str:
    """Assemble the branded assessment PDF HTML from a scorecard. Deterministic —
    the Markdown carries no H1, so the shared builder's title renders exactly once."""
    subtitle = "Genie Ontology Readiness assessment · Databricks"
    body_html = markdown_to_safe_html(assessment_markdown(sc or {}))
    return build_pdf_document(title or ASSESSMENT_TITLE, body_html, subtitle=subtitle)


