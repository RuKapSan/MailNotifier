#!/usr/bin/env python3
"""Mail.ru Calendar + Asana → Telegram Notifier.

Features:
- Calendar events via CalDAV or ICS, auto-accept invitations
- Asana: new task notifications, deadline reminders
- Bot commands: /today, /tomorrow, /week, /tasks, /mute, /unmute, /status
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, date
from pathlib import Path

import caldav
import requests
from dateutil import tz
from dotenv import load_dotenv
from icalendar import Calendar

load_dotenv()

# --- Config ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CALDAV_URL = os.getenv("CALDAV_URL", "https://calendar.mail.ru/.well-known/caldav")
CALDAV_USERNAME = os.getenv("CALDAV_USERNAME")
CALDAV_PASSWORD = os.getenv("CALDAV_PASSWORD")

ICS_URL = os.getenv("ICS_URL")

ASANA_TOKEN = os.getenv("ASANA_TOKEN")
ASANA_WORKSPACE_GID = os.getenv("ASANA_WORKSPACE_GID")

REMIND_BEFORE_MINUTES = [
    int(x.strip())
    for x in os.getenv("REMIND_BEFORE_MINUTES", "1440,60").split(",")
]
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

STATE_FILE = Path(__file__).parent / "sent_notifications.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# --- Runtime state ---
_started_at = None
_notifications_sent = 0
_muted_until: datetime | None = None

# --- Default state schema ---
_DEFAULT_STATE = {"sent": {}, "accepted": [], "asana_seen_tasks": []}


# --- State ---

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            for k, v in _DEFAULT_STATE.items():
                data.setdefault(k, v if not isinstance(v, list) else list(v))
            return data
        except json.JSONDecodeError:
            return dict(_DEFAULT_STATE)
    return dict(_DEFAULT_STATE)


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# --- Telegram ---

def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        },
        timeout=10,
    )
    if not resp.ok:
        log.error(f"Telegram error: {resp.status_code} {resp.text}")
    return resp.ok


def telegram_api(method: str, **kwargs) -> dict | None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=kwargs, timeout=10)
        if resp.ok:
            return resp.json().get("result")
    except Exception as e:
        log.error(f"Telegram API error ({method}): {e}")
    return None


# --- CalDAV client (cached) ---

_caldav_client = None


def get_caldav_client() -> caldav.DAVClient:
    global _caldav_client
    if _caldav_client is None:
        _caldav_client = caldav.DAVClient(
            url=CALDAV_URL,
            username=CALDAV_USERNAME,
            password=CALDAV_PASSWORD,
        )
    return _caldav_client


# --- Asana ---

def asana_api(endpoint: str, params: dict | None = None) -> dict | None:
    """Call Asana API."""
    url = f"https://app.asana.com/api/1.0{endpoint}"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
            params=params or {},
            timeout=15,
        )
        if resp.ok:
            return resp.json()
    except Exception as e:
        log.error(f"Asana API error ({endpoint}): {e}")
    return None


def asana_get_my_tasks() -> list[dict]:
    """Get incomplete tasks assigned to me."""
    result = asana_api(
        "/tasks",
        {
            "assignee": "me",
            "workspace": ASANA_WORKSPACE_GID,
            "completed_since": "now",
            "opt_fields": "name,due_on,due_at,modified_at,permalink_url,projects.name",
        },
    )
    if result:
        return result.get("data", [])
    return []


def check_asana_new_tasks():
    """Check for newly assigned Asana tasks and notify."""
    if not ASANA_TOKEN or not ASANA_WORKSPACE_GID:
        return

    state = load_state()
    seen = set(state.get("asana_seen_tasks", []))

    try:
        tasks = asana_get_my_tasks()
    except Exception as e:
        log.error(f"Asana fetch error: {e}")
        return

    for task in tasks:
        gid = task["gid"]
        if gid in seen:
            continue

        name = task.get("name", "Без названия")
        url = task.get("permalink_url", "")
        due = task.get("due_on") or ""
        projects = task.get("projects", [])
        project_name = projects[0]["name"] if projects else ""

        lines = ["<b>Новая задача в Asana</b>\n"]
        lines.append(f"<b>{name}</b>")
        if project_name:
            lines.append(f"Проект: {project_name}")
        if due:
            lines.append(f"Дедлайн: {due}")
        if url:
            lines.append(f"\n<a href=\"{url}\">Открыть в Asana</a>")

        send_telegram("\n".join(lines))
        log.info(f"Asana new task: {name}")

        seen.add(gid)

    state["asana_seen_tasks"] = list(seen)
    save_state(state)


def check_asana_deadlines():
    """Check for Asana tasks with approaching deadlines."""
    if not ASANA_TOKEN or not ASANA_WORKSPACE_GID:
        return

    state = load_state()
    now_local = datetime.now(tz.gettz(TIMEZONE))
    today_str = now_local.strftime("%Y-%m-%d")
    tomorrow_str = (now_local + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        tasks = asana_get_my_tasks()
    except Exception as e:
        log.error(f"Asana deadline check error: {e}")
        return

    for task in tasks:
        due = task.get("due_on")
        if not due:
            continue

        gid = task["gid"]
        name = task.get("name", "")
        url = task.get("permalink_url", "")

        if due == today_str:
            key = f"asana_deadline_{gid}_{due}_today"
        elif due == tomorrow_str:
            key = f"asana_deadline_{gid}_{due}_tomorrow"
        else:
            continue

        if key in state["sent"]:
            continue

        if due == today_str:
            header = "Дедлайн сегодня!"
        else:
            header = "Дедлайн завтра"

        lines = [f"<b>{header}</b>\n"]
        lines.append(f"<b>{name}</b>")
        lines.append(f"Срок: {due}")
        if url:
            lines.append(f"\n<a href=\"{url}\">Открыть</a>")

        send_telegram("\n".join(lines))
        state["sent"][key] = datetime.now(tz.UTC).isoformat()

    save_state(state)


# --- Auto-accept invitations ---

def accept_pending_events(calendars: list):
    """Find events where user is invited but hasn't accepted, and accept them."""
    state = load_state()
    now = datetime.now(tz.UTC)
    start = now - timedelta(hours=1)
    end = now + timedelta(days=30)

    for cal in calendars:
        try:
            results = cal.search(start=start, end=end, expand=False, event=True)
        except Exception as e:
            log.warning(f"Error searching calendar for acceptance: {e}")
            continue

        for event_obj in results:
            try:
                ical_data = event_obj.data
                ical = Calendar.from_ical(ical_data)

                for component in ical.walk():
                    if component.name != "VEVENT":
                        continue

                    uid = str(component.get("uid", ""))
                    if not uid or uid in state.get("accepted", []):
                        continue

                    attendees = component.get("attendee")
                    if attendees is None:
                        continue

                    if not isinstance(attendees, list):
                        attendees = [attendees]

                    needs_accept = False
                    for attendee in attendees:
                        email = str(attendee).replace("mailto:", "").lower()
                        if email == CALDAV_USERNAME.lower():
                            partstat = attendee.params.get("PARTSTAT", "NEEDS-ACTION")
                            if partstat == "NEEDS-ACTION":
                                needs_accept = True
                                break

                    if needs_accept:
                        summary = str(component.get("summary", "Без названия"))
                        if _do_accept(event_obj, ical_data, uid):
                            state.setdefault("accepted", []).append(uid)
                            save_state(state)

                            dtstart = component.get("dtstart")
                            dt = dtstart.dt if dtstart else None
                            time_str = ""
                            if dt:
                                local_tz = tz.gettz(TIMEZONE)
                                if isinstance(dt, datetime):
                                    time_str = dt.astimezone(local_tz).strftime(
                                        " (%d.%m %H:%M)"
                                    )

                            send_telegram(
                                f"<b>Принято приглашение</b>\n\n"
                                f"<b>{summary}</b>{time_str}"
                            )
                            log.info(f"Accepted invitation: {summary}")

            except Exception as e:
                log.warning(f"Error processing event for acceptance: {e}")


