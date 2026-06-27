#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any


CV_PATH = "cv.md"
INSPIRE_QUERY = os.environ.get("INSPIRE_QUERY", "a K.Mukaida.1")

DATA_DIR = Path("_data")
GENERATED_DIR = Path("generated")

PUBLICATIONS_CACHE = DATA_DIR / "publications_inspire.json"
TALKS_COMBINED = DATA_DIR / "talks_combined.json"

SUMMARY_START = "<!-- cv-publication-summary:start -->"
SUMMARY_END = "<!-- cv-publication-summary:end -->"

FULL_PUBLICATIONS_START = "<!-- cv-full-publications:start -->"
FULL_PUBLICATIONS_END = "<!-- cv-full-publications:end -->"

FULL_TALKS_START = "<!-- cv-full-talks:start -->"
FULL_TALKS_END = "<!-- cv-full-talks:end -->"


def fetch_inspire(size: int = 250) -> dict[str, Any]:
    params = {
        "q": INSPIRE_QUERY,
        "sort": "mostrecent",
        "size": str(size),
    }

    url = "https://inspirehep.net/api/literature?" + urllib.parse.urlencode(params)

    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def get_hits(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload.get("hits", {}).get("hits", [])


def get_total_count(payload: dict[str, Any]) -> int | None:
    total = payload.get("hits", {}).get("total")

    if isinstance(total, dict):
        value = total.get("value")
        if isinstance(value, int):
            return value

    if isinstance(total, int):
        return total

    return None


def get_metadata(hit: dict[str, Any]) -> dict[str, Any]:
    return hit.get("metadata", {})


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

    if "," in full_name:
        family, given = [part.strip() for part in full_name.split(",", 1)]
        if given and family:
            return f"{given} {family}"

    return full_name


def get_authors(meta: dict[str, Any], max_authors: int = 6) -> str:
    authors = [format_author_name(a) for a in meta.get("authors", [])]
    authors = [a for a in authors if a]

    if not authors:
        return ""

    if len(authors) == 1:
        return authors[0]

    if len(authors) <= max_authors:
        return ", ".join(authors[:-1]) + ", and " + authors[-1]

    return ", ".join(authors[:max_authors]) + ", et al."


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


def get_year(meta: dict[str, Any]) -> str:
    info = meta.get("publication_info", [])
    for item in info:
        year = item.get("year")
        if year:
            return str(year)

    earliest_date = str(meta.get("earliest_date", "")).strip()
    if earliest_date[:4].isdigit():
        return earliest_date[:4]

    return ""


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


def paper_sort_key(meta: dict[str, Any]) -> str:
    earliest_date = str(meta.get("earliest_date", "")).strip()
    if earliest_date:
        return earliest_date

    year = get_year(meta)
    return f"{year}-01-01" if year else ""


def usable_papers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    papers: list[dict[str, Any]] = []

    for hit in get_hits(payload):
        meta = get_metadata(hit)
        if get_title(meta):
            papers.append(meta)

    return sorted(papers, key=paper_sort_key, reverse=True)


def format_paper_markdown(meta: dict[str, Any]) -> str:
    title = get_title(meta)
    authors = get_authors(meta)
    arxiv = get_arxiv(meta)
    doi = get_doi(meta)
    journal_line = get_journal_line(meta)

    if arxiv:
        title_md = f"**[{title}](https://arxiv.org/abs/{arxiv})**"
    else:
        title_md = f"**{title}**"

    lines = [f"- {title_md}  "]

    if authors:
        lines.append(f"  {authors}  ")

    lines.append(f"  *{journal_line}*  ")

    links: list[str] = []
    if arxiv:
        links.append(f"[[arXiv](https://arxiv.org/abs/{arxiv})]")
    if doi:
        links.append(f"[[DOI](https://doi.org/{doi})]")

    if links:
        lines.append("  " + " ".join(links))

    return "\n".join(lines)


def tex_escape(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(ch, ch) for ch in text)


def tex_href(url: str, label: str) -> str:
    if not url:
        return tex_escape(label)
    return rf"\href{{{url}}}{{{tex_escape(label)}}}"


def format_paper_tex(meta: dict[str, Any]) -> str:
    title = get_title(meta)
    authors = get_authors(meta, max_authors=10)
    arxiv = get_arxiv(meta)
    doi = get_doi(meta)
    journal_line = get_journal_line(meta)

    title_tex = tex_escape(title)
    if arxiv:
        title_tex = tex_href(f"https://arxiv.org/abs/{arxiv}", title)

    pieces = [rf"\item \textbf{{{title_tex}}}\\"]

    if authors:
        pieces.append(tex_escape(authors) + r"\\")

    pieces.append(rf"\emph{{{tex_escape(journal_line)}}}")

    links: list[str] = []
    if arxiv:
        links.append(tex_href(f"https://arxiv.org/abs/{arxiv}", f"arXiv:{arxiv}"))
    if doi:
        links.append(tex_href(f"https://doi.org/{doi}", f"doi:{doi}"))

    if links:
        pieces.append(r"\\ " + " ".join(links))

    return "\n".join(pieces)


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
        "- Full publication and invited-talk lists below are generated automatically."
    )


