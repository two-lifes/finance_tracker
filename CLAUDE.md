# finance_tracker

Personal Telegram bot (aiogram 3): one balance per day → daily spending, "можно тратить сегодня",
loan reminders, payday plan. Single user (whitelist by `ALLOWED_USER_ID`). User-facing text is
Russian. Subscriptions are read-only from the subs_tracker bot's HTTP API.

## Commands (Windows, from project root)
- Install: `python -m venv .venv; .\.venv\Scripts\python -m pip install -e ".[dev]"`
- Run bot: `.\.venv\Scripts\python -m bot` (applies migrations on start; needs `.env`, see `.env.example`)
- Tests: `.\.venv\Scripts\python -m pytest`
- Lint: `.\.venv\Scripts\ruff check .`
- New migration: `.\.venv\Scripts\alembic revision --autogenerate -m "..."` (uses `DB_PATH`)
- Tests on Python 3.12 in Docker: `docker build --target test .`

## Layout
- `bot/services/` — logic. Pure (no I/O, unit-tested): `money`, `plural`, `dates`, `work_calendar`,
  `loans`, `subscriptions`, `obligations`, `limit`, `fx`. DB-backed: `loan_ops`, `budget`,
  `external` (API/CBR cache), `notifications`.
- `bot/handlers/` — Telegram; `bot/views.py` — all message texts; `bot/keyboards/` — buttons.
- `bot/scheduler.py` — 60 s loop: subscription sync (hourly), CBR rates (daily), reminders.
- Dialog tests: `tests/test_flows.py` via `tests/harness.py` (fake Telegram, fake subs API,
  fake CBR, controllable clock).

## Conventions
- Money: `int` minor units (kopecks), never float; rounding only in `money.round_minor`.
  Rates/percents: `Decimal`, stored as text.
- Time only via `bot.clock.utcnow()` / `user_today()`; datetimes in SQLite are UTC (`as_utc`).
- Bot-initiated messages go through the `notifications` table (plan → deliver), never sent directly.
- Network calls in the scheduler run outside `db_lock`; handlers may call the API (5 s timeout).
- Subscription dates come from the API as is; the bot never recomputes them. A charge is done when
  `/payments` has `(subscription_id, charge_date)`.
- Auto-debit loan dues are assumed paid when a balance for their date or later arrives
  ("не списалось" reverts it); manual dues only after "✅ Оплатил".

## Deploy
push to main → GitHub Actions (tests in Docker) → image `ghcr.io/two-lifes/finance_tracker` →
SSH → `deploy/deploy.sh` in `/opt/finance_tracker`. The container joins the external docker
network `bots_internal` and calls `http://subs-api:8080`. subs_tracker must listen on `0.0.0.0`
(`API_HOST`) and join the same network with alias `subs-api`; no ports are published.
