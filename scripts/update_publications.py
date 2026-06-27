#!/usr/bin/env python3

from __future__ import annotations

import json
import math
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

SELECTED_ARXIV_IDS = [
    "1312.3097",
    "1402.2846",
    "1609.05209",
    "1611.06130",
    "2011.09347",
    "2111.03082",
]

RECENT_CITATION_WINDOW_DAYS = int(os.environ.get("RECENT_CITATION_WINDOW_DAYS", "1095"))
NOTABLE_PUBLISHED_COUNT = int(os.environ.get("NOTABLE_PUBLISHED_COUNT", "6"))
CITING_FETCH_SIZE = int(os.environ.get("CITING_FETCH_SIZE", "120"))
MAX_ATTENTION_CANDIDATES = int(os.environ.get("MAX_ATTENTION_CANDIDATES", "60"))


def fetch_json(url: str, timeout: int = 60) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "kyoheimukaida-publications-updater/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def inspire_query(params: dict[str, str | int]) -> dict[str, Any]:
    return fetch_json("https://inspirehep.net/api/literature?" + urllib.parse.urlencode(params))


def fetch_author_papers(size: int = 250) -> dict[str, Any]:
    return inspire_query({"q": INSPIRE_QUERY, "sort": "mostrecent", "size": str(size)})


def fetch_citing_records(recid: int, size: int = CITING_FETCH_SIZE) -> dict[str, Any]:
    return inspire_query({"q": f"refersto:recid:{recid}", "sort": "mostrecent", "size": str(size)})