def update_block(text: str, start: str, end: str, replacement_body: str) -> str:
    pattern = re.compile(
        rf"{re.escape(start)}.*?{re.escape(end)}",
        flags=re.DOTALL,
    )

    replacement = f"{start}\n{replacement_body}\n{end}"
    new_text, count = pattern.subn(lambda _match: replacement, text)

    if count != 1:
        raise RuntimeError(f"Could not find exactly one block: {start} ... {end}")

    return new_text


def fetch_public_talks() -> list[Any]:
    """
    Import talk helpers from scripts/update_talks_from_ics.py.
    This keeps CV order and formatting consistent with talks.md.
    """
    try:
        from update_talks_from_ics import (
            deduplicate_talks,
            fetch_calendar_talks,
            load_history_talks,
            sort_talks_future_first,
        )
    except Exception as exc:
        raise RuntimeError(
            "Could not import talk helpers from scripts/update_talks_from_ics.py. "
            "Make sure that scripts/update_talks_from_ics.py is the future-first version."
        ) from exc

    return sort_talks_future_first(
        deduplicate_talks(fetch_calendar_talks() + load_history_talks())
    )


def format_talk_date(value: Any) -> str:
    date_label = getattr(value, "date_label", "")
    if date_label:
        return str(date_label)

    if hasattr(value, "start"):
        value = value.start

    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")

    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")

    return ""


def format_talk_markdown(event: Any) -> str:
    date_str = format_talk_date(event)

    if event.url:
        title_md = f"**[{event.title}]({event.url})**"
    else:
        title_md = f"**{event.title}**"

    details: list[str] = [event.kind, date_str]

    if getattr(event, "event", ""):
        details.append(event.event)

    if event.location:
        details.append(event.location)

    return f"- {title_md}  \n  " + ", ".join(details)


def format_talk_tex(event: Any) -> str:
    date_str = format_talk_date(event)

    title_tex = tex_escape(event.title)
    if event.url:
        title_tex = tex_href(event.url, event.title)

    details: list[str] = [event.kind, date_str]

    if getattr(event, "event", ""):
        details.append(event.event)

    if event.location:
        details.append(event.location)

    return rf"\item \textbf{{{title_tex}}}\\ " + tex_escape(", ".join(details))


def write_generated_files(papers: list[dict[str, Any]], talks: list[Any]) -> None:
    GENERATED_DIR.mkdir(exist_ok=True)

    publications_tex = (
        "\\begin{enumerate}\n"
        + "\n\n".join(format_paper_tex(paper) for paper in papers)
        + "\n\\end{enumerate}\n"
    )

    talks_tex = (
        "\\begin{enumerate}\n"
        + "\n\n".join(format_talk_tex(talk) for talk in talks)
        + "\n\\end{enumerate}\n"
    )

    cv_full_tex = (
        "% Auto-generated CV lists. Requires hyperref for clickable links.\n\n"
        "% Publications\n"
        + publications_tex
        + "\n% Invited talks\n"
        + talks_tex
    )

    (GENERATED_DIR / "publications_full.tex").write_text(publications_tex, encoding="utf-8")
    (GENERATED_DIR / "invited_talks_full.tex").write_text(talks_tex, encoding="utf-8")
    (GENERATED_DIR / "cv_full_lists.tex").write_text(cv_full_tex, encoding="utf-8")


def write_data_cache(payload: dict[str, Any], talks: list[Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)

    PUBLICATIONS_CACHE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    talk_records: list[dict[str, Any]] = []
    for talk in talks:
        talk_records.append(
            {
                "title": talk.title,
                "kind": talk.kind,
                "date": format_talk_date(talk),
                "location": talk.location,
                "event": getattr(talk, "event", ""),
                "url": talk.url,
                "source": getattr(talk, "source", ""),
            }
        )

    TALKS_COMBINED.write_text(
        json.dumps(talk_records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    payload = fetch_inspire(size=250)
    total_count = get_total_count(payload)
    papers = usable_papers(payload)
    talks = fetch_public_talks()

    publication_summary = format_publication_summary(total_count)
    full_publications = "\n\n".join(format_paper_markdown(paper) for paper in papers)
    full_talks = "\n\n".join(format_talk_markdown(talk) for talk in talks)

    write_generated_files(papers, talks)
    write_data_cache(payload, talks)

    with open(CV_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    text = update_block(text, SUMMARY_START, SUMMARY_END, publication_summary)
    text = update_block(text, FULL_PUBLICATIONS_START, FULL_PUBLICATIONS_END, full_publications)
    text = update_block(text, FULL_TALKS_START, FULL_TALKS_END, full_talks)

    with open(CV_PATH, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"Updated CV: {len(papers)} publications, {len(talks)} invited talks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
