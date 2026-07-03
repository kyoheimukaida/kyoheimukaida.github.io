#!/usr/bin/env python3

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DATA_DIR = Path("_data")
GENERATED_DIR = Path("generated")

MANUAL_PATH = DATA_DIR / "grants_manual.json"
GRANTS_DATA_PATH = DATA_DIR / "grants.json"
GRANTS_MD_PATH = Path("grants.md")
CV_PATH = Path("cv.md")
GRANTS_TEX_PATH = GENERATED_DIR / "grants.tex"

GRANTS_START = "<!-- grants-auto:start -->"
GRANTS_END = "<!-- grants-auto:end -->"
CV_GRANTS_START = "<!-- cv-grants:start -->"
CV_GRANTS_END = "<!-- cv-grants:end -->"

DEFAULT_KAKEN_SEARCH_URLS = [
    "https://kaken.nii.ac.jp/en/search/?kw=Kyohei%20Mukaida",
    "https://kaken.nii.ac.jp/ja/search/?kw=%E5%90%91%E7%94%B0%20%E4%BA%AB%E5%B9%B3",
]

TARGET_NAME_PATTERNS = [
    r"Kyohei\s+Mukaida",
    r"Mukaida\s+Kyohei",
    r"向田\s*享平",
]

PUBLIC_FIELDS = [
    "source",
    "public",
    "funder",
    "program",
    "grant_number",
    "title_en",
    "title_ja",
    "role",
    "period",
    "start_year",
    "end_year",
    "url",
    "notes_public",
]


def warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_key_text(value: str) -> str:
    value = clean_text(value).lower()
    value = re.sub(r"[^a-z0-9\u3040-\u30ff\u3400-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_year(value: Any) -> int | None:
    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_grant(record: dict[str, Any]) -> dict[str, Any]:
    normalized = {key: record.get(key, "") for key in PUBLIC_FIELDS}
    normalized["public"] = bool(record.get("public", False))
    normalized["source"] = clean_text(normalized.get("source")) or "manual"
    normalized["funder"] = clean_text(normalized.get("funder"))
    normalized["program"] = clean_text(normalized.get("program"))
    normalized["grant_number"] = clean_text(normalized.get("grant_number"))
    normalized["title_en"] = clean_text(normalized.get("title_en"))
    normalized["title_ja"] = clean_text(normalized.get("title_ja"))
    normalized["role"] = clean_text(normalized.get("role"))
    normalized["period"] = clean_text(normalized.get("period"))
    normalized["start_year"] = normalize_year(normalized.get("start_year"))
    normalized["end_year"] = normalize_year(normalized.get("end_year"))
    normalized["url"] = clean_text(normalized.get("url"))
    normalized["notes_public"] = clean_text(normalized.get("notes_public"))
    return normalized


def public_manual_records(path: Path = MANUAL_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError(f"Expected {path} to contain a JSON array.")

    records: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, dict) and item.get("public") is True:
            records.append(normalize_grant(item))

    return records


def existing_records(path: Path = GRANTS_DATA_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        warn(f"Could not parse {path}: {exc}")
        return []

    if not isinstance(payload, list):
        warn(f"Expected {path} to contain a JSON array.")
        return []

    return [
        normalize_grant(item)
        for item in payload
        if isinstance(item, dict) and item.get("public") is True
    ]


def fetch_url(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "kyoheimukaida-grants-updater/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def kaken_search_urls() -> list[str]:
    raw = os.environ.get("KAKEN_SEARCH_URLS", "").strip()
    if raw:
        return [url.strip() for url in re.split(r"[\n,]+", raw) if url.strip()]
    return DEFAULT_KAKEN_SEARCH_URLS


def absolute_url(url: str) -> str:
    return urllib.parse.urljoin("https://kaken.nii.ac.jp/", url)


def discover_kaken_project_urls(html_text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r'href=["\']([^"\']*/grant/KAKENHI-PROJECT-[^"\']+)["\']', html_text):
        url = absolute_url(match.group(1))
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def html_title(html_text: str) -> str:
    for pattern in (
        r"<h1[^>]*>(.*?)</h1>",
        r"<h2[^>]*>(.*?)</h2>",
        r"<title[^>]*>(.*?)</title>",
    ):
        match = re.search(pattern, html_text, flags=re.DOTALL | re.IGNORECASE)
        if match:
            title = clean_text(match.group(1))
            title = re.sub(r"\s*\|\s*KAKEN.*$", "", title)
            if title:
                return title
    return ""


def text_from_html(html_text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_text, flags=re.DOTALL | re.IGNORECASE)
    return clean_text(text)


def extract_grant_number(url: str, text: str) -> str:
    match = re.search(r"KAKENHI-PROJECT-([A-Za-z0-9-]+)", url)
    if match:
        return match.group(1)

    match = re.search(r"(?:Grant Number|課題番号)\s*[:：]?\s*([A-Za-z0-9-]+)", text)
    return match.group(1).strip() if match else ""


def extract_period(text: str) -> tuple[str, int | None, int | None]:
    match = re.search(
        r"(?:Project Period \(FY\)|研究期間 \(年度\))\s*"
        r"((?:19|20)\d{2})-\d{2}-\d{2}\s*[-–]\s*"
        r"((?:19|20)\d{2})-\d{2}-\d{2}",
        text,
    )
    if match:
        start = int(match.group(1))
        end = int(match.group(2))
        return f"{start}-{end}", start, end

    match = re.search(
        r"(?:Project Period \(FY\)|研究期間 \(年度\))\s*"
        r"((?:19|20)\d{2})\s*[-–~]\s*((?:19|20)\d{2})",
        text,
    )
    if match:
        start = int(match.group(1))
        end = int(match.group(2))
        return f"{start}-{end}", start, end

    return "", None, None


def extract_program(text: str) -> str:
    field_patterns = [
        r"Research Category\s+(.+?)\s+(?:Japan Grant Number|Allocation Type|Review Section|Section)",
        r"研究種目\s+(.+?)\s+(?:体系的番号|配分区分|応募区分|審査区分)",
    ]
    for pattern in field_patterns:
        match = re.search(pattern, text)
        if match:
            return clean_text(match.group(1))

    patterns = [
        r"Grant-in-Aid for [A-Za-z0-9 /(),-]+",
        r"KAKENHI\s*\([A-Za-z0-9 /(),-]+\)",
        r"(?:基盤研究|若手研究|学術変革領域研究|新学術領域研究|挑戦的研究)[^ ]*",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return clean_text(match.group(0))
    return ""


def project_header_text(text: str) -> str:
    start_patterns = [
        r"Project/Area Number",
        r"研究課題/領域番号",
    ]
    end_patterns = [
        r"Budget Amount",
        r"Keywords",
        r"配分額",
        r"キーワード",
        r"URL:",
    ]

    start = 0
    for pattern in start_patterns:
        match = re.search(pattern, text)
        if match:
            start = match.start()
            break

    end = len(text)
    for pattern in end_patterns:
        match = re.search(pattern, text[start:])
        if match:
            end = start + match.start()
            break

    return text[start:end]


def extract_role(text: str) -> str:
    header = project_header_text(text)
    target_name = "|".join(TARGET_NAME_PATTERNS)
    role_patterns = [
        (rf"Principal Investigator\s+(?:{target_name})\b", "Principal Investigator"),
        (rf"Co-?Investigator\s+(?:{target_name})\b", "Co-Investigator"),
        (rf"Collaborator\s+(?:{target_name})\b", "Collaborator"),
        (rf"Research Collaborator\s+(?:{target_name})\b", "Research Collaborator"),
        (rf"研究代表者\s+(?:{target_name})", "Principal Investigator"),
        (rf"研究分担者\s+(?:{target_name})", "Co-Investigator"),
        (rf"連携研究者\s+(?:{target_name})", "Collaborator"),
    ]

    for pattern, role in role_patterns:
        if re.search(pattern, header, flags=re.IGNORECASE):
            return role

    return ""


def parse_kaken_project(html_text: str, url: str) -> dict[str, Any] | None:
    text = text_from_html(html_text)
    title = html_title(html_text)
    grant_number = extract_grant_number(url, text)
    period, start_year, end_year = extract_period(text)
    program = extract_program(text)
    role = extract_role(text)

    if not grant_number or not title or not role:
        return None

    return normalize_grant(
        {
            "source": "KAKEN",
            "public": True,
            "funder": "JSPS KAKENHI",
            "program": program,
            "grant_number": grant_number,
            "title_en": title if re.search(r"[A-Za-z]", title) else "",
            "title_ja": title if not re.search(r"[A-Za-z]", title) else "",
            "role": role,
            "period": period,
            "start_year": start_year,
            "end_year": end_year,
            "url": url,
            "notes_public": "",
        }
    )


def fetch_kaken_records() -> tuple[list[dict[str, Any]], bool]:
    if os.environ.get("KAKEN_FETCH", "1").strip().lower() in {"0", "false", "no"}:
        return [], True

    records: list[dict[str, Any]] = []
    project_urls: list[str] = []
    seen_urls: set[str] = set()

    try:
        for search_url in kaken_search_urls():
            search_html = fetch_url(search_url)
            for url in discover_kaken_project_urls(search_html):
                if url not in seen_urls:
                    seen_urls.add(url)
                    project_urls.append(url)
    except Exception as exc:
        warn(f"KAKEN search failed; using manual/existing records only: {exc}")
        return [], False

    for url in project_urls[:30]:
        try:
            record = parse_kaken_project(fetch_url(url), url)
        except Exception as exc:
            warn(f"Could not parse KAKEN project {url}: {exc}")
            continue
        if record is not None:
            records.append(record)

    return records, True


def dedup_key(record: dict[str, Any]) -> str:
    grant_number = normalize_key_text(record.get("grant_number", ""))
    if grant_number:
        return f"grant:{grant_number}"

    title = record.get("title_en") or record.get("title_ja") or ""
    return "title:" + "|".join(
        [
            normalize_key_text(title),
            normalize_key_text(record.get("period", "")),
        ]
    )


def merge_record(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key in PUBLIC_FIELDS:
        value = override.get(key)
        if value not in (None, ""):
            merged[key] = value
    merged["public"] = True
    return normalize_grant(merged)


def merge_records(
    fetched: list[dict[str, Any]],
    manual: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    fetch_ok: bool,
) -> list[dict[str, Any]]:
    source_records = fetched if fetch_ok else existing
    merged: dict[str, dict[str, Any]] = {}

    for record in source_records:
        key = dedup_key(record)
        if key not in merged:
            merged[key] = record
        else:
            merged[key] = merge_record(merged[key], record)

    for record in manual:
        key = dedup_key(record)
        if key in merged:
            merged[key] = merge_record(merged[key], record)
        else:
            merged[key] = record

    records = [record for record in merged.values() if record.get("public") is True]
    records = [
        record
        for record in records
        if record.get("grant_number") or record.get("title_en") or record.get("title_ja")
    ]

    return sorted(
        records,
        key=lambda record: (
            record.get("end_year") or 0,
            record.get("start_year") or 0,
            record.get("grant_number") or "",
            record.get("title_en") or record.get("title_ja") or "",
        ),
        reverse=True,
    )


def grant_title(record: dict[str, Any]) -> str:
    return record.get("title_en") or record.get("title_ja") or "Untitled grant"


def grant_heading(record: dict[str, Any]) -> str:
    funder = record.get("funder", "")
    program = record.get("program", "")
    return " / ".join(part for part in [funder, program] if part) or "Grant"


def format_grant_markdown(record: dict[str, Any]) -> str:
    title = grant_title(record)
    if record.get("url"):
        title_md = f"**[{title}]({record['url']})**"
    else:
        title_md = f"**{title}**"

    lines = [f"- {title_md}  "]
    details = [grant_heading(record)]
    if record.get("role"):
        details.append(record["role"])
    if record.get("period"):
        details.append(record["period"])
    if record.get("grant_number"):
        details.append(f"Grant No. {record['grant_number']}")

    lines.append("  " + ", ".join(details))

    if record.get("notes_public"):
        lines.append(f"  {record['notes_public']}")

    return "\n".join(lines)


def grants_markdown(records: list[dict[str, Any]]) -> str:
    if not records:
        return "No public grants are currently listed."
    return "\n\n".join(format_grant_markdown(record) for record in records)


def cv_grants_markdown(records: list[dict[str, Any]]) -> str:
    if not records:
        return "No public grants are currently listed."
    return "\n\n".join(format_grant_markdown(record) for record in records)


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
    return "".join(replacements.get(ch, ch) for ch in str(text))


def format_grant_tex(record: dict[str, Any]) -> str:
    details = [grant_heading(record)]
    if record.get("role"):
        details.append(record["role"])
    if record.get("period"):
        details.append(record["period"])
    if record.get("grant_number"):
        details.append(f"Grant No. {record['grant_number']}")

    return (
        rf"\item \textbf{{{tex_escape(grant_title(record))}}}\\ "
        + tex_escape(", ".join(details))
    )


def grants_tex(records: list[dict[str, Any]]) -> str:
    if not records:
        return "% Auto-generated grants list.\n% No public grants are currently listed.\n"

    return (
        "% Auto-generated grants list.\n"
        "\\begin{itemize}\n"
        + "\n\n".join(format_grant_tex(record) for record in records)
        + "\n\\end{itemize}\n"
    )


def update_block(text: str, start: str, end: str, body: str) -> str:
    pattern = re.compile(rf"{re.escape(start)}.*?{re.escape(end)}", flags=re.DOTALL)
    replacement = f"{start}\n{body}\n{end}"
    new_text, count = pattern.subn(lambda _match: replacement, text)

    if count != 1:
        raise RuntimeError(f"Could not find exactly one block: {start} ... {end}")

    return new_text


def ensure_grants_page(text: str) -> str:
    if GRANTS_START in text and GRANTS_END in text:
        return text

    return (
        "---\n"
        "layout: default\n"
        "title: Grants and Funding\n"
        "---\n\n"
        "# Grants and Funding\n\n"
        "This page lists public research funding records. It is generated from "
        "public KAKENHI information when available and from manually curated "
        "public records in `_data/grants_manual.json`.\n\n"
        '<div class="record-list" markdown="1">\n\n'
        f"{GRANTS_START}\nNo public grants are currently listed.\n{GRANTS_END}\n\n"
        "</div>\n"
    )


def ensure_cv_grants_section(text: str) -> str:
    if CV_GRANTS_START in text and CV_GRANTS_END in text:
        return text

    section = (
        "## Grants / Funding\n\n"
        f"{CV_GRANTS_START}\n"
        "No public grants are currently listed.\n"
        f"{CV_GRANTS_END}\n\n"
    )

    marker = "## Publication summary"
    if marker in text:
        return text.replace(marker, section + marker, 1)

    return text.rstrip() + "\n\n" + section


def write_outputs(records: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    GENERATED_DIR.mkdir(exist_ok=True)

    GRANTS_DATA_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    grants_text = ensure_grants_page(
        GRANTS_MD_PATH.read_text(encoding="utf-8") if GRANTS_MD_PATH.exists() else ""
    )
    grants_text = update_block(grants_text, GRANTS_START, GRANTS_END, grants_markdown(records))
    GRANTS_MD_PATH.write_text(grants_text, encoding="utf-8")

    if CV_PATH.exists():
        cv_text = ensure_cv_grants_section(CV_PATH.read_text(encoding="utf-8"))
        cv_text = update_block(cv_text, CV_GRANTS_START, CV_GRANTS_END, cv_grants_markdown(records))
        CV_PATH.write_text(cv_text, encoding="utf-8")
    else:
        warn(f"{CV_PATH} does not exist; skipping CV grants block.")

    GRANTS_TEX_PATH.write_text(grants_tex(records), encoding="utf-8")


def sanity_check() -> None:
    grants_text = GRANTS_MD_PATH.read_text(encoding="utf-8")
    if GRANTS_START not in grants_text or GRANTS_END not in grants_text:
        raise RuntimeError(f"{GRANTS_MD_PATH} is missing grants markers.")

    if CV_PATH.exists():
        cv_text = CV_PATH.read_text(encoding="utf-8")
        if CV_GRANTS_START not in cv_text or CV_GRANTS_END not in cv_text:
            raise RuntimeError(f"{CV_PATH} is missing CV grants markers.")

    if not GRANTS_TEX_PATH.exists():
        raise RuntimeError(f"{GRANTS_TEX_PATH} was not written.")

    rendered = grants_text
    if CV_PATH.exists():
        rendered += "\n" + CV_PATH.read_text(encoding="utf-8")
    rendered += "\n" + GRANTS_TEX_PATH.read_text(encoding="utf-8")

    private_terms = ["reviewer", "pending application", "application draft", "budget"]
    lowered = rendered.lower()
    for term in private_terms:
        if term in lowered:
            raise RuntimeError(f"Possible private grants term rendered: {term}")


def main() -> int:
    manual = public_manual_records()
    existing = existing_records()
    fetched, fetch_ok = fetch_kaken_records()
    records = merge_records(fetched, manual, existing, fetch_ok)

    if not fetch_ok and existing:
        warn("Remote fetch failed; preserved existing public grants data.")

    write_outputs(records)
    sanity_check()

    print(
        f"Updated grants: {len(records)} public record(s) "
        f"({len(fetched)} fetched, {len(manual)} manual)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
