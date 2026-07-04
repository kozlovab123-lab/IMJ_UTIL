from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
import xlrd
import xlwt

from imj_util.config import settings
from imj_util.gigachat_client import is_remote_image_url
from imj_util.issues_reader import IssueRow, read_issues_xls, _normalize_header


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Скачать изображения из issues-examples-v2.xls в локальную папку",
    )
    parser.add_argument(
        "--issues-file",
        default="issues-examples-v2.xls",
        help="Путь к .xls с колонкой image_url",
    )
    parser.add_argument(
        "--output-dir",
        default="imj",
        help="Папка для сохранения файлов (по умолчанию imj/)",
    )
    parser.add_argument(
        "--image-field",
        default="image_url",
        help="Имя колонки с URL изображения",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Ограничить количество файлов (для теста)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        default=True,
        help="Пропускать уже скачанные файлы (по умолчанию включено)",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_false",
        dest="skip_existing",
        help="Перекачать все файлы заново",
    )
    parser.add_argument(
        "--rewrite-urls",
        action="store_true",
        help="Заменить image_url в .xls на локальные пути относительно папки проекта",
    )
    return parser


def local_filename(row: IssueRow) -> str:
    source = row.image_url
    if is_remote_image_url(source):
        path_name = urlparse(source).path.rsplit("/", 1)[-1] or "image.jpg"
    else:
        path_name = Path(source).name or "image.jpg"
    if "." not in path_name:
        path_name = f"{path_name}.jpg"
    return f"row{row.row_index:05d}_{path_name}"


def local_relative_path(row: IssueRow, output_dir: str = "imj") -> str:
    folder = output_dir.strip("/\\")
    return f"{folder}/{local_filename(row)}".replace("\\", "/")


def rewrite_image_urls_in_xls(
    *,
    issues_file: Path,
    output_dir: str = "imj",
    image_field: str = "image_url",
) -> int:
    workbook = xlrd.open_workbook(str(issues_file))
    sheet = workbook.sheet_by_index(0)
    if sheet.nrows == 0:
        return 0

    headers = [_normalize_header(sheet.cell_value(0, col)) for col in range(sheet.ncols)]
    if image_field not in headers:
        raise ValueError(
            f"В файле {issues_file.name} нет колонки '{image_field}'. "
            f"Доступные поля: {', '.join(headers)}"
        )
    image_col = headers.index(image_field)

    out_wb = xlwt.Workbook()
    out_sheet = out_wb.add_sheet(sheet.name or "Sheet1")
    updated = 0

    for row_idx in range(sheet.nrows):
        for col_idx in range(sheet.ncols):
            value = sheet.cell_value(row_idx, col_idx)
            if row_idx > 0 and col_idx == image_col:
                image_url = str(value).strip()
                if image_url and is_remote_image_url(image_url):
                    issue_row = IssueRow(
                        row_index=row_idx + 1,
                        image_url=image_url,
                        payload={},
                    )
                    value = local_relative_path(issue_row, output_dir)
                    updated += 1
            out_sheet.write(row_idx, col_idx, value)

    out_wb.save(str(issues_file))
    return updated


def download_images(
    *,
    issues_file: Path,
    output_dir: Path,
    image_field: str = "image_url",
    limit: int | None = None,
    skip_existing: bool = True,
    timeout: float | None = None,
) -> tuple[int, int, int]:
    rows = read_issues_xls(issues_file, image_url_field=image_field)
    if limit is not None:
        rows = rows[:limit]

    output_dir.mkdir(parents=True, exist_ok=True)
    timeout = timeout if timeout is not None else settings.image_download_timeout

    ok = 0
    skipped = 0
    errors = 0

    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        for index, row in enumerate(rows, start=1):
            if not is_remote_image_url(row.image_url):
                local_path = Path(row.image_url)
                if not local_path.is_absolute():
                    local_path = Path.cwd() / local_path
                if local_path.exists() and local_path.stat().st_size > 0:
                    skipped += 1
                else:
                    errors += 1
                    print(
                        f"Ошибка row={row.row_index}: локальный файл не найден: {row.image_url}",
                        file=sys.stderr,
                        flush=True,
                    )
                if index % 50 == 0 or index == len(rows):
                    print(
                        f"Прогресс: {index}/{len(rows)} | ok={ok} | skipped={skipped} | errors={errors}",
                        flush=True,
                    )
                continue

            target = output_dir / local_filename(row)
            if skip_existing and target.exists() and target.stat().st_size > 0:
                skipped += 1
                if index % 50 == 0 or index == len(rows):
                    print(
                        f"Прогресс: {index}/{len(rows)} | ok={ok} | skipped={skipped} | errors={errors}",
                        flush=True,
                    )
                continue

            try:
                response = client.get(row.image_url)
                response.raise_for_status()
                target.write_bytes(response.content)
                ok += 1
            except Exception as exc:
                errors += 1
                print(f"Ошибка row={row.row_index}: {exc}", file=sys.stderr, flush=True)

            if index % 10 == 0 or index == len(rows):
                print(
                    f"Прогресс: {index}/{len(rows)} | ok={ok} | skipped={skipped} | errors={errors}",
                    flush=True,
                )

    return ok, skipped, errors


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    issues_path = Path(args.issues_file)
    if not issues_path.is_absolute():
        issues_path = Path.cwd() / issues_path

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir

    if not issues_path.exists():
        print(f"Файл не найден: {issues_path}", file=sys.stderr)
        return 1

    if args.rewrite_urls:
        updated = rewrite_image_urls_in_xls(
            issues_file=issues_path,
            output_dir=args.output_dir,
            image_field=args.image_field,
        )
        print(f"Обновлено image_url: {updated} | файл: {issues_path}")
        return 0

    print(f"Старт: {issues_path.name} -> {output_dir}", flush=True)
    ok, skipped, errors = download_images(
        issues_file=issues_path,
        output_dir=output_dir,
        image_field=args.image_field,
        limit=args.limit,
        skip_existing=args.skip_existing,
    )
    print(f"Готово: ok={ok} | skipped={skipped} | errors={errors} | папка: {output_dir}")
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
