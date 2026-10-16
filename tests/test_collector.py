"""Офлайн-тести: фейковий Mastodon через `responses`, без мережі й без справжніх пауз."""

from __future__ import annotations

import dataclasses
import json
import re

import pytest
import requests
import responses

from collector.config import Target, settings
from collector.http_client import HttpClient, parse_retry_after
from collector.runner import run
from collector.sources.mastodon import html_to_text, normalize_status
from collector.storage import Storage

INST = "fake.social"
TAG_URL = re.compile(rf"https://{re.escape(INST)}/api/v1/timelines/tag/osint.*")
T = Target.parse(f"{INST}:tag:osint")


def status(i: int, **kw) -> dict:
    s = {
        "id": str(1000 + i),
        "uri": f"https://{INST}/users/u{i % 5}/statuses/{1000 + i}",
        "url": f"https://{INST}/@u{i % 5}/{1000 + i}",
        "created_at": f"2026-10-{1 + i % 28:02d}T10:00:00.000Z",
        "content": f"<p>Post #{i} about <a href='#'>#osint</a></p><p>line2<br>line3</p>",
        "language": "en",
        "account": {"acct": f"u{i % 5}", "display_name": f"User {i % 5}"},
        "media_attachments": [],
        "tags": [{"name": "osint"}],
    }
    s.update(kw)
    return s


class FakeFeed:
    """Емуляція пагінації Mastodon: newest-first, min_id/max_id/limit."""

    def __init__(self, n: int):
        self.items = [status(i) for i in range(n)]
        self.calls: list[dict] = []
        self.fail_first: list[tuple[int, dict]] = []  # (status, headers) — відповісти так перед нормальною

    def add(self, k: int) -> None:
        start = len(self.items)
        self.items += [status(i) for i in range(start, start + k)]

    def __call__(self, request):
        from urllib.parse import parse_qs, urlparse

        q = {k: v[0] for k, v in parse_qs(urlparse(request.url).query).items()}
        self.calls.append(q)
        if self.fail_first:
            code, headers = self.fail_first.pop(0)
            return code, headers, json.dumps({"error": "x"})
        limit = int(q.get("limit", 20))
        items = sorted(self.items, key=lambda s: int(s["id"]), reverse=True)
        if "min_id" in q:  # одразу після min_id: беремо найближчі старші за курсор
            items = sorted([s for s in items if int(s["id"]) > int(q["min_id"])], key=lambda s: int(s["id"]))[:limit]
            items.sort(key=lambda s: int(s["id"]), reverse=True)
        else:
            if "max_id" in q:
                items = [s for s in items if int(s["id"]) < int(q["max_id"])]
            items = items[:limit]
        headers = {"X-RateLimit-Limit": "300", "X-RateLimit-Remaining": "299",
                   "X-RateLimit-Reset": "2099-01-01T00:00:00.000Z"}
        return 200, headers, json.dumps(items)


@pytest.fixture
def cfg(tmp_path):
    return dataclasses.replace(
        settings, data_dir=tmp_path, targets=[T], tokens={}, page_limit=10, max_pages_per_target=3,
        backfill_pages=10, request_delay_s=0.0, backoff_base_s=0.01, max_retries=4, proxy=None,
    )


@pytest.fixture
def store(cfg):
    s = Storage(cfg.db_path)
    yield s
    s.close()


def client(cfg, store, sleeps=None):
    return HttpClient(cfg, on_response=store.log_http, sleep=(sleeps.append if sleeps is not None else lambda s: None))


# --- нормалізація -------------------------------------------------------------

def test_html_to_text():
    assert html_to_text("<p>a &amp; b</p><p>c<br>d</p>") == "a & b\n\nc\nd"
    assert html_to_text(None) == ""


def test_normalize_canonical_fields():
    r = normalize_status(status(1, media_attachments=[{"type": "image", "url": "https://x/1.jpg",
                                                       "preview_url": "https://x/1s.jpg"}]), T)
    for f in ("uid", "author", "url", "date", "text", "media"):
        assert r[f], f
    assert r["author"] == f"@u1@{INST}"
    assert r["media"][0]["type"] == "image"
    assert r["text"].startswith("Post #1")


def test_normalize_boost_uses_original():
    orig = status(7)
    boost = status(8, reblog=orig)
    r = normalize_status(boost, T)
    assert r["uid"] == orig["uri"] and r["extra"]["is_boost"] is True


# --- інкрементність і dedup ------------------------------------------------------

@responses.activate
def test_incremental_and_idempotent(cfg, store):
    feed = FakeFeed(25)
    responses.add_callback(responses.GET, TAG_URL, callback=feed)

    s1 = run(cfg, store=store, client=client(cfg, store))
    assert s1["new"] == 25 and s1["errors"] == 0
    assert store.get_cursor(T.key)["newest_id"] == "1024"

    # повторний запуск без нових постів: нічого не додано, запит пішов з min_id
    s2 = run(cfg, store=store, client=client(cfg, store))
    assert s2["new"] == 0 and store.count() == 25
    assert feed.calls[-1]["min_id"] == "1024"

    # з'явилось 7 нових — забираємо рівно їх
    feed.add(7)
    s3 = run(cfg, store=store, client=client(cfg, store))
    assert s3["new"] == 7 and s3["dup"] == 0 and store.count() == 32
    assert store.get_cursor(T.key)["newest_id"] == "1031"


