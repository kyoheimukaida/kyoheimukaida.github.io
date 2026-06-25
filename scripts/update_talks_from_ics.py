#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


INDEX_PATH = "index.md"
TALKS_PATH = "talks.md"
HISTORY_PATH = "_data/talks_history.json"

INDEX_START = "<!-- talks:start -->"
INDEX_END = "<!-- talks:end -->"

TALKS_START = "<!-- talks-auto:start -->"
TALKS_END = "<!-- talks-auto:end -->"

TOKYO = ZoneInfo("Asia/Tokyo")

TAGS = {
    "[INVITED]": "Invited talk",
    "[TALK]": "Talk",
    "[SEMINAR]": "Seminar",
    "[LECTURE]": "Lecture",
}


@dataclass
class TalkEvent:
    title: str
    kind: str
    start: datetime | date
    end: datetime | date | None
    location: str
    description: str
    url: str
    event: str = ""
    date_label: str = ""
    source: str = ""


def split_ics_urls(raw: str) -> list[str]:
    urls = re.split(r"[\n,;]+", raw.strip())
    return [u.strip() for u in urls if u.strip()]


def fetch_url(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "kyoheimukaida-github-pages-talks-updater/1.1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def unfold_ics_lines(text: str) -> list[str]:
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    lines: list[str] = []
    for line in raw_lines:
        if not line:
            continue
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)

    return lines


def unescape_ics(value: str) -> str:
    value = value.replace("\\n", "\n").replace("\\N", "\n")
    value = value.replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    return value.strip()


def parse_property(line: str) -> tuple[str, dict[str, str], str]:
    if ":" not in line:
        return line.upper(), {}, ""

    left, value = line.split(":", 1)
    parts = left.split(";")
    name = parts[0].upper()

    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, val = part.split("=", 1)
            params[key.upper()] = val.strip('"')

    return name, params, unescape_ics(value)


def parse_ics_datetime(value: str, params: dict[str, str]) -> datetime | date | None:
    if not value:
        return None

    if params.get("VALUE", "").upper() == "DATE":
        try:
            return datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            return None

    tzid = params.get("TZID", "")
    tz = ZoneInfo(tzid) if tzid else TOKYO

    try:
        if value.endswith("Z"):
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ")
            return dt.replace(tzinfo=timezone.utc).astimezone(TOKYO)

        dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
        return dt.replace(tzinfo=tz).astimezone(TOKYO)
    except ValueError:
        return None


def parse_ics_events(text: str) -> list[dict[str, Any]]:
    lines = unfold_ics_lines(text)

    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
            continue

        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue

        if current is None:
            continue

        name, params, value = parse_property(line)

        if name in {"SUMMARY", "LOCATION", "DESCRIPTION", "URL"}:
            current[name] = value
        elif name in {"DTSTART", "DTEND"}:
            current[name] = parse_ics_datetime(value, params)

    return events


def clean_title_and_kind(summary: str) -> tuple[str, str] | None:
    for tag, kind in TAGS.items():
        if tag in summary:
            title = summary.replace(tag, "").strip(" -:　")
            if title:
                return title, kind

    return None


def extract_url(description: str, explicit_url: str) -> str:
    if explicit_url:
        return explicit_url.strip()

    match = re.search(r"https?://\S+", description)
    if match:
        return match.group(0).rstrip(").,;")

    return ""


def extract_field(description: str, field: str) -> str:
    pattern = re.compile(rf"^{re.escape(field)}:\s*(.+)$", re.MULTILINE)
    match = pattern.search(description)
    if match:
        return match.group(1).strip()
    return ""


def make_talk_events(events: list[dict[str, Any]]) -> list[TalkEvent]:
    talks: list[TalkEvent] = []

    for event in events:
        summary = str(event.get("SUMMARY", "")).strip()
        cleaned = clean_title_and_kind(summary)

        if cleaned is None:
            continue

        title, kind = cleaned

        start = event.get("DTSTART")
        if start is None:
            continue

        description = str(event.get("DESCRIPTION", "")).strip()
        explicit_url = str(event.get("URL", "")).strip()
        event_name = extract_field(description, "Event")

        talks.append(
            TalkEvent(
                title=title,
                kind=kind,
                start=start,
                end=event.get("DTEND"),
                location=str(event.get("LOCATION", "")).strip(),
                description=description,
                url=extract_url(description, explicit_url),
                event=event_name,
                source="public-talk-calendar",
            )
        )

    talks.sort(key=lambda e: start_sort_key(e.start), reverse=True)
    return talks


def parse_history_date(value: str) -> tuple[datetime, str]:
    value = str(value).strip()

    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=TOKYO), value
        except ValueError:
            pass

    try:
        parsed_year = datetime.strptime(value, "%Y")
        return parsed_year.replace(tzinfo=TOKYO), value
    except ValueError:
        return datetime(1900, 1, 1, tzinfo=TOKYO), value


def load_history_talks(path: str = HISTORY_PATH) -> list[TalkEvent]:
    data_path = Path(path)
    if not data_path.exists():
        return []

    records = json.loads(data_path.read_text(encoding="utf-8"))

    talks: list[TalkEvent] = []
    for record in records:
        title = str(record.get("title", "")).strip()
        if not title:
            continue

        start, date_label = parse_history_date(str(record.get("date", "")))

        talks.append(
            TalkEvent(
                title=title,
                kind=str(record.get("kind", "Talk")).strip() or "Talk",
                start=start,
                end=None,
                location=str(record.get("location", "")).strip(),
                description=str(record.get("note", "")).strip(),
                url=str(record.get("url", "")).strip(),
                event=str(record.get("event", "")).strip(),
                date_label=date_label,
                source=str(record.get("source", "history")).strip() or "history",
            )
        )

    return talks


