#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PUBLICATIONS_PATH = "publications.md"
INSPIRE_QUERY = os.environ.get("INSPIRE_QUERY", "a K.Mukaida.1")

DATA_DIR = Path("_data")
HIGHLIGHTS_CACHE = DATA_DIR / "publications_highlights.json"

SELECTED_START = "<!-- publications-selected:start -->"
SELECTED_END = "<!-- publications-selected:end -->"

NOTABLE_START = "<!-- publications-notable:start -->"
NOTABLE_END = "<!-- publications-notable:end -->"

# Curated once. Metadata is updated automatically from INSPIRE.
SELECTED_ARXIV_IDS = [
    "1312.3097",   # Thermalization after/during Reheating
    "1402.2846",   # Dark Matter Production in Late Time Reheating
    "1609.05209",  # Violent Preheating
    "1611.06130",  # PBHs for LIGO/PTA
    "2011.09347",  # Wash-In Leptogenesis
    "2111.03082",  # Leptoflavorgenesis
]

# Objective automatic highlights based on recent attention.
# Tune these in .github/workflows/update-publications.yml.
RECENT_CITATION_WINDOW_DAYS = int(os.environ.get("RECENT_CITATION_WINDOW_DAYS", "730"))
RECENT_CITATION_THRESHOLD = int(os.environ.get("RECENT_CITATION_THRESHOLD", "5"))
RECENT_CITATION_SHARE_THRESHOLD = float(os.environ.get("RECENT_CITATION_SHARE_THRESHOLD", "0.12"))
RECENT_12M_BONUS_THRESHOLD = int(os.environ.get("RECENT_12M_BONUS_THRESHOLD", "3"))
NOTABLE_PUBLISHED_COUNT = int(os.environ.get("NOTABLE_PUBLISHED_COUNT", "6"))
CITING_FETCH_SIZE = int(os.environ.get("CITING_FETCH_SIZE", "150"))


def fetch_json(url: str, timeout: int = 60) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "kyoheimukaida-github-pages-publications-updater/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def inspire_literature_query(params: dict[str, str | int]) -> dict[str, Any]:
    url = "https://inspirehep.net/api/literature?" + urllib.parse.urlencode(params)
    return fetch_json(url)


def fetch_author_papers(size: int = 250) -> dict[str, Any]:
    return inspire_literature_query(
        {
            "q": INSPIRE_QUERY,
            "sort": "mostrecent",
            "size": str(size),
        }
    )


def fetch_citing_records(recid: int | str, size: int = CITING_FETCH_SIZE) -> dict[str, Any]:
    # INSPIRE search syntax for records that cite a given record.
    return inspire_literature_query(
        {
            "q": f"refersto:recid:{recid}",
            "sort": "mostrecent",
            "size": str(size),
        }
    )


