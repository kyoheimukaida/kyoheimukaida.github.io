#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date, datetime
from typing import Any


CV_PATH = "cv.md"
INSPIRE_QUERY = os.environ.get("INSPIRE_QUERY", "a K.Mukaida.1")

SUMMARY_START = "<!-- cv-publication-summary:start -->"
SUMMARY_END = "<!-- cv-publication-summary:end -->"

RECENT_PUBLICATIONS_START = "<!-- cv-recent-publications:start -->"
RECENT_PUBLICATIONS_END = "<!-- cv-recent-publications:end -->"

RECENT_TALKS_START = "<!-- cv-recent-talks:start -->"
RECENT_TALKS_END = "<!-- cv-recent-talks:end -->"


def fetch_inspire(size: int = 20) -> dict[str, Any]:
    params = {
        "q": INSPIRE_QUERY,
        "sort": "mostrecent",
        "size": str(size),
    }

    url = "https://inspirehep.net/api/literature?" + urllib.parse.urlencode(params)

    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def get_total_count(payload: dict[str, Any]) -> int | None:
    total = payload.get("hits", {}).get("total")

    if isinstance(total, dict):
        value = total.get("value")
        if isinstance(value, int):
            return value

    if isinstance(total, int):
        return total

    return None


def get_recent_papers(payload: dict[str, Any], n: int = 5) -> list[dict[str, Any]]:
    hits = payload.get("hits", {}).get("hits", [])

    papers: list[dict[str, Any]] = []

    for hit in hits:
        meta = hit.get("metadata", {})

        title = get_title(meta)
        arxiv = get_arxiv(meta)

        if not title or not arxiv:
            continue

        papers.append(meta)

        if len(papers) >= n:
            break

    return papers


def get_title(meta: dict[str, Any]) -> str:
    titles = meta.get("titles", [])
    if not titles:
        return ""

    return str(titles[0].get("title", "")).strip()


def format_author_name(author: dict[str, Any]) -> str:
    first = (
        author.get("first_name")
        or author.get("given_name")
        or author.get("given_names")
        or ""
    ).strip()

    last = (
        author.get("last_name")
        or author.get("family_name")
        or author.get("family_names")
        or ""
    ).strip()

    if first and last:
        return f"{first} {last}"

    full_name = (author.get("full_name") or "").strip()

    # INSPIRE often returns names as "Family, Given".
    if "," in full_name:
        family, given = [part.strip() for part in full_name.split(",", 1)]
        if given and family:
            return f"{given} {family}"

    return full_name


def get_authors(meta: dict[str, Any]) -> str:
    authors = [format_author_name(a) for a in meta.get("authors", [])]
    authors = [a for a in authors if a]

    if not authors:
        return ""

    if len(authors) == 1:
        return authors[0]

    if len(authors) <= 4:
        return ", ".join(authors[:-1]) + ", and " + authors[-1]

    return ", ".join(authors[:3]) + ", et al."


def get_arxiv(meta: dict[str, Any]) -> str:
    eprints = meta.get("arxiv_eprints", [])
    if not eprints:
        return ""

    return str(eprints[0].get("value", "")).strip()


def get_doi(meta: dict[str, Any]) -> str:
    dois = meta.get("dois", [])
    if not dois:
        return ""

    return str(dois[0].get("value", "")).strip()


def get_journal_line(meta: dict[str, Any]) -> str:
    info = meta.get("publication_info", [])

    if info:
        item = info[0]

        journal = str(item.get("journal_title", "")).strip()
        volume = str(item.get("journal_volume", "")).strip()
        artid = str(item.get("artid") or item.get("page_start") or "").strip()
        year = str(item.get("year", "")).strip()

        parts: list[str] = []

        if journal:
            parts.append(journal)
        if volume:
            parts.append(volume)
        if artid:
            parts.append(artid)

        line = " ".join(parts)

        if year:
            line += f" ({year})"

        if line.strip():
            return line.strip()

    earliest_date = str(meta.get("earliest_date", "")).strip()
    if earliest_date:
        return f"arXiv preprint ({earliest_date})"

    return "arXiv preprint"