def hits(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload.get("hits", {}).get("hits", [])


def meta(hit: dict[str, Any]) -> dict[str, Any]:
    return hit.get("metadata", {})


def title(m: dict[str, Any]) -> str:
    xs = m.get("titles", [])
    return str(xs[0].get("title", "")).strip() if xs else ""


def arxiv(m: dict[str, Any]) -> str:
    xs = m.get("arxiv_eprints", [])
    return str(xs[0].get("value", "")).strip() if xs else ""


def doi(m: dict[str, Any]) -> str:
    xs = m.get("dois", [])
    return str(xs[0].get("value", "")).strip() if xs else ""


def recid(m: dict[str, Any]) -> int | None:
    try:
        return int(m.get("control_number"))
    except (TypeError, ValueError):
        return None


def citation_count(m: dict[str, Any]) -> int:
    try:
        return int(m.get("citation_count", 0))
    except (TypeError, ValueError):
        return 0


def author_name(a: dict[str, Any]) -> str:
    first = (a.get("first_name") or a.get("given_name") or a.get("given_names") or "").strip()
    last = (a.get("last_name") or a.get("family_name") or a.get("family_names") or "").strip()
    if first and last:
        return f"{first} {last}"
    full = (a.get("full_name") or "").strip()
    if "," in full:
        family, given = [p.strip() for p in full.split(",", 1)]
        if given and family:
            return f"{given} {family}"
    return full


def authors(m: dict[str, Any], max_authors: int = 6) -> str:
    xs = [author_name(a) for a in m.get("authors", [])]
    xs = [x for x in xs if x]
    if not xs:
        return ""
    if len(xs) == 1:
        return xs[0]
    if len(xs) <= max_authors:
        return ", ".join(xs[:-1]) + ", and " + xs[-1]
    return ", ".join(xs[:max_authors]) + ", et al."


def pubinfo(m: dict[str, Any]) -> list[dict[str, Any]]:
    return [x for x in m.get("publication_info", []) if isinstance(x, dict)]


def is_published(m: dict[str, Any]) -> bool:
    for x in pubinfo(m):
        journal = str(x.get("journal_title", "")).strip()
        year = str(x.get("year", "")).strip()
        volume = str(x.get("journal_volume", "")).strip()
        artid = str(x.get("artid") or x.get("page_start") or "").strip()
        if journal and (year or volume or artid):
            return True
    return False


def journal_line(m: dict[str, Any]) -> str:
    xs = pubinfo(m)
    if xs:
        x = xs[0]
        journal = str(x.get("journal_title", "")).strip()
        volume = str(x.get("journal_volume", "")).strip()
        artid = str(x.get("artid") or x.get("page_start") or "").strip()
        year = str(x.get("year", "")).strip()
        parts = [p for p in [journal, volume, artid] if p]
        line = " ".join(parts)
        if year:
            line += f" ({year})"
        if line.strip():
            return line.strip()
    d = str(m.get("earliest_date", "")).strip()
    return f"arXiv preprint ({d})" if d else "arXiv preprint"


def parse_date(s: str) -> date | None:
    s = str(s).strip()
    if not s:
        return None
    for fmt, n in (("%Y-%m-%d", 10), ("%Y-%m", 7), ("%Y", 4)):
        try:
            return datetime.strptime(s[:n], fmt).date()
        except ValueError:
            pass
    return None


def record_date(m: dict[str, Any]) -> date | None:
    for key in ("earliest_date", "preprint_date", "date", "created"):
        d = parse_date(str(m.get(key, "")).strip())
        if d is not None:
            return d
    for x in pubinfo(m):
        y = x.get("year")
        if y:
            try:
                return date(int(y), 1, 1)
            except (TypeError, ValueError):
                pass
    return None


def date_key(m: dict[str, Any]) -> str:
    d = record_date(m)
    return d.isoformat() if d else ""


def paper_age_years(m: dict[str, Any]) -> float:
    d = record_date(m)
    if d is None:
        return 99.0
    return max((datetime.now(timezone.utc).date() - d).days / 365.25, 0.0)


def usable_papers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    papers = []
    for h in hits(payload):
        m = meta(h)
        if title(m) and arxiv(m):
            papers.append(m)
    return sorted(papers, key=date_key, reverse=True)


def arxiv_map(papers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {arxiv(p): p for p in papers if arxiv(p)}


def recent_citation_metrics(m: dict[str, Any]) -> dict[str, Any]:
    r = recid(m)
    total = citation_count(m)
    now = datetime.now(timezone.utc).date()
    cutoff_window = now - timedelta(days=RECENT_CITATION_WINDOW_DAYS)
    cutoff_12m = now - timedelta(days=365)

    recent_window = 0
    recent_12m = 0
    fetched = 0
    error = ""

    if r is not None:
        try:
            payload = fetch_citing_records(r)
            hs = hits(payload)
            fetched = len(hs)
            for h in hs:
                cm = meta(h)
                d = record_date(cm)
                if d is None:
                    continue
                if d >= cutoff_window:
                    recent_window += 1
                if d >= cutoff_12m:
                    recent_12m += 1
        except Exception as exc:
            error = str(exc)

    share = recent_window / total if total > 0 else 0.0
    age = paper_age_years(m)

    # Recent citations dominate. Total citations and recency are weak fallbacks,
    # so the section never becomes empty just because no paper passes a hard threshold.
    score = (
        recent_window * 10000
        + recent_12m * 3000
        + int(share * 500)
        + min(total, 300)
        + max(0, int(60 - age * 2))
    )

    return {
        "recid": r,
        "total_citations": total,
        "recent_citations_window": recent_window,
        "recent_citations_12m": recent_12m,
        "recent_citation_share": share,
        "paper_age_years": age,
        "attention_score": score,
        "fetched_citing_records": fetched,
        "window_days": RECENT_CITATION_WINDOW_DAYS,
        "error": error,
    }


def select_active_published(
    papers: list[dict[str, Any]],
    selected_ids: set[str],
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    candidates = [
        p for p in papers
        if arxiv(p) not in selected_ids and is_published(p) and recid(p) is not None
    ]

    # Keep API usage finite while allowing both cited classics and recent papers.
    candidates.sort(key=lambda p: (citation_count(p), date_key(p)), reverse=True)
    candidates = candidates[:MAX_ATTENTION_CANDIDATES]

    metrics: dict[str, dict[str, Any]] = {}
    for p in candidates:
        metrics[arxiv(p)] = recent_citation_metrics(p)

    candidates.sort(
        key=lambda p: (
            metrics[arxiv(p)]["attention_score"],
            metrics[arxiv(p)]["recent_citations_window"],
            date_key(p),
        ),
        reverse=True,
    )
    return candidates[:limit], metrics


def format_paper(m: dict[str, Any]) -> str:
    a = arxiv(m)
    arxiv_url = f"https://arxiv.org/abs/{a}"
    lines = [f"- **[{title(m)}]({arxiv_url})**  "]
    au = authors(m)
    if au:
        lines.append(f"  {au}  ")
    lines.append(f"  *{journal_line(m)}*  ")
    links = [f"[[arXiv:{a}]({arxiv_url})]"]
    d = doi(m)
    if d:
        links.append(f"[[DOI](https://doi.org/{d})]")
    lines.append("  " + " ".join(links))
    return "\n".join(lines)


def update_block(text: str, start: str, end: str, body: str) -> str:
    pat = re.compile(rf"{re.escape(start)}.*?{re.escape(end)}", flags=re.DOTALL)
    repl = f"{start}\n{body}\n{end}"
    new_text, count = pat.subn(lambda _m: repl, text)
    if count != 1:
        raise RuntimeError(f"Could not find exactly one block: {start} ... {end}")
    return new_text


def write_cache(selected: list[dict[str, Any]], active: list[dict[str, Any]], metrics: dict[str, dict[str, Any]]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    payload = {
        "selected_arxiv_ids": SELECTED_ARXIV_IDS,
        "ranking": {
            "window_days": RECENT_CITATION_WINDOW_DAYS,
            "notable_published_count": NOTABLE_PUBLISHED_COUNT,
            "citing_fetch_size": CITING_FETCH_SIZE,
            "max_attention_candidates": MAX_ATTENTION_CANDIDATES,
            "note": "Recent citation activity dominates the score; total citations and paper age are weak fallback tie-breakers.",
        },
        "selected": [
            {
                "title": title(p),
                "arxiv": arxiv(p),
                "citations_total": citation_count(p),
                "journal": journal_line(p),
            }
            for p in selected
        ],
        "recently_active_published": [
            {
                "title": title(p),
                "arxiv": arxiv(p),
                "journal": journal_line(p),
                **metrics.get(arxiv(p), {}),
            }
            for p in active
        ],
        "audited_candidates_by_score": sorted(
            [
                {"arxiv": k, **v}
                for k, v in metrics.items()
            ],
            key=lambda x: x.get("attention_score", 0),
            reverse=True,
        )[:50],
    }
    HIGHLIGHTS_CACHE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    payload = fetch_author_papers()
    papers = usable_papers(payload)
    amap = arxiv_map(papers)

    selected = []
    missing = []
    for aid in SELECTED_ARXIV_IDS:
        p = amap.get(aid)
        if p is None:
            missing.append(aid)
        else:
            selected.append(p)

    active, metrics = select_active_published(papers, set(SELECTED_ARXIV_IDS), NOTABLE_PUBLISHED_COUNT)

    selected_block = "\n\n".join(format_paper(p) for p in selected)
    if missing:
        selected_block += "\n\n- Missing selected arXiv IDs from INSPIRE fetch: " + ", ".join(missing)

    active_block = "\n\n".join(format_paper(p) for p in active)
    if not active_block:
        active_block = "- No published papers outside the selected list could be ranked automatically."

    write_cache(selected, active, metrics)

    with open(PUBLICATIONS_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    text = update_block(text, SELECTED_START, SELECTED_END, selected_block)
    text = update_block(text, NOTABLE_START, NOTABLE_END, active_block)

    with open(PUBLICATIONS_PATH, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"Updated publications.md: {len(selected)} selected, {len(active)} recently active published.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