def load_calendar_talks() -> list[TalkEvent]:
    raw_urls = os.environ.get("TALKS_ICS_URLS", "").strip()

    if not raw_urls:
        return []

    all_events: list[dict[str, Any]] = []

    for url in split_ics_urls(raw_urls):
        text = fetch_url(url)
        all_events.extend(parse_ics_events(text))

    return make_talk_events(all_events)


def normalize_for_key(value: str) -> str:
    value = value.lower()
    value = value.replace("&", "and")
    value = re.sub(r"[^a-z0-9ぁ-んァ-ン一-龥]+", "", value)
    return value


def year_month(value: datetime | date) -> str:
    if isinstance(value, datetime):
        dt = value.astimezone(TOKYO)
        return f"{dt.year:04d}-{dt.month:02d}"

    return f"{value.year:04d}-{value.month:02d}"


def dedupe_talks(talks: list[TalkEvent]) -> list[TalkEvent]:
    """
    Prefer calendar records over historical records.

    Historical data can contain multiple talks with the same title in the same month
    at different events, so do not dedupe history-history records by title alone.
    Calendar-history duplicates are deduped by title and year-month because the
    calendar entry is the manually normalized/current source.
    """
    seen_full: set[str] = set()
    seen_calendar_title_month: set[str] = set()
    deduped: list[TalkEvent] = []

    for talk in talks:
        title_key = normalize_for_key(talk.title)[:100]
        event_key = normalize_for_key(talk.event)[:100]
        location_key = normalize_for_key(talk.location)[:100]

        full_key = f"{year_month(talk.start)}::{title_key}::{event_key}::{location_key}"
        title_month_key = f"{year_month(talk.start)}::{title_key}"

        if full_key in seen_full:
            continue

        # If the same title/month already exists in the public calendar,
        # drop the historical seed entry and keep the calendar-normalized one.
        if talk.source != "public-talk-calendar" and title_month_key in seen_calendar_title_month:
            continue

        deduped.append(talk)
        seen_full.add(full_key)

        if talk.source == "public-talk-calendar":
            seen_calendar_title_month.add(title_month_key)

    return deduped


def load_all_talks() -> list[TalkEvent]:
    calendar_talks = load_calendar_talks()
    history_talks = load_history_talks()

    # Calendar first, so calendar-normalized entries win duplicates.
    combined = dedupe_talks(calendar_talks + history_talks)
    combined.sort(key=lambda e: start_sort_key(e.start), reverse=True)
    return combined


def start_sort_key(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(TOKYO)
    return datetime(value.year, value.month, value.day, tzinfo=TOKYO)


def format_date(value: datetime | date, date_label: str = "") -> str:
    if date_label:
        return date_label

    if isinstance(value, datetime):
        return value.astimezone(TOKYO).strftime("%Y-%m-%d")

    return value.strftime("%Y-%m-%d")


def format_talk(event: TalkEvent, include_event: bool = True) -> str:
    date_str = format_date(event.start, event.date_label)

    if event.url:
        title_md = f"**[{event.title}]({event.url})**"
    else:
        title_md = f"**{event.title}**"

    lines = [
        f"- {title_md}  ",
        f"  {event.kind}, {date_str}",
    ]

    details: list[str] = []
    if include_event and event.event:
        details.append(event.event)
    if event.location:
        details.append(event.location)

    if details:
        lines[-1] += ", " + ", ".join(details)

    return "\n".join(lines)


def format_talks_for_index(talks: list[TalkEvent], n: int = 3) -> str:
    now = datetime.now(TOKYO)

    upcoming = [t for t in talks if start_sort_key(t.start) >= now]
    past = [t for t in talks if start_sort_key(t.start) < now]

    upcoming.sort(key=lambda e: start_sort_key(e.start))
    past.sort(key=lambda e: start_sort_key(e.start), reverse=True)

    selected = (upcoming + past)[:n]

    if not selected:
        return "- No public talks found."

    return "\n\n".join(format_talk(t, include_event=False) for t in selected)


def format_talks_for_talks_page(talks: list[TalkEvent], n: int = 80) -> str:
    if not talks:
        return "No public talks found."

    return "\n\n".join(format_talk(t, include_event=True) for t in talks[:n])


def update_block(path: str, start: str, end: str, replacement_body: str) -> None:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    pattern = re.compile(
        rf"{re.escape(start)}.*?{re.escape(end)}",
        flags=re.DOTALL,
    )

    replacement = f"{start}\n{replacement_body}\n{end}"
    new_text, count = pattern.subn(replacement, text)

    if count != 1:
        raise RuntimeError(f"Could not find exactly one block in {path}: {start} ... {end}")

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)


def main() -> int:
    talks = load_all_talks()

    update_block(
        INDEX_PATH,
        INDEX_START,
        INDEX_END,
        format_talks_for_index(talks, n=3),
    )

    update_block(
        TALKS_PATH,
        TALKS_START,
        TALKS_END,
        format_talks_for_talks_page(talks, n=80),
    )

    print(f"Updated talks: {len(talks)} talk record(s) found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
