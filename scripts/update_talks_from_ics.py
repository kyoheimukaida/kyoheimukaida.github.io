#!/usr/bin/env python3

from __future__ import annotations

import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


INDEX_PATH = "index.md"
TALKS_PATH = "talks.md"

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


def split_ics_urls(raw: str) -> list[str]:
    urls = re.split(r"[\n,;]+", raw.strip())
    return [u.strip() for u in urls if u.strip()]


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
            )
        )

    talks.sort(key=lambda e: start_sort_key(e.start), reverse=True)
    return talks


def start_sort_key(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(TOKYO)
    return datetime(value.year, value.month, value.day, tzinfo=TOKYO)


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

    lines = [
        f"- {title_md}  ",
        f"  {event.kind}, {date_str}",
    ]

    if event.location:
        lines[-1] += f", {event.location}"

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

    return "\n\n".join(format_talk(t) for t in selected)


def format_talks_for_talks_page(talks: list[TalkEvent], n: int = 30) -> str:
    if not talks:
        return "No public talks found."

    return "\n\n".join(format_talk(t) for t in talks[:n])


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
    raw_urls = os.environ.get("TALKS_ICS_URLS", "").strip()

    if not raw_urls:
        raise RuntimeError("TALKS_ICS_URLS is not set.")

    all_events: list[dict[str, Any]] = []

    for url in split_ics_urls(raw_urls):
        text = fetch_url(url)
        all_events.extend(parse_ics_events(text))

    talks = make_talk_events(all_events)

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
        format_talks_for_talks_page(talks, n=30),
    )

    print(f"Updated talks: {len(talks)} public talk event(s) found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
