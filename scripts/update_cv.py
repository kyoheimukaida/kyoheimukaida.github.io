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
DATA_DIR = Path("_data")
TEX_DIR = Path("generated")

PUBLICATIONS_CACHE_PATH = DATA_DIR / "publications_inspire.json"
TALKS_CACHE_PATH = DATA_DIR / "talks_combined.json"
PUBLICATIONS_TEX_PATH = TEX_DIR / "publications_full.tex"
TALKS_TEX_PATH = TEX_DIR / "invited_talks_full.tex"
COMBINED_TEX_PATH = TEX_DIR / "cv_full_lists.tex"

INSPIRE_QUERY = os.environ.get("INSPIRE_QUERY", "a K.Mukaida.1")

SUMMARY_START = "<!-- cv-publication-summary:start -->"
SUMMARY_END = "<!-- cv-publication-summary:end -->"

FULL_PUBLICATIONS_START = "<!-- cv-full-publications:start -->"
FULL_PUBLICATIONS_END = "<!-- cv-full-publications:end -->"

FULL_TALKS_START = "<!-- cv-full-talks:start -->"
FULL_TALKS_END = "<!-- cv-full-talks:end -->"


# -----------------------------------------------------------------------------
# INSPIRE publication handling
# -----------------------------------------------------------------------------


