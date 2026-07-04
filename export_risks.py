from __future__ import annotations

import argparse
import sys
from pathlib import Path

from imj_util.config import settings
from imj_util.risk_export import export_elevated_risks_csv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Выгрузка в CSV анализов с риском выше low (medium, high, critical)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="data/elevated_risks.csv",
        help="Путь к выходному CSV (по умолчанию data/elevated_risks.csv)",
    )
    parser.add_argument(
        "--database",
        default=None,
        help="Путь к SQLite (по умолчанию DATABASE_PATH из .env)",
    )
    parser.add_argument(
        "--issues-file",
        default="issues-examples-v2.xls",
        help="XLS для подстановки полей строки, если в БД нет source_payload",
    )
    parser.add_argument(
        "--image-field",
        default="image_url",
        help="Имя колонки с URL изображения в XLS",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    database_path = Path(args.database) if args.database else settings.database_path
    if not database_path.is_absolute():
        database_path = Path.cwd() / database_path

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = Path.cwd() / output_path

    issues_path = Path(args.issues_file)
    if not issues_path.is_absolute():
        issues_path = Path.cwd() / issues_path

    if not database_path.exists():
        print(f"База не найдена: {database_path}", file=sys.stderr)
        return 1

    try:
        result = export_elevated_risks_csv(
            database_path=database_path,
            output_path=output_path,
            issues_file=issues_path if issues_path.exists() else None,
            image_field=args.image_field,
        )
    except Exception as exc:
        print(f"Ошибка выгрузки: {exc}", file=sys.stderr)
        return 1

    print(
        f"Готово: {result.exported_rows} строк с риском > low "
        f"(из {result.total_successful} успешных, пропущено {result.skipped_rows})"
    )
    print(f"Файл: {result.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