def format_paper(meta: dict[str, Any]) -> str:
    title = get_title(meta)
    authors = get_authors(meta)
    arxiv = get_arxiv(meta)
    doi = get_doi(meta)
    journal_line = get_journal_line(meta)

    arxiv_url = f"https://arxiv.org/abs/{arxiv}"

    lines = [
        f"- **[{title}]({arxiv_url})**  ",
    ]

    if authors:
        lines.append(f"  {authors}  ")

    lines.append(f"  *{journal_line}*  ")

    links = [f"[[arXiv](https://arxiv.org/abs/{arxiv})]"]

    if doi:
        links.append(f"[[DOI](https://doi.org/{doi})]")

    lines.append("  " + " ".join(links))

    return "\n".join(lines)


def update_block(text: str, start: str, end: str, replacement_body: str) -> str:
    pattern = re.compile(
        rf"{re.escape(start)}.*?{re.escape(end)}",
        flags=re.DOTALL,
    )

    replacement = f"{start}\n{replacement_body}\n{end}"

    new_text, count = pattern.subn(replacement, text)

    if count != 1:
        raise RuntimeError(f"Could not find exactly one block: {start} ... {end}")

    return new_text


def format_publication_summary(total_count: int | None) -> str:
    if total_count is None:
        return (
            "- Complete publication list: "
            "[INSPIRE author profile](https://inspirehep.net/authors/1309535)\n"
            "- Publication count could not be retrieved automatically."
        )

    return (
        "- Complete publication list: "
        "[INSPIRE author profile](https://inspirehep.net/authors/1309535)\n"
        f"- Number of INSPIRE literature records: **{total_count}**\n"
        "- Recent publications below are generated automatically from INSPIRE."
    )


def event_sort_key(event: Any) -> datetime:
    value = event.start

    if isinstance(value, datetime):
        return value

    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)

    return datetime.min


def format_date(value: Any, date_label: str = "") -> str:
    if date_label:
        return date_label

    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")

    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")

    return ""


def format_cv_talk(event: Any) -> str:
    date_str = format_date(event.start, getattr(event, "date_label", ""))

    title = event.title
    if event.url:
        title_md = f"**[{title}]({event.url})**"
    else:
        title_md = f"**{title}**"

    line = f"- {title_md}  \n  {event.kind}, {date_str}"

    details: list[str] = []
    if getattr(event, "event", ""):
        details.append(event.event)
    if event.location:
        details.append(event.location)

    if details:
        line += ", " + ", ".join(details)

    return line


def format_recent_talks(n: int = 10) -> str:
    try:
        from update_talks_from_ics import load_all_talks
    except Exception as exc:
        raise RuntimeError(
            "Could not import load_all_talks from scripts/update_talks_from_ics.py"
        ) from exc

    talks = load_all_talks()

    if not talks:
        return "No public talks found."

    selected = sorted(talks, key=event_sort_key, reverse=True)[:n]

    return "\n\n".join(format_cv_talk(event) for event in selected)


def main() -> int:
    payload = fetch_inspire(size=20)

    total_count = get_total_count(payload)
    recent_papers = get_recent_papers(payload, n=5)

    publication_summary = format_publication_summary(total_count)

    if recent_papers:
        recent_publications = "\n\n".join(format_paper(paper) for paper in recent_papers)
    else:
        recent_publications = "Recent publications could not be retrieved automatically."

    recent_talks = format_recent_talks(n=10)

    with open(CV_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    text = update_block(
        text,
        SUMMARY_START,
        SUMMARY_END,
        publication_summary,
    )

    text = update_block(
        text,
        RECENT_PUBLICATIONS_START,
        RECENT_PUBLICATIONS_END,
        recent_publications,
    )

    text = update_block(
        text,
        RECENT_TALKS_START,
        RECENT_TALKS_END,
        recent_talks,
    )

    with open(CV_PATH, "w", encoding="utf-8") as f:
        f.write(text)

    print("Updated automatic CV blocks in cv.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
