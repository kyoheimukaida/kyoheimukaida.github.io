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
HISTORY_PATH = Path("_data/talks_history.json")

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


@dataclass(frozen=True)
class TalkEvent:
    title: str
    kind: str
    start: datetime | date
    end: datetime | date | None
    location: str = ""
    description: str = ""
    url: str = ""
    event: str = ""
    source: str = ""


def split_ics_urls(raw: str) -> list[str]:
    urls = re.split(r"[\n,;]+", raw.strip())
    return [url.strip() for url in urls if url.strip()]


def fetch_url(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "kyoheimukaida-github-pages-talks-updater/1.0"},
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


def extract_event_name(description: str) -> str:
    for line in description.splitlines():
        if line.lower().startswith("event:"):
            return line.split(":", 1)[1].strip()
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

        talks.append(
            TalkEvent(
                title=title,
                kind=kind,
                start=start,
                end=event.get("DTEND"),
                location=str(event.get("LOCATION", "")).strip(),
                description=description,
                url=extract_url(description, explicit_url),
                event=extract_event_name(description),
                source="calendar",
            )
        )

    return talks


def parse_history_date(value: Any) -> datetime | date | None:
    if value is None:
        return None

    if isinstance(value, int):
        return date(value, 1, 1)

    text = str(value).strip()
    if not text:
        return None

    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        try:
            return datetime.strptime(match.group(0), "%Y-%m-%d").date()
        except ValueError:
            pass

    match = re.search(r"\b(19|20)\d{2}\b", text)
    if match:
        return date(int(match.group(0)), 1, 1)

    return None


def history_item_to_talk(item: dict[str, Any]) -> TalkEvent | None:
    title = (
        item.get("title")
        or item.get("talk_title")
        or item.get("summary")
        or item.get("name")
        or ""
    )
    title = str(title).strip()
    if not title:
        return None

    start = (
        item.get("start")
        or item.get("date")
        or item.get("year")
        or item.get("start_date")
        or item.get("when")
    )
    parsed_start = parse_history_date(start)
    if parsed_start is None:
        return None

    kind = str(item.get("kind") or item.get("type") or "Invited talk").strip()
    if kind.lower() in {"invited", "invited_talk", "invited talk"}:
        kind = "Invited talk"

    location = str(item.get("location") or item.get("place") or "").strip()
    event = str(item.get("event") or item.get("conference") or item.get("workshop") or "").strip()
    url = str(item.get("url") or "").strip()
    description = str(item.get("description") or item.get("notes") or "").strip()

    return TalkEvent(
        title=title,
        kind=kind,
        start=parsed_start,
        end=None,
        location=location,
        description=description,
        url=url,
        event=event,
        source="history",
    )


def load_history_talks(path: Path = HISTORY_PATH) -> list[TalkEvent]:
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if isinstance(payload, dict):
        if isinstance(payload.get("talks"), list):
            records = payload["talks"]
        elif isinstance(payload.get("items"), list):
            records = payload["items"]
        else:
            records = []
    elif isinstance(payload, list):
        records = payload
    else:
        records = []

    talks: list[TalkEvent] = []
    for record in records:
        if isinstance(record, dict):
            talk = history_item_to_talk(record)
            if talk is not None:
                talks.append(talk)

    return talks


def start_sort_key(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(TOKYO)
    return datetime(value.year, value.month, value.day, tzinfo=TOKYO)


def sort_talks_future_first(talks: list[TalkEvent]) -> list[TalkEvent]:
    """
    Sort by date descending.

    This puts the most future talk first, followed by recent past talks
    and then older historical records.
    """
    return sorted(talks, key=lambda event: start_sort_key(event.start), reverse=True)


def normalize_for_dedup(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def deduplicate_talks(talks: list[TalkEvent]) -> list[TalkEvent]:
    """
    Prefer calendar entries over historical entries when they describe the
    same title/date, because calendar entries usually have richer URLs and
    locations.
    """
    ordered = sorted(
        talks,
        key=lambda t: 0 if t.source == "calendar" else 1,
    )

    seen: set[tuple[str, str]] = set()
    result: list[TalkEvent] = []

    for talk in ordered:
        key = (
            normalize_for_dedup(talk.title),
            start_sort_key(talk.start).strftime("%Y-%m-%d"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(talk)

    return sort_talks_future_first(result)


def format_date(value: datetime | date) -> str:
    if isinstance(value, datetime):
        return value.astimezone(TOKYO).strftime("%Y-%m-%d")
    return value.strftime("%Y-%m-%d")


def format_talk(event: TalkEvent) -> str:
    date_str = format_date(event.start)

    if event.url:
        title_md = f"**[{event.title}]({event.url})**"
    else:
        title_md = f"**{event.title}**"

    details: list[str] = [event.kind, date_str]

    if event.event:
        details.append(event.event)

    if event.location:
        details.append(event.location)

    return f"- {title_md}  \n  " + ", ".join(details)


def format_talks_for_index(talks: list[TalkEvent], n: int = 3) -> str:
    selected = sort_talks_future_first(talks)[:n]

    if not selected:
        return "- No public talks found."

    return "\n\n".join(format_talk(talk) for talk in selected)


def format_talks_for_talks_page(talks: list[TalkEvent], n: int = 500) -> str:
    selected = sort_talks_future_first(talks)[:n]

    if not selected:
        return "No public talks found."

    return "\n\n".join(format_talk(talk) for talk in selected)


def update_block(path: str, start: str, end: str, replacement_body: str) -> None:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    pattern = re.compile(
        rf"{re.escape(start)}.*?{re.escape(end)}",
        flags=re.DOTALL,
    )

    replacement = f"{start}\n{replacement_body}\n{end}"
    new_text, count = pattern.subn(lambda _match: replacement, text)

    if count != 1:
        raise RuntimeError(f"Could not find exactly one block in {path}: {start} ... {end}")

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)


def fetch_calendar_talks() -> list[TalkEvent]:
    raw_urls = os.environ.get("TALKS_ICS_URLS", "").strip()

    if not raw_urls:
        return []

    all_events: list[dict[str, Any]] = []
    for url in split_ics_urls(raw_urls):
        text = fetch_url(url)
        all_events.extend(parse_ics_events(text))

    return make_talk_events(all_events)


def main() -> int:
    calendar_talks = fetch_calendar_talks()
    history_talks = load_history_talks()
    talks = deduplicate_talks(calendar_talks + history_talks)

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
        format_talks_for_talks_page(talks, n=500),
    )

    print(
        f"Updated talks: {len(talks)} total "
        f"({len(calendar_talks)} calendar, {len(history_talks)} history)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