def _do_accept(event_obj, ical_data: str, uid: str) -> bool:
    """Update PARTSTAT to ACCEPTED in the event and save back to server."""
    try:
        modified = ical_data
        username_lower = CALDAV_USERNAME.lower()

        lines = modified.split("\n")
        new_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            full_line = line
            while i + 1 < len(lines) and lines[i + 1].startswith((" ", "\t")):
                i += 1
                full_line += "\n" + lines[i]

            if (
                "ATTENDEE" in full_line
                and username_lower in full_line.lower()
                and "PARTSTAT=NEEDS-ACTION" in full_line
            ):
                full_line = full_line.replace(
                    "PARTSTAT=NEEDS-ACTION", "PARTSTAT=ACCEPTED"
                )

            new_lines.append(full_line)
            i += 1

        modified = "\n".join(new_lines)

        event_obj.data = modified
        event_obj.save()
        return True

    except Exception as e:
        log.error(f"Failed to accept event {uid}: {e}")
        return False


# --- Calendar fetching ---

def fetch_events_caldav(
    start: datetime, end: datetime, also_accept: bool = False
) -> list[dict]:
    """Fetch events from Mail.ru calendar via CalDAV."""
    client = get_caldav_client()
    principal = client.principal()
    calendars = principal.calendars()

    if not calendars:
        log.warning("No calendars found via CalDAV")
        return []

    log.debug(
        f"Found {len(calendars)} calendar(s): "
        f"{[c.get_display_name() for c in calendars]}"
    )

    if also_accept:
        accept_pending_events(calendars)

    events = []
    for cal in calendars:
        try:
            results = cal.search(start=start, end=end, expand=True, event=True)
            for event in results:
                events.extend(parse_vevent(event.data))
        except Exception as e:
            log.warning(f"Error fetching calendar: {e}")

    return events


