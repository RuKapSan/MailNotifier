#!/usr/bin/env python3
"""Mail.ru Calendar → Telegram Notifier.

Features:
- Fetches events via CalDAV or ICS
- Auto-accepts event invitations (PARTSTAT → ACCEPTED)
- Sends Telegram reminders (1 day before, 1 hour before)
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
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


# --- State ---

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {"sent": {}, "accepted": []}
    return {"sent": {}, "accepted": []}


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


# --- Auto-accept invitations ---

def accept_pending_events(calendars: list):
    """Find events where user is invited but hasn't accepted, and accept them."""
    state = load_state()
    now = datetime.now(tz.UTC)
    # Look ahead 30 days for events to accept
    start = now - timedelta(hours=1)
    end = now + timedelta(days=30)

    for cal in calendars:
        try:
            results = cal.search(start=start, end=end, expand=False, event=True)
        except Exception as e:
            log.warning(f"Error searching calendar '{cal.name}' for acceptance: {e}")
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

                    # Check attendees for our email
                    attendees = component.get("attendee")
                    if attendees is None:
                        continue

                    # attendee can be a single value or a list
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
        # Replace PARTSTAT=NEEDS-ACTION with PARTSTAT=ACCEPTED for our email
        modified = ical_data
        # Handle the case where our email appears in ATTENDEE line
        username_lower = CALDAV_USERNAME.lower()

        # Strategy: find ATTENDEE lines containing our email and change PARTSTAT
        lines = modified.split("\n")
        new_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            # Collect continuation lines
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
            log.warning(f"Error fetching calendar '{cal.name}': {e}")

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

    # Handle all-day events (date without time)
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


# --- Main loop ---

_accept_counter = 0


def check_and_notify():
    """Single check iteration: fetch events, send notifications."""
    global _accept_counter
    state = load_state()
    now = datetime.now(tz.UTC)

    # Look ahead window
    max_remind = max(REMIND_BEFORE_MINUTES) + 1
    start = now - timedelta(minutes=2)
    end = now + timedelta(minutes=max_remind + 5)

    # Accept invitations every 5 minutes (not every poll)
    _accept_counter += 1
    do_accept = _accept_counter % max(1, 300 // POLL_INTERVAL_SECONDS) == 0

    try:
        if ICS_URL:
            events = fetch_events_ics(start, end)
        elif CALDAV_USERNAME and CALDAV_PASSWORD:
            events = fetch_events_caldav(start, end, also_accept=do_accept)
        else:
            log.error(
                "No calendar source configured! "
                "Set CALDAV_USERNAME+CALDAV_PASSWORD or ICS_URL in .env"
            )
            return
    except Exception as e:
        log.error(f"Failed to fetch events: {e}")
        return

    for event in events:
        for minutes in REMIND_BEFORE_MINUTES:
            notify_time = event["start"] - timedelta(minutes=minutes)

            # Check if it's time to notify (within polling window)
            diff = abs((now - notify_time).total_seconds())
            if diff <= POLL_INTERVAL_SECONDS + 5:
                # Unique key to avoid duplicate notifications
                key = f"{event['uid']}_{event['start'].isoformat()}_{minutes}"
                if key in state["sent"]:
                    continue

                text = format_notification(event, minutes)
                if send_telegram(text):
                    state["sent"][key] = now.isoformat()
                    log.info(
                        f"Sent: '{event['summary']}' "
                        f"({minutes}min before, {event['start'].isoformat()})"
                    )

    # Cleanup entries older than 48h
    cutoff = (now - timedelta(hours=48)).isoformat()
    state["sent"] = {k: v for k, v in state["sent"].items() if v > cutoff}
    # Trim accepted list to last 500 entries
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
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        sys.exit(0 if test_connection() else 1)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        sys.exit(1)

    source = "ICS" if ICS_URL else "CalDAV" if CALDAV_USERNAME else "NONE"
    if source == "NONE":
        log.error("Configure CALDAV or ICS credentials in .env")
        sys.exit(1)

    log.info("Mail.ru Calendar Notifier started")
    log.info(f"Source: {source}")
    log.info(f"Remind: {[_human_time_delta(m) for m in REMIND_BEFORE_MINUTES]}")
    log.info(f"Poll: every {POLL_INTERVAL_SECONDS}s")
    log.info(f"TZ: {TIMEZONE}")
    log.info(f"Auto-accept: enabled")

    send_telegram(
        "<b>Calendar Notifier запущен</b>\n\n"
        f"Напоминания: {', '.join(_human_time_delta(m) for m in REMIND_BEFORE_MINUTES)}\n"
        f"Авто-принятие: включено"
    )

    while True:
        try:
            check_and_notify()
        except Exception as e:
            log.error(f"Error in check loop: {e}", exc_info=True)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
