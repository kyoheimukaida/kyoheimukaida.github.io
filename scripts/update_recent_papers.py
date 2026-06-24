#!/usr/bin/env python3

from __future__ import annotations

import os
import re
import sys
import urllib.parse
import urllib.request
from typing import Any


START = "<!-- recent-papers:start -->"
END = "<!-- recent-papers:end -->"

README_PATH = "README.md"

# INSPIRE author query.
# If this ever fails, try: authors.full_name:"Mukaida, Kyohei"
INSPIRE_QUERY = os.environ.get("INSPIRE_QUERY", "a K.Mukaida.1")


def fetch_recent_papers(n: int = 2) -> list[dict[str, Any]]:
    params = {
        "q": INSPIRE_QUERY,
        "sort": "mostrecent",
        "size": "10",
    }
    url = "https://inspirehep.net/api/literature?" + urllib.parse.urlencode(params)

    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read().decode("utf-8")

    import json

    payload = json.loads(data)
    hits = payload.get("hits", {}).get("hits", [])

    papers: list[dict[str, Any]] = []
    for hit in hits:
        meta = hit.get("metadata", {})

        # Require at least a title and an arXiv identifier for the README.
        title = get_title(meta)
        arxiv = get_arxiv(meta)
        if not title or not arxiv:
            continue

        papers.append(meta)
        if len(papers) >= n:
            break

    if len(papers) < n:
        raise RuntimeError(f"Could only find {len(papers)} usable papers from INSPIRE.")

    return papers


def get_title(meta: dict[str, Any]) -> str:
    titles = meta.get("titles", [])
    if not titles:
        return ""
    return titles[0].get("title", "").strip()


def get_authors(meta: dict[str, Any]) -> str:
    authors = [a.get("full_name", "").strip() for a in meta.get("authors", [])]
    authors = [a for a in authors if a]

    if not authors:
        return ""

    if len(authors) <= 4:
        if len(authors) == 1:
            return authors[0]
        return ", ".join(authors[:-1]) + ", and " + authors[-1]

    return ", ".join(authors[:3]) + ", et al."


def get_arxiv(meta: dict[str, Any]) -> str:
    eprints = meta.get("arxiv_eprints", [])
    if not eprints:
        return ""
    return eprints[0].get("value", "").strip()


def get_doi(meta: dict[str, Any]) -> str:
    dois = meta.get("dois", [])
    if not dois:
        return ""
    return dois[0].get("value", "").strip()


def get_journal_line(meta: dict[str, Any]) -> str:
    info = meta.get("publication_info", [])
    if info:
        item = info[0]
        journal = item.get("journal_title", "")
        volume = item.get("journal_volume", "")
        artid = item.get("artid") or item.get("page_start") or ""
        year = item.get("year", "")

        parts = []
        if journal:
            parts.append(journal)
        if volume:
            parts.append(str(volume))
        if artid:
            parts.append(str(artid))

        line = " ".join(parts)
        if year:
            line += f" ({year})"
        if line.strip():
            return line.strip()

    date = meta.get("earliest_date", "")
    if date:
        return f"arXiv preprint ({date})"

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


def update_readme(block: str) -> None:
    with open(README_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    pattern = re.compile(
        rf"{re.escape(START)}.*?{re.escape(END)}",
        flags=re.DOTALL,
    )

    replacement = f"{START}\n{block}\n{END}"

    new_text, count = pattern.subn(replacement, text)

    if count != 1:
        raise RuntimeError(
            f"Could not find exactly one recent-papers block in {README_PATH}."
        )

    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(new_text)


def main() -> int:
    papers = fetch_recent_papers(n=2)
    block = "\n\n".join(format_paper(p) for p in papers)
    update_readme(block)
    print("Updated recent papers block in README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