def fetch_events_ics(start: datetime, end: datetime) -> list[dict]:
    """Fetch events from ICS feed URL."""
    resp = requests.get(ICS_URL, timeout=30)
    resp.raise_for_status()

    cal = Calendar.from_ical(resp.text)
    events = []

    for component in cal.walk():
        if component.name == "VEVENT":
            event = extract_event_data(component)
            if event and start <= event["start"] <= end:
                events.append(event)

    return events


def fetch_events(start: datetime, end: datetime) -> list[dict]:
    """Fetch events from configured source."""
    if ICS_URL:
        return fetch_events_ics(start, end)
    elif CALDAV_USERNAME and CALDAV_PASSWORD:
        return fetch_events_caldav(start, end)
    return []


def parse_vevent(ical_text: str) -> list[dict]:
    """Parse iCalendar text and extract event data."""
    cal = Calendar.from_ical(ical_text)
    events = []

    for component in cal.walk():
        if component.name == "VEVENT":
            event = extract_event_data(component)
            if event:
                events.append(event)

    return events


def extract_event_data(component) -> dict | None:
    """Extract event data from a VEVENT component."""
    dtstart = component.get("dtstart")
    if not dtstart:
        return None

    dt = dtstart.dt
    local_tz = tz.gettz(TIMEZONE)

    if not isinstance(dt, datetime):
        dt = datetime.combine(dt, datetime.min.time())
        dt = dt.replace(tzinfo=local_tz)
        is_all_day = True
    else:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=local_tz)
        is_all_day = False

    dtend = component.get("dtend")
    end_dt = None
    if dtend:
        end_dt = dtend.dt
        if not isinstance(end_dt, datetime):
            end_dt = datetime.combine(end_dt, datetime.min.time())
            end_dt = end_dt.replace(tzinfo=local_tz)
        elif end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=local_tz)

    summary = str(component.get("summary", "Без названия"))
    location = str(component.get("location", "")) if component.get("location") else ""
    description = (
        str(component.get("description", "")) if component.get("description") else ""
    )
    uid = str(component.get("uid", ""))

    return {
        "uid": uid,
        "summary": summary,
        "start": dt,
        "end": end_dt,
        "location": location,
        "description": description,
        "is_all_day": is_all_day,
    }


# --- Notification formatting ---

