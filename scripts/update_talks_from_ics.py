#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


INDEX_PATH = "index.md"
TALKS_PATH = "talks.md"
HISTORY_PATH = Path("_data/talks_history.json")
TALKS_COMBINED_PATH = Path("_data/talks_combined.json")

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
    date_label: str
    location: str = ""
    description: str = ""
    url: str = ""
    event: str = ""
    source: str = ""


def format_date(value: datetime | date) -> str:
    if isinstance(value, datetime):
        return value.astimezone(TOKYO).strftime("%Y-%m-%d")
    return value.strftime("%Y-%m-%d")


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
                date_label=format_date(start),
                location=str(event.get("LOCATION", "")).strip(),
                description=description,
                url=extract_url(description, explicit_url),
                event=extract_event_name(description),
                source="calendar",
            )
        )

    return talks


def parse_history_date(value: Any) -> tuple[datetime | date, str] | None:
    if value is None:
        return None

    if isinstance(value, int):
        return date(value, 1, 1), f"{value:04d}"

    text = str(value).strip()
    if not text:
        return None

    match = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", text)
    if match:
        try:
            parsed = datetime.strptime(match.group(0), "%Y-%m-%d").date()
            return parsed, match.group(0)
        except ValueError:
            pass

    match = re.search(r"\b((?:19|20)\d{2})-(\d{2})\b", text)
    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        try:
            return date(year, month, 1), f"{year:04d}-{month:02d}"
        except ValueError:
            pass

    match = re.search(r"\b(19|20)\d{2}\b", text)
    if match:
        year = int(match.group(0))
        return date(year, 1, 1), f"{year:04d}"

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
    parsed = parse_history_date(start)
    if parsed is None:
        return None
    parsed_start, date_label = parsed

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
        date_label=date_label,
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


def normalize_text_for_dedup(text: str) -> str:
    text = re.sub(r"\[[^\]]+\]", " ", text)
    text = text.lower().replace("&", " and ")
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_dedup_token(token: str) -> str:
    aliases = {
        "cosmo": "cosmological",
        "cosmology": "cosmological",
        "colliders": "collider",
        "correlators": "correlator",
        "rules": "rule",
        "signals": "signal",
    }
    if token in aliases:
        return aliases[token]

    if len(token) > 4 and token.endswith("s"):
        return token[:-1]

    return token


def dedup_tokens(text: str) -> set[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "at",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
    return {
        canonical_dedup_token(token)
        for token in normalize_text_for_dedup(text).split()
        if token and token not in stopwords
    }


def token_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0

    return len(left & right) / len(left | right)


def date_label_for_dedup(talk: TalkEvent) -> str:
    return talk.date_label or format_date(talk.start)


def dates_compatible(left: TalkEvent, right: TalkEvent) -> bool:
    left_label = date_label_for_dedup(left)
    right_label = date_label_for_dedup(right)

    if left_label == right_label:
        return True

    if len(left_label) == 10 and len(right_label) == 7:
        return left_label.startswith(right_label + "-")

    if len(left_label) == 7 and len(right_label) == 10:
        return right_label.startswith(left_label + "-")

    if len(left_label) == 10 and len(right_label) == 10:
        return abs((start_sort_key(left.start) - start_sort_key(right.start)).days) <= 7

    return False


def title_similarity(left: TalkEvent, right: TalkEvent) -> float:
    return token_similarity(dedup_tokens(left.title), dedup_tokens(right.title))


def context_tokens(talk: TalkEvent) -> set[str]:
    generic = {
        "campus",
        "center",
        "centre",
        "conference",
        "cosmological",
        "event",
        "forum",
        "hall",
        "institute",
        "italy",
        "japan",
        "korea",
        "meeting",
        "netherlands",
        "physics",
        "spain",
        "switzerland",
        "symposium",
        "talk",
        "taiwan",
        "university",
        "workshop",
    }
    tokens = dedup_tokens(" ".join([talk.event, talk.location]))
    return {token for token in tokens if token not in generic}


def contexts_compatible(left: TalkEvent, right: TalkEvent) -> bool:
    left_context = normalize_text_for_dedup(" ".join([left.event, left.location]))
    right_context = normalize_text_for_dedup(" ".join([right.event, right.location]))

    if left_context and right_context and (
        left_context in right_context or right_context in left_context
    ):
        return True

    left_tokens = context_tokens(left)
    right_tokens = context_tokens(right)
    if not left_tokens or not right_tokens:
        return False

    return bool(left_tokens & right_tokens)


def same_talk(left: TalkEvent, right: TalkEvent) -> bool:
    if not dates_compatible(left, right):
        return False

    title_score = title_similarity(left, right)
    left_label = date_label_for_dedup(left)
    right_label = date_label_for_dedup(right)
    precise_date_match = len(left_label) == 10 and len(right_label) == 10

    if title_score >= 0.86 and (precise_date_match or contexts_compatible(left, right)):
        return True

    return title_score >= 0.58 and contexts_compatible(left, right)


def merge_duplicate_talk(preferred: TalkEvent, duplicate: TalkEvent) -> TalkEvent:
    return replace(
        preferred,
        event=preferred.event or duplicate.event,
        location=preferred.location or duplicate.location,
        description=preferred.description or duplicate.description,
        url=preferred.url or duplicate.url,
    )


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

    result: list[TalkEvent] = []

    for talk in ordered:
        duplicate_index = next(
            (index for index, existing in enumerate(result) if same_talk(talk, existing)),
            None,
        )
        if duplicate_index is not None:
            result[duplicate_index] = merge_duplicate_talk(result[duplicate_index], talk)
            continue
        result.append(talk)

    return sort_talks_future_first(result)


def format_talk(event: TalkEvent) -> str:
    date_str = event.date_label or format_date(event.start)

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


def load_cached_calendar_talks(path: Path = TALKS_COMBINED_PATH) -> list[TalkEvent]:
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, list):
        return []

    talks: list[TalkEvent] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("source") != "calendar":
            continue

        title = str(item.get("title") or "").strip()
        if not title:
            continue

        parsed = parse_history_date(item.get("date"))
        if parsed is None:
            continue

        parsed_start, date_label = parsed
        talks.append(
            TalkEvent(
                title=title,
                kind=str(item.get("kind") or "Invited talk").strip(),
                start=parsed_start,
                end=None,
                date_label=date_label,
                location=str(item.get("location") or "").strip(),
                url=str(item.get("url") or "").strip(),
                event=str(item.get("event") or "").strip(),
                source="calendar",
            )
        )

    return talks


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
        return load_cached_calendar_talks()

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
