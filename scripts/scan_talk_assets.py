#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from update_talks_from_ics import (
    HISTORY_PATH,
    TALK_ASSETS_PATH,
    TALKS_COMBINED_PATH,
    TalkEvent,
    attach_public_talk_assets,
    date_label_for_dedup,
    deduplicate_talks,
    load_cached_calendar_talks,
    load_history_talks,
    load_talk_asset_manifest,
    normalize_text_for_asset_match,
)

try:
    import fitz  # type: ignore[import-not-found]
except ImportError:
    fitz = None

try:
    from pypdf import PdfReader  # type: ignore[import-not-found]
except ImportError:
    PdfReader = None

logging.getLogger("pypdf").setLevel(logging.ERROR)


REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_DIRECTORY = Path(".local")
JSON_REPORT_NAME = "talk-asset-candidates.json"
MARKDOWN_REPORT_NAME = "talk-asset-candidates.md"
DECISIONS_REPORT_NAME = "talk-asset-decisions.json"
SUPPORTED_EXTENSIONS = {".pdf", ".pptx", ".key", ".odp"}
SOURCE_EXTENSIONS = {".pptx", ".key", ".odp"}
HIGH_CONFIDENCE_THRESHOLD = 75
MINIMUM_MATCH_SCORE = 35
REPORT_SCHEMA_VERSION = 3
DECISION_SCHEMA_VERSION = 1
DECISION_STATUSES = {
    "awaiting_approval",
    "surfaced",
    "approved",
    "rejected",
    "held",
    "published",
    "failed",
}

HARD_EXCLUDE_TOKENS = {
    "autosave",
    "backup",
    "backups",
    "bak",
    "recovered",
    "recovery",
    "temporary",
    "temp",
}
DRAFT_TOKENS = {
    "draft",
    "preliminary",
    "preview",
    "rough",
    "wip",
}
GENERIC_FILENAME_TOKENS = {
    "final",
    "keynote",
    "kmukaida",
    "mukaida",
    "presentation",
    "slide",
    "slides",
    "submitted",
    "talk",
}
GENERIC_METADATA_TITLES = {
    "keynote",
    "microsoft powerpoint",
    "powerpoint presentation",
    "presentation",
    "slides",
    "talk",
}
MATCH_STOPWORDS = {
    "a",
    "after",
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
PATH_ONLY_STOPWORDS = {
    "conference",
    "keynote",
    "lecture",
    "meeting",
    "pdf",
    "seminar",
    "slide",
    "slides",
    "talk",
    "workshop",
}
TOKEN_EXPANSIONS = {
    "axinf": {"axion", "inflation"},
    "axions": {"axion"},
    "axistock": {"axion", "stockholm"},
    "coll": {"collider"},
    "cosmo": {"cosmology"},
}
MONTH_PATTERN = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\b",
    flags=re.IGNORECASE,
)

CATEGORY_TITLES = {
    "high_confidence_talk_matches": "High-confidence talk matches",
    "ambiguous_matches": "Ambiguous matches",
    "no_website_talk_record": "No website talk record",
    "cover_text_unavailable": "Cover text unavailable",
    "non_final_or_low_priority_files": "Non-final or low-priority files",
}


@dataclass(frozen=True)
class TextLine:
    text: str
    font_size: float
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class PresentationFile:
    path: Path
    relative_path: Path
    extension: str
    digest: str
    is_draft: bool
    modified_date: str
    source_size: int
    source_mtime: str
    selection_reason: str
    presentation_source: str = ""
    duplicate_of: str = ""
    metadata_title: str = ""
    metadata_creator: str = ""
    first_page_text: str = ""
    cover_title: str = ""
    cover_title_candidates: tuple[str, ...] = ()
    extraction_method: str = "not_applicable"
    extraction_error: str = ""
    keynote_directory: bool = False
    key_sibling_exists: bool = False
    key_source_basename_matches: bool = False
    unique_pdf_with_key_source: bool = False
    conference_directory_evidence: str = ""


@dataclass(frozen=True)
class TalkScore:
    talk: TalkEvent
    score: int
    identity_score: int
    breakdown: dict[str, int]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MatchResult:
    best: TalkScore
    runner_up: TalkScore | None
    ambiguous: bool
    meets_minimum: bool


def instruction_for_mytalk_dir() -> str:
    return (
        "Set MYTALK_DIR to the local Dropbox/MyTalks directory, for example: "
        'MYTALK_DIR="$HOME/Library/CloudStorage/Dropbox/MyTalks" '
        "python3 scripts/scan_talk_assets.py"
    )


def pdf_dependency_instruction() -> str:
    return (
        "Install the page-one PDF extractor with "
        "`python3 -m pip install -r requirements-talk-assets.txt` "
        "(preferred package: PyMuPDF)."
    )


