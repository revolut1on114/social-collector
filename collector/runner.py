"""Один прогін колектора: обходить усі стрічки, кожна — в ізоляції від інших.

Порядок для кожної сторінки: отримали → нормалізували → INSERT OR IGNORE → зсунули курсор,
усе в одній транзакції. Якщо процес упаде посеред прогону, наступний запуск продовжить
з останньої збереженої сторінки, а дублікати відсіє PRIMARY KEY.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from collector.config import Settings, Target
from collector.http_client import HttpClient, PermanentError, RetriesExhausted
from collector.sources.mastodon import MastodonSource, normalize_status
from collector.storage import Storage

log = logging.getLogger("collector.runner")


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _collect_target(src: MastodonSource, store: Storage, target: Target, mode: str, cfg: Settings) -> dict:
    cur = store.get_cursor(target.key)
    st = {"pages": 0, "fetched": 0, "new": 0, "dup": 0, "mode": mode}

    if mode == "backfill":
        if cur["backfill_done"]:
            log.info("backfill already complete", extra={"target": target.key})
            st["skipped"] = "backfill_done"
            return st
        if not cur["oldest_id"]:
            log.info("no cursor yet: run incremental first", extra={"target": target.key})
            st["skipped"] = "no_cursor"
            return st
        pages = src.pages(target, max_id=cur["oldest_id"], max_pages=cfg.backfill_pages)
    elif cur["newest_id"]:
        pages = src.pages(target, min_id=cur["newest_id"], max_pages=cfg.max_pages_per_target)
    else:
        # перший запуск: беремо найсвіжіше і трохи назад — це «насіння» курсора
        pages = src.pages(target, max_pages=cfg.max_pages_per_target)

    got_any = False
    for page in pages:
        got_any = True
        rows = [normalize_status(s, target) for s in page]
        ids = [s["id"] for s in page]
        newest = max(ids, key=int)
        oldest = min(ids, key=int)
        with store.tx():
            new, dup = store.insert_posts(rows, src.run_id)
            store.save_cursor(target.key, newest_id=newest, oldest_id=oldest)
        st["pages"] += 1
        st["fetched"] += len(rows)
        st["new"] += new
        st["dup"] += dup
        log.info("page saved", extra={"target": target.key, "items": len(rows), "new": new, "dup": dup,
                                      "cursor": {"newest": newest, "oldest": oldest}})

    if mode == "backfill" and not got_any:
        with store.tx():
            store.save_cursor(target.key, backfill_done=True)
        log.info("backfill reached the beginning of the feed", extra={"target": target.key})
    return st


def run(cfg: Settings, mode: str = "incremental", targets: list[Target] | None = None,
        client: HttpClient | None = None, store: Storage | None = None) -> dict:
    store = store or Storage(cfg.db_path)
    run_id = new_run_id()
    store.start_run(run_id, mode)
    client = client or HttpClient(cfg, on_response=store.log_http)
    src = MastodonSource(client, cfg, run_id)

    stats: dict = {"run_id": run_id, "mode": mode, "new": 0, "dup": 0, "errors": 0, "targets": {}}
    log.info("run started", extra={"run_id": run_id, "items": len(targets or cfg.targets)})

    for target in targets or cfg.targets:
        try:
            t = _collect_target(src, store, target, mode, cfg)
        except PermanentError as e:
            # стрічка недоступна (401/403/404/422) — фіксуємо й ідемо далі
            t = {"error": f"HTTP {e.status}", "detail": str(e)[:300]}
            stats["errors"] += 1
            log.error("target failed (permanent)", extra={"target": target.key, "status": e.status, "error": str(e)[:300]})
        except RetriesExhausted as e:
            t = {"error": "retries_exhausted", "detail": str(e)[:300]}
            stats["errors"] += 1
            log.error("target failed (retries exhausted)", extra={"target": target.key, "error": str(e)[:300]})
        except Exception as e:  # noqa: BLE001 — один збій не валить увесь прогін
            t = {"error": type(e).__name__, "detail": str(e)[:300]}
            stats["errors"] += 1
            log.exception("target failed (unexpected)", extra={"target": target.key})
        stats["targets"][target.key] = t
        stats["new"] += t.get("new", 0)
        stats["dup"] += t.get("dup", 0)

    stats["requests"] = client.stats.requests
    stats["rate_limited"] = client.stats.rate_limited
    stats["retries"] = client.stats.retries
    stats["http_status"] = client.stats.by_status
    stats["total_in_db"] = store.count()
    store.finish_run(run_id, stats)
    log.info("run finished", extra={"run_id": run_id, "new": stats["new"], "dup": stats["dup"],
                                    "items": stats["total_in_db"], "error": stats["errors"] or None})
    return stats
