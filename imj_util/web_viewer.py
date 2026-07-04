from __future__ import annotations

import html
import json
import mimetypes
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from imj_util.config import settings
from imj_util.viewer_store import (
    AnalysisListItem,
    ReadOnlyAnalysisStore,
    VALID_RISK_LEVELS,
    escape_html,
    image_proxy_url,
)

from imj_util.gigachat_client import RISK_LEVELS, is_remote_image_url, resolve_local_image_path

PAGE_SIZE = 50

RISK_LEVEL_LABELS = {
    "none": "none (нет)",
    "low": "low (низкий)",
    "medium": "medium (средний)",
    "high": "high (высокий)",
    "critical": "critical (критический)",
}


def run_viewer(host: str = "127.0.0.1", port: int = 8765) -> None:
    store = ReadOnlyAnalysisStore(settings.database_path)

    class ViewerHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                query = urllib.parse.parse_qs(parsed.query)

                if path == "/":
                    self._render_index(query)
                elif path.startswith("/analysis/"):
                    analysis_id = int(path.split("/")[-1])
                    self._render_detail(analysis_id)
                elif path == "/events":
                    self._render_events()
                elif path == "/image":
                    self._proxy_image(query.get("url", [""])[0])
                else:
                    self._send_html("Страница не найдена", "<p>404</p>", status=404)
            except FileNotFoundError as exc:
                self._send_html("База не найдена", f"<p>{escape_html(exc)}</p>", status=503)
            except Exception as exc:
                self._send_html("Ошибка", f"<pre>{escape_html(exc)}</pre>", status=500)

        def log_message(self, format: str, *args) -> None:
            return

        def _render_index(self, query: dict[str, list[str]]) -> None:
            page = max(1, int(query.get("page", ["1"])[0]))
            status = query.get("status", [""])[0] or None
            if status not in {None, "success", "error"}:
                status = None

            selected_risks = _parse_risk_filter(query)
            risk_levels = selected_risks if selected_risks is not None else None

            total = store.count_analyses(status=status, risk_levels=risk_levels)
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(page, pages)
            offset = (page - 1) * PAGE_SIZE
            rows = store.list_analyses(
                offset=offset,
                limit=PAGE_SIZE,
                status=status,
                risk_levels=risk_levels,
            )
            stats = store.get_stats()

            filters = [
                ("Все", ""),
                ("Успех", "success"),
                ("Ошибки", "error"),
            ]
            filter_links = " ".join(
                f'<a class="pill{" active" if (code or None) == status else ""}" '
                f'href="{escape_html(_index_url(status=code, risks=selected_risks))}">{label}</a>'
                for label, code in filters
            )

            risk_summary = ", ".join(
                f"{level}: {count}" for level, count in sorted(stats.by_risk.items())
            ) or "нет данных"

            risk_filter_html = _render_risk_filter_form(status=status, selected_risks=selected_risks)
            pager_prev = (
                f'<a href="{escape_html(_index_url(page=page - 1, status=status, risks=selected_risks))}">← Назад</a>'
                if page > 1
                else ""
            )
            pager_next = (
                f'<a href="{escape_html(_index_url(page=page + 1, status=status, risks=selected_risks))}">Вперёд →</a>'
                if page < pages
                else ""
            )

            body = f"""
            <p class="muted">Список обновляется каждые 15 сек. Фильтры сохраняются при обновлении.</p>
            <section class="stats">
              <div><strong>Всего:</strong> {stats.total}</div>
              <div><strong>Успех:</strong> {stats.success}</div>
              <div><strong>Ошибки:</strong> {stats.errors}</div>
              <div><strong>Ручная проверка:</strong> {stats.manual_review}</div>
              <div><strong>Риски:</strong> {escape_html(risk_summary)}</div>
            </section>
            <div class="filters">{filter_links} <a class="pill" href="/events">Журнал событий</a></div>
            {risk_filter_html}
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Статус</th>
                  <th>Риск</th>
                  <th>Review</th>
                  <th>Обрез.</th>
                  <th>Уверен.</th>
                  <th>Строка</th>
                  <th>Создано</th>
                  <th>Изображение</th>
                </tr>
              </thead>
              <tbody>
                {_render_rows(rows)}
              </tbody>
            </table>
            <div class="pager">
              {pager_prev}
              <span>Стр. {page} / {pages}</span>
              {pager_next}
            </div>
            """
            hint = (
                '<p class="hint">Подсказка: двойной щелчок по <strong>ID</strong> в таблице '
                "открывает детальный просмотр записи.</p>"
            )
            self._send_html(
                "IMJ_UTIL — анализы",
                body,
                header_hint=hint,
                extra_script=_index_page_script(),
            )

        def _render_detail(self, analysis_id: int) -> None:
            item = store.get_analysis(analysis_id)
            if item is None:
                self._send_html("Не найдено", "<p>Запись не найдена</p>", status=404)
                return

            display = _build_display_payload(item)
            report_json = html.escape(json.dumps(item.report, ensure_ascii=False, indent=2))
            payload_json = html.escape(
                json.dumps(item.source_payload, ensure_ascii=False, indent=2)
            )
            output_json = html.escape(json.dumps(display, ensure_ascii=False, indent=2))
            api_usage_json = html.escape(
                json.dumps(item.api_usage or display.get("api_usage") or {}, ensure_ascii=False, indent=2)
            )
            api_response_json = html.escape(
                json.dumps(item.api_response or display.get("api_response") or {}, ensure_ascii=False, indent=2)
            )
            raw_response = html.escape(item.raw_response or display.get("raw_response") or "")
            risks_html = _render_risks_table(item.risks or item.report.get("risks", []))
            recommendations_html = _render_recommendations(item.recommendations or item.report.get("recommendations", []))

            body = f"""
            <p><a href="/" class="viewer-back-index">← К списку</a> · <a href="/events">Журнал событий</a></p>
            <section class="detail">
              <h2>Анализ #{item.id}</h2>
              <p><strong>Статус:</strong> {escape_html(item.status)}</p>
              <p><strong>Риск:</strong> {escape_html(item.overall_risk_level)}</p>
              <p><strong>Ручная проверка:</strong> {'да' if item.manual_review_required else 'нет'}</p>
              <p><strong>Причина ручной проверки:</strong> {escape_html(item.manual_review_reason or item.report.get('manual_review_reason') or '—')}</p>
              <p><strong>Уверенность:</strong> {escape_html(item.confidence if item.confidence is not None else item.report.get('confidence', '—'))}</p>
              <p><strong>Модель:</strong> {escape_html(item.model_name)}</p>
              <p><strong>Обрезанный JSON:</strong> {'да' if item.parsed_from_truncated_json else 'нет'}</p>
              <p><strong>Файл Excel:</strong> {escape_html(item.source_file)}</p>
              <p><strong>Строка Excel:</strong> {escape_html(item.source_row_index)}</p>
              <p><strong>Создано:</strong> {escape_html(item.created_at)}</p>
              <p><strong>URL:</strong> <a href="{escape_html(item.image_url)}" target="_blank">{escape_html(item.image_url)}</a></p>
              <p><strong>Дисклеймер:</strong> {escape_html(item.disclaimer or item.report.get('disclaimer') or '—')}</p>
              {f'<p><strong>Текст на изображении:</strong> {escape_html(item.text_on_image or item.report.get("text_on_image"))}</p>' if (item.text_on_image or item.report.get("text_on_image")) else ''}
              {f'<p><strong>Ошибка:</strong> {escape_html(item.error_message)}</p>' if item.error_message else ''}
            </section>
            <section class="detail">
              <h3>Превью</h3>
              <img src="{image_proxy_url(item.image_url)}" alt="preview" class="preview">
            </section>
            <section class="detail">
              <h3>Риски по категориям</h3>
              {risks_html}
            </section>
            <section class="detail">
              <h3>Рекомендации</h3>
              {recommendations_html}
            </section>
            <section class="detail">
              <h3>Полный выход утилиты (output_json)</h3>
              <pre>{output_json}</pre>
            </section>
            <section class="detail">
              <h3>Использование API (api_usage_json)</h3>
              <pre>{api_usage_json}</pre>
            </section>
            <section class="detail">
              <h3>Ответ API (api_response_json)</h3>
              <pre>{api_response_json}</pre>
            </section>
            <section class="detail">
              <h3>Сырой ответ модели (raw_response)</h3>
              <pre>{raw_response or '—'}</pre>
            </section>
            <section class="detail">
              <h3>Отчёт JSON (report_json)</h3>
              <pre>{report_json}</pre>
            </section>
            <section class="detail">
              <h3>Исходные поля Excel (source_payload_json)</h3>
              <pre>{payload_json}</pre>
            </section>
            """
            self._send_html(f"Анализ #{item.id}", body, extra_script=_restore_index_links_script())

        def _render_events(self) -> None:
            events = store.list_events(limit=200)
            rows = []
            for event in events:
                context_json = html.escape(
                    json.dumps(event.context or {}, ensure_ascii=False, indent=2)
                )
                rows.append(
                    f"<tr><td>{event.id}</td><td>{escape_html(event.event_type)}</td>"
                    f"<td>{escape_html(event.created_at)}</td>"
                    f"<td>{escape_html(event.message)}</td>"
                    f"<td>{escape_html(event.analysis_id)}</td>"
                    f"<td class='muted'>{escape_html(event.image_url)}</td></tr>"
                    f"<tr><td colspan='6'><details><summary>context_json</summary>"
                    f"<pre>{context_json}</pre></details></td></tr>"
                )
            body = f"""
            <p><a href="/" class="viewer-back-index">← К списку анализов</a></p>
            <section class="detail">
              <h2>Журнал событий утилиты</h2>
              <table>
                <thead><tr><th>ID</th><th>Тип</th><th>Время</th><th>Сообщение</th><th>analysis_id</th><th>URL</th></tr></thead>
                <tbody>{''.join(rows) if rows else '<tr><td colspan="6">Событий нет</td></tr>'}</tbody>
              </table>
            </section>
            """
            self._send_html("Журнал событий", body, extra_script=_restore_index_links_script())

        def _proxy_image(self, image_url: str) -> None:
            if not image_url:
                self.send_error(400)
                return
            if is_remote_image_url(image_url):
                request = urllib.request.Request(
                    image_url,
                    headers={"User-Agent": "IMJ_UTIL-Viewer/1.0"},
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    content = response.read()
                    content_type = response.headers.get("Content-Type", "image/jpeg")
            else:
                path = resolve_local_image_path(image_url)
                content = path.read_bytes()
                content_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _send_html(
            self,
            title: str,
            body: str,
            status: int = 200,
            header_hint: str = "",
            extra_script: str = "",
        ) -> None:
            page = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>{escape_html(title)}</title>
  <style>
    body {{ font-family: Segoe UI, sans-serif; margin: 24px; background: #f7f7f8; color: #111; }}
    h1, h2, h3 {{ margin-top: 0; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; }}
    th, td {{ border-bottom: 1px solid #e5e5e5; padding: 8px 10px; text-align: left; vertical-align: top; }}
    th {{ background: #efefef; }}
    .stats, .detail, .filters, .pager {{ margin: 16px 0; }}
    .stats {{ display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }}
    .stats div, .detail {{ background: #fff; padding: 12px 14px; border-radius: 8px; }}
    .pill {{ display: inline-block; margin-right: 8px; padding: 6px 10px; background: #fff; border-radius: 999px; text-decoration: none; color: #111; border: 1px solid #ddd; }}
    .pill.active {{ background: #1f6feb; color: #fff; border-color: #1f6feb; }}
    .preview {{ max-width: 480px; max-height: 480px; border: 1px solid #ddd; border-radius: 8px; }}
    pre {{ white-space: pre-wrap; word-break: break-word; background: #111; color: #f5f5f5; padding: 12px; border-radius: 8px; }}
    a {{ color: #1f6feb; }}
    .muted {{ color: #666; font-size: 12px; }}
    .hint {{ margin: 0 0 12px; padding: 10px 12px; background: #fff8e6; border: 1px solid #f0d78c; border-radius: 8px; font-size: 14px; }}
    .risk-filter {{ background: #fff; padding: 12px 14px; border-radius: 8px; margin: 16px 0; border: 1px solid #e5e5e5; }}
    .risk-filter legend {{ font-weight: 600; padding: 0 4px; }}
    .risk-filter-options {{ display: flex; flex-wrap: wrap; gap: 10px 16px; margin: 10px 0; }}
    .risk-filter-options label {{ display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }}
    .risk-filter-actions {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
    .risk-filter button {{ padding: 6px 12px; border-radius: 6px; border: 1px solid #1f6feb; background: #1f6feb; color: #fff; cursor: pointer; }}
    .risk-filter .pill {{ margin-right: 0; }}
    tr.analysis-row {{ cursor: pointer; }}
    tr.analysis-row:hover {{ background: #f3f8ff; }}
    td.analysis-id {{ font-weight: 600; }}
  </style>
</head>
<body>
  <h1>IMJ_UTIL Viewer</h1>
  <p class="muted">Только чтение.</p>
  {header_hint}
  {body}
  {extra_script}
</body>
</html>"""
            encoded = page.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer((host, port), ViewerHandler)
    print(f"Viewer: http://{host}:{port}  (read-only, DB: {settings.database_path})")
    print("Остановка: Ctrl+C")
    server.serve_forever()


def _parse_risk_filter(query: dict[str, list[str]]) -> list[str] | None:
    """None = все уровни; иначе список выбранных overall_risk_level."""
    raw_values = query.get("risks", [])
    if not raw_values:
        return None

    selected: list[str] = []
    for value in raw_values:
        for part in value.split(","):
            level = part.strip().lower()
            if level in VALID_RISK_LEVELS and level not in selected:
                selected.append(level)

    if not selected or set(selected) == VALID_RISK_LEVELS:
        return None

    return selected


def _index_url(
    *,
    page: int | None = None,
    status: str | None = None,
    risks: list[str] | None = None,
) -> str:
    params: list[tuple[str, str]] = []
    if page and page > 1:
        params.append(("page", str(page)))
    if status:
        params.append(("status", status))
    if risks:
        for risk in risks:
            params.append(("risks", risk))
    if not params:
        return "/"
    return "/?" + urllib.parse.urlencode(params)


def _render_risk_filter_form(
    *,
    status: str | None,
    selected_risks: list[str] | None,
) -> str:
    show_all = selected_risks is None
    status_field = (
        f'<input type="hidden" name="status" value="{escape_html(status)}">' if status else ""
    )

    checkboxes = []
    for level in RISK_LEVELS:
        checked = show_all or level in selected_risks
        label = RISK_LEVEL_LABELS.get(level, level)
        checkboxes.append(
            f'<label><input type="checkbox" name="risks" value="{level}"'
            f'{" checked" if checked else ""}> {escape_html(label)}</label>'
        )

    all_active = " active" if show_all else ""
    all_href = escape_html(_index_url(status=status, risks=None))

    return f"""
    <form class="risk-filter" method="get" action="/">
      {status_field}
      <fieldset>
        <legend>Уровень риска (независимо от фильтра статуса)</legend>
        <div class="risk-filter-options">
          {''.join(checkboxes)}
        </div>
        <div class="risk-filter-actions">
          <button type="submit">Применить</button>
          <a class="pill{all_active}" href="{all_href}">Все уровни</a>
          <span class="muted">Снимите все галочки и нажмите «Применить» — покажутся все уровни</span>
        </div>
      </fieldset>
    </form>
    """


def _render_rows(rows: list[AnalysisListItem]) -> str:
    if not rows:
        return '<tr><td colspan="9">Записей нет</td></tr>'
    parts = []
    for row in rows:
        parts.append(
            f"""
            <tr class="analysis-row">
              <td class="analysis-id"><a href="/analysis/{row.id}">{row.id}</a></td>
              <td>{escape_html(row.status)}</td>
              <td>{escape_html(row.overall_risk_level)}</td>
              <td>{'да' if row.manual_review_required else 'нет'}</td>
              <td>{'да' if row.parsed_from_truncated_json else 'нет'}</td>
              <td>{escape_html(row.confidence)}</td>
              <td>{escape_html(row.source_row_index)}</td>
              <td>{escape_html(row.created_at)}</td>
              <td class="muted">{escape_html(row.image_url)}</td>
            </tr>
            """
        )
    return "".join(parts)


def _index_page_script() -> str:
    return """<script>
(function () {
  const KEY = 'imj_util_viewer_index';
  sessionStorage.setItem(KEY, window.location.pathname + window.location.search);
  window.setInterval(function () { window.location.reload(); }, 15000);
  document.querySelectorAll('td.analysis-id').forEach(function (cell) {
    const link = cell.querySelector('a');
    if (!link) return;
    cell.addEventListener('dblclick', function () { window.location.href = link.href; });
  });
})();
</script>"""


def _restore_index_links_script() -> str:
    return """<script>
(function () {
  const saved = sessionStorage.getItem('imj_util_viewer_index');
  if (!saved) return;
  document.querySelectorAll('a.viewer-back-index').forEach(function (a) {
    a.setAttribute('href', saved);
  });
})();
</script>"""


def _build_display_payload(item) -> dict:
    if item.output:
        return item.output
    return {
        "status": item.status,
        "parsed_from_truncated_json": item.parsed_from_truncated_json,
        "image_url": item.image_url,
        "overall_risk_level": item.overall_risk_level,
        "manual_review_required": item.manual_review_required,
        "manual_review_reason": item.manual_review_reason or item.report.get("manual_review_reason"),
        "text_on_image": item.text_on_image or item.report.get("text_on_image"),
        "confidence": item.confidence if item.confidence is not None else item.report.get("confidence"),
        "recommendations": item.recommendations or item.report.get("recommendations", []),
        "disclaimer": item.disclaimer or item.report.get("disclaimer"),
        "risks": item.risks or item.report.get("risks", []),
        "report": item.report,
        "raw_response": item.raw_response,
        "api_usage": item.api_usage,
        "api_response": item.api_response,
        "source_payload": item.source_payload,
        "parsed_from_truncated_json": item.parsed_from_truncated_json,
        "note": "Собрано из legacy-полей (output_json отсутствует)",
    }


def _render_risks_table(risks: list) -> str:
    if not risks:
        return "<p>—</p>"
    rows = []
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        signs = risk.get("detected_signs") or []
        signs_text = "; ".join(str(s) for s in signs) if signs else "—"
        rows.append(
            "<tr>"
            f"<td>{escape_html(risk.get('category'))}</td>"
            f"<td>{escape_html(risk.get('risk_level'))}</td>"
            f"<td>{escape_html(signs_text)}</td>"
            f"<td>{escape_html(risk.get('description'))}</td>"
            "</tr>"
        )
    if not rows:
        return "<p>—</p>"
    return (
        "<table><thead><tr><th>Категория</th><th>Риск</th><th>Признаки</th><th>Описание</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_recommendations(recommendations: list) -> str:
    if not recommendations:
        return "<p>—</p>"
    items = "".join(f"<li>{escape_html(item)}</li>" for item in recommendations)
    return f"<ul>{items}</ul>"
