"""Налаштування з env / .env (одне місце, без магії). Секрети лише тут, у коді їх немає."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # .env необов'язковий: у Docker змінні приходять через env_file
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]


@dataclass(frozen=True)
class Target:
    """Одна «стрічка» джерела: instance + тип + значення.

    Формати у MASTODON_TARGETS:
      mastodon.social:tag:osint        — публічна стрічка хештегу
      mstdn.social:account:bellingcat  — пости акаунта (acct без @)
      mastodon.social:public:local     — локальна публічна стрічка інстансу
    """

    instance: str
    kind: str
    value: str

    @property
    def key(self) -> str:
        return f"{self.instance}:{self.kind}:{self.value}"

    @classmethod
    def parse(cls, spec: str) -> "Target":
        parts = spec.strip().split(":", 2)
        if len(parts) != 3 or parts[1] not in {"tag", "account", "public"}:
            raise ValueError(f"Невірний target '{spec}'. Формат: instance:tag|account|public:value")
        return cls(parts[0].lower(), parts[1], parts[2].lstrip("#@"))


def _tokens() -> dict[str, list[str]]:
    """MASTODON_TOKENS=mastodon.social=tokA|tokB,infosec.exchange=tokC → {instance: [tokens]}.

    Токени необов'язкові: публічні стрічки читаються анонімно. Кілька токенів на інстанс —
    ротація (бонус), щоб жоден акаунт не «світився» і ліміт ділився між ними.
    """
    out: dict[str, list[str]] = {}
    for item in _list("MASTODON_TOKENS"):
        if "=" not in item:
            continue
        inst, toks = item.split("=", 1)
        out[inst.strip().lower()] = [t.strip() for t in toks.split("|") if t.strip()]
    return out


@dataclass(frozen=True)
class Settings:
    targets: list[Target] = field(
        default_factory=lambda: [
            Target.parse(s)
            for s in _list(
                "MASTODON_TARGETS",
                "mastodon.social:tag:osint,mastodon.social:tag:ukraine,mstdn.social:account:bellingcat",
            )
        ]
    )
    tokens: dict[str, list[str]] = field(default_factory=_tokens)

    # Партія: один прогін = невелика порція, курсор рухається поступово
    page_limit: int = _int("PAGE_LIMIT", 40)  # статусів за запит (максимум API — 40)
    max_pages_per_target: int = _int("MAX_PAGES_PER_TARGET", 5)  # сторінок на стрічку за прогін
    backfill_pages: int = _int("BACKFILL_PAGES", 10)  # сторінок назад у історію за backfill

    # Повага до лімітів
    request_delay_s: float = _float("REQUEST_DELAY_S", 1.0)  # пауза між запитами (не менше 0.5)
    max_retries: int = _int("MAX_RETRIES", 5)
    backoff_base_s: float = _float("BACKOFF_BASE_S", 2.0)
    backoff_max_s: float = _float("BACKOFF_MAX_S", 300.0)
    low_remaining: int = _int("RATELIMIT_LOW_REMAINING", 20)  # коли Remaining нижче — пригальмовуємо
    timeout_s: float = _float("HTTP_TIMEOUT_S", 20.0)

    # Мережа
    proxy: str | None = os.getenv("COLLECTOR_PROXY") or None  # http://user:pass@host:port, socks5h://...
    user_agent: str = os.getenv(
        "USER_AGENT", "social-collector/1.0 (+educational OSINT homework; contact via repo)"
    )

    # Зберігання
    data_dir: Path = Path(os.getenv("DATA_DIR", "data"))
    db_name: str = os.getenv("DB_NAME", "posts.sqlite")

    # Режим «демона» (docker compose)
    loop_interval_s: int = _int("LOOP_INTERVAL_S", 900)

    # Логи
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_format: str = os.getenv("LOG_FORMAT", "json")  # json | text

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_name


settings = Settings()
