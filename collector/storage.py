"""SQLite: канонічна таблиця posts + курсори + журнал прогонів і HTTP-відповідей.

Dedup-ключ — `uid` (ActivityPub URI статусу). Він глобально унікальний: той самий пост,
побачений через різні інстанси чи під різними хештегами, має однаковий URI, тому
INSERT OR IGNORE робить повторний запуск ідемпотентним.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Канонічна модель: author · url · date · text (+ media) — обов'язкові поля з ДЗ
CANONICAL_FIELDS = ("uid", "source", "author", "url", "date", "text", "media")

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    uid           TEXT PRIMARY KEY,          -- dedup-ключ (ActivityPub URI)
    source        TEXT NOT NULL,             -- 'mastodon'
    author        TEXT NOT NULL,             -- @user@instance
    url           TEXT NOT NULL,             -- посилання на пост
    date          TEXT NOT NULL,             -- ISO-8601 UTC, коли пост створено
    text          TEXT NOT NULL,             -- очищений від HTML текст
    media         TEXT NOT NULL DEFAULT '[]',-- JSON: [{type,url,preview_url,description}]
    author_name   TEXT,
    lang          TEXT,
    target        TEXT NOT NULL,             -- стрічка, через яку пост знайдено вперше
    native_id     TEXT,                      -- id на інстансі збору
    extra         TEXT NOT NULL DEFAULT '{}',-- JSON: теги, лічильники, reply/boost
    collected_at  TEXT NOT NULL,
    run_id        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_posts_date   ON posts(date);
CREATE INDEX IF NOT EXISTS ix_posts_target ON posts(target);

CREATE TABLE IF NOT EXISTS cursors (
    target        TEXT PRIMARY KEY,
    newest_id     TEXT,                      -- для інкрементного збору (min_id)
    oldest_id     TEXT,                      -- для backfill (max_id)
    backfill_done INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    mode          TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    requests      INTEGER DEFAULT 0,
    new           INTEGER DEFAULT 0,
    dup           INTEGER DEFAULT 0,
    errors        INTEGER DEFAULT 0,
    rate_limited  INTEGER DEFAULT 0,
    details       TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS http_log (
    ts            TEXT NOT NULL,
    run_id        TEXT,
    instance      TEXT,
    status        INTEGER,
    rl_limit      INTEGER,
    rl_remaining  INTEGER,
    rl_reset      TEXT,
    retry_after   TEXT,
    duration_ms   INTEGER,
    token         TEXT                       -- лише короткий хеш, не сам токен
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- posts ---------------------------------------------------------------
    def insert_posts(self, rows: list[dict], run_id: str) -> tuple[int, int]:
        """Вставляє записи; повертає (нових, дублікатів). Курсор оновлюється в тій самій транзакції."""
        new = 0
        ts = now_iso()
        for r in rows:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO posts
                   (uid, source, author, url, date, text, media, author_name, lang, target,
                    native_id, extra, collected_at, run_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    r["uid"], r["source"], r["author"], r["url"], r["date"], r["text"],
                    json.dumps(r.get("media", []), ensure_ascii=False), r.get("author_name"),
                    r.get("lang"), r["target"], r.get("native_id"),
                    json.dumps(r.get("extra", {}), ensure_ascii=False), ts, run_id,
                ),
            )
            new += cur.rowcount
        return new, len(rows) - new

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]

    # --- cursors -------------------------------------------------------------
    def get_cursor(self, target: str) -> dict:
        row = self.conn.execute("SELECT * FROM cursors WHERE target=?", (target,)).fetchone()
        return dict(row) if row else {"target": target, "newest_id": None, "oldest_id": None, "backfill_done": 0}

    def save_cursor(self, target: str, *, newest_id=None, oldest_id=None, backfill_done=None) -> None:
        cur = self.get_cursor(target)
        newest = _max_id(cur["newest_id"], newest_id)
        oldest = _min_id(cur["oldest_id"], oldest_id)
        done = cur["backfill_done"] if backfill_done is None else int(backfill_done)
        self.conn.execute(
            """INSERT INTO cursors(target, newest_id, oldest_id, backfill_done, updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(target) DO UPDATE SET newest_id=excluded.newest_id,
                 oldest_id=excluded.oldest_id, backfill_done=excluded.backfill_done,
                 updated_at=excluded.updated_at""",
            (target, newest, oldest, done, now_iso()),
        )

    # --- runs / http_log -----------------------------------------------------
    def start_run(self, run_id: str, mode: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO runs(run_id, mode, started_at) VALUES(?,?,?)", (run_id, mode, now_iso()))

    def finish_run(self, run_id: str, stats: dict) -> None:
        with self.tx() as c:
            c.execute(
                """UPDATE runs SET finished_at=?, requests=?, new=?, dup=?, errors=?, rate_limited=?, details=?
                   WHERE run_id=?""",
                (now_iso(), stats.get("requests", 0), stats.get("new", 0), stats.get("dup", 0),
                 stats.get("errors", 0), stats.get("rate_limited", 0),
                 json.dumps(stats.get("targets", {}), ensure_ascii=False), run_id),
            )

    def log_http(self, **kw) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO http_log(ts, run_id, instance, status, rl_limit, rl_remaining, rl_reset,
                   retry_after, duration_ms, token) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (now_iso(), kw.get("run_id"), kw.get("instance"), kw.get("status"), kw.get("rl_limit"),
                 kw.get("rl_remaining"), kw.get("rl_reset"), kw.get("retry_after"), kw.get("duration_ms"),
                 kw.get("token")),
            )

    # --- export --------------------------------------------------------------
    def iter_posts(self) -> Iterator[dict]:
        for row in self.conn.execute("SELECT * FROM posts ORDER BY date DESC"):
            d = dict(row)
            d["media"] = json.loads(d["media"])
            d["extra"] = json.loads(d["extra"])
            yield d

    def export_jsonl(self, path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for d in self.iter_posts():
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
                n += 1
        return n

    def export_csv(self, path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        cols = ["uid", "source", "author", "author_name", "url", "date", "text", "media", "lang", "target",
                "collected_at"]
        n = 0
        # utf-8-sig — щоб Excel на Windows одразу відкрив кирилицю
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for d in self.iter_posts():
                d["media"] = json.dumps(d["media"], ensure_ascii=False)
                w.writerow(d)
                n += 1
        return n


def _as_int(x) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _max_id(a, b):
    """Mastodon id — рядок із числом (snowflake). Порівнюємо як числа."""
    if a is None:
        return b
    if b is None:
        return a
    return a if (_as_int(a) or 0) >= (_as_int(b) or 0) else b


def _min_id(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a if (_as_int(a) or 0) <= (_as_int(b) or 0) else b