def _human_time_delta(minutes: int) -> str:
    """Format minutes into human-readable Russian string."""
    if minutes >= 1440:
        days = minutes // 1440
        if days == 1:
            return "завтра"
        return f"через {days} дн."
    elif minutes >= 60:
        hours = minutes // 60
        if hours == 1:
            return "через 1 час"
        elif hours < 5:
            return f"через {hours} часа"
        else:
            return f"через {hours} часов"
    elif minutes == 0:
        return "сейчас"
    elif minutes == 1:
        return "через 1 минуту"
    elif minutes < 5:
        return f"через {minutes} минуты"
    else:
        return f"через {minutes} минут"


def format_notification(event: dict, minutes_before: int) -> str:
    """Format a Telegram notification message."""
    local_tz = tz.gettz(TIMEZONE)
    start = event["start"].astimezone(local_tz)

    time_delta = _human_time_delta(minutes_before)

    lines = [f"<b>Событие {time_delta}!</b>", ""]
    lines.append(f"<b>{event['summary']}</b>")

    if event["is_all_day"]:
        lines.append(f"Весь день — {start.strftime('%d.%m.%Y')}")
    else:
        date_str = start.strftime("%d.%m")
        time_str = start.strftime("%H:%M")
        if event["end"]:
            end = event["end"].astimezone(local_tz)
            time_str += f" — {end.strftime('%H:%M')}"
        lines.append(f"{date_str}, {time_str}")

    if event["location"]:
        lines.append(f"\n{event['location']}")

    if event["description"]:
        desc = event["description"][:300]
        if len(event["description"]) > 300:
            desc += "..."
        lines.append(f"\n{desc}")

    return "\n".join(lines)


def format_event_line(event: dict) -> str:
    """Format a single event as one line for listings."""
    local_tz = tz.gettz(TIMEZONE)
    start = event["start"].astimezone(local_tz)

    if event["is_all_day"]:
        time_part = "весь день"
    else:
        time_part = start.strftime("%H:%M")
        if event["end"]:
            end = event["end"].astimezone(local_tz)
            time_part += f"–{end.strftime('%H:%M')}"

    line = f"  <b>{time_part}</b>  {event['summary']}"
    if event["location"]:
        line += f" ({event['location']})"
    return line


# --- Bot commands ---

def _cmd_today() -> str:
    local_tz = tz.gettz(TIMEZONE)
    now_local = datetime.now(local_tz)
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    events = fetch_events(day_start.astimezone(tz.UTC), day_end.astimezone(tz.UTC))
    events.sort(key=lambda e: e["start"])

    if not events:
        return "Сегодня событий нет"

    header = f"<b>Сегодня, {now_local.strftime('%d.%m')}</b>\n"
    lines = [header] + [format_event_line(e) for e in events]
    return "\n".join(lines)


