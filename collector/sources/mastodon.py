"""Mastodon: публічні стрічки через офіційний REST API (без скрейпінгу HTML).

Пагінація Mastodon (id статусів — числові snowflake-рядки, ростуть з часом):
  без параметрів   — найновіша сторінка;
  min_id=X         — статуси одразу ПІСЛЯ X (рухаємось уперед: інкрементний збір);
  max_id=X         — статуси ДО X (рухаємось назад: backfill історії).
"""

from __future__ import annotations

import html
import logging
import re
from html.parser import HTMLParser
from typing import Iterator

from collector.config import Settings, Target
from collector.http_client import HttpClient

log = logging.getLogger("collector.mastodon")

SOURCE = "mastodon"


# --- нормалізація -------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """HTML статусу → простий текст: <br> і </p> стають переносами, посилання — їхнім текстом."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")
        # Mastodon ховає «https://» і хвіст довгих URL у <span class="invisible"> — для тексту вони потрібні,
        # тому нічого не пропускаємо; але <script>/<style> на всяк випадок ігноруємо.
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "p":
            self.parts.append("\n\n")
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(content: str | None) -> str:
    if not content:
        return ""
    p = _TextExtractor()
    p.feed(content)
    p.close()
    text = html.unescape("".join(p.parts))
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_status(status: dict, target: Target) -> dict:
    """Статус Mastodon → канонічний запис (author · url · date · text · media)."""
    s = status.get("reblog") or status  # для бустів зберігаємо оригінал
    acct = (s.get("account") or {}).get("acct", "unknown")
    if "@" not in acct:  # локальний акаунт інстансу збору
        acct = f"{acct}@{target.instance}"
    text = html_to_text(s.get("content"))
    if s.get("spoiler_text"):
        text = f"[CW: {s['spoiler_text']}]\n{text}"
    media = [
        {
            "type": m.get("type"),
            "url": m.get("remote_url") or m.get("url"),
            "preview_url": m.get("preview_url"),
            "description": m.get("description"),
        }
        for m in s.get("media_attachments") or []
    ]
    card = s.get("card") or {}
    return {
        "uid": s.get("uri") or s.get("url") or f"{target.instance}/{s['id']}",
        "source": SOURCE,
        "author": "@" + acct,
        "author_name": (s.get("account") or {}).get("display_name") or None,
        "url": s.get("url") or s.get("uri"),
        "date": s.get("created_at"),
        "text": text,
        "media": media,
        "lang": s.get("language"),
        "target": target.key,
        "native_id": status.get("id"),
        "extra": {
            "instance": target.instance,
            "tags": [t.get("name") for t in s.get("tags") or []],
            "replies": s.get("replies_count"),
            "reblogs": s.get("reblogs_count"),
            "favourites": s.get("favourites_count"),
            "in_reply_to_id": s.get("in_reply_to_id"),
            "is_boost": bool(status.get("reblog")),
            "sensitive": s.get("sensitive"),
            "card_url": card.get("url"),
        },
    }


# --- запити ---------------------------------------------------------------------

class MastodonSource:
    def __init__(self, client: HttpClient, cfg: Settings, run_id: str):
        self.client = client
        self.cfg = cfg
        self.run_id = run_id
        self._account_ids: dict[str, str] = {}

    def _endpoint(self, target: Target) -> tuple[str, dict]:
        if target.kind == "tag":
            return f"/api/v1/timelines/tag/{target.value}", {}
        if target.kind == "public":
            return "/api/v1/timelines/public", {"local": "true"} if target.value == "local" else {}
        # account
        if target.key not in self._account_ids:
            acc, _ = self.client.get_json(target.instance, "/api/v1/accounts/lookup",
                                          {"acct": target.value}, run_id=self.run_id)
            self._account_ids[target.key] = acc["id"]
        return f"/api/v1/accounts/{self._account_ids[target.key]}/statuses", {"exclude_reblogs": "true"}

    def pages(self, target: Target, *, min_id: str | None = None, max_id: str | None = None,
              max_pages: int) -> Iterator[list[dict]]:
        """Генерує сторінки статусів. Напрям: min_id → вперед, max_id → назад, нічого → найновіше й назад."""
        path, base = self._endpoint(target)
        forward = min_id is not None
        cursor_min, cursor_max = min_id, max_id
        for _ in range(max_pages):
            params = dict(base, limit=self.cfg.page_limit)
            if cursor_min:
                params["min_id"] = cursor_min
            if cursor_max:
                params["max_id"] = cursor_max
            data, _headers = self.client.get_json(target.instance, path, params, run_id=self.run_id)
            if not data:
                return
            yield data
            ids = [int(s["id"]) for s in data]
            if forward:
                cursor_min = str(max(ids))
            else:
                cursor_max = str(min(ids))
            if len(data) < self.cfg.page_limit:
                return
