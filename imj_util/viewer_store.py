from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from imj_util.database import StoredAnalysis
from imj_util.gigachat_client import RISK_LEVELS

VALID_RISK_LEVELS = set(RISK_LEVELS)


@dataclass
class AnalysisStats:
    total: int
    success: int
    errors: int
    manual_review: int
    by_risk: dict[str, int]


@dataclass
class AnalysisListItem:
    id: int
    image_url: str
    overall_risk_level: str
    manual_review_required: bool
    status: str
    source_row_index: int | None
    created_at: str
    error_message: str | None
    confidence: float | None
    parsed_from_truncated_json: bool


class ReadOnlyAnalysisStore:
    """Read-only access to the analysis database for the web viewer."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.resolve()

    def _connect(self) -> sqlite3.Connection:
        if not self._database_path.exists():
            raise FileNotFoundError(f"База не найдена: {self._database_path}")
        uri = self._database_path.as_posix()
        conn = sqlite3.connect(f"file:///{uri}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def get_stats(self) -> AnalysisStats:
        with self._connect() as conn:
            total = conn.execute("SELECT count(*) FROM image_analyses").fetchone()[0]
            success = conn.execute(
                "SELECT count(*) FROM image_analyses WHERE status='success'"
            ).fetchone()[0]
            errors = conn.execute(
                "SELECT count(*) FROM image_analyses WHERE status='error'"
            ).fetchone()[0]
            manual_review = conn.execute(
                "SELECT count(*) FROM image_analyses WHERE manual_review_required=1"
            ).fetchone()[0]
            risk_rows = conn.execute(
                """
                SELECT overall_risk_level, count(*)
                FROM image_analyses
                WHERE status='success'
                GROUP BY overall_risk_level
                ORDER BY overall_risk_level
                """
            ).fetchall()
        return AnalysisStats(
            total=total,
            success=success,
            errors=errors,
            manual_review=manual_review,
            by_risk={row[0]: row[1] for row in risk_rows},
        )

    def count_analyses(
        self,
        status: str | None = None,
        risk_levels: list[str] | None = None,
    ) -> int:
        where_sql, params = _build_analysis_filters(status=status, risk_levels=risk_levels)
        with self._connect() as conn:
            return conn.execute(
                f"SELECT count(*) FROM image_analyses{where_sql}",
                params,
            ).fetchone()[0]

    def list_analyses(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
        status: str | None = None,
        risk_levels: list[str] | None = None,
    ) -> list[AnalysisListItem]:
        where_sql, params = _build_analysis_filters(status=status, risk_levels=risk_levels)
        query = f"""
            SELECT id, image_url, overall_risk_level, manual_review_required,
                   status, source_row_index, created_at, error_message, confidence,
                   parsed_from_truncated_json
            FROM image_analyses
            {where_sql}
            ORDER BY id DESC LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            AnalysisListItem(
                id=row["id"],
                image_url=row["image_url"],
                overall_risk_level=row["overall_risk_level"],
                manual_review_required=bool(row["manual_review_required"]),
                status=row["status"],
                source_row_index=row["source_row_index"],
                created_at=row["created_at"],
                error_message=row["error_message"],
                confidence=row["confidence"] if "confidence" in row.keys() else None,
                parsed_from_truncated_json=bool(
                    row["parsed_from_truncated_json"]
                    if "parsed_from_truncated_json" in row.keys()
                    else 0
                ),
            )
            for row in rows
        ]

    def list_successful_latest_per_url(self) -> list[StoredAnalysis]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM image_analyses
                WHERE status = 'success'
                ORDER BY id ASC
                """
            ).fetchall()
        return _latest_successful_per_url_rows(rows)

    def get_analysis(self, analysis_id: int) -> StoredAnalysis | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM image_analyses WHERE id = ?",
                (analysis_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_stored(row)

    def list_events(self, limit: int = 100):
        from imj_util.database import AnalysisDatabase

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM utility_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [AnalysisDatabase._row_to_event(row) for row in rows]


def _build_analysis_filters(
    *,
    status: str | None,
    risk_levels: list[str] | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if risk_levels:
        placeholders = ", ".join("?" for _ in risk_levels)
        clauses.append(f"overall_risk_level IN ({placeholders})")
        params.extend(risk_levels)
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def _row_to_stored(row: sqlite3.Row) -> StoredAnalysis:
    from imj_util.database import AnalysisDatabase

    return AnalysisDatabase._row_to_stored(row)


def _latest_successful_per_url_rows(rows: list[sqlite3.Row]) -> list[StoredAnalysis]:
    latest: dict[str, StoredAnalysis] = {}
    for row in rows:
        stored = _row_to_stored(row)
        latest[stored.image_url] = stored
    return list(latest.values())


def escape_html(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def image_proxy_url(image_url: str) -> str:
    return f"/image?url={quote(image_url, safe='')}"
