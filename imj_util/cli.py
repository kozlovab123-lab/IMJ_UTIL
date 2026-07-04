from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from imj_util.analyzer import ImageAnalyzer
from imj_util.config import settings
from imj_util.issues_reader import read_issues_xls


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Анализ изображений через GigaChat с сохранением в SQLite",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="Проанализировать изображение")
    analyze_parser.add_argument(
        "--url",
        help="URL изображения (по умолчанию TEST_IMAGE_URL из .env)",
    )

    show_parser = subparsers.add_parser("show", help="Показать сохранённый анализ")
    show_parser.add_argument("id", type=int, help="ID записи в БД")

    list_parser = subparsers.add_parser("list", help="Список последних анализов")
    list_parser.add_argument("--limit", type=int, default=20, help="Количество записей")

    batch_parser = subparsers.add_parser("analyze-batch", help="Пакетный анализ из XLS")
    batch_parser.add_argument(
        "--issues-file",
        default="issues-examples-v2.xls",
        help="Путь к .xls файлу с колонкой image_url",
    )
    batch_parser.add_argument(
        "--image-field",
        default="image_url",
        help="Имя колонки с URL изображения",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    analyzer = ImageAnalyzer()

    if args.command == "analyze":
        image_url = args.url or settings.test_image_url
        if not image_url:
            print("Укажите --url или задайте TEST_IMAGE_URL в .env", file=sys.stderr)
            return 1

        print(f"Анализ: {image_url}")
        analyzer.log_event("analyze_start", message=f"Анализ: {image_url}", image_url=image_url)
        outcome = analyzer.analyze_and_save(image_url)
        if outcome.error:
            print(f"Ошибка: {outcome.error}", file=sys.stderr)
            print(f"Запись об ошибке сохранена с id={outcome.analysis_id}", file=sys.stderr)
            return 1

        stored = outcome.stored
        assert stored is not None
        print(f"Сохранено в БД: id={stored.id}")
        print(f"Общий риск: {stored.overall_risk_level}")
        print(f"Ручная проверка: {'да' if stored.manual_review_required else 'нет'}")
        print(json.dumps(stored.report, ensure_ascii=False, indent=2))
        return 0

    if args.command == "analyze-batch":
        source_path = Path(args.issues_file)
        if not source_path.is_absolute():
            source_path = Path.cwd() / source_path
        source_name = source_path.name

        try:
            rows = read_issues_xls(source_path, image_url_field=args.image_field)
        except Exception as exc:
            print(f"Ошибка чтения входного файла: {exc}", file=sys.stderr)
            return 1

        total = len(rows)
        if total == 0:
            print("Входной файл не содержит строк с image_url")
            return 0

        print(f"Старт пакетного анализа: {total} изображений", flush=True)
        analyzer.log_event(
            "batch_start",
            message=f"Старт пакетного анализа: {total} изображений",
            context={"total": total, "issues_file": str(source_path), "image_field": args.image_field},
            source_file=source_name,
        )
        success_count = 0
        cached_count = 0
        error_count = 0

        for index, row in enumerate(rows, start=1):
            cached = analyzer.get_successful_analysis(row.image_url)
            if cached is not None:
                cached_count += 1
                success_count += 1
                progress = (
                    f"Прогресс: {index}/{total} | ok={success_count} | "
                    f"cached={cached_count} | errors={error_count}"
                )
                print(progress, flush=True)
                analyzer.log_event(
                    "batch_progress",
                    message=progress,
                    context={
                        "index": index,
                        "total": total,
                        "ok": success_count,
                        "cached": cached_count,
                        "errors": error_count,
                        "cached_from_id": cached.id,
                    },
                    source_file=source_name,
                    source_row_index=row.row_index,
                    image_url=row.image_url,
                    analysis_id=cached.id,
                )
                analyzer.log_event(
                    "analysis_cached",
                    message=f"Пропуск: ранее обработано без ошибок (id={cached.id})",
                    context={
                        "cached_from_id": cached.id,
                        "source_payload": row.payload,
                    },
                    source_file=source_name,
                    source_row_index=row.row_index,
                    image_url=row.image_url,
                    analysis_id=cached.id,
                )
                continue

            check = analyzer.check_resources(
                source_file=source_name,
                source_row_index=row.row_index,
                image_url=row.image_url,
            )
            if not check.available:
                reason = "исчерпаны ресурсы GigaChat" if check.exhausted else "ресурс недоступен"
                abort_message = (
                    f"Досрочное завершение: {reason}. "
                    f"Обработано {index - 1}/{total}. Причина: {check.message}"
                )
                print(abort_message, file=sys.stderr, flush=True)
                analyzer.log_event(
                    "batch_abort",
                    message=abort_message,
                    context={
                        "index": index,
                        "total": total,
                        "reason": reason,
                        "check_message": check.message,
                        "exhausted": check.exhausted,
                    },
                    source_file=source_name,
                    source_row_index=row.row_index,
                    image_url=row.image_url,
                )
                return 2 if check.exhausted else 1

            outcome = analyzer.analyze_and_save(
                row.image_url,
                source_file=source_name,
                source_row_index=row.row_index,
                source_payload=row.payload,
            )
            if outcome.error:
                error_count += 1
                if outcome.resources_exhausted:
                    abort_message = (
                        f"Досрочное завершение: исчерпаны ресурсы GigaChat. "
                        f"Обработано {index}/{total}. Причина: {outcome.error}"
                    )
                    print(abort_message, file=sys.stderr, flush=True)
                    analyzer.log_event(
                        "batch_abort",
                        message=abort_message,
                        context={
                            "index": index,
                            "total": total,
                            "reason": "resources_exhausted",
                            "error": outcome.error,
                            "analysis_id": outcome.analysis_id,
                        },
                        source_file=source_name,
                        source_row_index=row.row_index,
                        image_url=row.image_url,
                        analysis_id=outcome.analysis_id,
                    )
                    return 2
            else:
                success_count += 1

            progress = (
                f"Прогресс: {index}/{total} | ok={success_count} | "
                f"cached={cached_count} | errors={error_count}"
            )
            print(progress, flush=True)
            analyzer.log_event(
                "batch_progress",
                message=progress,
                context={
                    "index": index,
                    "total": total,
                    "ok": success_count,
                    "cached": cached_count,
                    "errors": error_count,
                    "analysis_id": outcome.analysis_id,
                    "status": "error" if outcome.error else "success",
                },
                source_file=source_name,
                source_row_index=row.row_index,
                image_url=row.image_url,
                analysis_id=outcome.analysis_id,
            )

        done_message = (
            f"Готово: обработано {total}/{total} | ok={success_count} | "
            f"cached={cached_count} | errors={error_count}"
        )
        print(done_message, flush=True)
        analyzer.log_event(
            "batch_complete",
            message=done_message,
            context={
                "total": total,
                "ok": success_count,
                "cached": cached_count,
                "errors": error_count,
            },
            source_file=source_name,
        )
        return 0

    if args.command == "show":
        stored = analyzer.get_analysis(args.id)
        if stored is None:
            print(f"Запись id={args.id} не найдена", file=sys.stderr)
            return 1
        print(json.dumps(_stored_to_dict(stored), ensure_ascii=False, indent=2))
        return 0

    if args.command == "list":
        rows = analyzer.list_analyses(limit=args.limit)
        if not rows:
            print("Записей нет")
            return 0
        for row in rows:
            status = row.status
            review = "review" if row.manual_review_required else "ok"
            print(
                f"{row.id:>4}  [{status}]  risk={row.overall_risk_level:<8}  "
                f"{review:<6}  {row.created_at}  {row.image_url}"
            )
        return 0

    return 1


def _stored_to_dict(stored) -> dict:
    return {
        "id": stored.id,
        "image_url": stored.image_url,
        "overall_risk_level": stored.overall_risk_level,
        "manual_review_required": stored.manual_review_required,
        "model_name": stored.model_name,
        "status": stored.status,
        "error_message": stored.error_message,
        "source_file": stored.source_file,
        "source_row_index": stored.source_row_index,
        "source_payload": stored.source_payload,
        "text_on_image": stored.text_on_image,
        "confidence": stored.confidence,
        "recommendations": stored.recommendations,
        "manual_review_reason": stored.manual_review_reason,
        "disclaimer": stored.disclaimer,
        "risks": stored.risks,
        "api_usage": stored.api_usage,
        "api_response": stored.api_response,
        "output": stored.output,
        "parsed_from_truncated_json": stored.parsed_from_truncated_json,
        "created_at": stored.created_at,
        "report": stored.report,
        "raw_response": stored.raw_response,
    }


if __name__ == "__main__":
    raise SystemExit(main())
