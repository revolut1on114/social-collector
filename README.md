# social-collector

Стійкий **інкрементний** колектор публічних постів **Mastodon** через офіційний REST API. Складає все в одну канонічну таблицю SQLite (`author · url · date · text · media`), поважає ліміти інстансів і переживає збої окремих стрічок.

| Вимога ДЗ | Як зроблено |
|---|---|
| Інкрементний збір | курсор `newest_id` на кожну стрічку, запити з `min_id` — тягнемо лише нове; партія обмежена `MAX_PAGES_PER_TARGET` |
| Канонічна модель | SQLite `data/posts.sqlite`, таблиця `posts` + експорт `data/export/posts.jsonl` і `posts.csv` |
| Повага до лімітів | пауза між запитами, адаптивне гальмування за `X-RateLimit-Remaining`, на 429 — очікування `Retry-After` / `X-RateLimit-Reset`, на 5xx — експоненційний backoff з jitter |
| Стійкість і dedup | кожна стрічка в `try/except`; `INSERT OR IGNORE` за `uid` (ActivityPub URI); сторінка й курсор — в одній транзакції |
| Конфіг і запуск | `.env` (поза git), `requirements.txt`, `Dockerfile`, `compose.yaml` |
| **Бонуси** | ротація кількох токенів · проксі-вихід · HTML-дашборд · backfill історії · Docker Compose |

Чому Mastodon: офіційний відкритий API без ключів для публічних стрічок, чесні заголовки `X-RateLimit-*`, курсорна пагінація за id. Збір лише публічних постів, які автори самі опублікували для всіх.

Звіт про джерело й зустрінуті ліміти — [docs/ЗВІТ.md](docs/ЗВІТ.md).

**Результат прогонів 16.10.2026:** 1731 унікальний запис від 134 авторів (3 стрічки, 2 інстанси), 53 запити, 0 відповідей 429, 70 дублікатів відсіяно. Дані лежать у `data/posts.sqlite` і `data/export/posts.jsonl` / `posts.csv`, статистика — у `data/export/dashboard.html`.

---

## Швидкий старт (Windows)

Подвійним кліком:

| Скрипт | Що робить |
|---|---|
| `scripts\setup.bat` | `.venv`, залежності, `.env` з `.env.example` |
| `scripts\run.bat` | інкрементний прогін + експорт |
| `scripts\backfill.bat` | одноразовий добір історії |
| `scripts\homework.bat` | тести → збір 200+ → експорт → відкриває дашборд |
| `scripts\test.bat` | офлайн-тести |
| `scripts\finish-homework.bat` | тести → збір 200+ → дашборд → git commit/push |

## Запуск вручну

```bash
python -m venv .venv && .venv\Scripts\activate      # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
copy .env.example .env                              # cp на Linux

python -m collector run                 # інкрементний прогін
python -m collector backfill            # добір історії назад
python -m collector homework --min 200  # run → backfill до 200+ → повторний run → export
python -m collector export              # data/export/posts.jsonl, posts.csv, dashboard.html
python -m collector stats               # курсори, коди відповідей, останні прогони
python -m collector run --target mastodon.social:tag:osint   # лише одна стрічка
```

## Docker

```bash
docker compose run --rm collector                                   # один прогін
docker compose run --rm collector python -m collector homework --min 200
docker compose --profile loop up -d                                 # збір кожні 15 хв
docker compose --profile dashboard up -d                            # http://localhost:8080/dashboard.html
docker compose run --rm collector pytest -q
```

`data/` монтується як том, тож курсори й БД переживають перезапуск контейнера.

---

## Канонічна модель

Таблиця `posts` (SQLite). Обов'язкові поля з ДЗ — `author`, `url`, `date`, `text`, плюс `media`.

| Поле | Приклад | Примітка |
|---|---|---|
| `uid` | `https://infosec.exchange/users/x/statuses/1133…` | **PRIMARY KEY, dedup-ключ** (ActivityPub URI) |
| `source` | `mastodon` | |
| `author` | `@bellingcat@mstdn.social` | завжди повний `@user@instance` |
| `url` | `https://mstdn.social/@bellingcat/1133…` | людське посилання |
| `date` | `2026-10-15T09:12:44.000Z` | дата публікації, UTC |
| `text` | `New investigation…` | HTML → текст, CW додається як `[CW: …]` |
| `media` | `[{"type":"image","url":…,"preview_url":…,"description":…}]` | JSON |
| `author_name`, `lang` | | |
| `target` | `mastodon.social:tag:osint` | стрічка, де пост знайдено вперше |
| `extra` | `{"tags":[…],"reblogs":3,"favourites":10,…}` | JSON з лічильниками |
| `collected_at`, `run_id` | | коли й яким прогоном зібрано |

