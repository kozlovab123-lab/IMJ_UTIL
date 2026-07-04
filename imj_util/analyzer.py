from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Any

from imj_util.config import Settings, settings
from imj_util.database import AnalysisDatabase, StoredAnalysis
from imj_util.gigachat_client import (
    AnalysisResult,
    GigaChatClient,
    ResourceCheckResult,
    ResourceExhaustedError,
    format_resource_error,
    image_hash,
    is_resource_exhausted_text,
)


@dataclass
class AnalyzeOutcome:
    analysis_id: int
    stored: StoredAnalysis | None
    error: str | None = None
    resources_exhausted: bool = False
    skipped: bool = False


class ImageAnalyzer:
    def __init__(
        self,
        app_settings: Settings | None = None,
        database: AnalysisDatabase | None = None,
        gigachat_client: GigaChatClient | None = None,
    ) -> None:
        self._settings = app_settings or settings
        self._database = database or AnalysisDatabase(self._settings.database_path)
        self._gigachat = gigachat_client or GigaChatClient(self._settings)

    def log_event(
        self,
        event_type: str,
        message: str | None = None,
        *,
        context: dict[str, Any] | None = None,
        source_file: str | None = None,
        source_row_index: int | None = None,
        image_url: str | None = None,
        analysis_id: int | None = None,
    ) -> int:
        return self._database.save_utility_event(
            event_type=event_type,
            message=message,
            context=context,
            source_file=source_file,
            source_row_index=source_row_index,
            image_url=image_url,
            analysis_id=analysis_id,
        )

    def analyze_and_save(
        self,
        image_url: str,
        *,
        source_file: str | None = None,
        source_row_index: int | None = None,
        source_payload: dict | None = None,
        skip_if_cached: bool = False,
    ) -> AnalyzeOutcome:
        if skip_if_cached:
            cached = self._database.get_successful_by_image_url(image_url)
            if cached is not None:
                self.log_event(
                    "analysis_cached",
                    message=f"Пропуск: ранее обработано без ошибок (id={cached.id})",
                    context={
                        "cached_from_id": cached.id,
                        "source_payload": source_payload or {},
                        "overall_risk_level": cached.overall_risk_level,
                    },
                    source_file=source_file,
                    source_row_index=source_row_index,
                    image_url=image_url,
                    analysis_id=cached.id,
                )
                return AnalyzeOutcome(
                    analysis_id=cached.id,
                    stored=cached,
                    skipped=True,
                )

        url_hash = image_hash(image_url)
        try:
            result = self._gigachat.analyze_image_url(image_url)
            output = _build_success_output(
                image_url=image_url,
                result=result,
                source_payload=source_payload,
            )
            analysis_id = self._database.save_success(
                image_url=image_url,
                image_hash=url_hash,
                overall_risk_level=result.report.get("overall_risk_level", "none"),
                manual_review_required=bool(result.report.get("manual_review_required")),
                model_name=result.model_name,
                report=result.report,
                raw_response=result.raw_response,
                source_file=source_file,
                source_row_index=source_row_index,
                source_payload=source_payload,
                output=output,
                api_usage=result.api_usage,
                api_response=result.api_response,
                parsed_from_truncated_json=result.parsed_from_truncated_json,
            )
            stored = self._database.get_by_id(analysis_id)
            self.log_event(
                "analysis_success",
                message=_terminal_success_message(stored),
                context=output,
                source_file=source_file,
                source_row_index=source_row_index,
                image_url=image_url,
                analysis_id=analysis_id,
            )
            return AnalyzeOutcome(analysis_id=analysis_id, stored=stored)
        except Exception as exc:
            error_message = str(exc)
            if isinstance(exc, ResourceExhaustedError):
                error_message = str(exc)
            elif is_resource_exhausted_text(error_message):
                error_message = format_resource_error(error_message)

            resources_exhausted = isinstance(exc, ResourceExhaustedError) or is_resource_exhausted_text(
                error_message
            )
            if resources_exhausted:
                self._gigachat.mark_resources_exhausted(error_message)

            output = _build_error_output(
                image_url=image_url,
                error_message=error_message,
                source_payload=source_payload,
                exc=exc,
            )
            analysis_id = self._database.save_error(
                image_url=image_url,
                image_hash=url_hash,
                error_message=error_message,
                source_file=source_file,
                source_row_index=source_row_index,
                source_payload=source_payload,
                output=output,
            )
            self.log_event(
                "analysis_error",
                message=error_message,
                context=output,
                source_file=source_file,
                source_row_index=source_row_index,
                image_url=image_url,
                analysis_id=analysis_id,
            )
            return AnalyzeOutcome(
                analysis_id=analysis_id,
                stored=self._database.get_by_id(analysis_id),
                error=error_message,
                resources_exhausted=resources_exhausted,
            )

    def check_resources(
        self,
        *,
        source_file: str | None = None,
        source_row_index: int | None = None,
        image_url: str | None = None,
    ) -> ResourceCheckResult:
        result = self._gigachat.check_resources()
        check_id = self._database.save_resource_check(
            source_file=source_file,
            source_row_index=source_row_index,
            available=result.available,
            exhausted=result.exhausted,
            message=result.message,
            balance=result.raw_balance,
        )
        self.log_event(
            "resource_check",
            message=result.message,
            context={
                "resource_check_id": check_id,
                "available": result.available,
                "exhausted": result.exhausted,
                "balance": _jsonable(result.raw_balance),
            },
            source_file=source_file,
            source_row_index=source_row_index,
            image_url=image_url,
        )
        return result

    def list_analyses(self, limit: int = 20) -> list[StoredAnalysis]:
        return self._database.list_recent(limit=limit)

    def list_events(self, limit: int = 100):
        return self._database.list_recent_events(limit=limit)

    def get_analysis(self, analysis_id: int) -> StoredAnalysis | None:
        return self._database.get_by_id(analysis_id)

    def get_successful_analysis(self, image_url: str) -> StoredAnalysis | None:
        return self._database.get_successful_by_image_url(image_url)


