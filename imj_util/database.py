from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS image_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_url TEXT NOT NULL,
    image_hash TEXT,
    overall_risk_level TEXT NOT NULL,
    manual_review_required INTEGER NOT NULL DEFAULT 0,
    model_name TEXT,
    report_json TEXT NOT NULL,
    raw_response TEXT,
    status TEXT NOT NULL DEFAULT 'success',
    error_message TEXT,
    source_file TEXT,
    source_row_index INTEGER,
    source_payload_json TEXT,
    text_on_image TEXT,
    confidence REAL,
    recommendations_json TEXT,
    manual_review_reason TEXT,
    disclaimer TEXT,
    risks_json TEXT,
    api_usage_json TEXT,
    api_response_json TEXT,
    output_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gigachat_resource_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT,
    source_row_index INTEGER,
    available INTEGER NOT NULL,
    exhausted INTEGER NOT NULL,
    message TEXT,
    balance_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS utility_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    message TEXT,
    context_json TEXT,
    source_file TEXT,
    source_row_index INTEGER,
    image_url TEXT,
    analysis_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analyses_image_url ON image_analyses(image_url);
CREATE INDEX IF NOT EXISTS idx_analyses_created_at ON image_analyses(created_at);
CREATE INDEX IF NOT EXISTS idx_analyses_url_status ON image_analyses(image_url, status);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON utility_events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON utility_events(event_type);
"""


@dataclass
class StoredAnalysis:
    id: int
    image_url: str
    image_hash: str | None
    overall_risk_level: str
    manual_review_required: bool
    model_name: str | None
    report: dict[str, Any]
    raw_response: str | None
    status: str
    error_message: str | None
    source_file: str | None
    source_row_index: int | None
    source_payload: dict[str, Any]
    text_on_image: str | None
    confidence: float | None
    recommendations: list[Any]
    manual_review_reason: str | None
    disclaimer: str | None
    risks: list[Any]
    api_usage: dict[str, Any] | None
    api_response: dict[str, Any] | None
    output: dict[str, Any] | None
    parsed_from_truncated_json: bool
    created_at: str


@dataclass
class UtilityEvent:
    id: int
    event_type: str
    message: str | None
    context: dict[str, Any]
    source_file: str | None
    source_row_index: int | None
    image_url: str | None
    analysis_id: int | None
    created_at: str


class AnalysisDatabase:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._ensure_optional_columns(conn)

    @staticmethod
    def _ensure_optional_columns(conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(image_analyses)").fetchall()
        }
        optional_columns = {
            "source_file": "TEXT",
            "source_row_index": "INTEGER",
            "source_payload_json": "TEXT",
            "text_on_image": "TEXT",
            "confidence": "REAL",
            "recommendations_json": "TEXT",
            "manual_review_reason": "TEXT",
            "disclaimer": "TEXT",
            "risks_json": "TEXT",
            "api_usage_json": "TEXT",
            "api_response_json": "TEXT",
            "output_json": "TEXT",
            "parsed_from_truncated_json": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, col_type in optional_columns.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE image_analyses ADD COLUMN {name} {col_type}")

        event_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='utility_events'"
        ).fetchone()
        if event_table is None:
            conn.executescript(
                """
                CREATE TABLE utility_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    message TEXT,
                    context_json TEXT,
                    source_file TEXT,
                    source_row_index INTEGER,
                    image_url TEXT,
                    analysis_id INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_created_at ON utility_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_events_type ON utility_events(event_type);
                """
            )
        conn.commit()

    def save_success(
        self,
        *,
        image_url: str,
        image_hash: str | None,
        overall_risk_level: str,
        manual_review_required: bool,
        model_name: str,
        report: dict[str, Any],
        raw_response: str,
        source_file: str | None = None,
        source_row_index: int | None = None,
        source_payload: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        api_usage: dict[str, Any] | None = None,
        api_response: dict[str, Any] | None = None,
        parsed_from_truncated_json: bool = False,
    ) -> int:
        created_at = datetime.now(timezone.utc).isoformat()
        extras = _extract_report_fields(report)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO image_analyses (
                    image_url, image_hash, overall_risk_level, manual_review_required,
                    model_name, report_json, raw_response, status, source_file,
                    source_row_index, source_payload_json, text_on_image, confidence,
                    recommendations_json, manual_review_reason, disclaimer, risks_json,
                    api_usage_json, api_response_json, output_json,
                    parsed_from_truncated_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'success', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    image_url,
                    image_hash,
                    overall_risk_level,
                    int(manual_review_required),
                    model_name,
                    json.dumps(report, ensure_ascii=False),
                    raw_response,
                    source_file,
                    source_row_index,
                    json.dumps(source_payload or {}, ensure_ascii=False),
                    extras["text_on_image"],
                    extras["confidence"],
                    extras["recommendations_json"],
                    extras["manual_review_reason"],
                    extras["disclaimer"],
                    extras["risks_json"],
                    json.dumps(api_usage or {}, ensure_ascii=False),
                    json.dumps(api_response or {}, ensure_ascii=False, default=str),
                    json.dumps(output or {}, ensure_ascii=False, default=str),
                    int(parsed_from_truncated_json),
                    created_at,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def save_error(
        self,
        *,
        image_url: str,
        image_hash: str | None,
        error_message: str,
        source_file: str | None = None,
        source_row_index: int | None = None,
        source_payload: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
    ) -> int:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO image_analyses (
                    image_url, image_hash, overall_risk_level, manual_review_required,
                    report_json, status, error_message, source_file, source_row_index,
                    source_payload_json, output_json, created_at
                ) VALUES (?, ?, 'none', 0, '{}', 'error', ?, ?, ?, ?, ?, ?)
                """,
                (
                    image_url,
                    image_hash,
                    error_message,
                    source_file,
                    source_row_index,
                    json.dumps(source_payload or {}, ensure_ascii=False),
                    json.dumps(output or {}, ensure_ascii=False, default=str),
                    created_at,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def save_resource_check(
        self,
        *,
        source_file: str | None,
        source_row_index: int | None,
        available: bool,
        exhausted: bool,
        message: str,
        balance: Any,
    ) -> int:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO gigachat_resource_checks (
                    source_file, source_row_index, available, exhausted,
                    message, balance_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_file,
                    source_row_index,
                    int(available),
                    int(exhausted),
                    message,
                    json.dumps(balance, ensure_ascii=False, default=str),
                    created_at,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def save_utility_event(
        self,
        *,
        event_type: str,
        message: str | None = None,
        context: dict[str, Any] | None = None,
        source_file: str | None = None,
        source_row_index: int | None = None,
        image_url: str | None = None,
        analysis_id: int | None = None,
    ) -> int:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO utility_events (
                    event_type, message, context_json, source_file,
                    source_row_index, image_url, analysis_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    message,
                    json.dumps(context or {}, ensure_ascii=False, default=str),
                    source_file,
                    source_row_index,
                    image_url,
                    analysis_id,
                    created_at,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def get_by_id(self, analysis_id: int) -> StoredAnalysis | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM image_analyses WHERE id = ?",
                (analysis_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_stored(row)

    def get_successful_by_image_url(self, image_url: str) -> StoredAnalysis | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM image_analyses
                WHERE image_url = ? AND status = 'success'
                ORDER BY id DESC
                LIMIT 1
                """,
                (image_url,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_stored(row)

    def list_recent(self, limit: int = 20) -> list[StoredAnalysis]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM image_analyses
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_stored(row) for row in rows]

    def list_recent_events(self, limit: int = 100) -> list[UtilityEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM utility_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    @staticmethod
    def _row_to_stored(row: sqlite3.Row) -> StoredAnalysis:
        keys = row.keys()
        return StoredAnalysis(
            id=row["id"],
            image_url=row["image_url"],
            image_hash=row["image_hash"],
            overall_risk_level=row["overall_risk_level"],
            manual_review_required=bool(row["manual_review_required"]),
            model_name=row["model_name"],
            report=json.loads(row["report_json"]),
            raw_response=row["raw_response"],
            status=row["status"],
            error_message=row["error_message"],
            source_file=row["source_file"] if "source_file" in keys else None,
            source_row_index=row["source_row_index"] if "source_row_index" in keys else None,
            source_payload=(
                json.loads(row["source_payload_json"])
                if "source_payload_json" in keys and row["source_payload_json"]
                else {}
            ),
            text_on_image=row["text_on_image"] if "text_on_image" in keys else None,
            confidence=row["confidence"] if "confidence" in keys else None,
            recommendations=_loads_json_list(row["recommendations_json"] if "recommendations_json" in keys else None),
            manual_review_reason=row["manual_review_reason"] if "manual_review_reason" in keys else None,
            disclaimer=row["disclaimer"] if "disclaimer" in keys else None,
            risks=_loads_json_list(row["risks_json"] if "risks_json" in keys else None),
            api_usage=_loads_json_dict(row["api_usage_json"] if "api_usage_json" in keys else None),
            api_response=_loads_json_dict(row["api_response_json"] if "api_response_json" in keys else None),
            output=_loads_json_dict(row["output_json"] if "output_json" in keys else None),
            parsed_from_truncated_json=bool(
                row["parsed_from_truncated_json"] if "parsed_from_truncated_json" in keys else 0
            ),
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> UtilityEvent:
        return UtilityEvent(
            id=row["id"],
            event_type=row["event_type"],
            message=row["message"],
            context=json.loads(row["context_json"]) if row["context_json"] else {},
            source_file=row["source_file"],
            source_row_index=row["source_row_index"],
            image_url=row["image_url"],
            analysis_id=row["analysis_id"],
            created_at=row["created_at"],
        )


def _extract_report_fields(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "text_on_image": report.get("text_on_image"),
        "confidence": report.get("confidence"),
        "recommendations_json": json.dumps(report.get("recommendations", []), ensure_ascii=False),
        "manual_review_reason": report.get("manual_review_reason"),
        "disclaimer": report.get("disclaimer"),
        "risks_json": json.dumps(report.get("risks", []), ensure_ascii=False),
    }


def _loads_json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    data = json.loads(value)
    return data if isinstance(data, list) else []


def _loads_json_dict(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    data = json.loads(value)
    return data if isinstance(data, dict) else None
