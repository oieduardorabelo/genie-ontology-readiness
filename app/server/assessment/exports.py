"""Summary and per-pillar CSV exports matching the App's drill-down columns."""

import csv
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

SUMMARY_COLUMNS = [
    {"key": "pillar", "label": "Pillar"},
    {"key": "score", "label": "Score"},
    {"key": "level", "label": "Level"},
    {"key": "maturity", "label": "Maturity"},
    {"key": "available", "label": "Available"},
    {"key": "gaps", "label": "Gaps"},
]


def _write_csv(path: Path, columns: list[dict], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow([f"{c['label']} ({c['unit']})" if c.get("unit") else c["label"] for c in columns])
        for row in rows:
            values = [row.get(c["key"]) for c in columns]
            writer.writerow([str(value).lower() if isinstance(value, bool) else value for value in values])


def export_csvs(scorecard: dict, output_dir: Path) -> None:
    """Write a summary, each nonempty drill-down, and a ZIP of those CSV files."""
    csv_dir = output_dir / "csv"
    csv_dir.mkdir(exist_ok=True)
    summary = [{
        "pillar": p["name"], "score": p["score"], "level": p["level"], "maturity": p["level_label"],
        "available": "yes" if p["available"] else "no", "gaps": " | ".join(p["gaps"]) or "—",
    } for p in scorecard["pillars"]]
    _write_csv(csv_dir / "summary.csv", SUMMARY_COLUMNS, summary)
    paths = [csv_dir / "summary.csv"]
    for pillar in scorecard["pillars"]:
        drill_down = pillar.get("drill_down") or {}
        if not drill_down.get("rows"):
            continue
        safe_key = "".join(c if c.isalnum() or c in "_-" else "_" for c in pillar["key"])
        path = csv_dir / f"{safe_key}.csv"
        _write_csv(path, drill_down["columns"], drill_down["rows"])
        paths.append(path)
    with ZipFile(output_dir / "readiness-csv.zip", "w", compression=ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, arcname=path.name)