def get_hits(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload.get("hits", {}).get("hits", [])


def get_metadata(hit: dict[str, Any]) -> dict[str, Any]:
    return hit.get("metadata", {})


def get_title(meta: dict[str, Any]) -> str:
    titles = meta.get("titles", [])
    if not titles:
        return ""
    return str(titles[0].get("title", "")).strip()


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


def get_recid(meta: dict[str, Any]) -> int | None:
    value = meta.get("control_number")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_citation_count(meta: dict[str, Any]) -> int:
    value = meta.get("citation_count", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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

    # INSPIRE often returns "Family, Given".
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


def publication_info_items(meta: dict[str, Any]) -> list[dict[str, Any]]:
    info = meta.get("publication_info", [])
    return [item for item in info if isinstance(item, dict)]


def is_published(meta: dict[str, Any]) -> bool:
    """
    Treat a paper as published if INSPIRE has journal publication metadata.

    This excludes bare arXiv preprints from the recently-cited-published section.
    """
    for item in publication_info_items(meta):
        journal = str(item.get("journal_title", "")).strip()
        year = str(item.get("year", "")).strip()
        volume = str(item.get("journal_volume", "")).strip()
        artid = str(item.get("artid") or item.get("page_start") or "").strip()
        if journal and (year or volume or artid):
            return True

    return False


def get_journal_line(meta: dict[str, Any]) -> str:
    info = publication_info_items(meta)

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


def get_year(meta: dict[str, Any]) -> str:
    for item in publication_info_items(meta):
        year = item.get("year")
        if year:
            return str(year)

    earliest_date = str(meta.get("earliest_date", "")).strip()
    if earliest_date[:4].isdigit():
        return earliest_date[:4]

    return ""


def parse_date(text: str) -> date | None:
    text = str(text).strip()
    if not text:
        return None

    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(text[: len(fmt)], fmt)
            return parsed.date()
        except ValueError:
            continue

    return None


def paper_date_key(meta: dict[str, Any]) -> str:
    earliest_date = str(meta.get("earliest_date", "")).strip()
    if earliest_date:
        return earliest_date

    year = get_year(meta)
    return f"{year}-01-01" if year else ""


def usable_papers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    papers: list[dict[str, Any]] = []

    for hit in get_hits(payload):
        meta = get_metadata(hit)
        if get_title(meta) and get_arxiv(meta):
            papers.append(meta)

    return sorted(papers, key=paper_date_key, reverse=True)


def build_arxiv_map(papers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {get_arxiv(paper): paper for paper in papers if get_arxiv(paper)}


def count_recent_citations(recid: int) -> dict[str, Any]:
    payload = fetch_citing_records(recid, size=CITING_FETCH_SIZE)
    hits = get_hits(payload)

    now = datetime.now(timezone.utc).date()
    cutoff_window = now - timedelta(days=RECENT_CITATION_WINDOW_DAYS)
    cutoff_12m = now - timedelta(days=365)

    recent_window = 0
    recent_12m = 0
    seen_dates: list[str] = []

    for hit in hits:
        meta = get_metadata(hit)
        citing_date = parse_date(str(meta.get("earliest_date", "")).strip())

        if citing_date is None:
            continue

        if citing_date >= cutoff_window:
            recent_window += 1
            seen_dates.append(citing_date.isoformat())

        if citing_date >= cutoff_12m:
            recent_12m += 1

    return {
        "recent_window": recent_window,
        "recent_12m": recent_12m,
        "fetched_citing_records": len(hits),
        "cutoff_window": cutoff_window.isoformat(),
        "cutoff_12m": cutoff_12m.isoformat(),
        "recent_citing_dates": seen_dates[:20],
    }


def attention_metrics(meta: dict[str, Any]) -> dict[str, Any]:
    recid = get_recid(meta)
    total = get_citation_count(meta)

    if recid is None:
        recent = {
            "recent_window": 0,
            "recent_12m": 0,
            "fetched_citing_records": 0,
            "cutoff_window": "",
            "cutoff_12m": "",
            "recent_citing_dates": [],
        }
    else:
        recent = count_recent_citations(recid)

    recent_window = int(recent["recent_window"])
    recent_12m = int(recent["recent_12m"])

    share = recent_window / total if total > 0 else 0.0

    # Ranking score prioritizes genuine recent attention.
    # The 12-month count breaks ties toward papers with very recent movement.
    score = (recent_window * 1000) + (recent_12m * 100) + int(share * 100)

    return {
        "recid": recid,
        "total_citations": total,
        "recent_citations_window": recent_window,
        "recent_citations_12m": recent_12m,
        "recent_citation_share": share,
        "attention_score": score,
        **recent,
    }


def passes_recent_attention_threshold(metrics: dict[str, Any]) -> bool:
    recent_window = int(metrics["recent_citations_window"])
    recent_12m = int(metrics["recent_citations_12m"])
    share = float(metrics["recent_citation_share"])

    # Main condition:
    # enough citations in the recent window and not merely ancient cumulative impact.
    if (
        recent_window >= RECENT_CITATION_THRESHOLD
        and share >= RECENT_CITATION_SHARE_THRESHOLD
    ):
        return True

    # Bonus condition:
    # a clear very-recent burst in the last 12 months can pass even if the
    # share is slightly diluted by older citations.
    if recent_12m >= RECENT_12M_BONUS_THRESHOLD:
        return True

    return False


def select_recently_cited_published(
    papers: list[dict[str, Any]],
    selected_arxiv_ids: set[str],
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    metrics_by_arxiv: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []

    published_candidates = [
        paper
        for paper in papers
        if get_arxiv(paper) not in selected_arxiv_ids
        and is_published(paper)
        and get_recid(paper) is not None
        and get_citation_count(paper) > 0
    ]

    for paper in published_candidates:
        arxiv = get_arxiv(paper)
        metrics = attention_metrics(paper)
        metrics_by_arxiv[arxiv] = metrics

        if passes_recent_attention_threshold(metrics):
            candidates.append(paper)

    candidates.sort(
        key=lambda paper: (
            metrics_by_arxiv[get_arxiv(paper)]["attention_score"],
            paper_date_key(paper),
        ),
        reverse=True,
    )

    return candidates[:limit], metrics_by_arxiv


def format_paper(meta: dict[str, Any], metrics: dict[str, Any] | None = None) -> str:
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

    links = [f"[[arXiv:{arxiv}]({arxiv_url})]"]
    if doi:
        links.append(f"[[DOI](https://doi.org/{doi})]")

    lines.append("  " + " ".join(links))

    # Do not display citation metrics on the public page by default.
    # They are stored in _data/publications_highlights.json for auditing.

    return "\n".join(lines)


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


def write_highlights_cache(
    selected_papers: list[dict[str, Any]],
    recently_cited_papers: list[dict[str, Any]],
    metrics_by_arxiv: dict[str, dict[str, Any]],
) -> None:
    DATA_DIR.mkdir(exist_ok=True)

    payload = {
        "selected_arxiv_ids": SELECTED_ARXIV_IDS,
        "recent_attention_criteria": {
            "window_days": RECENT_CITATION_WINDOW_DAYS,
            "recent_citation_threshold": RECENT_CITATION_THRESHOLD,
            "recent_citation_share_threshold": RECENT_CITATION_SHARE_THRESHOLD,
            "recent_12m_bonus_threshold": RECENT_12M_BONUS_THRESHOLD,
            "notable_published_count": NOTABLE_PUBLISHED_COUNT,
            "citing_fetch_size": CITING_FETCH_SIZE,
        },
        "selected": [
            {
                "title": get_title(paper),
                "arxiv": get_arxiv(paper),
                "citations_total": get_citation_count(paper),
                "journal": get_journal_line(paper),
            }
            for paper in selected_papers
        ],
        "recently_cited_published": [
            {
                "title": get_title(paper),
                "arxiv": get_arxiv(paper),
                "journal": get_journal_line(paper),
                **metrics_by_arxiv.get(get_arxiv(paper), {}),
            }
            for paper in recently_cited_papers
        ],
    }

    HIGHLIGHTS_CACHE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    payload = fetch_author_papers(size=250)
    papers = usable_papers(payload)
    arxiv_map = build_arxiv_map(papers)

    selected_papers: list[dict[str, Any]] = []
    missing_selected: list[str] = []

    for arxiv_id in SELECTED_ARXIV_IDS:
        paper = arxiv_map.get(arxiv_id)
        if paper is None:
            missing_selected.append(arxiv_id)
        else:
            selected_papers.append(paper)

    selected_set = set(SELECTED_ARXIV_IDS)
    recently_cited_papers, metrics_by_arxiv = select_recently_cited_published(
        papers=papers,
        selected_arxiv_ids=selected_set,
        limit=NOTABLE_PUBLISHED_COUNT,
    )

    selected_block = "\n\n".join(format_paper(paper) for paper in selected_papers)
    if missing_selected:
        selected_block += (
            "\n\n"
            "- Missing selected arXiv IDs from INSPIRE fetch: "
            + ", ".join(missing_selected)
        )

    if recently_cited_papers:
        notable_block = "\n\n".join(
            format_paper(paper, metrics_by_arxiv.get(get_arxiv(paper)))
            for paper in recently_cited_papers
        )
    else:
        notable_block = (
            "- No published papers outside the selected list currently pass "
            "the recent-attention threshold."
        )

    write_highlights_cache(selected_papers, recently_cited_papers, metrics_by_arxiv)

    with open(PUBLICATIONS_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    text = update_block(text, SELECTED_START, SELECTED_END, selected_block)
    text = update_block(text, NOTABLE_START, NOTABLE_END, notable_block)

    with open(PUBLICATIONS_PATH, "w", encoding="utf-8") as f:
        f.write(text)

    print(
        f"Updated publications.md: "
        f"{len(selected_papers)} selected, "
        f"{len(recently_cited_papers)} recently cited published "
        f"(window={RECENT_CITATION_WINDOW_DAYS} days, "
        f"threshold={RECENT_CITATION_THRESHOLD}, "
        f"share={RECENT_CITATION_SHARE_THRESHOLD})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