Чому `uid` = URI, а не `id`: числовий `id` локальний для інстансу — той самий пост на `mastodon.social` і `infosec.exchange` має різні id, але однаковий URI. Тож пост із `#osint`, що потрапив і в `#ukraine`, зберігається один раз.

Службові таблиці: `cursors` (newest/oldest id на стрічку, прапорець завершення backfill), `runs` (підсумок кожного прогону), `http_log` (код відповіді та `X-RateLimit-*` кожного запиту — з неї зроблено звіт).

## Як працює інкрементність

```
перший запуск   GET …/tag/osint?limit=40                 → найсвіжіше + до 5 сторінок назад (max_id)
наступні        GET …/tag/osint?limit=40&min_id=<newest> → лише те, що з'явилося після курсора
backfill        GET …/tag/osint?limit=40&max_id=<oldest> → назад в історію, поки стрічка не скінчиться
```

Після кожної сторінки в одній транзакції: `INSERT OR IGNORE` записів + зсув курсора. Якщо процес вбити посеред прогону, наступний запуск продовжить з останньої збереженої сторінки, а дублікати не з'являться. Партія невелика (`MAX_PAGES_PER_TARGET × PAGE_LIMIT` = 200 на стрічку), тож курсор рухається поступово.

## Ліміти й помилки

| Ситуація | Реакція |
|---|---|
| кожен запит | пауза `REQUEST_DELAY_S` (1 с) між запитами до одного інстансу |
| `X-RateLimit-Remaining` < 20 | розтягуємо залишок квоти рівномірно до `X-RateLimit-Reset` |
| **429** | чекаємо `Retry-After` (секунди або HTTP-дата) або до `X-RateLimit-Reset`; якщо є інший токен — перемикаємось на нього без очікування |
| 5xx, обрив з'єднання, таймаут | експоненційний backoff `2, 4, 8, 16…` с з jitter, до `MAX_RETRIES` |
| 401 / 403 / 404 / 410 / 422 | не повторюємо: стрічка позначається помилкою, прогін іде далі |
| будь-яке інше виключення в стрічці | логуємо зі стектрейсом, інші стрічки збираються |

Логи — JSON-рядки в `data/logs/collector.log` (і в консоль). Кожна відповідь логується з кодом і заголовками лімітів:

```json
{"ts": "…", "level": "INFO", "msg": "response", "url": "https://mastodon.social/api/v1/timelines/tag/osint?limit=40", "status": 200, "duration_ms": 412, "ratelimit": {"limit": 300, "remaining": 297, "reset": "2026-10-16T07:35:00.000Z"}, "token": "anon"}
```

## Ротація токенів і проксі (бонуси)

```ini
MASTODON_TOKENS=mastodon.social=tokenA|tokenB,infosec.exchange=tokenC
COLLECTOR_PROXY=socks5h://127.0.0.1:9050
```

Для кожного запиту обирається токен без cooldown з найбільшим `Remaining` (при рівності — найменш використаний), тож навантаження розподіляється, і жоден акаунт не «світиться». У БД і логах — лише короткий хеш токена (`tok-1a2b3c4d`), не сам токен. Без токенів працює анонімно (`anon`). Для SOCKS-проксі додайте `pip install requests[socks]`.

## Дашборд (бонус)

`python -m collector export` генерує `data/export/dashboard.html` — статичну сторінку без зовнішніх залежностей: кількість записів, авторів, медіа, розподіл за стрічками/днями/мовами, коди HTTP, побачені ліміти, курсори й історія прогонів.

## Тести

```bash
pytest -q
```

13 офлайн-тестів на фейковому Mastodon (`responses`), без мережі й справжніх пауз: інкрементність і повторний запуск без дублів, поступовий рух курсора, backfill до кінця стрічки, dedup між стрічками, 429 з `Retry-After`, backoff на 5xx, ізоляція зламаної стрічки, ротація токенів, експорт.

## Структура

```
collector/
  config.py          налаштування з env/.env, формат стрічок і токенів
  logging_setup.py   JSON-логи в консоль і data/logs/collector.log
  http_client.py     пейсинг, 429/Retry-After, backoff, ротація токенів, проксі
  sources/mastodon.py  пагінація min_id/max_id, HTML → текст, нормалізація
  storage.py         SQLite: posts / cursors / runs / http_log, експорт JSONL/CSV
  runner.py          один прогін: ізоляція стрічок, транзакція «сторінка + курсор»
  dashboard.py       статичний HTML-дашборд
  __main__.py        CLI
tests/               pytest на фейковому API
data/                posts.sqlite, export/, logs/  — результати, комітяться для здачі
docs/ЗВІТ.md         звіт про джерело та ліміти
scripts/             .bat для Windows
```

## Що здати (чеклист)

- [ ] `scripts\finish-homework.bat` — тести, збір 200+, дашборд, коміт
- [ ] перевірити `data/export/posts.jsonl` (≥ 200 рядків) і `docs/ЗВІТ.md`
- [ ] запушити в GitHub