def fetch_inspire_page(page: int, size: int) -> dict[str, Any]:
    params = {
        "q": INSPIRE_QUERY,
        "sort": "mostrecent",
        "size": str(size),
        "page": str(page),
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


def fetch_all_inspire_records(size: int = 100) -> tuple[list[dict[str, Any]], int | None]:
    """Fetch all INSPIRE literature records for the configured author query."""
    all_metadata: list[dict[str, Any]] = []
    total_count: int | None = None
    page = 1

    while True:
        payload = fetch_inspire_page(page=page, size=size)
        if total_count is None:
            total_count = get_total_count(payload)

        hits = payload.get("hits", {}).get("hits", [])
        if not hits:
            break

        for hit in hits:
            meta = hit.get("metadata", {})
            if get_title(meta):
                all_metadata.append(meta)

        if total_count is not None and len(all_metadata) >= total_count:
            break

        if len(hits) < size:
            break

        page += 1

    return all_metadata, total_count


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

    # INSPIRE commonly returns names as "Family, Given".
    if "," in full_name:
        family, given = [part.strip() for part in full_name.split(",", 1)]
        if given and family:
            return f"{given} {family}"

    return full_name


def get_authors(meta: dict[str, Any], *, abbreviate_after: int | None = None) -> str:
    authors = [format_author_name(a) for a in meta.get("authors", [])]
    authors = [a for a in authors if a]

    if not authors:
        return ""

    if abbreviate_after is not None and len(authors) > abbreviate_after:
        return ", ".join(authors[:abbreviate_after]) + ", et al."

    if len(authors) == 1:
        return authors[0]

    return ", ".join(authors[:-1]) + ", and " + authors[-1]


def get_author_list(meta: dict[str, Any]) -> list[str]:
    return [format_author_name(a) for a in meta.get("authors", []) if format_author_name(a)]


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
    if info:
        year = info[0].get("year")
        if year:
            return str(year)

    earliest_date = str(meta.get("earliest_date", "")).strip()
    if len(earliest_date) >= 4:
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

    return ""


def normalize_publication(meta: dict[str, Any]) -> dict[str, Any]:
    arxiv = get_arxiv(meta)
    doi = get_doi(meta)
    return {
        "title": get_title(meta),
        "authors": get_author_list(meta),
        "year": get_year(meta),
        "journal": get_journal_line(meta),
        "arxiv": arxiv,
        "doi": doi,
        "earliest_date": str(meta.get("earliest_date", "")).strip(),
        "arxiv_url": f"https://arxiv.org/abs/{arxiv}" if arxiv else "",
        "doi_url": f"https://doi.org/{doi}" if doi else "",
    }


def format_publication_markdown(meta: dict[str, Any]) -> str:
    title = get_title(meta)
    authors = get_authors(meta)
    arxiv = get_arxiv(meta)
    doi = get_doi(meta)
    journal_line = get_journal_line(meta)

    title_url = f"https://arxiv.org/abs/{arxiv}" if arxiv else ""
    title_md = f"**[{title}]({title_url})**" if title_url else f"**{title}**"

    lines = [f"- {title_md}  "]

    if authors:
        lines.append(f"  {authors}  ")

    if journal_line:
        lines.append(f"  *{journal_line}*  ")

    links: list[str] = []
    if arxiv:
        links.append(f"[[arXiv](https://arxiv.org/abs/{arxiv})]")
    if doi:
        links.append(f"[[DOI](https://doi.org/{doi})]")

    if links:
        lines.append("  " + " ".join(links))

    return "\n".join(lines)


def format_publication_summary(records: list[dict[str, Any]], total_count: int | None) -> str:
    years = [get_year(record) for record in records]
    years = [year for year in years if year]

    year_span = ""
    if years:
        year_span = f"\n- Years covered: **{min(years)}--{max(years)}**"

    count_text = str(total_count if total_count is not None else len(records))

    return (
        "- Complete publication metadata: "
        "[INSPIRE author profile](https://inspirehep.net/authors/1309535)\n"
        f"- Number of INSPIRE literature records: **{count_text}**"
        f"{year_span}\n"
        "- The full publication list below is generated automatically from INSPIRE."
    )


# -----------------------------------------------------------------------------
# Talk handling via scripts/update_talks_from_ics.py
# -----------------------------------------------------------------------------


def load_all_talks_from_public_sources() -> list[Any]:
    try:
        from update_talks_from_ics import load_all_talks
    except Exception as exc:
        raise RuntimeError(
            "Could not import load_all_talks from scripts/update_talks_from_ics.py"
        ) from exc

    return load_all_talks()


def talk_sort_key(event: Any) -> datetime:
    value = event.start
    if isinstance(value, datetime):
        # Strip timezone for stable comparison across date-only and datetime events.
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return datetime.min


def talk_date_label(event: Any) -> str:
    if getattr(event, "date_label", ""):
        return str(event.date_label)

    value = event.start
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return ""


def normalize_talk(event: Any) -> dict[str, Any]:
    return {
        "title": str(event.title).strip(),
        "kind": str(getattr(event, "kind", "Invited talk")).strip() or "Invited talk",
        "date": talk_date_label(event),
        "event": str(getattr(event, "event", "")).strip(),
        "location": str(getattr(event, "location", "")).strip(),
        "url": str(getattr(event, "url", "")).strip(),
        "source": str(getattr(event, "source", "")).strip(),
    }


def format_talk_markdown(event: Any) -> str:
    title = str(event.title).strip()
    url = str(getattr(event, "url", "")).strip()
    kind = str(getattr(event, "kind", "Invited talk")).strip() or "Invited talk"
    date_label = talk_date_label(event)
    event_name = str(getattr(event, "event", "")).strip()
    location = str(getattr(event, "location", "")).strip()

    title_md = f"**[{title}]({url})**" if url else f"**{title}**"

    details = [kind]
    if date_label:
        details.append(date_label)
    if event_name:
        details.append(event_name)
    if location:
        details.append(location)

    return f"- {title_md}  \n  " + ", ".join(details)


# -----------------------------------------------------------------------------
# TeX generation
# -----------------------------------------------------------------------------


LATEX_REPLACEMENTS = {
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


def tex_escape(value: str) -> str:
    return "".join(LATEX_REPLACEMENTS.get(char, char) for char in str(value))


def tex_href(url: str, label: str) -> str:
    url = str(url).strip()
    if not url:
        return tex_escape(label)
    return rf"\href{{{url}}}{{{tex_escape(label)}}}"


def format_publication_tex_item(meta: dict[str, Any]) -> str:
    title = tex_escape(get_title(meta))
    authors = tex_escape(get_authors(meta))
    journal_line = tex_escape(get_journal_line(meta))
    arxiv = get_arxiv(meta)
    doi = get_doi(meta)

    pieces: list[str] = []
    if authors:
        pieces.append(authors)
    if journal_line:
        pieces.append(rf"\emph{{{journal_line}}}")

    links: list[str] = []
    if arxiv:
        links.append(tex_href(f"https://arxiv.org/abs/{arxiv}", f"arXiv:{arxiv}"))
    if doi:
        links.append(tex_href(f"https://doi.org/{doi}", f"doi:{doi}"))

    if links:
        pieces.append(", ".join(links))

    body = ". ".join(piece for piece in pieces if piece)
    if body:
        return rf"\item \textbf{{{title}}}. {body}."
    return rf"\item \textbf{{{title}}}."


def format_talk_tex_item(event: Any) -> str:
    title_raw = str(event.title).strip()
    kind = tex_escape(str(getattr(event, "kind", "Invited talk")).strip() or "Invited talk")
    date_label = tex_escape(talk_date_label(event))
    event_name = tex_escape(str(getattr(event, "event", "")).strip())
    location = tex_escape(str(getattr(event, "location", "")).strip())
    url = str(getattr(event, "url", "")).strip()

    title_tex = rf"\textbf{{{tex_escape(title_raw)}}}"
    if url:
        title_tex = rf"\href{{{url}}}{{\textbf{{{tex_escape(title_raw)}}}}}"

    details = [kind]
    if date_label:
        details.append(date_label)
    if event_name:
        details.append(event_name)
    if location:
        details.append(location)

    return rf"\item {title_tex}. " + ", ".join(details) + "."


def make_tex_list(items: list[str]) -> str:
    return "\n".join([r"\begin{enumerate}", *items, r"\end{enumerate}", ""])


def write_tex_outputs(publications: list[dict[str, Any]], talks: list[Any]) -> None:
    TEX_DIR.mkdir(parents=True, exist_ok=True)

    publication_items = [format_publication_tex_item(record) for record in publications]
    talk_items = [format_talk_tex_item(event) for event in talks]

    publications_tex = (
        "% Auto-generated by scripts/update_cv.py.\n"
        "% Requires \\usepackage{hyperref} if links are desired.\n"
        + make_tex_list(publication_items)
    )

    talks_tex = (
        "% Auto-generated by scripts/update_cv.py.\n"
        "% Requires \\usepackage{hyperref} if links are desired.\n"
        + make_tex_list(talk_items)
    )

    combined_tex = (
        "% Auto-generated by scripts/update_cv.py.\n"
        "% Requires \\usepackage{hyperref} if links are desired.\n"
        r"\section*{Publications}" + "\n"
        + make_tex_list(publication_items)
        + "\n"
        + r"\section*{Invited Talks}" + "\n"
        + make_tex_list(talk_items)
    )

    PUBLICATIONS_TEX_PATH.write_text(publications_tex, encoding="utf-8")
    TALKS_TEX_PATH.write_text(talks_tex, encoding="utf-8")
    COMBINED_TEX_PATH.write_text(combined_tex, encoding="utf-8")


# -----------------------------------------------------------------------------
# Markdown CV update
# -----------------------------------------------------------------------------


def write_json_outputs(publications: list[dict[str, Any]], talks: list[Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    publication_records = [normalize_publication(record) for record in publications]
    talk_records = [normalize_talk(event) for event in talks]

    PUBLICATIONS_CACHE_PATH.write_text(
        json.dumps(publication_records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    TALKS_CACHE_PATH.write_text(
        json.dumps(talk_records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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


def main() -> int:
    publications, total_count = fetch_all_inspire_records(size=100)
    talks = load_all_talks_from_public_sources()
    talks = sorted(talks, key=talk_sort_key, reverse=True)

    publication_summary = format_publication_summary(publications, total_count)
    full_publications = "\n\n".join(format_publication_markdown(record) for record in publications)
    full_talks = "\n\n".join(format_talk_markdown(event) for event in talks)

    if not full_publications:
        full_publications = "Publication list could not be retrieved automatically."
    if not full_talks:
        full_talks = "No invited talks found in the talk data sources."

    write_json_outputs(publications, talks)
    write_tex_outputs(publications, talks)

    with open(CV_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    text = update_block(text, SUMMARY_START, SUMMARY_END, publication_summary)
    text = update_block(text, FULL_PUBLICATIONS_START, FULL_PUBLICATIONS_END, full_publications)
    text = update_block(text, FULL_TALKS_START, FULL_TALKS_END, full_talks)

    with open(CV_PATH, "w", encoding="utf-8") as f:
        f.write(text)

    print(
        f"Updated CV: {len(publications)} publication record(s), "
        f"{len(talks)} talk record(s)."
    )
    print(f"Wrote JSON data to {DATA_DIR}/")
    print(f"Wrote TeX fragments to {TEX_DIR}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
