from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import xlrd


@dataclass(frozen=True)
class IssueRow:
    row_index: int
    image_url: str
    payload: dict[str, Any]


def read_issues_xls(file_path: Path, image_url_field: str = "image_url") -> list[IssueRow]:
    if not file_path.exists():
        raise FileNotFoundError(f"Файл не найден: {file_path}")

    workbook = xlrd.open_workbook(str(file_path))
    sheet = workbook.sheet_by_index(0)
    if sheet.nrows == 0:
        return []

    headers = [_normalize_header(sheet.cell_value(0, col)) for col in range(sheet.ncols)]
    if image_url_field not in headers:
        raise ValueError(
            f"В файле {file_path.name} нет колонки '{image_url_field}'. "
            f"Доступные поля: {', '.join(headers)}"
        )

    image_col = headers.index(image_url_field)
    rows: list[IssueRow] = []
    for row_idx in range(1, sheet.nrows):
        row_payload: dict[str, Any] = {}
        for col_idx, header in enumerate(headers):
            if not header:
                continue
            row_payload[header] = _cell_to_python(sheet.cell_value(row_idx, col_idx))

        image_url = str(row_payload.get(image_url_field, "")).strip()
        if not image_url:
            continue

        rows.append(
            IssueRow(
                row_index=row_idx + 1,
                image_url=image_url,
                payload=row_payload,
            )
        )

    return rows


def _normalize_header(value: Any) -> str:
    return str(value).strip()


def _cell_to_python(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value
