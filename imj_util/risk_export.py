from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imj_util.database import StoredAnalysis
from imj_util.gigachat_client import RISK_RANK
from imj_util.issues_reader import read_issues_xls
from imj_util.viewer_store import ReadOnlyAnalysisStore

CATEGORY_LABELS = {
    "sexual_content": "сексуализированное содержание",
    "violence_humiliation": "насилие и унижение",
    "minors": "несовершеннолетние",
    "discrimination_hate": "дискриминация и враждебность",
    "portrait_rights": "права изображённых лиц",
    "lgbt_propaganda": "пропаганда (критерии ТЗ)",
}


@dataclass(frozen=True)
class ExportResult:
    output_path: Path
    exported_rows: int
    skipped_rows: int
    total_successful: int


def is_risk_above_low(level: str | None) -> bool:
    if not level:
        return False
    return RISK_RANK.get(level.strip().lower(), 0) > RISK_RANK["low"]


def export_elevated_risks_csv(
    *,
    database_path: Path,
    output_path: Path,
    issues_file: Path | None = None,
    image_field: str = "image_url",
) -> ExportResult:
    """Read-only export; safe to run while analyze-batch writes to the DB.

    Rows are read without a single snapshot transaction — the CSV may mix
    records from slightly different moments, which is acceptable.
    """
    store = ReadOnlyAnalysisStore(database_path)
    issues_by_row, issues_by_url, issue_columns = _load_issues_index(issues_file, image_field)

    analyses = store.list_successful_latest_per_url()
    export_rows: list[dict[str, Any]] = []
    skipped = 0

    for stored in analyses:
        elevated = get_elevated_risks(stored)
        if not elevated and not is_risk_above_low(stored.overall_risk_level):
            skipped += 1
            continue

        payload = _resolve_issue_payload(stored, issues_by_row, issues_by_url)
        row = dict(payload)
        row["analysis_id"] = stored.id
        row["overall_risk_level"] = stored.overall_risk_level
        row["manual_review_required"] = stored.manual_review_required
        row["confidence"] = stored.confidence
        row["analysis_created_at"] = stored.created_at
        row["elevated_risk_categories"] = "; ".join(
            _category_label(risk.get("category", "")) for risk in elevated
        )
        row["elevated_risks_summary"] = _format_elevated_summary(elevated)

        for risk in elevated:
            category = str(risk.get("category", "")).strip()
            if not category:
                continue
            row[f"risk_{category}"] = risk.get("risk_level", "")
            row[f"signs_{category}"] = _format_signs(risk.get("detected_signs"))

        export_rows.append(row)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = _build_fieldnames(issue_columns, export_rows)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in export_rows:
            writer.writerow({name: _csv_value(row.get(name)) for name in fieldnames})

    return ExportResult(
        output_path=output_path,
        exported_rows=len(export_rows),
        skipped_rows=skipped,
        total_successful=len(analyses),
    )


def get_elevated_risks(stored: StoredAnalysis) -> list[dict[str, Any]]:
    risks = stored.risks or stored.report.get("risks", [])
    elevated: list[dict[str, Any]] = []
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        if is_risk_above_low(str(risk.get("risk_level", "none"))):
            elevated.append(risk)
    return elevated


def _load_issues_index(
    issues_file: Path | None,
    image_field: str,
) -> tuple[dict[int, dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    if issues_file is None or not issues_file.exists():
        return {}, {}, []

    rows = read_issues_xls(issues_file, image_url_field=image_field)
    by_row: dict[int, dict[str, Any]] = {}
    by_url: dict[str, dict[str, Any]] = {}
    columns: list[str] = []

    for issue in rows:
        by_row[issue.row_index] = issue.payload
        by_url[issue.image_url] = issue.payload
        for key in issue.payload:
            if key not in columns:
                columns.append(key)

    return by_row, by_url, columns


def _resolve_issue_payload(
    stored: StoredAnalysis,
    issues_by_row: dict[int, dict[str, Any]],
    issues_by_url: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if stored.source_payload:
        return dict(stored.source_payload)
    if stored.source_row_index is not None and stored.source_row_index in issues_by_row:
        return dict(issues_by_row[stored.source_row_index])
    if stored.image_url in issues_by_url:
        return dict(issues_by_url[stored.image_url])
    return {"image_url": stored.image_url}


def _build_fieldnames(issue_columns: list[str], rows: list[dict[str, Any]]) -> list[str]:
    meta = [
        "analysis_id",
        "overall_risk_level",
        "manual_review_required",
        "confidence",
        "analysis_created_at",
        "elevated_risk_categories",
        "elevated_risks_summary",
    ]

    issue_keys: list[str] = list(issue_columns)
    for row in rows:
        for key in row:
            if key.startswith(("risk_", "signs_")) or key in meta:
                continue
            if key not in issue_keys:
                issue_keys.append(key)

    risk_sign_cols: list[str] = []
    for row in rows:
        for key in row:
            if key.startswith(("risk_", "signs_")) and key not in risk_sign_cols:
                risk_sign_cols.append(key)
    risk_sign_cols.sort()

    return meta + issue_keys + risk_sign_cols


def _format_elevated_summary(elevated: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for risk in elevated:
        category = _category_label(str(risk.get("category", "")))
        level = risk.get("risk_level", "")
        signs = _format_signs(risk.get("detected_signs"))
        description = str(risk.get("description", "")).strip()
        chunk = f"{category} [{level}]"
        if signs:
            chunk += f": {signs}"
        if description:
            chunk += f" ({description})"
        parts.append(chunk)
    return " | ".join(parts)


def _format_signs(signs: Any) -> str:
    if not signs:
        return ""
    if isinstance(signs, list):
        return "; ".join(str(item).strip() for item in signs if str(item).strip())
    return str(signs).strip()


def _category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category or "неизвестная категория")


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