def _cmd_tomorrow() -> str:
    local_tz = tz.gettz(TIMEZONE)
    now_local = datetime.now(local_tz)
    day_start = (now_local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    day_end = day_start + timedelta(days=1)

    events = fetch_events(day_start.astimezone(tz.UTC), day_end.astimezone(tz.UTC))
    events.sort(key=lambda e: e["start"])

    if not events:
        return "Завтра событий нет"

    header = f"<b>Завтра, {day_start.strftime('%d.%m')}</b>\n"
    lines = [header] + [format_event_line(e) for e in events]
    return "\n".join(lines)


def _cmd_week() -> str:
    local_tz = tz.gettz(TIMEZONE)
    now_local = datetime.now(local_tz)
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = day_start + timedelta(days=7)

    events = fetch_events(day_start.astimezone(tz.UTC), week_end.astimezone(tz.UTC))
    events.sort(key=lambda e: e["start"])

    if not events:
        return "На этой неделе событий нет"

    # Group by day
    days: dict[str, list] = {}
    for e in events:
        local_start = e["start"].astimezone(local_tz)
        day_key = local_start.strftime("%d.%m %a")
        days.setdefault(day_key, []).append(e)

    day_names = {"Mon": "Пн", "Tue": "Вт", "Wed": "Ср", "Thu": "Чт",
                 "Fri": "Пт", "Sat": "Сб", "Sun": "Вс"}

    lines = [f"<b>Неделя: {len(events)} событий</b>\n"]
    for day_key, day_events in days.items():
        for en_name, ru_name in day_names.items():
            day_key_display = day_key.replace(en_name, ru_name)
        lines.append(f"\n<b>{day_key_display}</b>")
        for e in day_events:
            lines.append(format_event_line(e))

    return "\n".join(lines)


def _cmd_mute(args: str) -> str:
    global _muted_until
    try:
        hours = float(args.strip()) if args.strip() else 1
    except ValueError:
        return "Использование: /mute [часы]\nПример: /mute 2"

    _muted_until = datetime.now(tz.UTC) + timedelta(hours=hours)
    local_tz = tz.gettz(TIMEZONE)
    until_str = _muted_until.astimezone(local_tz).strftime("%H:%M")
    return f"Уведомления выключены до {until_str}"


def _cmd_unmute() -> str:
    global _muted_until
    _muted_until = None
    return "Уведомления включены"


def _cmd_status() -> str:
    local_tz = tz.gettz(TIMEZONE)
    now = datetime.now(tz.UTC)

    uptime = now - _started_at if _started_at else timedelta()
    hours = int(uptime.total_seconds() // 3600)
    mins = int((uptime.total_seconds() % 3600) // 60)

    state = load_state()
    source = "ICS" if ICS_URL else "CalDAV"

    lines = [
        "<b>Статус</b>\n",
        f"Аптайм: {hours}ч {mins}мин",
        f"Уведомлений отправлено: {_notifications_sent}",
        f"Приглашений принято: {len(state.get('accepted', []))}",
        f"Источник: {source}",
        f"Asana: {'подключена' if ASANA_TOKEN else 'не настроена'}",
        f"Напоминания: {', '.join(_human_time_delta(m) for m in REMIND_BEFORE_MINUTES)}",
        f"Интервал проверки: {POLL_INTERVAL_SECONDS}с",
    ]

    if _muted_until:
        if now < _muted_until:
            until_str = _muted_until.astimezone(local_tz).strftime("%H:%M")
            lines.append(f"\nЗвук выключен до {until_str}")
        else:
            lines.append("\nЗвук: включён")
    else:
        lines.append("\nЗвук: включён")

    return "\n".join(lines)


def _cmd_tasks() -> str:
    if not ASANA_TOKEN:
        return "Asana не настроена"

    tasks = asana_get_my_tasks()
    if not tasks:
        return "Нет открытых задач в Asana"

    # Split into with deadline and without
    with_due = [t for t in tasks if t.get("due_on")]
    no_due = [t for t in tasks if not t.get("due_on")]

    # Sort by deadline
    with_due.sort(key=lambda t: t["due_on"])

    lines = [f"<b>Asana: {len(tasks)} задач</b>\n"]

    if with_due:
        lines.append("<b>С дедлайном:</b>")
        for t in with_due:
            due = t["due_on"]
            url = t.get("permalink_url", "")
            name = t.get("name", "?")
            if url:
                lines.append(f"  {due} — <a href=\"{url}\">{name}</a>")
            else:
                lines.append(f"  {due} — {name}")

    if no_due:
        lines.append(f"\n<b>Без дедлайна ({len(no_due)}):</b>")
        for t in no_due[:10]:
            url = t.get("permalink_url", "")
            name = t.get("name", "?")
            if url:
                lines.append(f"  <a href=\"{url}\">{name}</a>")
            else:
                lines.append(f"  {name}")
        if len(no_due) > 10:
            lines.append(f"  ... ещё {len(no_due) - 10}")

    return "\n".join(lines)


def _cmd_help() -> str:
    lines = [
        "<b>Команды</b>\n",
        "<b>Календарь:</b>",
        "/today — события на сегодня",
        "/tomorrow — события на завтра",
        "/week — события на неделю",
    ]
    if ASANA_TOKEN:
        lines.append("\n<b>Asana:</b>")
        lines.append("/tasks — мои задачи")
    lines.extend([
        "\n<b>Управление:</b>",
        "/mute [часы] — выключить уведомления",
        "/unmute — включить уведомления",
        "/status — статус бота",
    ])
    return "\n".join(lines)


COMMANDS = {
    "/today": lambda args: _cmd_today(),
    "/tomorrow": lambda args: _cmd_tomorrow(),
    "/week": lambda args: _cmd_week(),
    "/tasks": lambda args: _cmd_tasks(),
    "/mute": lambda args: _cmd_mute(args),
    "/unmute": lambda args: _cmd_unmute(),
    "/status": lambda args: _cmd_status(),
    "/start": lambda args: _cmd_help(),
    "/help": lambda args: _cmd_help(),
}


# --- Telegram bot polling (commands) ---

def bot_polling_loop():
    """Poll Telegram for incoming commands in a separate thread."""
    last_update_id = 0

    while True:
        try:
            result = telegram_api(
                "getUpdates",
                offset=last_update_id + 1,
                timeout=30,
                allowed_updates=["message"],
            )

            if not result:
                time.sleep(5)
                continue

            for update in result:
                last_update_id = update["update_id"]
                message = update.get("message")
                if not message:
                    continue

                # Only respond to authorized user
                chat_id = str(message.get("chat", {}).get("id", ""))
                if chat_id != TELEGRAM_CHAT_ID:
                    log.warning(f"Ignored message from unauthorized chat: {chat_id}")
                    continue

                text = message.get("text", "").strip()
                if not text.startswith("/"):
                    continue

                # Parse command and args
                parts = text.split(maxsplit=1)
                cmd = parts[0].lower().split("@")[0]  # strip @botname
                args = parts[1] if len(parts) > 1 else ""

                handler = COMMANDS.get(cmd)
                if handler:
                    try:
                        response = handler(args)
                        send_telegram(response)
                    except Exception as e:
                        log.error(f"Command {cmd} failed: {e}", exc_info=True)
                        send_telegram(f"Ошибка: {e}")

        except Exception as e:
            log.error(f"Bot polling error: {e}")
            time.sleep(10)


# --- Main loop ---

_accept_counter = 0


def check_and_notify():
    """Single check iteration: fetch events, send notifications."""
    global _accept_counter, _notifications_sent, _muted_until
    state = load_state()
    now = datetime.now(tz.UTC)

    # Check mute
    is_muted = _muted_until and now < _muted_until

    # Look ahead window
    max_remind = max(REMIND_BEFORE_MINUTES) + 1
    start = now - timedelta(minutes=2)
    end = now + timedelta(minutes=max_remind + 5)

    # Periodic tasks every 5 minutes
    _accept_counter += 1
    periodic = _accept_counter % max(1, 300 // POLL_INTERVAL_SECONDS) == 0

    if periodic:
        check_asana_new_tasks()
        check_asana_deadlines()

    try:
        if ICS_URL:
            events = fetch_events_ics(start, end)
        elif CALDAV_USERNAME and CALDAV_PASSWORD:
            events = fetch_events_caldav(start, end, also_accept=periodic)
        else:
            return
    except Exception as e:
        log.error(f"Failed to fetch events: {e}")
        return

    for event in events:
        for minutes in REMIND_BEFORE_MINUTES:
            notify_time = event["start"] - timedelta(minutes=minutes)

            diff = abs((now - notify_time).total_seconds())
            if diff <= POLL_INTERVAL_SECONDS + 5:
                key = f"{event['uid']}_{event['start'].isoformat()}_{minutes}"
                if key in state["sent"]:
                    continue

                if not is_muted:
                    text = format_notification(event, minutes)
                    if send_telegram(text):
                        _notifications_sent += 1
                        state["sent"][key] = now.isoformat()
                        log.info(
                            f"Sent: '{event['summary']}' "
                            f"({minutes}min before)"
                        )
                else:
                    # Still mark as sent so we don't spam after unmute
                    state["sent"][key] = now.isoformat()
                    log.info(f"Muted, skipped: '{event['summary']}'")

    # Cleanup
    cutoff = (now - timedelta(hours=48)).isoformat()
    state["sent"] = {k: v for k, v in state["sent"].items() if v > cutoff}
    if len(state.get("accepted", [])) > 500:
        state["accepted"] = state["accepted"][-500:]
    save_state(state)


def test_connection():
    """Test calendar and Telegram connections."""
    print("=== Testing Telegram ===")
    if send_telegram("Calendar Notifier: тестовое сообщение"):
        print("Telegram: OK")
    else:
        print("Telegram: FAILED")
        return False

    print("\n=== Testing Calendar ===")
    now = datetime.now(tz.UTC)
    start = now - timedelta(days=1)
    end = now + timedelta(days=7)

    try:
        if ICS_URL:
            events = fetch_events_ics(start, end)
            print(f"ICS: OK, found {len(events)} events in next 7 days")
        elif CALDAV_USERNAME and CALDAV_PASSWORD:
            events = fetch_events_caldav(start, end, also_accept=False)
            print(f"CalDAV: OK, found {len(events)} events in next 7 days")
        else:
            print("ERROR: No calendar source configured in .env")
            return False

        for e in events[:10]:
            local_tz = tz.gettz(TIMEZONE)
            t = e["start"].astimezone(local_tz).strftime("%d.%m %H:%M")
            print(f"  - [{t}] {e['summary']}")

        if len(events) > 10:
            print(f"  ... and {len(events) - 10} more")

    except Exception as e:
        print(f"Calendar: FAILED — {e}")
        import traceback
        traceback.print_exc()
        return False

    print("\n=== Testing Auto-Accept ===")
    try:
        client = get_caldav_client()
        principal = client.principal()
        calendars = principal.calendars()
        print(f"Calendars available: {[c.get_display_name() for c in calendars]}")
        print("Auto-accept: ready")
    except Exception as e:
        print(f"Auto-accept: FAILED — {e}")

    print("\nAll good!")
    return True


def main():
    global _started_at

    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        sys.exit(0 if test_connection() else 1)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        sys.exit(1)

    source = "ICS" if ICS_URL else "CalDAV" if CALDAV_USERNAME else "NONE"
    if source == "NONE":
        log.error("Configure CALDAV or ICS credentials in .env")
        sys.exit(1)

    _started_at = datetime.now(tz.UTC)

    log.info("Mail.ru Calendar Notifier started")
    log.info(f"Source: {source}")
    log.info(f"Remind: {[_human_time_delta(m) for m in REMIND_BEFORE_MINUTES]}")
    log.info(f"Poll: every {POLL_INTERVAL_SECONDS}s")
    log.info(f"TZ: {TIMEZONE}")
    log.info("Auto-accept: enabled")
    log.info(f"Asana: {'enabled' if ASANA_TOKEN else 'disabled'}")
    log.info("Bot commands: enabled")

    # Register bot commands with Telegram
    telegram_api(
        "setMyCommands",
        commands=[
            {"command": "today", "description": "События на сегодня"},
            {"command": "tomorrow", "description": "События на завтра"},
            {"command": "week", "description": "События на неделю"},
            {"command": "tasks", "description": "Мои задачи в Asana"},
            {"command": "mute", "description": "Выключить уведомления (часы)"},
            {"command": "unmute", "description": "Включить уведомления"},
            {"command": "status", "description": "Статус бота"},
        ],
    )

    send_telegram(
        "<b>Calendar Notifier запущен</b>\n\n"
        f"Напоминания: {', '.join(_human_time_delta(m) for m in REMIND_BEFORE_MINUTES)}\n"
        f"Авто-принятие: включено\n"
        f"Asana: {'подключена' if ASANA_TOKEN else 'не настроена'}\n"
        f"Команды: /help"
    )

    # Start bot command listener in background thread
    bot_thread = threading.Thread(target=bot_polling_loop, daemon=True)
    bot_thread.start()
    log.info("Bot command listener started")

    # Main notification loop
    while True:
        try:
            check_and_notify()
        except Exception as e:
            log.error(f"Error in check loop: {e}", exc_info=True)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