@responses.activate
def test_cursor_moves_gradually_in_small_batches(cfg, store):
    feed = FakeFeed(5)
    responses.add_callback(responses.GET, TAG_URL, callback=feed)
    run(cfg, store=store, client=client(cfg, store))
    feed.add(50)  # більше, ніж 3 сторінки × 10
    s = run(cfg, store=store, client=client(cfg, store))
    assert s["new"] == 30  # партія обмежена, курсор посунувся на 30
    s = run(cfg, store=store, client=client(cfg, store))
    assert s["new"] == 20 and store.count() == 55


@responses.activate
def test_backfill_until_done(cfg, store):
    feed = FakeFeed(65)
    responses.add_callback(responses.GET, TAG_URL, callback=feed)
    cfg = dataclasses.replace(cfg, max_pages_per_target=1)
    run(cfg, store=store, client=client(cfg, store))
    assert store.count() == 10
    run(cfg, "backfill", store=store, client=client(cfg, store))
    assert store.count() == 65
    assert store.get_cursor(T.key)["backfill_done"] == 0  # дійшли до кінця по len<limit
    s = run(cfg, "backfill", store=store, client=client(cfg, store))
    assert s["new"] == 0
    assert store.get_cursor(T.key)["backfill_done"] == 1


def test_dedup_by_uid_across_targets(cfg, store):
    t2 = Target.parse(f"{INST}:tag:ukraine")
    rows = [normalize_status(status(1), T), normalize_status(status(1), t2)]
    with store.tx():
        new, dup = store.insert_posts(rows, "r1")
    assert (new, dup) == (1, 1) and store.count() == 1


# --- ліміти й помилки ---------------------------------------------------------------

def test_parse_retry_after():
    assert parse_retry_after("30", now=0) == 30
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412480 - 10) == pytest.approx(10, abs=1)
    assert parse_retry_after(None, now=0) is None


@responses.activate
def test_429_respects_retry_after(cfg, store):
    feed = FakeFeed(5)
    feed.fail_first = [(429, {"Retry-After": "7", "X-RateLimit-Remaining": "0"})]
    responses.add_callback(responses.GET, TAG_URL, callback=feed)
    sleeps: list[float] = []
    c = client(cfg, store, sleeps)
    s = run(cfg, store=store, client=c)
    assert s["new"] == 5 and s["rate_limited"] == 1
    assert any(6.9 <= x <= 8.1 for x in sleeps), sleeps  # чекали Retry-After (+1с запасу), а не вгадували
    codes = [r[0] for r in store.conn.execute("SELECT status FROM http_log")]
    assert 429 in codes and 200 in codes


@responses.activate
def test_5xx_backoff_then_success(cfg, store):
    feed = FakeFeed(3)
    feed.fail_first = [(503, {}), (502, {})]
    responses.add_callback(responses.GET, TAG_URL, callback=feed)
    sleeps: list[float] = []
    s = run(cfg, store=store, client=client(cfg, store, sleeps))
    assert s["new"] == 3 and len([x for x in sleeps if x > 0]) >= 2


@responses.activate
def test_one_failing_target_does_not_break_run(cfg, store):
    feed = FakeFeed(4)
    responses.add_callback(responses.GET, TAG_URL, callback=feed)
    responses.add(responses.GET, re.compile(rf"https://{re.escape(INST)}/api/v1/timelines/tag/broken.*"),
                  status=404, json={"error": "Record not found"})
    responses.add(responses.GET, re.compile(r"https://down\.example/.*"), body=requests.ConnectionError("boom"))
    bad = Target.parse(f"{INST}:tag:broken")
    down = Target.parse("down.example:tag:x")
    cfg2 = dataclasses.replace(cfg, targets=[bad, down, T], max_retries=2)
    s = run(cfg2, store=store, client=client(cfg2, store))
    assert s["errors"] == 2
    assert s["targets"][T.key]["new"] == 4
    assert s["targets"][bad.key]["error"] == "HTTP 404"
    assert s["targets"][down.key]["error"] == "retries_exhausted"


@responses.activate
def test_token_rotation_on_429(cfg, store):
    cfg = dataclasses.replace(cfg, tokens={INST: ["tokA", "tokB"]})
    seen: list[str] = []

    def cb(request):
        auth = request.headers.get("Authorization", "")
        seen.append(auth)
        if auth == "Bearer tokA":
            return 429, {"Retry-After": "120"}, "{}"
        return 200, {"X-RateLimit-Remaining": "250"}, json.dumps([status(1)])

    responses.add_callback(responses.GET, TAG_URL, callback=cb)
    sleeps: list[float] = []
    s = run(cfg, store=store, client=client(cfg, store, sleeps))
    assert s["new"] == 1
    assert "Bearer tokB" in seen
    assert not any(x > 60 for x in sleeps), "з другим токеном не треба чекати 120 с"
    # токени не потрапляють у БД — лише хеш
    assert all(t.startswith("tok-") for (t,) in store.conn.execute("SELECT token FROM http_log"))


def test_exports(cfg, store, tmp_path):
    with store.tx():
        store.insert_posts([normalize_status(status(i), T) for i in range(3)], "r")
    assert store.export_jsonl(tmp_path / "p.jsonl") == 3
    first = json.loads((tmp_path / "p.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert {"author", "url", "date", "text", "media"} <= first.keys()
    assert store.export_csv(tmp_path / "p.csv") == 3
