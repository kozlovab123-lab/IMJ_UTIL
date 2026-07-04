from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole

from imj_util.config import Settings
from imj_util.image_prepare import prepare_image_for_upload
from imj_util.prompts import ANALYSIS_PROMPT

RISK_LEVELS = ("none", "low", "medium", "high", "critical")
RISK_RANK = {level: index for index, level in enumerate(RISK_LEVELS)}
RISK_CATEGORIES = (
    "sexual_content",
    "violence_humiliation",
    "minors",
    "discrimination_hate",
    "portrait_rights",
    "lgbt_propaganda",
)


@dataclass
class AnalysisResult:
    report: dict[str, Any]
    raw_response: str
    model_name: str
    api_usage: dict[str, Any] | None = None
    api_response: dict[str, Any] | None = None
    image_meta: dict[str, Any] | None = None
    prompt_text: str = ANALYSIS_PROMPT
    parsed_from_truncated_json: bool = False


@dataclass
class ParsedAnalysis:
    report: dict[str, Any]
    parsed_from_truncated_json: bool = False


@dataclass
class ResourceCheckResult:
    available: bool
    exhausted: bool
    message: str
    raw_balance: Any


class ResourceExhaustedError(RuntimeError):
    """Raised when GigaChat resources are exhausted."""


class GigaChatClient:
    def __init__(self, settings: Settings) -> None:
        settings.validate()
        self._settings = settings
        self._resources_exhausted = False
        self._resources_exhausted_message: str | None = None
        self._client = GigaChat(
            credentials=settings.gigachat_basic_auth,
            verify_ssl_certs=settings.gigachat_verify_ssl,
            scope=settings.gigachat_scope,
            timeout=settings.gigachat_timeout,
        )
        self._image_download_timeout = settings.image_download_timeout

    def mark_resources_exhausted(self, message: str) -> None:
        self._resources_exhausted = True
        self._resources_exhausted_message = format_resource_error(message)

    def analyze_image_url(self, image_url: str) -> AnalysisResult:
        image_bytes, filename, content_type = self._download_image(image_url)
        image_bytes, filename, convert_meta = prepare_image_for_upload(
            image_bytes,
            filename,
            content_type,
        )
        image_meta = {
            "filename": filename,
            "size_bytes": len(image_bytes),
            "image_url": image_url,
            "content_type": content_type,
            **convert_meta,
        }
        try:
            file_id = self._upload_image(image_bytes, filename)
        except Exception as exc:
            if is_resource_exhausted_text(str(exc)):
                message = format_resource_error(str(exc))
                self.mark_resources_exhausted(message)
                raise ResourceExhaustedError(message) from exc
            raise
        try:
            try:
                response = self._client.chat(
                    Chat(
                        model=self._settings.gigachat_vision_model,
                        messages=[
                            Messages(
                                role=MessagesRole.USER,
                                content=ANALYSIS_PROMPT,
                                attachments=[file_id],
                            )
                        ],
                        temperature=0.1,
                    )
                )
            except Exception as exc:
                if is_resource_exhausted_text(str(exc)):
                    message = format_resource_error(str(exc))
                    self.mark_resources_exhausted(message)
                    raise ResourceExhaustedError(message) from exc
                raise
        finally:
            try:
                self._client.delete_file(file_id)
            except Exception:
                pass

        raw_response = response.choices[0].message.content
        parsed = parse_analysis_response(raw_response)
        report = parsed.report
        report["image_url"] = image_url
        return AnalysisResult(
            report=report,
            raw_response=raw_response,
            model_name=self._settings.gigachat_vision_model,
            api_usage=_serialize_usage(response),
            api_response=_serialize_model(response),
            image_meta=image_meta,
            parsed_from_truncated_json=parsed.parsed_from_truncated_json,
        )

    def _download_image(self, image_url: str) -> tuple[bytes, str, str | None]:
        if is_remote_image_url(image_url):
            with httpx.Client(
                follow_redirects=True,
                timeout=self._image_download_timeout,
            ) as client:
                response = client.get(image_url)
                response.raise_for_status()
                content = response.content
                content_type = response.headers.get("Content-Type", "").split(";")[0].strip() or None

            path = urlparse(image_url).path
            filename = path.rsplit("/", 1)[-1] if path else "image.jpg"
            if "." not in filename:
                filename = f"{filename}.jpg"
            return content, filename, content_type

        path = resolve_local_image_path(image_url)
        content = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0]
        return content, path.name, content_type

    def _upload_image(self, image_bytes: bytes, filename: str) -> str:
        uploaded = self._client.upload_file(
            (filename, io.BytesIO(image_bytes)),
            purpose="general",
        )
        return uploaded.id_

    def check_resources(self) -> ResourceCheckResult:
        if self._resources_exhausted:
            return ResourceCheckResult(
                available=False,
                exhausted=True,
                message=self._resources_exhausted_message or "Ресурсы GigaChat исчерпаны.",
                raw_balance=None,
            )

        try:
            balance = self._client.get_balance()
            exhausted, exhaustion_message = is_balance_exhausted_for_model(
                balance,
                self._settings.gigachat_vision_model,
            )
            if exhausted:
                message = exhaustion_message or "Баланс GigaChat исчерпан."
                self.mark_resources_exhausted(message)
                return ResourceCheckResult(
                    available=False,
                    exhausted=True,
                    message=message,
                    raw_balance=balance,
                )

            model_balance = get_balance_for_model(balance, self._settings.gigachat_vision_model)
            balance_hint = (
                f"{self._settings.gigachat_vision_model}: {model_balance}"
                if model_balance is not None
                else "баланс получен"
            )
            return ResourceCheckResult(
                available=True,
                exhausted=False,
                message=f"Ресурсы GigaChat доступны ({balance_hint}).",
                raw_balance=balance,
            )
        except Exception as exc:
            if is_rate_limit_text(str(exc)):
                return ResourceCheckResult(
                    available=True,
                    exhausted=False,
                    message="Проверка баланса пропущена: превышен лимит запросов (429).",
                    raw_balance=None,
                )
            if is_resource_exhausted_text(str(exc)):
                message = format_resource_error(str(exc))
                self.mark_resources_exhausted(message)
            exhausted = is_resource_exhausted_text(str(exc))
            return ResourceCheckResult(
                available=not exhausted,
                exhausted=exhausted,
                message=format_resource_error(str(exc)) if exhausted else str(exc),
                raw_balance=None,
            )

    @staticmethod
    def is_resource_exhausted_error(exc: BaseException) -> bool:
        return is_resource_exhausted_text(str(exc))


