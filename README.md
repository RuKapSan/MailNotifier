# Mail.ru Calendar → Telegram Notifier

Оповещения из календаря Mail.ru прямо в Telegram.

## Что умеет

- Уведомления о событиях (за день, за час — настраивается)
- Авто-принятие приглашений на события (PARTSTAT → ACCEPTED)
- Работает с корпоративными календарями Mail.ru
- Подключение через CalDAV или ICS-ссылку

## Быстрый старт

### 1. Клонируй и установи

```bash
git clone https://github.com/YOUR_USERNAME/MailNotifier.git
cd MailNotifier
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Создай Telegram-бота

1. Напиши `/newbot` в [@BotFather](https://t.me/BotFather)
2. Скопируй токен бота
3. Узнай свой chat ID через [@userinfobot](https://t.me/userinfobot)

### 3. Получи доступ к календарю

**Способ 1: CalDAV (рекомендуется)**

1. Зайди на [mail.ru](https://mail.ru) → Настройки → Безопасность
2. Раздел «Пароли для внешних приложений»
3. Создай новый пароль (назови «Calendar Notifier»)
4. Скопируй сгенерированный пароль

**Способ 2: ICS-ссылка** (только уведомления, без авто-принятия)

1. Зайди на [calendar.mail.ru](https://calendar.mail.ru)
2. Нажми ⚙ рядом с нужным календарём → Экспорт
3. Скопируй iCal-ссылку

### 4. Настрой

```bash
cp .env.example .env
nano .env  # заполни токены и пароли
```

### 5. Проверь подключение

```bash
python3 main.py --test
```

### 6. Запусти

```bash
python3 main.py
```

## Автозапуск (systemd)

```bash
sudo tee /etc/systemd/system/mail-notifier.service << 'EOF'
[Unit]
Description=Mail.ru Calendar Telegram Notifier
After=network.target

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
| `CALDAV_PASSWORD` | Пароль приложения | `xA60Lf...` |
| `ICS_URL` | ICS-ссылка (альтернатива CalDAV) | `https://calendar.mail.ru/...` |
| `REMIND_BEFORE_MINUTES` | За сколько минут напоминать | `1440,60` (день + час) |
| `POLL_INTERVAL_SECONDS` | Интервал проверки | `60` |
| `TIMEZONE` | Часовой пояс | `Europe/Moscow` |

## Лицензия

MIT
