#!/usr/bin/env python3

from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import sys
import unicodedata
import urllib.request
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo


INDEX_PATH = "index.md"
TALKS_PATH = "talks.md"
HISTORY_PATH = Path("_data/talks_history.json")
TALKS_COMBINED_PATH = Path("_data/talks_combined.json")
TALK_ASSETS_PATH = Path("_data/talk_assets.json")

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

URL_PATTERN = re.compile(r"https?://[^\s<>'\"`]+", flags=re.IGNORECASE)
ASSET_DATE_PATTERN = re.compile(r"\d{4}(?:-\d{2}(?:-\d{2})?)?")
ALLOWED_ASSET_TYPES = {"slides", "video", "recording", "poster", "notes"}


class PlainTextHTMLParser(HTMLParser):
    BLOCK_TAGS = {
        "article",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "ol",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
    IGNORED_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.ignored_depth = 0

    def add_newline(self) -> None:
        if self.chunks and not self.chunks[-1].endswith("\n"):
            self.chunks.append("\n")

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()
        if tag in self.IGNORED_TAGS:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return

        if tag == "br":
            self.add_newline()
        elif tag in self.BLOCK_TAGS:
            self.add_newline()
        elif tag == "a":
            href = dict(attrs).get("href")
            if href and re.match(r"https?://", href.strip(), flags=re.IGNORECASE):
                self.chunks.append(f" {href.strip()} ")

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.IGNORED_TAGS:
            if self.ignored_depth:
                self.ignored_depth -= 1
            return
        if self.ignored_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.add_newline()

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.chunks.append(data)

    def text(self) -> str:
        return "".join(self.chunks)


@dataclass(frozen=True)
class TalkAsset:
    type: str
    label: str
    url: str
    candidate_id: str = ""
    sha256: str = ""
    approved_at: str = ""


@dataclass(frozen=True)
class TalkAssetManifestEntry:
    date_label: str
    title: str
    event: str
    aliases: tuple[str, ...]
    assets: tuple[TalkAsset, ...]


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
    assets: tuple[TalkAsset, ...] = ()


def format_date(value: datetime | date) -> str:
    if isinstance(value, datetime):
        return value.astimezone(TOKYO).strftime("%Y-%m-%d")
    return value.strftime("%Y-%m-%d")


def split_ics_urls(raw: str) -> list[str]:
    urls = re.split(r"[\n,;]+", raw.strip())
    return [url.strip() for url in urls if url.strip()]


def configured_ics_feed_urls() -> list[str]:
    return split_ics_urls(os.environ.get("TALKS_ICS_URLS", ""))


def redact_configured_ics_feed_urls(value: str) -> str:
    for private_url in configured_ics_feed_urls():
        value = value.replace(private_url, "")
        value = value.replace(html.unescape(private_url), "")
    return value


def fetch_url(url: str) -> str:
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "kyoheimukaida-github-pages-talks-updater/1.0"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception:
        raise RuntimeError("Failed to fetch a configured talks ICS feed.") from None


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


def sanitize_ics_text(value: str) -> str:
    decoded = redact_configured_ics_feed_urls(value)
    for _ in range(3):
        unescaped = html.unescape(decoded)
        if unescaped == decoded:
            break
        decoded = unescaped
    decoded = redact_configured_ics_feed_urls(decoded)

    parser = PlainTextHTMLParser()
    parser.feed(decoded)
    parser.close()

    text = redact_configured_ics_feed_urls(parser.text())
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\N{NO-BREAK SPACE}", " ")
    text = re.sub(r"[\t\f\v ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
            current[name] = sanitize_ics_text(value)
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


def strip_url_punctuation(candidate: str) -> str:
    candidate = candidate.rstrip(".,;:!?")
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        while (
            candidate.endswith(closing)
            and candidate.count(closing) > candidate.count(opening)
        ):
            candidate = candidate[:-1]
    return candidate


def is_configured_ics_feed_url(candidate: str) -> bool:
    normalized_candidate = candidate.rstrip("/")
    return any(
        normalized_candidate == url.rstrip("/")
        for url in configured_ics_feed_urls()
    )


def contains_configured_ics_feed_url(value: str) -> bool:
    decoded_values = {value, html.unescape(value), unquote(html.unescape(value))}
    return any(
        private_url and private_url in decoded
        for private_url in configured_ics_feed_urls()
        for decoded in decoded_values
    )


def looks_like_local_path(value: str) -> bool:
    return bool(
        re.match(
            r"^(?:/|~/|\$HOME/|\\\\|[A-Za-z]:[\\/]|file:)",
            value.strip(),
            flags=re.IGNORECASE,
        )
    )


def validate_http_url(candidate: str) -> str:
    candidate = strip_url_punctuation(candidate.strip())
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {"http", "https"}:
            return ""
        if not parsed.netloc or parsed.hostname is None:
            return ""
    except ValueError:
        return ""

    if is_configured_ics_feed_url(candidate):
        return ""
    return candidate


def normalize_text_for_asset_match(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(
        r"\[(?:INVITED|TALK|SEMINAR|LECTURE)\]",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = value.lower().replace("&", " and ")
    normalized: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        normalized.append(" " if category.startswith(("P", "S")) else character)
    return re.sub(r"\s+", " ", "".join(normalized)).strip()


def validate_asset_date_label(value: str) -> str:
    value = value.strip()
    if not ASSET_DATE_PATTERN.fullmatch(value):
        raise RuntimeError(
            "Talk asset date must use YYYY, YYYY-MM, or YYYY-MM-DD."
        )

    try:
        if len(value) == 4:
            date(int(value), 1, 1)
        elif len(value) == 7:
            datetime.strptime(value, "%Y-%m")
        else:
            datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise RuntimeError("Talk asset date is not a valid calendar date.") from None
    return value


def date_labels_compatible_for_assets(left: str, right: str) -> bool:
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) not in {4, 7}:
        return False
    return longer.startswith(shorter + "-")


def validate_public_asset_url(raw_url: str) -> str:
    url = raw_url.strip()
    if not url:
        return ""
    if any(
        character.isspace()
        or ord(character) < 32
        or character in "<>\"'`\\"
        for character in url
    ):
        raise ValueError("URL contains unsafe characters")

    if url.startswith("/assets/talk-slides/"):
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise ValueError("URL could not be parsed") from None
        path_parts = Path(unquote(parsed.path)).parts
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.path.startswith("//")
            or ".." in path_parts
            or not parsed.path.lower().endswith(".pdf")
            or not re.fullmatch(
                r"/assets/talk-slides/[a-z0-9][a-z0-9.-]*\.pdf",
                parsed.path,
            )
        ):
            raise ValueError(
                "repository slide URL must be a clean /assets/talk-slides/*.pdf path"
            )
        return url

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _port = parsed.port
    except ValueError:
        raise ValueError("URL could not be parsed") from None

    if parsed.scheme.lower() != "https" or not parsed.netloc or not hostname:
        raise ValueError("URL must be an absolute https:// URL")
    if parsed.username or parsed.password:
        raise ValueError("URL must not contain embedded credentials")
    if is_configured_ics_feed_url(url) or contains_configured_ics_feed_url(url):
        raise ValueError("URL must not be the configured private ICS feed")

    hostname = hostname.rstrip(".").lower()
    if (
        hostname == "localhost"
        or hostname.endswith((".localhost", ".local", ".lan", ".internal"))
        or "." not in hostname
    ):
        raise ValueError("URL must not reference localhost or a LAN host")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("URL must not reference a private or local address")

    lower_url = url.lower()
    lower_path = parsed.path.lower()
    if "/calendar/ical/" in lower_url or lower_path.endswith(".ics"):
        raise ValueError("URL must not be an ICS feed")
    if hostname.endswith("dropbox.com") and (
        "/scl/fo/" in lower_path
        or lower_path.startswith("/sh/")
        or lower_path.startswith("/home/")
    ):
        raise ValueError("Dropbox folder links are not allowed")

    return url


def extract_url(description: str, explicit_url: str) -> str:
    for raw_value in (explicit_url, description):
        plain_text = sanitize_ics_text(raw_value)
        for match in URL_PATTERN.finditer(plain_text):
            candidate = validate_http_url(match.group(0))
            if candidate:
                return candidate

    return ""


def extract_event_name(description: str) -> str:
    for line in sanitize_ics_text(description).splitlines():
        if line.lower().startswith("event:"):
            return line.split(":", 1)[1].strip()
    return ""


def make_talk_events(events: list[dict[str, Any]]) -> list[TalkEvent]:
    talks: list[TalkEvent] = []

    for event in events:
        summary = sanitize_ics_text(str(event.get("SUMMARY", "")))
        cleaned = clean_title_and_kind(summary)

        if cleaned is None:
            continue

        title, kind = cleaned

        start = event.get("DTSTART")
        if start is None:
            continue

        description = sanitize_ics_text(str(event.get("DESCRIPTION", "")))
        explicit_url = sanitize_ics_text(str(event.get("URL", "")))

        talks.append(
            TalkEvent(
                title=title,
                kind=kind,
                start=start,
                end=event.get("DTEND"),
                date_label=format_date(start),
                location=sanitize_ics_text(str(event.get("LOCATION", ""))),
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
    url = extract_url("", str(item.get("url") or ""))
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


def normalize_url_for_dedup(url: str) -> str:
    return re.sub(r"/+$", "", url.strip().lower())


def duplicate_sanity_fields(talk: TalkEvent) -> tuple[str, str, str, str, str]:
    return (
        date_label_for_dedup(talk),
        normalize_text_for_dedup(talk.title),
        normalize_text_for_dedup(talk.event),
        normalize_text_for_dedup(talk.location),
        normalize_url_for_dedup(talk.url),
    )


def duplicate_sanity_match(left: TalkEvent, right: TalkEvent) -> bool:
    """Use date, title, context, and URL evidence to flag residual duplicates."""
    if duplicate_sanity_fields(left) == duplicate_sanity_fields(right):
        return True

    if same_talk(left, right):
        return True

    if not dates_compatible(left, right):
        return False

    left_url = normalize_url_for_dedup(left.url)
    right_url = normalize_url_for_dedup(right.url)
    return bool(
        left_url
        and left_url == right_url
        and title_similarity(left, right) >= 0.58
    )


def validate_no_duplicate_talks(talks: list[TalkEvent]) -> None:
    for index, left in enumerate(talks):
        for right in talks[index + 1 :]:
            if duplicate_sanity_match(left, right):
                raise RuntimeError(
                    "Duplicate generated talks remain after deduplication: "
                    f"{date_label_for_dedup(left)} {left.title!r} and "
                    f"{date_label_for_dedup(right)} {right.title!r}."
                )


def warn_talk_asset(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def load_talk_asset_manifest(
    path: Path = TALK_ASSETS_PATH,
) -> list[TalkAssetManifestEntry]:
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Invalid JSON in {path}: line {error.lineno}, column {error.colno}."
        ) from None

    if not isinstance(payload, list):
        raise RuntimeError(f"{path} must contain a JSON array.")

    entries: list[TalkAssetManifestEntry] = []
    forbidden_path_keys = {"local_path", "path", "source_path"}
    allowed_entry_keys = {"date", "title", "event", "aliases", "assets"}
    allowed_asset_keys = {
        "type",
        "label",
        "url",
        "public",
        "candidate_id",
        "sha256",
        "approved_at",
    }

    for entry_index, raw_entry in enumerate(payload, start=1):
        context = f"Talk asset entry {entry_index}"
        if not isinstance(raw_entry, dict):
            raise RuntimeError(f"{context} must be a JSON object.")
        if forbidden_path_keys & set(raw_entry):
            raise RuntimeError(f"{context} must not contain a local path field.")
        if set(raw_entry) - allowed_entry_keys:
            raise RuntimeError(f"{context} contains an unsupported field.")

        raw_date = raw_entry.get("date")
        raw_title = raw_entry.get("title")
        if not isinstance(raw_date, str) or not isinstance(raw_title, str):
            raise RuntimeError(f"{context} requires string date and title fields.")
        date_label = validate_asset_date_label(raw_date)
        title = raw_title.strip()
        if not title:
            raise RuntimeError(f"{context} title must not be empty.")
        if looks_like_local_path(title) or contains_configured_ics_feed_url(title):
            raise RuntimeError(f"{context} title contains private local data.")

        raw_event = raw_entry.get("event", "")
        if raw_event is None:
            raw_event = ""
        if not isinstance(raw_event, str):
            raise RuntimeError(f"{context} event must be a string.")
        if (
            looks_like_local_path(raw_event)
            or contains_configured_ics_feed_url(raw_event)
        ):
            raise RuntimeError(f"{context} event contains private local data.")

        raw_aliases = raw_entry.get("aliases", [])
        if not isinstance(raw_aliases, list) or not all(
            isinstance(alias, str) for alias in raw_aliases
        ):
            raise RuntimeError(f"{context} aliases must be an array of strings.")
        aliases = tuple(alias.strip() for alias in raw_aliases if alias.strip())
        if any(
            looks_like_local_path(alias) or contains_configured_ics_feed_url(alias)
            for alias in aliases
        ):
            raise RuntimeError(f"{context} alias contains private local data.")

        raw_assets = raw_entry.get("assets", [])
        if not isinstance(raw_assets, list):
            raise RuntimeError(f"{context} assets must be a JSON array.")

        public_assets: list[TalkAsset] = []
        seen_assets: set[tuple[str, str, str]] = set()
        for asset_index, raw_asset in enumerate(raw_assets, start=1):
            asset_context = f"{context}, asset {asset_index}"
            if not isinstance(raw_asset, dict):
                raise RuntimeError(f"{asset_context} must be a JSON object.")
            if forbidden_path_keys & set(raw_asset):
                raise RuntimeError(
                    f"{asset_context} must not contain a local path field."
                )
            if set(raw_asset) - allowed_asset_keys:
                raise RuntimeError(f"{asset_context} contains an unsupported field.")

            asset_type = raw_asset.get("type")
            if not isinstance(asset_type, str) or asset_type not in ALLOWED_ASSET_TYPES:
                allowed = ", ".join(sorted(ALLOWED_ASSET_TYPES))
                raise RuntimeError(
                    f"{asset_context} type must be one of: {allowed}."
                )

            label = raw_asset.get("label", "")
            url = raw_asset.get("url", "")
            public = raw_asset.get("public", False)
            if not isinstance(label, str) or not isinstance(url, str):
                raise RuntimeError(f"{asset_context} label and url must be strings.")
            if not isinstance(public, bool):
                raise RuntimeError(f"{asset_context} public must be true or false.")
            if looks_like_local_path(label) or contains_configured_ics_feed_url(label):
                raise RuntimeError(
                    f"{asset_context} label contains private local data."
                )

            validated_url = ""
            if url.strip():
                try:
                    validated_url = validate_public_asset_url(url)
                except ValueError as error:
                    raise RuntimeError(f"{asset_context}: {error}.") from None

            if not public:
                continue
            if not validated_url:
                warn_talk_asset(
                    f"{asset_context} is public but has no URL; it will not be shown."
                )
                continue
            if not label.strip():
                raise RuntimeError(f"{asset_context} public label must not be empty.")

            candidate_id = raw_asset.get("candidate_id", "")
            sha256 = raw_asset.get("sha256", "")
            approved_at = raw_asset.get("approved_at", "")
            if not all(
                isinstance(value, str)
                for value in (candidate_id, sha256, approved_at)
            ):
                raise RuntimeError(
                    f"{asset_context} candidate_id, sha256, and approved_at "
                    "must be strings when present."
                )
            if candidate_id and not re.fullmatch(
                r"TA-\d{4}(?:-\d{2}(?:-\d{2})?)?-[A-F0-9]{4,16}",
                candidate_id,
            ):
                raise RuntimeError(f"{asset_context} candidate_id is invalid.")
            if sha256 and not re.fullmatch(r"[a-f0-9]{64}", sha256):
                raise RuntimeError(f"{asset_context} sha256 is invalid.")
            if approved_at:
                try:
                    datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
                except ValueError:
                    raise RuntimeError(
                        f"{asset_context} approved_at is not ISO 8601."
                    ) from None

            key = (asset_type, label.strip(), validated_url)
            if key in seen_assets:
                raise RuntimeError(f"{asset_context} duplicates another public asset.")
            seen_assets.add(key)
            public_assets.append(
                TalkAsset(
                    type=asset_type,
                    label=label.strip(),
                    url=validated_url,
                    candidate_id=candidate_id,
                    sha256=sha256,
                    approved_at=approved_at,
                )
            )

        entries.append(
            TalkAssetManifestEntry(
                date_label=date_label,
                title=title,
                event=raw_event.strip(),
                aliases=aliases,
                assets=tuple(public_assets),
            )
        )

    return entries


def require_unique_asset_match(
    candidates: list[int],
    entry: TalkAssetManifestEntry,
    rule: str,
) -> int | None:
    if len(candidates) > 1:
        raise RuntimeError(
            "Ambiguous talk asset match for "
            f"{entry.date_label} {entry.title!r} using {rule}: "
            f"matched {len(candidates)} talk records."
        )
    return candidates[0] if candidates else None


def match_talk_asset_entry(
    entry: TalkAssetManifestEntry,
    talks: list[TalkEvent],
) -> int | None:
    normalized_title = normalize_text_for_asset_match(entry.title)

    if len(entry.date_label) == 10:
        exact_title = [
            index
            for index, talk in enumerate(talks)
            if date_label_for_dedup(talk) == entry.date_label
            and normalize_text_for_asset_match(talk.title) == normalized_title
        ]
        match = require_unique_asset_match(
            exact_title,
            entry,
            "exact full date and exact title",
        )
        if match is not None:
            return match

        normalized_aliases = {
            normalize_text_for_asset_match(alias) for alias in entry.aliases
        }
        exact_alias = [
            index
            for index, talk in enumerate(talks)
            if date_label_for_dedup(talk) == entry.date_label
            and normalize_text_for_asset_match(talk.title) in normalized_aliases
        ]
        match = require_unique_asset_match(
            exact_alias,
            entry,
            "exact full date and explicit alias",
        )
        if match is not None:
            return match

    compatible = [
        index
        for index, talk in enumerate(talks)
        if date_labels_compatible_for_assets(
            entry.date_label,
            date_label_for_dedup(talk),
        )
        and normalize_text_for_asset_match(talk.title) == normalized_title
    ]
    if len(compatible) > 1 and entry.event:
        normalized_event = normalize_text_for_asset_match(entry.event)
        event_matches = [
            index
            for index in compatible
            if normalize_text_for_asset_match(talks[index].event) == normalized_event
        ]
        if event_matches:
            compatible = event_matches

    return require_unique_asset_match(
        compatible,
        entry,
        "compatible date label and exact title",
    )


def attach_public_talk_assets(
    talks: list[TalkEvent],
    entries: list[TalkAssetManifestEntry],
) -> list[TalkEvent]:
    result = list(talks)
    matched_talks: dict[int, TalkAssetManifestEntry] = {}

    for entry in entries:
        if not entry.assets:
            continue
        match_index = match_talk_asset_entry(entry, result)
        if match_index is None:
            warn_talk_asset(
                "approved asset did not match a talk record: "
                f"{entry.date_label} {entry.title!r}."
            )
            continue
        if match_index in matched_talks:
            previous = matched_talks[match_index]
            raise RuntimeError(
                "Multiple manifest entries target the same talk: "
                f"{previous.date_label} {previous.title!r} and "
                f"{entry.date_label} {entry.title!r}. "
                "Combine their assets into one manifest entry."
            )
        matched_talks[match_index] = entry
        result[match_index] = replace(
            result[match_index],
            assets=entry.assets,
        )

    return result


def escape_asset_label(label: str) -> str:
    label = re.sub(r"\s+", " ", label).strip()
    label = html.escape(label, quote=True)
    return label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def format_talk_asset(asset: TalkAsset) -> str:
    safe_url = asset.url.replace("(", "%28").replace(")", "%29")
    return f"[{escape_asset_label(asset.label)}]({safe_url})"


def format_talk(
    event: TalkEvent,
    *,
    include_assets: bool = False,
) -> str:
    date_str = event.date_label or format_date(event.start)
    title = redact_configured_ics_feed_urls(event.title)
    url = validate_http_url(event.url)

    if url:
        title_md = f"**[{title}]({url})**"
    else:
        title_md = f"**{title}**"

    details: list[str] = [event.kind, date_str]

    if event.event:
        details.append(redact_configured_ics_feed_urls(event.event))

    if event.location:
        details.append(redact_configured_ics_feed_urls(event.location))

    lines = [f"- {title_md}<br>", "  " + ", ".join(details)]
    if include_assets and event.assets:
        lines[-1] += "<br>"
        links = " · ".join(format_talk_asset(asset) for asset in event.assets)
        lines.append("  " + links)
    return "\n".join(lines)


def format_talks_for_index(talks: list[TalkEvent], n: int = 3) -> str:
    selected = sort_talks_future_first(talks)[:n]

    if not selected:
        return "- No public talks found."

    return "\n\n".join(
        format_talk(talk, include_assets=False) for talk in selected
    )


def format_talks_for_talks_page(talks: list[TalkEvent], n: int = 500) -> str:
    selected = sort_talks_future_first(talks)[:n]

    if not selected:
        return "No public talks found."

    return "\n\n".join(
        format_talk(talk, include_assets=True) for talk in selected
    )


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
                url=extract_url("", str(item.get("url") or "")),
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
    validate_no_duplicate_talks(talks)
    talks = attach_public_talk_assets(talks, load_talk_asset_manifest())

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
