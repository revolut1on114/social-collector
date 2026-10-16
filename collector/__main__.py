"""CLI.

  python -m collector run                 інкрементний прогін (нове після курсора)
  python -m collector backfill            одноразовий добір історії (назад від найстарішого)
  python -m collector loop                run кожні LOOP_INTERVAL_S секунд (для docker compose)
  python -m collector export              data/export/posts.jsonl + posts.csv
  python -m collector dashboard           data/export/dashboard.html
  python -m collector stats               коротка статистика в консоль
  python -m collector homework --min 200  run → backfill (поки не набереться N) → export → dashboard
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from collector import dashboard
from collector.config import Target, settings
from collector.logging_setup import setup_logging
from collector.runner import run
from collector.storage import Storage

log = logging.getLogger("collector")


def _targets(args) -> list[Target] | None:
    return [Target.parse(t) for t in args.target] if args.target else None


def _export(store: Storage) -> dict:
    out = settings.data_dir / "export"
    n1 = store.export_jsonl(out / "posts.jsonl")
    n2 = store.export_csv(out / "posts.csv")
    dashboard.build(store, out / "dashboard.html")
    log.info("exported", extra={"items": n1, "path": str(out)})
    return {"jsonl": n1, "csv": n2, "dir": str(out)}


def _print(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="collector", description="Інкрементний колектор публічних постів Mastodon")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "backfill", "loop", "homework"):
        sp = sub.add_parser(name)
        sp.add_argument("--target", action="append", help="instance:tag|account|public:value (можна кілька)")
        if name == "homework":
            sp.add_argument("--min", type=int, default=200, help="мінімум записів у БД")
            sp.add_argument("--max-rounds", type=int, default=6)
    sub.add_parser("export")
    sub.add_parser("dashboard")
    sub.add_parser("stats")
    args = p.parse_args(argv)

    setup_logging()
    store = Storage(settings.db_path)
    try:
        if args.cmd == "run":
            _print(run(settings, "incremental", _targets(args), store=store))
        elif args.cmd == "backfill":
            _print(run(settings, "backfill", _targets(args), store=store))
        elif args.cmd == "loop":
            while True:
                try:
                    run(settings, "incremental", _targets(args), store=store)
                except Exception:  # noqa: BLE001 — демон не повинен помирати
                    log.exception("run crashed, will retry next cycle")
                log.info("sleeping", extra={"sleep_s": settings.loop_interval_s})
                time.sleep(settings.loop_interval_s)
        elif args.cmd == "export":
            _print(_export(store))
        elif args.cmd == "dashboard":
            _print({"dashboard": str(dashboard.build(store, settings.data_dir / "export" / "dashboard.html"))})
        elif args.cmd == "stats":
            c = store.conn
            _print({
                "total": store.count(),
                "by_target": dict(c.execute("SELECT target, COUNT(*) FROM posts GROUP BY target").fetchall()),
                "cursors": [dict(r) for r in c.execute("SELECT * FROM cursors")],
                "http_status": dict(c.execute("SELECT status, COUNT(*) FROM http_log GROUP BY status").fetchall()),
                "last_runs": [dict(r) for r in c.execute(
                    "SELECT run_id, mode, requests, new, dup, errors, rate_limited FROM runs "
                    "ORDER BY started_at DESC LIMIT 5")],
            })
        elif args.cmd == "homework":
            summary = [run(settings, "incremental", _targets(args), store=store)]
            rounds = 0
            while store.count() < args.min and rounds < args.max_rounds:
                rounds += 1
                st = run(settings, "backfill", _targets(args), store=store)
                summary.append(st)
                if st["new"] == 0:
                    break
            # другий інкрементний прогін — демонстрація ідемпотентності: нових ~0, дублів 0
            summary.append(run(settings, "incremental", _targets(args), store=store))
            res = _export(store)
            _print({"total_in_db": store.count(), "export": res,
                    "runs": [{k: s[k] for k in ("run_id", "mode", "new", "dup", "errors", "requests",
                                                "rate_limited")} for s in summary]})
            if store.count() < args.min:
                log.warning("less than requested minimum collected", extra={"items": store.count()})
                return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
