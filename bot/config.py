import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def _load_dotenv(path: Path) -> None:
    # Locally there is no Docker env_file, so read KEY=VALUE pairs ourselves;
    # real environment variables always win.
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_user_id: int
    db_path: Path
    # e.g. socks5://user:pass@host:port; None means direct connection
    proxy_url: str | None = None
    # Baked into the Docker image by CI; empty for local runs
    app_version: str | None = None
    app_title: str | None = None
    # Set by deploy.sh when it rolled back from this version
    failed_version: str | None = None
    # Subscription bot API; subscriptions are off while either is empty
    subs_api_url: str | None = None
    subs_api_token: str | None = None

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"


def load_config() -> Config:
    _load_dotenv(Path(".env"))
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("BOT_TOKEN is not set")
    raw_user_id = os.environ.get("ALLOWED_USER_ID", "").strip()
    try:
        allowed_user_id = int(raw_user_id)
    except ValueError:
        raise ConfigError("ALLOWED_USER_ID must be a numeric Telegram user id") from None
    db_path = Path(os.environ.get("DB_PATH", "data/finance.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    subs_url = _optional("SUBS_API_URL")
    if subs_url and not subs_url.startswith(("http://", "https://")):
        raise ConfigError("SUBS_API_URL must start with http:// or https://")
    return Config(
        bot_token=token,
        allowed_user_id=allowed_user_id,
        db_path=db_path,
        proxy_url=_optional("PROXY_URL"),
        app_version=_optional("APP_VERSION"),
        app_title=_optional("APP_TITLE"),
        failed_version=_optional("FAILED_VERSION"),
        subs_api_url=subs_url,
        subs_api_token=_optional("SUBS_API_TOKEN"),
    )


def _optional(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None
