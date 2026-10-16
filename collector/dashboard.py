"""Бонус: статичний HTML-дашборд зі статистикою збору (без серверів і зовнішніх бібліотек)."""

from __future__ import annotations

import html
from pathlib import Path

from collector.storage import Storage, now_iso


def _bars(rows: list[tuple[str, int]], width: int = 560, bar_h: int = 18) -> str:
    if not rows:
        return "<p class='muted'>немає даних</p>"
    mx = max(v for _, v in rows) or 1
    label_w = 230
    h = len(rows) * (bar_h + 6) + 4
    out = [f"<svg viewBox='0 0 {width} {h}' width='100%' role='img'>"]
    for i, (label, v) in enumerate(rows):
        y = i * (bar_h + 6) + 2
        w = max(2, int((width - label_w - 60) * v / mx))
        out.append(
            f"<text x='{label_w - 8}' y='{y + bar_h - 5}' text-anchor='end' class='lbl'>{html.escape(label[:34])}</text>"
            f"<rect x='{label_w}' y='{y}' width='{w}' height='{bar_h}' rx='3' class='bar'/>"
            f"<text x='{label_w + w + 6}' y='{y + bar_h - 5}' class='val'>{v}</text>"
        )
    out.append("</svg>")
    return "".join(out)


def build(store: Storage, out: Path) -> Path:
    c = store.conn
    total = store.count()
    authors = c.execute("SELECT COUNT(DISTINCT author) FROM posts").fetchone()[0]
    with_media = c.execute("SELECT COUNT(*) FROM posts WHERE media != '[]'").fetchone()[0]
    span = c.execute("SELECT MIN(date), MAX(date) FROM posts").fetchone()
    by_target = [(r[0], r[1]) for r in c.execute(
        "SELECT target, COUNT(*) FROM posts GROUP BY target ORDER BY 2 DESC")]
    by_day = [(r[0], r[1]) for r in c.execute(
        "SELECT substr(date,1,10) d, COUNT(*) FROM posts GROUP BY d ORDER BY d DESC LIMIT 14")]
    by_lang = [(r[0] or "?", r[1]) for r in c.execute(
        "SELECT lang, COUNT(*) FROM posts GROUP BY lang ORDER BY 2 DESC LIMIT 8")]
    by_status = [(str(r[0]), r[1]) for r in c.execute(
        "SELECT status, COUNT(*) FROM http_log GROUP BY status ORDER BY 2 DESC")]
    rl = c.execute(
        "SELECT instance, MAX(rl_limit), MIN(rl_remaining), COUNT(*) FROM http_log GROUP BY instance").fetchall()
    runs = c.execute(
        "SELECT run_id, mode, started_at, finished_at, requests, new, dup, errors, rate_limited "
        "FROM runs ORDER BY started_at DESC LIMIT 20").fetchall()
    cursors = c.execute("SELECT target, newest_id, oldest_id, backfill_done, updated_at FROM cursors").fetchall()

    def table(head: list[str], rows) -> str:
        th = "".join(f"<th>{html.escape(h)}</th>" for h in head)
        trs = "".join("<tr>" + "".join(f"<td>{html.escape(str(v if v is not None else '—'))}</td>" for v in r)
                      + "</tr>" for r in rows)
        return f"<div class='tw'><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>"

    kpis = [("записів у БД", total), ("унікальних авторів", authors), ("з медіа", with_media),
            ("прогонів", len(runs)), ("HTTP 429 за весь час", sum(v for k, v in by_status if k == "429"))]
    kpi_html = "".join(f"<div class='kpi'><b>{v}</b><span>{html.escape(k)}</span></div>" for k, v in kpis)

    page = f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Collector stats</title>
<style>
:root{{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e3e3de;--accent:#3b6fd8}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecece8;--muted:#9a9a94;--card:#1f1f1d;--line:#33332f;--accent:#7aa2f7}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif}}
main{{max-width:980px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 10px}}
.muted{{color:var(--muted)}} .kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}}
.kpi b{{display:block;font-size:24px}} .kpi span{{color:var(--muted);font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
.bar{{fill:var(--accent)}} .lbl,.val{{fill:var(--fg);font-size:12px}}
.tw{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}}
</style></head><body><main>
<h1>Статистика збору · Mastodon</h1>
<p class="muted">Згенеровано {now_iso()} · дані з {html.escape(str(span[0]))} по {html.escape(str(span[1]))}</p>
<div class="kpis">{kpi_html}</div>
<div class="grid">
<div class="card"><h2>Записи за стрічками</h2>{_bars(by_target)}</div>
<div class="card"><h2>Записи за днями (дата поста)</h2>{_bars(by_day)}</div>
<div class="card"><h2>Мови</h2>{_bars(by_lang)}</div>
<div class="card"><h2>HTTP-коди відповідей</h2>{_bars(by_status)}</div>
</div>
<h2>Ліміти, які бачили (X-RateLimit-*)</h2>
{table(["інстанс", "Limit", "мін. Remaining", "запитів"], rl)}
<h2>Курсори</h2>
{table(["стрічка", "newest_id", "oldest_id", "backfill завершено", "оновлено"], cursors)}
<h2>Останні прогони</h2>
{table(["run_id", "режим", "старт", "фініш", "запитів", "нових", "дублів", "помилок", "429"], runs)}
</main></body></html>"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out
