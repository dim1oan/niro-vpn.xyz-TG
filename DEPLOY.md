# Руководство: запуск VPN-бота на чистом сервере Ubuntu VPS

Пошаговая инструкция развёртывания этого Telegram-бота на «голой» VPS с Ubuntu
(22.04 / 24.04) через терминал — от первого входа по SSH до работающего сервиса
с автозапуском.

Весь процесс занимает ~15–20 минут.

---

## Что понадобится

| Что | Где взять |
|---|---|
| VPS с Ubuntu 22.04/24.04, минимум 1 ГБ RAM | любой хостинг |
| IP сервера и root-пароль (или SSH-ключ) | выдаёт хостинг после заказа |
| Токен бота (`BOT_TOKEN`) | [@BotFather](https://t.me/BotFather) в Telegram |
| Ваш Telegram ID (`ADMIN_IDS`) | [@userinfobot](https://t.me/userinfobot) |
| Данные панели 3x-ui (URL, логин, пароль, inbound ID) | панель вашего VPN-сервера |

> Бот **не управляет VPN-сервером напрямую** — он общается с уже настроенной
> панелью 3x-ui по HTTP API. Панель должна быть запущена и доступна с VPS.

---

## Шаг 0. Вход на сервер

На своём компьютере откройте терминал и подключитесь:

```bash
ssh root@IP_ВАШЕГО_СЕРВЕРА
```

При первом входе подтвердите отпечаток ключа (`yes`), затем введите пароль.
Все команды ниже выполняются **на сервере**.

---

## Шаг 1. Подготовка системы

Обновите пакеты и установите базовые утилиты:

```bash
apt update && apt upgrade -y
apt install -y git curl nano ca-certificates tzdata build-essential
```

Установите правильный часовой пояс (тот же, что будет в `.env`):

```bash
timedatectl set-timezone Europe/Moscow
timedatectl   # проверка
```

### (Рекомендуется) Создать отдельного пользователя

Работать постоянно под root — плохая практика:

```bash
adduser --disabled-password --gecos "" vpnbot
usermod -aG sudo vpnbot
```

Дальше все команды выполняем от этого пользователя:

```bash
su - vpnbot
```

---

## Шаг 2. Установка Python 3.11+

**Ubuntu 24.04** — Python 3.12 уже установлен:

```bash
python3 --version    # должно быть >= 3.11
```

**Ubuntu 22.04** — там Python 3.10, нужен новее (PPA deadsnakes):

```bash
sudo apt install -y software-properties-common
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev
```

---

## Шаг 3. Загрузка кода проекта

### Вариант А — из Git-репозитория

```bash
cd /opt
sudo mkdir -p vpn_tg && sudo chown vpnbot:vpnbot vpn_tg
git clone https://github.com/<ВАШ_ЛОГИН>/<ВАШ_РЕПОЗИТОРИЙ>.git /opt/vpn_tg
```

### Вариант Б — скопировать проект со своего компьютера

Выполняется **на локальной машине**, не на сервере:

```bash
rsync -avz --exclude '.venv' --exclude '.git' --exclude 'data/bot.db' \
      ~/путь/к/vpn_tg/ root@IP_СЕРВЕРА:/opt/vpn_tg/
```

Затем на сервере поправьте права:

```bash
sudo chown -R vpnbot:vpnbot /opt/vpn_tg
```

Проверка:

```bash
ls /opt/vpn_tg     # должны видеть app/, requirements.txt, alembic.ini и т.д.
```

---

## Шаг 4. Виртуальное окружение и зависимости

```bash
cd /opt/vpn_tg
python3 -m venv .venv
.venv/bin/pip install --upgrade pip wheel
.venv/bin/pip install -r requirements.txt
```

Проверка (должен пройти без ошибок):

```bash
.venv/bin/python -c "import aiogram, sqlalchemy; print('ok')"
```

---

## Шаг 5. Файл конфигурации `.env`

Скопируйте шаблон и отредактируйте:

```bash
cp .env.example .env
nano .env
```

Обязательно заполните:

```ini
# Telegram
BOT_TOKEN=1234567890:AA...          # токен от @BotFather
ADMIN_IDS=8468512348                # ваш Telegram ID (можно несколько через запятую)
ADMIN_CHAT_ID=                      # чат для алертов; пусто = писать первому админу
BRAND_NAME=MyVPN                    # название бренда в сообщениях
TIMEZONE=Europe/Moscow

# Хранилище (SQLite — достаточно для старта)
DATABASE_URL=sqlite+aiosqlite:///./data/bot.db

# Секретный ключ шифрования (Fernet). Сгенерируйте командой ниже!
SECRET_KEY=

# 3x-ui — данные вашей панели
XUI_BASE_URL=https://1.2.3.4:31653/СекретныйПуть
XUI_USERNAME=логин_панели
XUI_PASSWORD=пароль_панели
XUI_INBOUND_ID=2                    # ID inbound'а VLESS+Reality в панели
SERVER_HOST=1.2.3.4                 # публичный IP VPN-сервера
SERVER_CODE=fr-1
SERVER_COUNTRY=Франция
SERVER_FLAG=🇫🇷
```

Где взять значения для блока 3x-ui (см. также README):

- `XUI_BASE_URL` — полный адрес панели вместе с webBasePath,
  например `https://217.60.37.143:31653/VXfRulHrgoqgyLoE0L`;
- `XUI_INBOUND_ID` — колонка ID напротив inbound'а в списке панели;
- `SERVER_HOST` — публичный IP/домен VPN-сервера (**не** listen из inbound);
- `XUI_SUB_URL` — база subscription-ссылок, например
  `https://панель.example.com:2096/sub` (если пользуетесь подписками).

Закройте nano: `Ctrl+O`, `Enter`, `Ctrl+X`.

### Сгенерировать SECRET_KEY

Если оставили поле пустым — бот сам создаст ключ при первом старте. Но лучше
задать его заранее одной командой:

```bash
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Скопируйте строку вида `T5yuVSocQ9Q7TW3Bzx9cwQ84kkib59sUFnIeKegg4hE=` и вставьте
в `SECRET_KEY=` файла `.env`.

> ⚠️ **Никогда не меняйте SECRET_KEY после запуска в продакшене** — иначе бот не
> сможет расшифровать сохранённые пароли панелей серверов.

### Защитите конфиг с секретами

```bash
chmod 600 .env
mkdir -p data
```

---

## Шаг 6. Миграции базы данных и начальные данные

Из корня `/opt/vpn_tg`:

```bash
# создать/обновить таблицы
.venv/bin/alembic upgrade head

# добавить первый сервер из .env (после первого раза скажет "уже существует")
.venv/bin/python -m app.cli seed_server

# добавить тарифы (trial, 1m, 3m, 6m, 12m)
.venv/bin/python -m app.cli seed_plans
```

Появится файл базы данных `data/bot.db`.

---

## Шаг 7. Пробный запуск вручную

```bash
PYTHONUNBUFFERED=1 .venv/bin/python -m app
```

Успех выглядит так:

```
{"event": "scheduler.started", ...}
{"event": "polling.started", ...}
Run polling for bot @ваш_бот id=...
Start polling
```

А вам в Telegram от первого админа придёт сообщение «🚀 Бот запущен (polling)».
Откройте бота в Telegram, нажмите `/start` и пройдитесь по меню.

Остановить пробный запуск: `Ctrl+C`.

> 💡 `PYTHONUNBUFFERED=1` отключает буферизацию вывода — логи пишутся сразу,
> что важно при запуске как фонового сервиса.

### Типичные ошибки при первом запуске

| Сообщение | Причина | Решение |
|---|---|---|
| `BOT_TOKEN не задан` | пустой `.env` / не тот каталог | проверьте `.env`, запускайте из `/opt/vpn_tg` |
| `Conflict: terminated by other getUpdates request` | бот уже где-то запущен (второй процесс или webhook) | остановите дубликат: `pkill -f "python -m app"` |
| Ошибки подключения к XUI | неверный URL/логин/пароль панели или закрыт порт | проверьте `XUI_BASE_URL/USERNAME/PASSWORD`, `curl -k <URL>` с сервера |
| `SECRET_KEY не задан` | нет ключа | сгенерируйте (Шаг 5) |
| Директория `data/` не создана | ручной перенос без data | `mkdir -p data` |

---

## Шаг 8. Автозапуск через systemd

Чтобы бот жил всегда: переживал перезагрузку сервера и сам перезапускался
при падении, оформим его как системную службу.

Создайте файл службы:

```bash
sudo nano /etc/systemd/system/vpnbot.service
```

Содержимое:

```ini
[Unit]
Description=VPN Telegram Bot (aiogram polling)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=vpnbot
Group=vpnbot
WorkingDirectory=/opt/vpn_tg
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/vpn_tg/.venv/bin/python -m app
Restart=always
RestartSec=5
StandardOutput=append:/opt/vpn_tg/data/bot.log
StandardError=append:/opt/vpn_tg/data/bot.log

# безопасность процесса
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

> Если вы назвали пользователя иначе — замените `vpnbot` в `User=/Group=`
> везде, включая `/etc/systemd/system/vpnbot.service`.
>
> Если каталог проекта другой — поправьте `WorkingDirectory=`,
> `ExecStart=` и пути к логам.

Активация:

```bash
sudo systemctl daemon-reload
sudo systemctl enable vpnbot        # автозапуск при загрузке сервера
sudo systemctl start vpnbot         # старт сейчас
```

---

## Шаг 9. Управление ботом в повседневной работе

```bash
sudo systemctl status vpnbot        # статус (активен/упал, PID)
sudo systemctl restart vpnbot       # перезапуск (после правки кода или .env)
sudo systemctl stop vpnbot          # остановить
sudo systemctl start vpnbot         # запустить снова

# живой хвост логов (главная команда для отладки)
tail -f /opt/vpn_tg/data/bot.log
journalctl -u vpnbot -f             # то же самое через journald
journalctl -u vpnbot --since today  # логи за сегодня
```

Разовые фоновые задачи из README (без остановки бота):

```bash
cd /opt/vpn_tg
.venv/bin/python -m app.cli run_job reconcile_panel   # сверка сирот с панелью
.venv/bin/python -m app.cli run_job reconcile_payments
```

Админ-команды бота: `/admin` (панель управления), промокод:
`/newpromo SUMMER percent 20 100`. Рефанд Stars: `/refund <charge_id>`.

---

## Шаг 10. Файрвол

Для режима long polling боту нужны только **исходящие** соединения.
Минимальный безопасный набор правил:

```bash
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status verbose
```

Если включаете вебхуки или ЮKassa (см. ниже) — дополнительно откройте
нужные порты (например, `sudo ufw allow 443/tcp`) либо ставьте nginx.

---

## Как обновлять бота (релизы)

```bash
sudo systemctl stop vpnbot
cd /opt/vpn_tg
git pull                                  # вариант А; для rsync — повторите rsync
.venv/bin/pip install -r requirements.txt # если менялись зависимости
.venv/bin/alembic upgrade head            # если добавлялись миграции
sudo systemctl start vpnbot
tail -f data/bot.log                      # убедиться, что поднялся
```

---

## Резервные копии

База целиком в одном файле — копировать просто:

```bash
# ручной бэкап
cp /opt/vpn_tg/data/bot.db /root/backup_bot_$(date +%F).db

# автокопия каждый день в 4:00 (crontab -e)
0 4 * * * cp /opt/vpn_tg/data/bot.db /root/backups/bot_$(date +\%F).db
```

Также сохраняйте отдельно `/opt/vpn_tg/.env` — восстановить сервер без него
(токен, секретный ключ, креды панелей) невозможно.

Если перейдёте на PostgreSQL — см. раздел «Бэкапы БД» в README.

---

## Опционально: переход на PostgreSQL

SQLite выдержит сотни пользователей, но при росте:

```bash
sudo apt install -y postgresql postgresql-contrib
sudo -u postgres psql -c "CREATE USER vpnbot WITH PASSWORD 'СЛОЖНЫЙ_ПАРОЛЬ';"
sudo -u postgres psql -c "CREATE DATABASE vpnbot OWNER vpnbot;"
```

В `.env` поменяйте одну строку:

```ini
DATABASE_URL=postgresql+asyncpg://vpnbot:СЛОЖНЫЙ_ПАРОЛЬ@localhost:5432/vpnbot
```

Затем заново:

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli seed_server
.venv/bin/python -m app.cli seed_plans
sudo systemctl restart vpnbot
```

Пароли серверов в старой базе зашифрованы тем же `SECRET_KEY` — при переезде
ключ менять нельзя.

---

## Опционально: приём оплат картами (ЮKassa)

1. Личный кабинет ЮKassa → Интеграция → HTTP-уведомления:
   URL `https://<домен>/webhooks/yookassa`, событие `payment.succeeded`.
2. В `.env` заполните:

   ```ini
   YOOKASSA_SHOP_ID=...
   YOOKASSA_SECRET_KEY=...
   WEBHOOK_ENABLED=true
   WEBHOOK_BASE_URL=https://bot.example.com   # домен с HTTPS-сертификатом
   WEB_PORT=8080
   ```

3. Перед доменом поставьте nginx + certbot (терминально):

   ```bash
   sudo apt install -y nginx certbot python3-certbot-nginx
   sudo certbot --nginx -d bot.example.com          # получит SSL бесплатно
   ```

   И проксируйте трафик на порт бота:

   ```bash
   sudo tee /etc/nginx/sites-available/bot <<'EOF'
   server {
       listen 80;
       server_name bot.example.com;
       location / {
           proxy_pass http://127.0.0.1:8080;
           proxy_set_header Host $host;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       }
   }
   EOF
   sudo ln -s /etc/nginx/sites-available/bot /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   ```

4. `sudo systemctl restart vpnbot` — Telegram-вебхук выставится автоматически.

Пока `WEBHOOK_ENABLED=false`, оплаченные через ЮKassa платежи всё равно
подтягиваются фоновой задачей `reconcile_payments` каждые 5 минут.

---

## Чек-лист «сервер готов»

- [ ] `systemctl status vpnbot` → `active (running)`
- [ ] В логе нет ошибок: `grep -i error /opt/vpn_tg/data/bot.log | tail`
- [ ] `/start` в Telegram отвечает, тарифы видны
- [ ] Пробная подписка выдаёт рабочий ключ (тестовое подключение клиентом)
- [ ] `systemctl reboot` → бот поднялся сам после перезагрузки
- [ ] Настроен ежедневный бэкап `bot.db` + копия `.env`
