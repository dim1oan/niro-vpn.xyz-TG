# VPN Bot — Telegram-бот продажи VPN на базе панели 3x-ui

Production-ready бот для продажи VLESS+Reality доступа. Бот не управляет сервером напрямую —
он создаёт/продлевает клиентов через HTTP API панели [3x-ui](https://github.com/MHSanaei/3x-ui),
где inbound'ы настроены вручную.

## Стек

- Python 3.11+, aiogram 3.x (Router, FSM), long polling / webhook (aiohttp)
- SQLAlchemy 2.0 async (dev: SQLite `aiosqlite`, prod: PostgreSQL `asyncpg`) + Alembic
- APScheduler (7 фоновых задач), httpx (retry + релогин), pydantic-settings
- Платежи: ЮKassa (карта), Telegram Stars (XTR), ручная оплата, внутренний баланс
- qrcode[pil] (QR подписки), structlog (JSON-логи), Fernet (шифрование паролей панелей)

## Быстрый старт

```bash
cp .env.example .env          # заполнить BOT_TOKEN, SECRET_KEY, креды панели
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

SECRET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

make upgrade                  # alembic-миграции
make seed                     # тарифы + первый сервер (Франция) из .env
make run                      # long polling
```

Docker:

```bash
cp .env.example .env && $EDITOR .env
docker compose up -d --build
docker compose exec bot alembic upgrade head
docker compose exec bot python -m app.cli seed_server
docker compose exec bot python -m app.cli seed_plans
```

## Настройка 3x-ui

1. В панели создайте inbound **VLESS + TCP + Reality** (`security: reality`).
2. `webBasePath` может быть кастомным — укажите полный base URL в `XUI_BASE_URL`,
   например `https://panel.example.com:2053/MySecretPath`.
3. Где что брать:
   - `XUI_INBOUND_ID` — ID inbound'а (колонка ID в списке);
   - `SERVER_HOST` — публичный IP/домен сервера (**не** listen из inbound);
   - `pbk/sni/sid` — из настроек Reality inbound'а (publicKey, serverNames[0], shortIds[0]);
     бот берёт их сам из API при генерации ссылки;
   - `XUI_SUB_URL` — база subscription-ссылок (`https://panel.example.com:2096/sub`);
   - юзер/пароль панели — в `XUI_USERNAME`/`XUI_PASSWORD` (в БД хранятся зашифрованными Fernet).
4. У клиента, создаваемого ботом: `totalGB=0` (безлимит), `limitIp=plan.device_limit`,
   `expiryTime` = конец оплаченного периода (мс epoch).

## Настройка вебхука ЮKassa

1. Личный кабинет ЮKassa → Интеграция → HTTP-уведомления:
   URL `https://<ваш домен>/webhooks/yookassa`, события `payment.succeeded`.
2. `.env`: `WEBHOOK_ENABLED=true`, `WEBHOOK_BASE_URL=https://bot.example.com`, `WEB_PORT=8080`.
3. Telegram webhook ставится автоматически на `<WEBHOOK_BASE_URL>/webhook/telegram`.
4. Вебхук проверяет IP отправителя по официальным сетям ЮKassa и **всегда** перепроверяет
   статус платежа повторным запросом к API.
5. Страховка от потерянных вебхуков: задача `reconcile_payments` каждые 5 минут.

## Тарифы (сид)

| code | Название | Дней | Цена | Stars | Устройств |
|------|----------|------|------|-------|-----------|
| trial | Пробный | 3 | 0 ₽ | 0 | 1 |
| 1m | 1 месяц | 30 | 199 ₽ | 150 | 3 |
| 3m | 3 месяца | 90 | 499 ₽ | 375 | 3 |
| 6m | 6 месяцев | 180 | 899 ₽ | 675 | 3 |
| 12m | 1 год | 365 | 1490 ₽ | 1120 | 5 |

Редактирование — таблица `plans` или админ-панель бота.

## Фоновые задачи

| Задача | Период | Действие |
|---|---|---|
| sync_traffic | 15 мин | трафик клиентов → БД |
| expire_check | 10 мин | истёкшие → expired + enable=false |
| notify_expiring | 1 час | напоминания за 3д/1д/3ч |
| cleanup_deleted | 1 день | delClient после N дней истечения |
| reconcile_payments | 5 мин | добить pending ЮKassa |
| reconcile_panel | 6 час | сверка сирот БД ↔ панель |
| retry_provisioning | 2 мин | повтор выдачи упавших (≤3 попыток) |

Разовый прогон: `python -m app.cli run_job <name>` (например `reconcile_panel`).

## Админ-панель

`/admin` — статистика (MRR, выручка, конверсия триал→оплата), пользователи (бан/выдача/баланс),
подписки, серверы, тарифы, промокоды, рассылка сегментами с rate-limit, очередь ручных платежей,
экспорт CSV `/export_payments`. Промокод: `/newpromo SUMMER percent 20 100`.

Поддержка: сообщение пользователя пересылается в админ-чат, ответ — кнопкой «Ответить».
Возврат Stars: `/refund <telegram_payment_charge_id>`.

## Безопасность

- Пароли панелей шифруются Fernet (`SECRET_KEY`); произвольная строка детерминированно деривируется.
- Никаких секретов в логах (редактор вырезает password/token/cookie/vless).
- Идемпотентность платежей: уникальный индекс `payments.external_id` + блокировка строки.
- Все callback_data — через aiogram CallbackData фабрики; callback чужой клавиатуры игнорируется.
- Один pending-платёж на пользователя; суммы всегда берутся из БД.

## Добавление второго сервера/страны

1. Поднимите новый сервер с 3x-ui, настройте VLESS+Reality inbound.
2. Вставьте строку в таблицу `servers` (пароли — зашифруйте тем же ключом):

```sql
INSERT INTO servers (code, country_name, country_flag, xui_base_url,
                     xui_username_enc, xui_password_enc, server_host,
                     inbound_id, sub_url, max_clients, is_active, sort_order)
VALUES ('de-1', 'Германия', '🇩🇪', 'https://panel2.example.com/secretpath',
         '<fernet>', '<fernet>', '185.9.9.9', 1, 'https://panel2.example.com/sub', 300, true, 200);
```

Меню выбора страны появится автоматически, когда активных стран больше одной.
Балансировка: выбирается сервер страны с наименьшим числом активных ключей (< `max_clients`).

## Как масштабировать

- **Страны**: просто новые записи в `servers` — код выбора сервера уже мульти-серверный.
- **PostgreSQL**: поменяйте `DATABASE_URL=postgresql+asyncpg://...`; миграции те же
  (SQLite только для dev). `SELECT ... FOR UPDATE` включается автоматически.
- **Вынос воркера**: планировщик стартует вместе с ботом; чтобы разделить процессы,
  запустите второй контейнер с командой `python -c "from app.tasks.scheduler import *"`-обёрткой
  (см. `app/tasks/jobs/__init__.py`) без диспетчера — задачи идемпотентны и защищены локами.
- **Нагрузка**: polling → webhook; Redis для FSM (`REDIS_URL`); горизонтальное масштабирование
  веб-слоя за nginx, БД — managed Postgres с репликами для чтения статистики.

## Разработка

```bash
make test      # pytest (24 теста: ссылки, XuiClient/respx, продление, идемпотентность, промокод, интеграция)
make lint      # ruff + mypy
make format
```

## Бэкапы БД

```bash
# Postgres (docker compose)
docker compose exec postgres pg_dump -U vpnbot vpnbot | gzip > backup_$(date +%F).sql.gz
# восстановление
gunzip -c backup_2026-01-01.sql.gz | docker compose exec -T postgres psql -U vpnbot vpnbot
```
