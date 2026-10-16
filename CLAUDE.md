# CLAUDE.md

Навчальний колектор публічних постів Mastodon. Мова коментарів і документації — українська.

## Що де

- `collector/http_client.py` — єдине місце мережевих запитів: пейсинг, 429 (`Retry-After`, `X-RateLimit-Reset`), backoff на 5xx, ротація токенів (`InstanceState.pick`), проксі. `PermanentError` — не повторювати, `RetriesExhausted` — вичерпано спроби.
- `collector/sources/mastodon.py` — `MastodonSource.pages()` (min_id вперед, max_id назад) і `normalize_status()` — єдине місце, де формується канонічний запис.
- `collector/storage.py` — схема SQLite, `insert_posts()` (INSERT OR IGNORE за `uid`), курсори, `http_log`, експорт.
- `collector/runner.py` — `run()`: стрічки в ізоляції, «сторінка + курсор» в одній транзакції.
- `tests/test_collector.py` — фейковий Mastodon `FakeFeed`.

## Команди

```bash
pytest -q                               # завжди після змін
python -m collector run                 # інкрементний прогін
python -m collector backfill
python -m collector homework --min 200
python -m collector stats
```

## Правила

- Канонічний контракт не ламати: `uid, source, author, url, date, text, media` обов'язкові; нові поля — в `extra`.
- Dedup-ключ — `uid` (ActivityPub URI). Не міняти на локальний `id`.
- Курсор зсувається лише в тій самій транзакції, що й вставка сторінки.
- Не зменшувати `REQUEST_DELAY_S` нижче 0.5 і не прибирати обробку 429. Жодних обходів анти-абʼюзу.
- Токени не логувати й не писати в БД — лише хеш (`_token_tag`).
- `data/` — результати прогонів; не редагувати вручну.
