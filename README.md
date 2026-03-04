# Mail.ru Calendar + Asana → Telegram

Рабочие уведомления в одном месте — Telegram-бот как хаб.

## Что умеет

**Календарь Mail.ru:**
- Уведомления о событиях (за день, за час — настраивается)
- Авто-принятие приглашений (PARTSTAT → ACCEPTED)
- Работает с корпоративными календарями
- CalDAV или ICS-ссылка

**Asana:**
- Уведомления о новых назначенных задачах
- Напоминания о дедлайнах (сегодня / завтра)

**Бот-команды:**
- `/today` — события на сегодня
- `/tomorrow` — события на завтра
- `/week` — события на неделю
- `/tasks` — мои задачи в Asana
- `/mute [часы]` — выключить уведомления
- `/unmute` — включить уведомления
- `/status` — статус бота

## Быстрый старт

### 1. Установи

```bash
git clone https://github.com/RuKapSan/MailNotifier.git
cd MailNotifier
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Создай Telegram-бота

1. Напиши `/newbot` в [@BotFather](https://t.me/BotFather)
2. Скопируй токен бота
3. Узнай свой chat ID через [@userinfobot](https://t.me/userinfobot)

### 3. Подключи календарь

**Способ 1: CalDAV (рекомендуется)**

1. Зайди на [mail.ru](https://mail.ru) → Настройки → Безопасность
2. Раздел «Пароли для внешних приложений»
3. Создай новый пароль (назови «Calendar Notifier»)
4. Скопируй сгенерированный пароль

**Способ 2: ICS-ссылка** (только уведомления, без авто-принятия)

1. Зайди на [calendar.mail.ru](https://calendar.mail.ru)
2. Нажми ⚙ рядом с нужным календарём → Экспорт
3. Скопируй iCal-ссылку

### 4. Подключи Asana (опционально)

1. Зайди на [app.asana.com/0/my-apps](https://app.asana.com/0/my-apps)
2. Создай Personal Access Token
3. Узнай ID воркспейса:
```bash
curl -s https://app.asana.com/api/1.0/users/me \
  -H "Authorization: Bearer YOUR_TOKEN" | jq '.data.workspaces'
```

### 5. Настрой и запусти

```bash
cp .env.example .env
nano .env  # заполни токены и пароли
python3 main.py --test  # проверить подключение
python3 main.py         # запустить
```

## Автозапуск (systemd)

```bash
sudo tee /etc/systemd/system/mail-notifier.service << 'EOF'
[Unit]
Description=Mail.ru Calendar + Asana Telegram Notifier
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/MailNotifier
ExecStart=/path/to/MailNotifier/venv/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now mail-notifier
```

## Настройки (.env)

| Переменная | Описание | Пример |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота | `123456:ABC...` |
| `TELEGRAM_CHAT_ID` | Твой Telegram ID | `123456789` |
| `CALDAV_URL` | CalDAV сервер | `https://calendar.mail.ru/.well-known/caldav` |
| `CALDAV_USERNAME` | Email | `user@mail.ru` |
| `CALDAV_PASSWORD` | Пароль приложения | `app_password` |
| `ICS_URL` | ICS-ссылка (альтернатива CalDAV) | `https://calendar.mail.ru/...` |
| `ASANA_TOKEN` | Personal Access Token | `2/123.../456...:abc...` |
| `ASANA_WORKSPACE_GID` | ID воркспейса | `1206271359967827` |
| `REMIND_BEFORE_MINUTES` | За сколько минут напоминать | `1440,60` (день + час) |
| `POLL_INTERVAL_SECONDS` | Интервал проверки | `60` |
| `TIMEZONE` | Часовой пояс | `Europe/Moscow` |

## Лицензия

MIT