def common_mytalk_directories() -> list[Path]:
    home = Path.home()
    folder_names = ("MyTalks", "MyTalk", "Mytalk")
    candidates = [
        dropbox_root / folder_name
        for dropbox_root in (home / "Dropbox", home / "Dropbox (Personal)")
        for folder_name in folder_names
    ]

    cloud_storage = home / "Library" / "CloudStorage"
    if cloud_storage.is_dir():
        try:
            candidates.extend(
                item / folder_name
                for item in cloud_storage.iterdir()
                if item.is_dir() and item.name.lower().startswith("dropbox")
                for folder_name in folder_names
            )
        except OSError:
            pass

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def resolve_mytalk_directory() -> Path:
    configured = os.environ.get("MYTALK_DIR", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_dir():
            raise RuntimeError(
                "MYTALK_DIR is not an accessible directory. "
                + instruction_for_mytalk_dir()
            )
        return path.resolve()

    candidates = common_mytalk_directories()
    if not candidates:
        raise RuntimeError(
            "No Dropbox/MyTalks directory was found. " + instruction_for_mytalk_dir()
        )
    if len(candidates) > 1:
        raise RuntimeError(
            "Multiple Dropbox/MyTalks directories were found; refusing to choose one. "
            + instruction_for_mytalk_dir()
        )
    return candidates[0]


def normalize_visible_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\u00ad", "").replace("\u200b", "")
    return re.sub(r"\s+", " ", value).strip()


def normalized_tokens(value: str, *, path_only: bool = False) -> set[str]:
    ignored = MATCH_STOPWORDS | (PATH_ONLY_STOPWORDS if path_only else set())
    tokens: set[str] = set()
    for token in normalize_text_for_asset_match(value).split():
        if token in ignored or len(token) <= 1:
            continue
        tokens.update(TOKEN_EXPANSIONS.get(token, {token}))
    return tokens


def file_name_tokens(path: Path) -> set[str]:
    return normalized_tokens(path.stem)


def is_generic_filename(path: Path) -> bool:
    tokens = file_name_tokens(path)
    return not tokens or tokens <= GENERIC_FILENAME_TOKENS


def is_hidden_path(relative_path: Path) -> bool:
    return any(part.startswith(".") for part in relative_path.parts)


def is_hard_excluded(path: Path) -> bool:
    lower_name = path.name.lower()
    if lower_name == ".ds_store" or lower_name.startswith(("~$", "~")):
        return True
    if path.suffix.lower() in {".bak", ".tmp", ".temp", ".swp"}:
        return True
    return bool(file_name_tokens(path) & HARD_EXCLUDE_TOKENS)


def is_draft_file(path: Path) -> bool:
    return bool(file_name_tokens(path) & DRAFT_TOKENS)


def canonical_file_stem(path: Path) -> str:
    ignored = {
        "copy",
        "final",
        "presentation",
        "revised",
        "revision",
        "slide",
        "slides",
        "submitted",
        "talk",
    } | DRAFT_TOKENS | HARD_EXCLUDE_TOKENS
    tokens = [
        token
        for token in normalize_text_for_asset_match(path.stem).split()
        if token not in ignored
        and not re.fullmatch(r"(?:rev|v|ver|version)\d+", token)
    ]
    return " ".join(tokens)


def content_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_dir():
        digest.update(b"keynote-package\0")
        for nested in sorted(item for item in path.rglob("*") if item.is_file()):
            try:
                relative_name = str(nested.relative_to(path)).encode(
                    "utf-8",
                    errors="surrogateescape",
                )
                size = nested.stat().st_size
            except OSError:
                continue
            digest.update(relative_name)
            digest.update(b"\0")
            digest.update(str(size).encode("ascii"))
            digest.update(b"\0")
        return digest.hexdigest()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_presentation_paths(source: Path) -> list[Path]:
    discovered: list[Path] = []

    def on_walk_error(_error: OSError) -> None:
        print(
            "Warning: one unreadable Dropbox directory was skipped.",
            file=sys.stderr,
        )

    for directory, directory_names, file_names in os.walk(source, onerror=on_walk_error):
        parent = Path(directory)
        descend_into: list[str] = []
        for name in directory_names:
            path = parent / name
            if name.startswith(".") or file_name_tokens(path) & HARD_EXCLUDE_TOKENS:
                continue
            if path.suffix.lower() == ".key":
                if not path.is_symlink():
                    discovered.append(path)
                continue
            descend_into.append(name)
        directory_names[:] = descend_into

        for file_name in file_names:
            path = parent / file_name
            try:
                relative_path = path.relative_to(source)
            except ValueError:
                continue
            if (
                path.suffix.lower() not in SUPPORTED_EXTENSIONS
                or is_hidden_path(relative_path)
                or is_hard_excluded(path)
                or path.is_symlink()
                or not path.is_file()
            ):
                continue
            discovered.append(path)

    return discovered


def directory_evidence(relative_path: Path) -> str:
    useful: list[str] = []
    for part in relative_path.parent.parts:
        normalized = normalize_visible_text(part.replace("_", " ").replace("-", " "))
        if not normalized or normalize_text_for_asset_match(normalized) in {
            "keynote",
            "pdf",
            "slides",
        }:
            continue
        useful.append(normalized)
    return " / ".join(useful)


def quick_pdf_creator(path: Path) -> str:
    try:
        if fitz is not None:
            with fitz.open(path) as document:
                metadata = document.metadata or {}
                return normalize_visible_text(
                    " ".join(
                        str(metadata.get(key) or "")
                        for key in ("creator", "producer")
                    )
                ).lower()
        if PdfReader is not None:
            metadata = PdfReader(str(path)).metadata or {}
            return normalize_visible_text(
                " ".join(
                    str(metadata.get(key) or "")
                    for key in ("/Creator", "/Producer")
                )
            ).lower()
    except Exception:
        return ""
    return ""


def preferred_pdf_for_source(source_path: Path, folder_pdfs: list[Path]) -> tuple[Path | None, str]:
    source_stem = canonical_file_stem(source_path)
    matching_stems = [
        pdf
        for pdf in folder_pdfs
        if source_stem and canonical_file_stem(pdf) == source_stem
    ]
    if matching_stems:
        return (
            sorted(
                matching_stems,
                key=lambda path: (is_draft_file(path), len(path.name), path.name.lower()),
            )[0],
            "PDF basename matches a presentation source in the same talk directory",
        )
    if len(folder_pdfs) == 1:
        return (
            folder_pdfs[0],
            "only PDF beside a presentation source in the same talk directory",
        )

    creator_hints = {
        ".key": ("keynote",),
        ".pptx": ("powerpoint",),
        ".odp": ("libreoffice", "openoffice"),
    }
    hints = creator_hints.get(source_path.suffix.lower(), ())
    creator_exports = [
        pdf
        for pdf in folder_pdfs
        if any(hint in quick_pdf_creator(pdf) for hint in hints)
    ]
    if len(creator_exports) == 1:
        return (
            creator_exports[0],
            "unique PDF generated by the presentation application in the same talk directory",
        )
    return None, ""


def build_presentation_files(source: Path, paths: list[Path]) -> list[PresentationFile]:
    sorted_paths = sorted(paths, key=lambda path: str(path.relative_to(source)).lower())
    pdfs_by_parent: dict[Path, list[Path]] = {}
    keys_by_parent: dict[Path, list[Path]] = {}
    for path in sorted_paths:
        relative = path.relative_to(source)
        if path.suffix.lower() == ".pdf":
            pdfs_by_parent.setdefault(relative.parent, []).append(path)
        elif path.suffix.lower() == ".key":
            keys_by_parent.setdefault(relative.parent, []).append(path)

    selected_entries: list[tuple[Path, str, Path]] = []
    selected_paths: set[Path] = set()
    presentation_sources = [
        path for path in sorted_paths if path.suffix.lower() in SOURCE_EXTENSIONS
    ]
    for source_path in presentation_sources:
        relative_source = source_path.relative_to(source)
        preferred_pdf, selection_reason = preferred_pdf_for_source(
            source_path,
            pdfs_by_parent.get(relative_source.parent, []),
        )
        selected_path = preferred_pdf or source_path
        if selected_path in selected_paths:
            continue
        selected_paths.add(selected_path)
        selected_entries.append(
            (
                selected_path,
                selection_reason
                or f"{source_path.suffix.lower()} source has no unambiguous PDF export in the same directory",
                source_path,
            )
        )

    seen_digests: dict[str, str] = {}
    files: list[PresentationFile] = []
    for path, selection_reason, source_path in selected_entries:
        relative = path.relative_to(source)
        extension = path.suffix.lower()
        sibling_pdfs = pdfs_by_parent.get(relative.parent, [])
        sibling_keys = keys_by_parent.get(relative.parent, [])
        keynote_directory = any(
            normalize_text_for_asset_match(part) == "keynote"
            for part in relative.parent.parts
        )
        key_sibling_exists = extension == ".pdf" and bool(sibling_keys)
        stem = canonical_file_stem(path)
        matching_keys = [
            key_path
            for key_path in sibling_keys
            if canonical_file_stem(key_path)
            and canonical_file_stem(key_path) == stem
        ]
        key_source_basename_matches = extension == ".pdf" and bool(matching_keys)
        unique_pdf_with_key_source = (
            extension == ".pdf" and len(sibling_pdfs) == 1 and bool(sibling_keys)
        )

        if extension == ".pdf":
            if key_source_basename_matches:
                presentation_source = str(matching_keys[0].relative_to(source))
            elif unique_pdf_with_key_source:
                presentation_source = str(sibling_keys[0].relative_to(source))
            elif keynote_directory:
                presentation_source = (
                    str(source_path.relative_to(source))
                )
            elif key_sibling_exists:
                presentation_source = str(sibling_keys[0].relative_to(source))
            else:
                presentation_source = str(source_path.relative_to(source))
        else:
            presentation_source = str(relative)

        try:
            digest = content_digest(path)
            file_stat = path.stat()
            modified_date = datetime.fromtimestamp(file_stat.st_mtime).date().isoformat()
            source_size = file_stat.st_size
            source_mtime = datetime.fromtimestamp(
                file_stat.st_mtime,
                tz=timezone.utc,
            ).isoformat()
        except OSError:
            print("Warning: one unreadable presentation file was skipped.", file=sys.stderr)
            continue

        duplicate_key = f"{digest}\x1f{canonical_file_stem(path)}"
        duplicate_of = seen_digests.get(duplicate_key, "")
        if not duplicate_of:
            seen_digests[duplicate_key] = str(relative)
        files.append(
            PresentationFile(
                path=path,
                relative_path=relative,
                extension=extension,
                digest=digest,
                is_draft=is_draft_file(path),
                modified_date=modified_date,
                source_size=source_size,
                source_mtime=source_mtime,
                selection_reason=selection_reason,
                presentation_source=presentation_source,
                duplicate_of=duplicate_of,
                keynote_directory=keynote_directory,
                key_sibling_exists=key_sibling_exists,
                key_source_basename_matches=key_source_basename_matches,
                unique_pdf_with_key_source=unique_pdf_with_key_source,
                conference_directory_evidence=directory_evidence(relative),
            )
        )
    return files


def obvious_non_title_line(line: TextLine, page_height: float) -> bool:
    text = normalize_visible_text(line.text)
    normalized = normalize_text_for_asset_match(text)
    if not normalized or line.y0 > page_height * 0.84:
        return True
    if re.fullmatch(r"(?:slide\s*)?\d+(?:\s*/\s*\d+)?", normalized):
        return True
    if re.search(r"(?:https?://|www\.|\S+@\S+)", text, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"[\d\s./,:-]+", text):
        return True
    if MONTH_PATTERN.search(text) and re.search(r"\b(?:19|20)\d{2}\b", text):
        return True
    if re.search(
        r"\b(?:university|universit[aä]t|institute|institut|department|"
        r"laboratory|collaboration|research center|research centre)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return True
    author_tokens = normalized_tokens(text)
    if author_tokens and author_tokens <= {"kyohei", "mukaida", "向田", "京平"}:
        return True
    return False


def title_candidates_from_lines(
    lines: list[TextLine],
    page_height: float,
) -> tuple[str, ...]:
    visible = [line for line in lines if not obvious_non_title_line(line, page_height)]
    if not visible:
        return ()
    max_size = max(line.font_size for line in visible)
    minimum_size = max(13.0, max_size * 0.58)
    eligible = [
        line
        for line in visible
        if line.font_size >= minimum_size and line.y0 <= page_height * 0.76
    ]
    if not eligible:
        eligible = [line for line in visible if line.y0 <= page_height * 0.76][:4]
    eligible.sort(key=lambda line: (line.y0, line.x0))

    groups: list[list[TextLine]] = []
    for line in eligible:
        if not groups:
            groups.append([line])
            continue
        previous = groups[-1][-1]
        gap = line.y0 - previous.y1
        size_ratio = min(line.font_size, previous.font_size) / max(
            line.font_size, previous.font_size
        )
        if gap <= max(page_height * 0.055, max(line.font_size, previous.font_size) * 1.1) and size_ratio >= 0.62:
            groups[-1].append(line)
        else:
            groups.append([line])

    ranked: list[tuple[float, str]] = []
    for group in groups:
        for start in range(len(group)):
            for end in range(start + 1, min(len(group), start + 4) + 1):
                selected = group[start:end]
                text = normalize_visible_text(" ".join(line.text for line in selected))
                tokens = normalized_tokens(text)
                if not tokens or len(text) > 320:
                    continue
                largest = max(line.font_size for line in selected)
                average = sum(line.font_size for line in selected) / len(selected)
                top = min(line.y0 for line in selected)
                position_bonus = max(0.0, 15.0 * (1.0 - top / max(page_height, 1.0)))
                length_bonus = min(len(tokens), 12) * 0.8
                multi_line_bonus = 3.0 if len(selected) > 1 else 0.0
                ranked.append(
                    (
                        largest * 2.5 + average + position_bonus + length_bonus + multi_line_bonus,
                        text,
                    )
                )

    candidates: list[str] = []
    for _score, text in sorted(ranked, reverse=True):
        normalized = normalize_text_for_asset_match(text)
        if not normalized or any(
            normalized == normalize_text_for_asset_match(existing)
            or normalized in normalize_text_for_asset_match(existing)
            for existing in candidates
        ):
            continue
        candidates.append(text)
        if len(candidates) == 4:
            break
    return tuple(candidates)


def pymupdf_extract(path: Path) -> dict[str, object]:
    if fitz is None:
        raise RuntimeError("PyMuPDF unavailable")
    with fitz.open(path) as document:
        metadata = document.metadata or {}
        if document.page_count < 1:
            return {
                "metadata_title": normalize_visible_text(metadata.get("title", ""))[:500],
                "metadata_creator": normalize_visible_text(metadata.get("creator", ""))[:500],
                "first_page_text": "",
                "cover_title_candidates": (),
            }
        page = document.load_page(0)
        page_dict = page.get_text("dict", sort=True)
        lines: list[TextLine] = []
        raw_lines: list[str] = []
        for block in page_dict.get("blocks", []):
            for raw_line in block.get("lines", []):
                spans = [span for span in raw_line.get("spans", []) if span.get("text")]
                text = normalize_visible_text(" ".join(str(span["text"]) for span in spans))
                if not text:
                    continue
                raw_lines.append(text)
                bbox = raw_line.get("bbox") or (
                    min(float(span["bbox"][0]) for span in spans),
                    min(float(span["bbox"][1]) for span in spans),
                    max(float(span["bbox"][2]) for span in spans),
                    max(float(span["bbox"][3]) for span in spans),
                )
                lines.append(
                    TextLine(
                        text=text,
                        font_size=max(float(span.get("size", 0.0)) for span in spans),
                        x0=float(bbox[0]),
                        y0=float(bbox[1]),
                        x1=float(bbox[2]),
                        y1=float(bbox[3]),
                    )
                )
        first_page_text = "\n".join(raw_lines)[:20000]
        return {
            "metadata_title": normalize_visible_text(metadata.get("title", ""))[:500],
            "metadata_creator": normalize_visible_text(metadata.get("creator", ""))[:500],
            "first_page_text": first_page_text,
            "cover_title_candidates": title_candidates_from_lines(lines, float(page.rect.height)),
        }


def fallback_title_candidates(text: str) -> tuple[str, ...]:
    lines = [
        TextLine(line, 18.0, 0.0, float(index * 20), 1.0, float(index * 20 + 18))
        for index, raw_line in enumerate(text.splitlines()[:12])
        if (line := normalize_visible_text(raw_line))
    ]
    return title_candidates_from_lines(lines, max(240.0, len(lines) * 24.0))


def pypdf_extract(path: Path) -> dict[str, object]:
    if PdfReader is None:
        raise RuntimeError("pypdf unavailable")
    reader = PdfReader(str(path))
    metadata = reader.metadata or {}
    text = reader.pages[0].extract_text() if reader.pages else ""
    first_page_text = "\n".join(
        normalize_visible_text(line) for line in (text or "").splitlines() if line.strip()
    )[:20000]
    return {
        "metadata_title": normalize_visible_text(str(metadata.get("/Title", "")))[:500],
        "metadata_creator": normalize_visible_text(str(metadata.get("/Creator", "")))[:500],
        "first_page_text": first_page_text,
        "cover_title_candidates": fallback_title_candidates(first_page_text),
    }


def enrich_pdf_file(item: PresentationFile) -> PresentationFile:
    if item.extension != ".pdf":
        return item
    try:
        if fitz is not None:
            extracted = pymupdf_extract(item.path)
            method = "pymupdf_spans"
        elif PdfReader is not None:
            extracted = pypdf_extract(item.path)
            method = "pypdf_first_page_text"
        else:
            return replace(
                item,
                extraction_method="unavailable_missing_dependency",
                extraction_error=pdf_dependency_instruction(),
            )
    except Exception as error:  # corrupt or unsupported PDFs remain reviewable
        return replace(
            item,
            extraction_method="cover_text_unavailable",
            extraction_error=type(error).__name__,
        )

    candidates = tuple(extracted["cover_title_candidates"])
    text = str(extracted["first_page_text"])
    return replace(
        item,
        metadata_title=str(extracted["metadata_title"]),
        metadata_creator=str(extracted["metadata_creator"]),
        first_page_text=text,
        cover_title=candidates[0] if candidates else "",
        cover_title_candidates=candidates,
        extraction_method=method if text else "cover_text_unavailable",
    )


def token_coverage(needles: set[str], haystack: set[str]) -> tuple[int, float]:
    if not needles:
        return 0, 0.0
    overlap = len(needles & haystack)
    return overlap, overlap / len(needles)


def title_evidence_score(talk_title: str, evidence: str, maximum: int) -> tuple[int, str]:
    title_normalized = normalize_text_for_asset_match(talk_title)
    evidence_normalized = normalize_text_for_asset_match(evidence)
    if not title_normalized or not evidence_normalized:
        return 0, ""
    title_tokens = normalized_tokens(talk_title)
    evidence_tokens = normalized_tokens(evidence)
    overlap, coverage = token_coverage(title_tokens, evidence_tokens)
    precision = overlap / len(evidence_tokens) if evidence_tokens else 0.0
    exact = title_normalized in evidence_normalized or evidence_normalized in title_normalized
    sequence = SequenceMatcher(None, title_normalized, evidence_normalized).ratio()

    if exact and (overlap >= 2 or len(title_tokens) <= 2):
        return maximum, "normalized title matches"
    if overlap >= 2 and coverage >= 0.80:
        return round(maximum * (0.82 + 0.18 * precision)), "strong title-token match"
    if overlap >= 2 and coverage >= 0.60:
        return round(maximum * (0.58 + 0.25 * coverage + 0.10 * precision)), "compatible title-token match"
    if overlap >= 2 and (coverage >= 0.40 or sequence >= 0.58):
        return round(maximum * (0.30 + 0.30 * coverage + 0.10 * precision)), "partial title-token match"
    if len(title_tokens) <= 2 and overlap == len(title_tokens) and precision >= 0.5:
        return round(maximum * 0.72), "short title-token match"
    return 0, ""


def best_cover_title_score(item: PresentationFile, talk: TalkEvent) -> tuple[int, str, str]:
    best = (0, "", "")
    for candidate in item.cover_title_candidates:
        score, reason = title_evidence_score(talk.title, candidate, 80)
        if score > best[0]:
            best = (score, reason, candidate)
    return best


def path_context(item: PresentationFile) -> str:
    return " ".join(item.relative_path.parent.parts)


def date_path_score(date_label: str, context: str) -> tuple[int, str]:
    digits = re.sub(r"\D", "", context)
    parts = date_label.split("-")
    year = parts[0]
    if len(parts) == 3:
        full = "".join(parts)
        separated = r"\D*".join(re.escape(part) for part in parts)
        if full in digits or re.search(rf"(?<!\d){separated}(?!\d)", context):
            return 35, "full talk date appears in the directory path"
    if len(parts) >= 2:
        year_month = "".join(parts[:2])
        separated = r"\D*".join(re.escape(part) for part in parts[:2])
        if year_month in digits or re.search(rf"(?<!\d){separated}(?!\d)", context):
            return 25, "talk year and month appear in the directory path"
    if re.search(rf"(?<!\d){re.escape(year)}(?!\d)", context):
        return (20 if len(date_label) == 4 else 15), "talk year appears in the directory path"
    return 0, ""


def directory_similarity_score(
    talk_value: str,
    context: str,
    maximum: int,
    label: str,
) -> tuple[int, str]:
    talk_tokens = normalized_tokens(talk_value, path_only=True)
    context_tokens = normalized_tokens(context, path_only=True)
    overlap, coverage = token_coverage(talk_tokens, context_tokens)
    if not overlap:
        return 0, ""
    minimum = 1 if len(talk_tokens) <= 2 else 2
    if overlap < minimum and coverage < 0.5:
        return 0, ""
    score = max(1, round(maximum * coverage))
    return score, f"{overlap}/{len(talk_tokens)} informative {label} tokens appear in directories"


def filename_score(item: PresentationFile, talk: TalkEvent) -> tuple[int, str]:
    if is_generic_filename(item.path):
        return 0, "generic filename contributes no title evidence"
    score, reason = title_evidence_score(talk.title, item.path.stem, 6)
    return score, (f"weak filename fallback: {reason}" if reason else "")


def quality_bonus(item: PresentationFile) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if item.keynote_directory:
        score += 5
        reasons.append("PDF is under an application directory named keynote")
    if item.key_sibling_exists:
        score += 5
        reasons.append("Keynote source exists in the same talk directory")
    if item.key_source_basename_matches:
        score += 4
        reasons.append("PDF basename corresponds to the nearby Keynote source")
    elif item.unique_pdf_with_key_source:
        score += 3
        reasons.append("directory contains one PDF and a Keynote source")
    return min(score, 12), reasons


def score_file_for_talk(item: PresentationFile, talk: TalkEvent) -> TalkScore:
    breakdown = {
        "cover_title": 0,
        "date_path": 0,
        "event_directory": 0,
        "location_directory": 0,
        "pdf_metadata_title": 0,
        "filename_fallback": 0,
        "candidate_quality": 0,
        "draft_penalty": 0,
    }
    reasons: list[str] = []

    cover_score, cover_reason, _candidate = best_cover_title_score(item, talk)
    breakdown["cover_title"] = cover_score
    if cover_reason:
        reasons.append(f"visible cover title: {cover_reason}")

    directory_context = path_context(item)
    date_score, date_reason = date_path_score(date_label_for_dedup(talk), directory_context)
    breakdown["date_path"] = date_score
    if date_reason:
        reasons.append(date_reason)

    event_score, event_reason = directory_similarity_score(
        talk.event,
        directory_context,
        24,
        "event",
    )
    breakdown["event_directory"] = event_score
    if event_reason:
        reasons.append(event_reason)

    location_score, location_reason = directory_similarity_score(
        talk.location,
        directory_context,
        10,
        "location",
    )
    breakdown["location_directory"] = location_score
    if location_reason:
        reasons.append(location_reason)

    metadata_normalized = normalize_text_for_asset_match(item.metadata_title)
    if metadata_normalized and metadata_normalized not in GENERIC_METADATA_TITLES:
        metadata_score, metadata_reason = title_evidence_score(
            talk.title,
            item.metadata_title,
            12,
        )
        breakdown["pdf_metadata_title"] = metadata_score
        if metadata_reason:
            reasons.append(f"PDF metadata title: {metadata_reason}")

    fallback_score, fallback_reason = filename_score(item, talk)
    breakdown["filename_fallback"] = fallback_score
    if fallback_reason and fallback_score:
        reasons.append(fallback_reason)

    candidate_quality, quality_reasons = quality_bonus(item)
    breakdown["candidate_quality"] = candidate_quality
    reasons.extend(quality_reasons)

    if item.is_draft:
        breakdown["draft_penalty"] = -20
        reasons.append("draft-like filename lowers candidate priority")

    identity_score = sum(
        breakdown[key]
        for key in (
            "cover_title",
            "date_path",
            "event_directory",
            "location_directory",
            "pdf_metadata_title",
            "filename_fallback",
        )
    )
    score = max(0, identity_score + candidate_quality + breakdown["draft_penalty"])
    return TalkScore(
        talk=talk,
        score=score,
        identity_score=identity_score,
        breakdown=breakdown,
        reasons=tuple(reasons),
    )


def plausible_talk_scores(item: PresentationFile, talks: list[TalkEvent]) -> list[TalkScore]:
    scored = [score_file_for_talk(item, talk) for talk in talks]
    return [
        result
        for result in scored
        if result.breakdown["cover_title"] >= 20
        or result.breakdown["date_path"] + result.breakdown["event_directory"] >= 15
        or result.breakdown["event_directory"] + result.breakdown["location_directory"] >= 12
        or result.breakdown["pdf_metadata_title"] >= 6
    ]


def decisive_directory_separation(
    best_breakdown: dict[str, int],
    runner_up_breakdown: dict[str, int],
) -> bool:
    directory_keys = ("date_path", "event_directory", "location_directory")
    best_support = sum(best_breakdown.get(key, 0) for key in directory_keys)
    runner_up_support = sum(runner_up_breakdown.get(key, 0) for key in directory_keys)
    return best_support >= 10 and best_support >= runner_up_support + 10


def best_talk_match(item: PresentationFile, talks: list[TalkEvent]) -> MatchResult | None:
    plausible = plausible_talk_scores(item, talks)
    plausible.sort(
        key=lambda result: (
            result.score,
            result.identity_score,
            date_label_for_dedup(result.talk),
            result.talk.title,
        ),
        reverse=True,
    )
    if not plausible:
        return None
    best = plausible[0]
    meets_minimum = best.score >= MINIMUM_MATCH_SCORE and best.identity_score >= 30
    runner_up = plausible[1] if len(plausible) > 1 else None
    close_runner_up = bool(
        runner_up
        and runner_up.identity_score >= 30
        and runner_up.score >= best.score - 10
        and not decisive_directory_separation(best.breakdown, runner_up.breakdown)
        and (
            date_label_for_dedup(runner_up.talk),
            normalize_text_for_asset_match(runner_up.talk.title),
        )
        != (
            date_label_for_dedup(best.talk),
            normalize_text_for_asset_match(best.talk.title),
        )
    )
    insufficient_title_separation = (
        best.breakdown["cover_title"] < 40 and best.score < HIGH_CONFIDENCE_THRESHOLD
    )
    return MatchResult(
        best=best,
        runner_up=runner_up,
        ambiguous=close_runner_up or insufficient_title_separation,
        meets_minimum=meets_minimum,
    )


def confidence_label(match: MatchResult | None) -> str:
    if match is None or not match.meets_minimum:
        return "none"
    if (
        not match.ambiguous
        and match.best.score >= HIGH_CONFIDENCE_THRESHOLD
        and match.best.identity_score >= 60
        and match.best.breakdown["cover_title"] >= 40
    ):
        return "high"
    return "ambiguous"


def classify_file(item: PresentationFile, match: MatchResult | None) -> str:
    if item.extension != ".pdf" or item.is_draft or item.duplicate_of:
        return "non_final_or_low_priority_files"
    if not item.first_page_text:
        return "cover_text_unavailable"
    if confidence_label(match) == "high":
        return "high_confidence_talk_matches"
    if match is not None and match.meets_minimum:
        return "ambiguous_matches"
    return "no_website_talk_record"


def legacy_filename_title_gate_passed(item: PresentationFile, match: MatchResult | None) -> bool:
    if match is None or not match.meets_minimum or is_generic_filename(item.path):
        return False
    title_tokens = normalized_tokens(match.best.talk.title)
    filename_tokens = file_name_tokens(item.path)
    overlap = len(title_tokens & filename_tokens)
    minimum = 1 if len(title_tokens) <= 2 else 2
    return overlap >= minimum


def load_talks() -> list[TalkEvent]:
    calendar = load_cached_calendar_talks(REPO_ROOT / TALKS_COMBINED_PATH)
    history = load_history_talks(REPO_ROOT / HISTORY_PATH)
    talks = deduplicate_talks(calendar + history)
    entries = load_talk_asset_manifest(REPO_ROOT / TALK_ASSETS_PATH)
    return attach_public_talk_assets(talks, entries)


def stable_candidate_id(
    talk_date: str,
    talk_title: str,
    source_sha256: str,
) -> str:
    identity = "\x1f".join(
        [
            talk_date,
            normalize_text_for_asset_match(talk_title),
            source_sha256.lower(),
        ]
    )
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8].upper()
    return f"TA-{talk_date}-{suffix}"


def candidate_key(candidate: dict[str, object]) -> str:
    candidate_id = str(candidate.get("candidate_id", ""))
    if candidate_id:
        return candidate_id
    return stable_candidate_id(
        str(candidate.get("talk_date", "")),
        str(candidate.get("talk_title", "")),
        str(candidate.get("source_sha256") or candidate.get("content_sha256", "")),
    )


def load_previous_candidate_keys(report_path: Path) -> set[str]:
    if not report_path.exists():
        return set()
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    candidates = payload.get("candidates", []) if isinstance(payload, dict) else []
    if not isinstance(candidates, list):
        return set()
    return {
        candidate_key(candidate)
        for candidate in candidates
        if isinstance(candidate, dict)
        and ("source_sha256" in candidate or "content_sha256" in candidate)
    }


def load_local_decisions(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Invalid JSON in the local talk-asset decision file: "
            f"line {error.lineno}, column {error.colno}."
        ) from None
    if not isinstance(payload, dict):
        raise RuntimeError("The local talk-asset decision file must be a JSON object.")
    raw_decisions = payload.get("decisions", {})
    if not isinstance(raw_decisions, dict):
        raise RuntimeError("The local talk-asset decisions field must be an object.")

    decisions: dict[str, dict[str, object]] = {}
    for candidate_id, raw_decision in raw_decisions.items():
        if not isinstance(candidate_id, str) or not isinstance(raw_decision, dict):
            raise RuntimeError("A local talk-asset decision entry is invalid.")
        status = raw_decision.get("status")
        if status not in DECISION_STATUSES:
            raise RuntimeError(
                f"Local decision {candidate_id!r} has an unsupported status."
            )
        decisions[candidate_id] = raw_decision
    return decisions


def write_local_decisions(
    path: Path,
    decisions: dict[str, dict[str, object]],
) -> None:
    payload = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "decisions": {key: decisions[key] for key in sorted(decisions)},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def review_priority(record: dict[str, object]) -> tuple[int, int, str, str]:
    return (
        int(record.get("score", 0)),
        int(record.get("identity_score", 0)),
        str(record.get("talk_date", "")),
        str(record.get("candidate_id", "")),
    )


def surface_review_candidate(
    records: list[dict[str, object]],
    decisions: dict[str, dict[str, object]],
    decisions_path: Path,
) -> dict[str, object] | None:
    by_id = {
        str(record["candidate_id"]): record
        for record in records
        if record.get("candidate_id")
    }
    surfaced_ids = [
        candidate_id
        for candidate_id, decision in decisions.items()
        if decision.get("status") == "surfaced"
    ]
    if len(surfaced_ids) > 1:
        raise RuntimeError(
            "Multiple talk-slide candidates are marked surfaced; "
            "the local decision queue must be repaired."
        )

    if surfaced_ids:
        candidate_id = surfaced_ids[0]
        current = by_id.get(candidate_id)
        if (
            current
            and current.get("classification") == "high_confidence_talk_matches"
            and not current.get("already_published")
        ):
            current["status"] = "surfaced"
            return current
        previous = decisions[candidate_id]
        decisions[candidate_id] = {
            **previous,
            "status": "held",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "reason": "surfaced candidate is no longer an unpublished high-confidence match",
        }

    unsurfaced = [
        record
        for record in records
        if record.get("classification") == "high_confidence_talk_matches"
        and record.get("status") == "awaiting_approval"
        and not decisions.get(str(record.get("candidate_id", "")), {}).get(
            "surfaced_at"
        )
    ]
    if not unsurfaced:
        if surfaced_ids:
            write_local_decisions(decisions_path, decisions)
        return None

    candidate = max(unsurfaced, key=review_priority)
    candidate_id = str(candidate["candidate_id"])
    timestamp = datetime.now(timezone.utc).isoformat()
    decisions[candidate_id] = {
        "status": "surfaced",
        "updated_at": timestamp,
        "surfaced_at": timestamp,
        "talk_date": str(candidate.get("talk_date", "")),
        "talk_title_normalized": normalize_text_for_asset_match(
            str(candidate.get("talk_title", ""))
        ),
        "source_sha256": str(candidate.get("source_sha256", "")),
    }
    candidate["status"] = "surfaced"
    write_local_decisions(decisions_path, decisions)
    return candidate


def format_approval_card(candidate: dict[str, object]) -> str:
    candidate_id = safe_console_text(candidate["candidate_id"])
    conference_name = safe_console_text(
        candidate.get("talk_event")
        or candidate.get("conference_directory_evidence")
        or ""
    )
    parent_folder_name = safe_console_text(candidate.get("parent_folder_name", ""))
    if not parent_folder_name:
        relative_value = str(
            candidate.get("relative_source_path")
            or candidate.get("relative_path")
            or ""
        )
        parent_folder_name = Path(relative_value).parent.name if relative_value else ""
    if not parent_folder_name:
        parent_folder_name = "MYTALK_DIR"
    return "\n".join(
        [
            f"[{candidate_id}]",
            "",
            "Talk:",
            safe_console_text(candidate["talk_title"]),
            "",
            "Date:",
            safe_console_text(candidate["talk_date"]),
            "",
            "Conference:",
            conference_name,
            "",
            "PDF:",
            safe_console_text(candidate["candidate_filename"]),
            "",
            "Parent folder:",
            parent_folder_name,
            "",
            "Cover title:",
            safe_console_text(candidate["cover_title"]),
            "",
            "Confidence:",
            f"{candidate['score']} / {candidate['confidence']}",
            "",
            "Status:",
            "Surfaced for review",
            "",
            "Reply:",
            f"{'o / ok':<24} publish this candidate to the website",
            f"{'n':<24} reject this candidate",
            f"{'hold':<24} hold this candidate",
            f"{'next':<24} surface the next candidate",
        ]
    )


def candidate_status(
    *,
    candidate_id: str,
    category: str,
    already_published: bool,
    decisions: dict[str, dict[str, object]],
) -> str:
    if already_published:
        return "published"
    decision = decisions.get(candidate_id, {})
    status = decision.get("status")
    if status in DECISION_STATUSES:
        return str(status)
    if category == "high_confidence_talk_matches":
        return "awaiting_approval"
    return "held"


def record_for_file(
    item: PresentationFile,
    match: MatchResult | None,
    previous_keys: set[str],
    decisions: dict[str, dict[str, object]],
) -> dict[str, object]:
    talk = match.best.talk if match and match.meets_minimum else None
    approved = bool(
        talk and any(asset.type == "slides" for asset in talk.assets)
    )
    category = classify_file(item, match)
    candidate_id = (
        stable_candidate_id(
            date_label_for_dedup(talk),
            talk.title,
            item.digest,
        )
        if talk and item.extension == ".pdf"
        else ""
    )
    status = (
        candidate_status(
            candidate_id=candidate_id,
            category=category,
            already_published=approved,
            decisions=decisions,
        )
        if candidate_id
        else "held"
    )
    score_breakdown = match.best.breakdown if match else {
        "cover_title": 0,
        "date_path": 0,
        "event_directory": 0,
        "location_directory": 0,
        "pdf_metadata_title": 0,
        "filename_fallback": 0,
        "candidate_quality": quality_bonus(item)[0],
        "draft_penalty": -20 if item.is_draft else 0,
    }
    record: dict[str, object] = {
        "candidate_id": candidate_id,
        "status": status,
        "talk_date": date_label_for_dedup(talk) if talk else "",
        "talk_title": talk.title if talk else "",
        "talk_event": talk.event if talk else "",
        "candidate_filename": item.path.name if item.extension == ".pdf" else "",
        "material_filename": item.path.name,
        "parent_folder_name": item.relative_path.parent.name or "MYTALK_DIR",
        "relative_path": str(item.relative_path),
        "relative_source_path": str(item.relative_path),
        "file_type": item.extension.removeprefix("."),
        "content_sha256": item.digest,
        "source_sha256": item.digest,
        "source_size": item.source_size,
        "source_mtime": item.source_mtime,
        "presentation_source": item.presentation_source,
        "source_selection_reason": item.selection_reason,
        "duplicate_of_relative_path": item.duplicate_of,
        "extracted_cover_title_candidate": (
            best_cover_title_score(item, talk)[2] if talk else item.cover_title
        ),
        "cover_title": (
            best_cover_title_score(item, talk)[2] if talk else item.cover_title
        ),
        "cover_title_candidates": list(item.cover_title_candidates),
        "first_page_extraction_method": item.extraction_method,
        "first_page_text": item.first_page_text,
        "extraction_error": item.extraction_error,
        "pdf_metadata_title": item.metadata_title,
        "pdf_metadata_creator": item.metadata_creator,
        "match_score": match.best.score if match else 0,
        "score": match.best.score if match else 0,
        "identity_score": match.best.identity_score if match else 0,
        "score_breakdown": score_breakdown,
        "match_reason": "; ".join(match.best.reasons) if match else "",
        "conference_directory_evidence": item.conference_directory_evidence,
        "keynote_directory": item.keynote_directory,
        "key_sibling_exists": item.key_sibling_exists,
        "key_source_basename_matches": item.key_source_basename_matches,
        "unique_pdf_with_key_source": item.unique_pdf_with_key_source,
        "confidence_category": CATEGORY_TITLES[category],
        "confidence": confidence_label(match),
        "classification": category,
        "ambiguous_candidate_match": bool(
            match and match.meets_minimum and match.ambiguous
        ),
        "runner_up_talk": (
            {
                "date": date_label_for_dedup(match.runner_up.talk),
                "title": match.runner_up.talk.title,
                "score": match.runner_up.score,
            }
            if match and match.runner_up
            else None
        ),
        "approved_public_asset_exists": approved,
        "already_published": approved,
        "public_url_missing": bool(talk and not approved),
        "legacy_filename_title_gate_passed": legacy_filename_title_gate_passed(item, match),
    }
    record["new"] = (
        category == "high_confidence_talk_matches"
        and status == "awaiting_approval"
        and candidate_key(record) not in previous_keys
    )
    return record


def score_distribution(records: list[dict[str, object]]) -> dict[str, int]:
    pdf_records = [record for record in records if record["file_type"] == "pdf"]
    return {
        "zero": sum(int(record["match_score"]) == 0 for record in pdf_records),
        "one_to_34": sum(1 <= int(record["match_score"]) <= 34 for record in pdf_records),
        "35_to_74": sum(35 <= int(record["match_score"]) <= 74 for record in pdf_records),
        "75_or_more": sum(int(record["match_score"]) >= 75 for record in pdf_records),
    }


def markdown_cell(value: object) -> str:
    if isinstance(value, dict):
        value = ", ".join(f"{key}={number}" for key, number in value.items() if number)
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def write_reports(
    report_directory: Path,
    records: list[dict[str, object]],
    summary: dict[str, object],
) -> None:
    report_directory.mkdir(parents=True, exist_ok=True)
    json_path = report_directory / JSON_REPORT_NAME
    markdown_path = report_directory / MARKDOWN_REPORT_NAME
    categories = {
        key: [record for record in records if record["classification"] == key]
        for key in CATEGORY_TITLES
    }
    candidates = categories["high_confidence_talk_matches"] + categories["ambiguous_matches"]
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_directory": "MYTALK_DIR",
        "summary": summary,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "categories": categories,
        "files": records,
    }
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Talk slide candidates",
        "",
        "This local-only report does not approve or publish any asset.",
        "All paths are relative to `MYTALK_DIR`. Raw first-page text is retained in the JSON report.",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key.replace('_', ' ').title()}: {markdown_cell(value)}")

    fields = (
        "candidate_id",
        "status",
        "talk_date",
        "talk_title",
        "talk_event",
        "candidate_filename",
        "parent_folder_name",
        "relative_path",
        "extracted_cover_title_candidate",
        "first_page_extraction_method",
        "match_score",
        "score_breakdown",
        "conference_directory_evidence",
        "keynote_directory",
        "key_sibling_exists",
        "approved_public_asset_exists",
        "public_url_missing",
    )
    headers = (
        "Candidate ID",
        "Status",
        "Talk date",
        "Website talk title",
        "Candidate conference",
        "Candidate PDF",
        "Parent folder",
        "Relative path",
        "Extracted cover title",
        "Extraction method",
        "Score",
        "Score breakdown",
        "Conference-directory evidence",
        "Keynote directory",
        ".key sibling",
        "Public asset registered",
        "Public URL needed",
    )
    for category, title in CATEGORY_TITLES.items():
        lines.extend(["", f"## {title}", ""])
        category_records = categories[category]
        if not category_records:
            lines.append("None.")
            continue
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join("---" for _ in headers) + " |")
        for record in category_records:
            lines.append(
                "| " + " | ".join(markdown_cell(record[field]) for field in fields) + " |"
            )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def report_directory() -> Path:
    configured = os.environ.get("TALK_ASSET_REPORT_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return REPO_ROOT / REPORT_DIRECTORY


def safe_console_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def safe_runtime_error(error: RuntimeError) -> str:
    message = str(error)
    replacements = [
        (str(REPO_ROOT), "<repository>"),
        (str(Path.home()), "$HOME"),
    ]
    configured_source = os.environ.get("MYTALK_DIR", "").strip()
    if configured_source:
        replacements.append((str(Path(configured_source).expanduser()), "MYTALK_DIR"))
    for local_path, replacement in replacements:
        if local_path:
            message = message.replace(local_path, replacement)
    return message


def validate_pdf_dependency() -> None:
    if fitz is None and PdfReader is None:
        raise RuntimeError(
            "No Python PDF text extractor is installed. " + pdf_dependency_instruction()
        )
    if fitz is None:
        print(
            "Warning: PyMuPDF is not installed; using the pypdf first-page text fallback. "
            + pdf_dependency_instruction(),
            file=sys.stderr,
        )


def main() -> int:
    try:
        validate_pdf_dependency()
        source = resolve_mytalk_directory()
        talks = load_talks()
        discovered = discover_presentation_paths(source)
        files = build_presentation_files(source, discovered)
        files = [enrich_pdf_file(item) for item in files]

        report_dir = report_directory()
        json_report = report_dir / JSON_REPORT_NAME
        decisions = load_local_decisions(report_dir / DECISIONS_REPORT_NAME)
        previous_keys = load_previous_candidate_keys(json_report)
        records = [
            record_for_file(
                item,
                best_talk_match(item, talks),
                previous_keys,
                decisions,
            )
            for item in files
        ]
        category_counts = Counter(str(record["classification"]) for record in records)
        pdf_records = [record for record in records if record["file_type"] == "pdf"]
        summary: dict[str, object] = {
            "presentation_materials_examined": len(records),
            "pdfs_examined": len(pdf_records),
            "pdfs_with_extractable_first_page_text": sum(
                bool(record["first_page_text"]) for record in pdf_records
            ),
            "pdfs_with_cover_title_candidates": sum(
                bool(record["extracted_cover_title_candidate"]) for record in pdf_records
            ),
            "pdfs_failing_legacy_filename_title_gate_now_with_useful_evidence": sum(
                int(record["identity_score"]) > 0
                and not bool(record["legacy_filename_title_gate_passed"])
                for record in pdf_records
            ),
            "score_distribution": score_distribution(records),
            "classification_counts": {
                category: category_counts.get(category, 0) for category in CATEGORY_TITLES
            },
            "pdf_extractor": "PyMuPDF" if fitz is not None else "pypdf fallback",
        }
        records.sort(
            key=lambda record: (
                list(CATEGORY_TITLES).index(str(record["classification"])),
                -int(record["match_score"]),
                str(record["relative_path"]).lower(),
            )
        )
        surfaced_candidate = surface_review_candidate(
            records,
            decisions,
            report_dir / DECISIONS_REPORT_NAME,
        )
        write_reports(report_dir, records, summary)
    except RuntimeError as error:
        print(f"Error: {safe_runtime_error(error)}", file=sys.stderr)
        return 1
    except OSError:
        print(
            "Error: could not read the local talk files or write the local report.",
            file=sys.stderr,
        )
        return 1

    distribution = summary["score_distribution"]
    print(
        "Scan summary: "
        f"{summary['pdfs_examined']} PDFs; "
        f"{summary['pdfs_with_extractable_first_page_text']} with extractable page-one text; "
        f"score bands {safe_console_text(distribution)}."
    )
    if surfaced_candidate is None:
        print("No unsurfaced high-confidence talk-slide candidates found.")
        return 0
    print(format_approval_card(surfaced_candidate))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