def is_resource_exhausted_text(text: str) -> bool:
    if is_rate_limit_text(text):
        return False
    lower = text.lower()
    markers = (
        "402",
        "payment required",
        "insufficient",
        "quota exceeded",
        "out of",
        "resource exhausted",
        "превышен лимит баланса",
        "исчерпа",
        "недостаточно средств",
        "недостаточно токенов",
        "требуется оплата",
    )
    return any(marker in lower for marker in markers)


def format_resource_error(message: str) -> str:
    lower = message.lower()
    if "402" in lower or "payment required" in lower or "требуется оплата" in lower:
        return "Баланс GigaChat исчерпан (402 Payment Required)."
    return message


def is_rate_limit_text(text: str) -> bool:
    lower = text.lower()
    return "429" in lower or "too many requests" in lower


def _serialize_model(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return {"raw": str(value)}


def _serialize_usage(response: Any) -> dict[str, Any] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump(mode="json")
    return {"raw": str(usage)}


def is_remote_image_url(image_url: str) -> bool:
    return image_url.lower().startswith(("http://", "https://"))


def resolve_local_image_path(image_url: str, base_dir: Path | None = None) -> Path:
    path = Path(image_url)
    if not path.is_absolute():
        path = (base_dir or Path.cwd()) / path
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Локальный файл изображения не найден: {image_url}")
    return path


def image_hash(image_url: str, image_bytes: bytes | None = None) -> str:
    if image_bytes is not None:
        return hashlib.sha256(image_bytes).hexdigest()
    return hashlib.sha256(image_url.encode("utf-8")).hexdigest()


def parse_analysis_response(raw_response: str) -> ParsedAnalysis:
    text = _strip_response_markdown(raw_response)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        salvaged = _salvage_truncated_analysis_json(text)
        if salvaged is None:
            raise ValueError(f"GigaChat вернул не-JSON ответ: {raw_response[:500]}")
        return ParsedAnalysis(
            report=_normalize_analysis_report(salvaged, truncated_recovery=True),
            parsed_from_truncated_json=True,
        )

    if not isinstance(data, dict):
        raise ValueError("Ответ GigaChat должен быть JSON-объектом")

    return ParsedAnalysis(
        report=_normalize_analysis_report(data, truncated_recovery=False),
        parsed_from_truncated_json=False,
    )


def _strip_response_markdown(raw_response: str) -> str:
    text = raw_response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _normalize_analysis_report(data: dict[str, Any], *, truncated_recovery: bool) -> dict[str, Any]:
    overall = data.get("overall_risk_level", "none")
    if overall not in RISK_LEVELS:
        overall = _normalize_risk_level(str(overall))
        data["overall_risk_level"] = overall

    risks = data.get("risks")
    if not isinstance(risks, list):
        data["risks"] = _build_risks_from_legacy(data)
    elif truncated_recovery:
        data["risks"] = _complete_risks_list(risks)
        if data.get("overall_risk_level", "none") == "none":
            data["overall_risk_level"] = _max_risk_level(data["risks"])

    data.setdefault("recommendations", [])
    data.setdefault("manual_review_required", overall in {"high", "critical"})
    data.setdefault(
        "disclaimer",
        "Выводы носят рекомендательный характер; окончательное решение принимает человек.",
    )
    if truncated_recovery:
        data.setdefault(
            "manual_review_reason",
            "Ответ модели был обрезан; оценка восстановлена из неполного JSON.",
        )
        data["manual_review_required"] = True
    return data


def _salvage_truncated_analysis_json(text: str) -> dict[str, Any] | None:
    if not text.startswith("{"):
        return None

    risks = _extract_json_objects_after_key(text, "risks")
    if not risks:
        return None

    data: dict[str, Any] = {"risks": risks}
    for field in (
        "overall_risk_level",
        "text_on_image",
        "manual_review_reason",
        "disclaimer",
    ):
        value = _extract_scalar_field(text, field)
        if value is not None:
            data[field] = value

    confidence = _extract_number_field(text, "confidence")
    if confidence is not None:
        data["confidence"] = confidence

    recommendations = _extract_string_list_field(text, "recommendations")
    if recommendations is not None:
        data["recommendations"] = recommendations

    manual_review = _extract_bool_field(text, "manual_review_required")
    if manual_review is not None:
        data["manual_review_required"] = manual_review

    return data


def _extract_json_objects_after_key(text: str, key: str) -> list[dict[str, Any]]:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*\[', text)
    if not match:
        return []

    objects: list[dict[str, Any]] = []
    index = match.end()
    length = len(text)
    while index < length:
        while index < length and text[index] in " \t\n\r,":
            index += 1
        if index >= length or text[index] == "]":
            break
        if text[index] != "{":
            break
        obj, next_index = _read_json_object(text, index)
        if obj is None:
            break
        objects.append(obj)
        index = next_index
    return objects


def _read_json_object(text: str, start: int) -> tuple[dict[str, Any] | None, int]:
    if start >= len(text) or text[start] != "{":
        return None, start

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                chunk = text[start : index + 1]
                try:
                    parsed = json.loads(chunk)
                except json.JSONDecodeError:
                    return None, index + 1
                if isinstance(parsed, dict):
                    return parsed, index + 1
                return None, index + 1
    return None, start


def _extract_scalar_field(text: str, field: str) -> Any:
    match = re.search(
        rf'"{re.escape(field)}"\s*:\s*(null|"([^"\\]|\\.)*")',
        text,
    )
    if not match:
        return None
    token = match.group(1)
    if token == "null":
        return None
    try:
        return json.loads(token)
    except json.JSONDecodeError:
        return None


def _extract_number_field(text: str, field: str) -> float | None:
    match = re.search(rf'"{re.escape(field)}"\s*:\s*(-?\d+(?:\.\d+)?)', text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _extract_bool_field(text: str, field: str) -> bool | None:
    match = re.search(rf'"{re.escape(field)}"\s*:\s*(true|false)', text, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).lower() == "true"


def _extract_string_list_field(text: str, field: str) -> list[str] | None:
    match = re.search(rf'"{re.escape(field)}"\s*:\s*\[', text)
    if not match:
        return None
    items, _ = _read_json_array(text, match.end())
    if items is None:
        return None
    return [str(item) for item in items if isinstance(item, str)]


def _read_json_array(text: str, start: int) -> tuple[list[Any] | None, int]:
    items: list[Any] = []
    index = start
    length = len(text)
    while index < length:
        while index < length and text[index] in " \t\n\r,":
            index += 1
        if index >= length:
            break
        if text[index] == "]":
            return items, index + 1
        if text[index] == '"':
            item, index = _read_json_string(text, index)
            if item is None:
                return items if items else None, index
            items.append(item)
            continue
        if text[index] == "{":
            item, index = _read_json_object(text, index)
            if item is None:
                return items if items else None, index
            items.append(item)
            continue
        return items if items else None, index
    return items if items else None, index


def _read_json_string(text: str, start: int) -> tuple[str | None, int]:
    if start >= len(text) or text[start] != '"':
        return None, start
    escape = False
    for index in range(start + 1, len(text)):
        char = text[index]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
        elif char == '"':
            try:
                return json.loads(text[start : index + 1]), index + 1
            except json.JSONDecodeError:
                return None, index + 1
    return None, start


def _complete_risks_list(risks: list[Any]) -> list[dict[str, Any]]:
    by_category: dict[str, dict[str, Any]] = {}
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        category = str(risk.get("category", "")).strip()
        if not category:
            continue
        level = risk.get("risk_level", "none")
        if level not in RISK_LEVELS:
            level = _normalize_risk_level(str(level))
        by_category[category] = {
            **risk,
            "category": category,
            "risk_level": level,
            "detected_signs": risk.get("detected_signs") or [],
        }

    completed: list[dict[str, Any]] = []
    for category in RISK_CATEGORIES:
        if category in by_category:
            completed.append(by_category[category])
        else:
            completed.append(
                {
                    "category": category,
                    "risk_level": "none",
                    "detected_signs": [],
                    "legal_area": None,
                    "description": "Категория не попала в обрезанный ответ модели.",
                }
            )
    return completed


def _max_risk_level(risks: list[dict[str, Any]]) -> str:
    best = "none"
    for risk in risks:
        level = risk.get("risk_level", "none")
        if level not in RISK_LEVELS:
            level = _normalize_risk_level(str(level))
        if RISK_RANK[level] > RISK_RANK[best]:
            best = level
    return best


def _normalize_risk_level(value: str) -> str:
    normalized = value.strip().lower()
    mapping = {
        "нет": "none",
        "нет риска": "none",
        "низкий": "low",
        "средний": "medium",
        "высокий": "high",
        "критический": "critical",
    }
    return mapping.get(normalized, "none")


def _build_risks_from_legacy(data: dict[str, Any]) -> list[dict[str, Any]]:
    risks = []
    for category in RISK_CATEGORIES:
        if category in data and isinstance(data[category], dict):
            risks.append({"category": category, **data[category]})
    return risks


def _is_rate_limit_error(exc: BaseException) -> bool:
    return is_rate_limit_text(str(exc))


def get_balance_for_model(balance: Any, model_name: str) -> float | None:
    model_key = model_name.strip().lower()
    for usage, value in iter_balance_entries(balance):
        if usage.strip().lower() == model_key:
            return value
    return None


def is_balance_exhausted_for_model(balance: Any, model_name: str) -> tuple[bool, str | None]:
    model_balance = get_balance_for_model(balance, model_name)
    if model_balance is None:
        return False, None
    if model_balance <= 0:
        return True, f"Баланс модели {model_name} исчерпан ({model_balance})."
    return False, None


def iter_balance_entries(balance: Any) -> list[tuple[str, float]]:
    if hasattr(balance, "balance"):
        items = getattr(balance, "balance")
        if isinstance(items, list):
            entries: list[tuple[str, float]] = []
            for item in items:
                if hasattr(item, "usage") and hasattr(item, "value"):
                    entries.append((str(item.usage), float(item.value)))
                elif isinstance(item, dict) and "usage" in item and "value" in item:
                    entries.append((str(item["usage"]), float(item["value"])))
            return entries

    if isinstance(balance, dict) and isinstance(balance.get("balance"), list):
        entries = []
        for item in balance["balance"]:
            if isinstance(item, dict) and "usage" in item and "value" in item:
                entries.append((str(item["usage"]), float(item["value"])))
        return entries

    return []


def _is_zero_or_negative_balance(balance: Any) -> bool:
    entries = iter_balance_entries(balance)
    if entries:
        return all(value <= 0 for _, value in entries)

    candidates = []
    for attr_name in ("balance", "value", "amount", "available", "left"):
        if hasattr(balance, attr_name):
            candidates.append(getattr(balance, attr_name))
    if isinstance(balance, dict):
        for key in ("balance", "value", "amount", "available", "left"):
            if key in balance:
                candidates.append(balance[key])

    for candidate in candidates:
        try:
            value = float(candidate)
            return value <= 0
        except (TypeError, ValueError):
            continue
    return False