def _build_success_output(
    *,
    image_url: str,
    result: AnalysisResult,
    source_payload: dict | None,
) -> dict[str, Any]:
    report = result.report
    return {
        "status": "success",
        "image_url": image_url,
        "parsed_from_truncated_json": result.parsed_from_truncated_json,
        "overall_risk_level": report.get("overall_risk_level"),
        "manual_review_required": report.get("manual_review_required"),
        "manual_review_reason": report.get("manual_review_reason"),
        "text_on_image": report.get("text_on_image"),
        "confidence": report.get("confidence"),
        "recommendations": report.get("recommendations", []),
        "disclaimer": report.get("disclaimer"),
        "risks": report.get("risks", []),
        "report": report,
        "raw_response": result.raw_response,
        "model_name": result.model_name,
        "prompt_text": result.prompt_text,
        "image_meta": result.image_meta,
        "api_usage": result.api_usage,
        "api_response": result.api_response,
        "source_payload": source_payload or {},
        "terminal": {
            "overall_risk_level": report.get("overall_risk_level"),
            "manual_review_required": report.get("manual_review_required"),
            "parsed_from_truncated_json": result.parsed_from_truncated_json,
        },
    }


def _build_error_output(
    *,
    image_url: str,
    error_message: str,
    source_payload: dict | None,
    exc: Exception,
) -> dict[str, Any]:
    return {
        "status": "error",
        "image_url": image_url,
        "error_message": error_message,
        "error_type": type(exc).__name__,
        "traceback": traceback.format_exc(),
        "source_payload": source_payload or {},
        "terminal": {
            "error": error_message,
        },
    }


def _terminal_success_message(stored: StoredAnalysis | None) -> str | None:
    if stored is None:
        return None
    review = "да" if stored.manual_review_required else "нет"
    truncated = "да" if stored.parsed_from_truncated_json else "нет"
    return (
        f"Сохранено в БД: id={stored.id}; "
        f"Общий риск: {stored.overall_risk_level}; "
        f"Ручная проверка: {review}; "
        f"Обрезанный JSON: {truncated}"
    )


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (str, int, float, bool, list, dict)):
        return value
    return str(value)
