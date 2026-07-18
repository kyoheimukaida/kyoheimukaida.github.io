#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

import scan_talk_assets as scanner
from update_talks_from_ics import (
    HISTORY_PATH,
    INDEX_END,
    INDEX_PATH,
    INDEX_START,
    TALK_ASSETS_PATH,
    TALKS_COMBINED_PATH,
    TALKS_END,
    TALKS_PATH,
    TALKS_START,
    TalkAssetManifestEntry,
    TalkEvent,
    date_label_for_dedup,
    deduplicate_talks,
    load_cached_calendar_talks,
    load_history_talks,
    load_talk_asset_manifest,
    match_talk_asset_entry,
    normalize_text_for_asset_match,
    validate_no_duplicate_talks,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = Path(".local") / scanner.JSON_REPORT_NAME
DECISIONS_PATH = Path(".local") / scanner.DECISIONS_REPORT_NAME
SLIDE_DIRECTORY = Path("assets/talk-slides")
DEFAULT_MAX_PUBLIC_SLIDE_MB = 25
WORKFLOW_NAME = "Update public talks"
ALLOWED_DECISION_STATUSES = scanner.DECISION_STATUSES


class PublicationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublicationResult:
    candidate_id: str
    changed: bool
    message: str
    public_url: str = ""
    commit_sha: str = ""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slugify_talk_title(title: str, source_sha256: str) -> str:
    normalized = unicodedata.normalize("NFKD", title).replace("&", " and ")
    ascii_title = normalized.encode("ascii", errors="ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    if not slug:
        slug = f"talk-{source_sha256[:8]}"
    return slug[:96].rstrip("-")


def destination_filename(candidate: dict[str, object]) -> str:
    date_label = str(candidate.get("talk_date", "")).strip()
    if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", date_label):
        raise PublicationError("Candidate talk date is invalid; run a new scan.")
    source_sha256 = str(candidate.get("source_sha256", ""))
    title_slug = slugify_talk_title(str(candidate.get("talk_title", "")), source_sha256)
    return f"{date_label}-{title_slug}.pdf"


def read_json(path: Path, expected_type: type) -> object:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PublicationError(f"Required local file is missing: {path.name}.") from None
    except json.JSONDecodeError as error:
        raise PublicationError(
            f"Invalid JSON in {path.name}: line {error.lineno}, column {error.colno}."
        ) from None
    if not isinstance(payload, expected_type):
        raise PublicationError(f"{path.name} has an invalid top-level JSON type.")
    return payload


def load_decisions(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    payload = read_json(path, dict)
    assert isinstance(payload, dict)
    decisions = payload.get("decisions", {})
    if not isinstance(decisions, dict):
        raise PublicationError("The local decision file has an invalid decisions field.")
    result: dict[str, dict[str, object]] = {}
    for candidate_id, decision in decisions.items():
        if not isinstance(candidate_id, str) or not isinstance(decision, dict):
            raise PublicationError("The local decision file contains an invalid entry.")
        if decision.get("status") not in ALLOWED_DECISION_STATUSES:
            raise PublicationError(
                f"The local decision for {candidate_id} has an invalid status."
            )
        result[candidate_id] = decision
    return result


def write_decisions(path: Path, decisions: dict[str, dict[str, object]]) -> None:
    payload = {
        "schema_version": scanner.DECISION_SCHEMA_VERSION,
        "decisions": {key: decisions[key] for key in sorted(decisions)},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_manifest_payload(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    payload = read_json(path, list)
    assert isinstance(payload, list)
    if not all(isinstance(entry, dict) for entry in payload):
        raise PublicationError("The talk asset manifest must contain JSON objects.")
    return list(payload)


def canonical_manifest_payload(
    entries: list[dict[str, object]],
) -> list[dict[str, object]]:
    normalized_entries: list[dict[str, object]] = []
    for entry in entries:
        normalized: dict[str, object] = {
            "date": entry.get("date", ""),
            "title": entry.get("title", ""),
        }
        if entry.get("event"):
            normalized["event"] = entry["event"]
        if entry.get("aliases"):
            normalized["aliases"] = entry["aliases"]
        assets = entry.get("assets", [])
        if not isinstance(assets, list):
            raise PublicationError("A talk asset manifest assets field is invalid.")
        normalized_assets: list[dict[str, object]] = []
        for asset in assets:
            if not isinstance(asset, dict):
                raise PublicationError("A talk asset manifest asset is invalid.")
            ordered_asset: dict[str, object] = {}
            for key in (
                "type",
                "label",
                "url",
                "public",
                "candidate_id",
                "sha256",
                "approved_at",
            ):
                if key in asset:
                    ordered_asset[key] = asset[key]
            normalized_assets.append(ordered_asset)
        normalized["assets"] = sorted(
            normalized_assets,
            key=lambda asset: (
                str(asset.get("type", "")),
                str(asset.get("label", "")),
                str(asset.get("url", "")),
                str(asset.get("candidate_id", "")),
            ),
        )
        normalized_entries.append(normalized)
    return sorted(
        normalized_entries,
        key=lambda entry: (
            str(entry.get("date", "")),
            normalize_text_for_asset_match(str(entry.get("title", ""))),
        ),
    )


class TalkAssetPublisher:
    def __init__(
        self,
        *,
        repo_root: Path = REPO_ROOT,
        source_root: Path | None = None,
        report_path: Path | None = None,
        decisions_path: Path | None = None,
        max_public_bytes: int | None = None,
        git_enabled: bool = True,
        workflow_enabled: bool = True,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.source_root = source_root.resolve() if source_root else None
        self.report_path = report_path or self.repo_root / REPORT_PATH
        self.decisions_path = decisions_path or self.repo_root / DECISIONS_PATH
        if max_public_bytes is not None and max_public_bytes <= 0:
            raise PublicationError("The public PDF size limit must be positive.")
        self.max_public_bytes = (
            max_public_bytes
            if max_public_bytes is not None
            else self._configured_max_bytes()
        )
        self.git_enabled = git_enabled
        self.workflow_enabled = workflow_enabled
        self.now = now

    @staticmethod
    def _configured_max_bytes() -> int:
        raw = os.environ.get("MAX_PUBLIC_SLIDE_MB", "").strip()
        if not raw:
            megabytes = DEFAULT_MAX_PUBLIC_SLIDE_MB
        else:
            try:
                megabytes = int(raw)
            except ValueError:
                raise PublicationError("MAX_PUBLIC_SLIDE_MB must be a positive integer.") from None
            if megabytes <= 0:
                raise PublicationError("MAX_PUBLIC_SLIDE_MB must be a positive integer.")
        return megabytes * 1024 * 1024

    def _load_candidates(self) -> list[dict[str, object]]:
        payload = read_json(self.report_path, dict)
        assert isinstance(payload, dict)
        candidates = payload.get("candidates", [])
        if not isinstance(candidates, list) or not all(
            isinstance(candidate, dict) for candidate in candidates
        ):
            raise PublicationError("The local candidate report is invalid; run a new scan.")
        return list(candidates)

    def _effective_status(
        self,
        candidate: dict[str, object],
        decisions: dict[str, dict[str, object]],
    ) -> str:
        if candidate.get("already_published"):
            return "published"
        candidate_id = str(candidate.get("candidate_id", ""))
        decision = decisions.get(candidate_id)
        if decision:
            return str(decision["status"])
        return str(candidate.get("status", "awaiting_approval"))

    def _select_candidate(
        self,
        candidate_id: str | None,
        *,
        require_awaiting: bool,
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        candidates = self._load_candidates()
        decisions = load_decisions(self.decisions_path)
        by_id = {
            str(candidate.get("candidate_id", "")): candidate
            for candidate in candidates
            if candidate.get("candidate_id")
        }
        if candidate_id:
            candidate = by_id.get(candidate_id)
            if candidate is None:
                raise PublicationError(f"Candidate {candidate_id} was not found; run a new scan.")
        else:
            surfaced = [
                candidate
                for candidate in candidates
                if self._effective_status(candidate, decisions) == "surfaced"
            ]
            if len(surfaced) != 1:
                raise PublicationError(
                    "A bare review action requires exactly one surfaced candidate; "
                    "run review or next, or provide a candidate ID."
                )
            candidate = surfaced[0]

        status = self._effective_status(candidate, decisions)
        if require_awaiting and status not in {
            "awaiting_approval",
            "surfaced",
        }:
            if status == "published":
                return candidate, decisions
            raise PublicationError(
                f"Candidate {candidate.get('candidate_id')} is {status} and cannot be approved."
            )
        return candidate, decisions

    def _decision_entry(
        self,
        candidate: dict[str, object],
        previous: dict[str, object],
        status: str,
        *,
        reason: str = "",
        public_url: str = "",
    ) -> dict[str, object]:
        timestamp = self.now().isoformat()
        entry: dict[str, object] = {
            "status": status,
            "updated_at": timestamp,
            "talk_date": str(candidate.get("talk_date", "")),
            "talk_title_normalized": normalize_text_for_asset_match(
                str(candidate.get("talk_title", ""))
            ),
            "source_sha256": str(candidate.get("source_sha256", "")),
        }
        if previous.get("surfaced_at"):
            entry["surfaced_at"] = previous["surfaced_at"]
        if status == "surfaced" and "surfaced_at" not in entry:
            entry["surfaced_at"] = timestamp
        if reason:
            entry["reason"] = reason
        if public_url:
            entry["public_url"] = public_url
        return entry

    def _write_decision(
        self,
        candidate: dict[str, object],
        decisions: dict[str, dict[str, object]],
        status: str,
        *,
        reason: str = "",
        public_url: str = "",
    ) -> None:
        candidate_id = str(candidate["candidate_id"])
        decisions[candidate_id] = self._decision_entry(
            candidate,
            decisions.get(candidate_id, {}),
            status,
            reason=reason,
            public_url=public_url,
        )
        write_decisions(self.decisions_path, decisions)

    def _review_queue(
        self,
    ) -> tuple[
        list[dict[str, object]],
        dict[str, dict[str, object]],
        dict[str, object] | None,
    ]:
        candidates = self._load_candidates()
        decisions = load_decisions(self.decisions_path)
        by_id = {
            str(candidate["candidate_id"]): candidate
            for candidate in candidates
            if candidate.get("candidate_id")
        }
        surfaced_ids = {
            candidate_id
            for candidate_id, decision in decisions.items()
            if decision.get("status") == "surfaced"
        } | {
            str(candidate["candidate_id"])
            for candidate in candidates
            if candidate.get("candidate_id")
            and self._effective_status(candidate, decisions) == "surfaced"
        }
        if len(surfaced_ids) > 1:
            raise PublicationError(
                "Multiple candidates are surfaced; repair the local decision queue."
            )
        surfaced_id = next(iter(surfaced_ids), "")
        current = by_id.get(surfaced_id) if surfaced_id else None
        if current and (
            current.get("classification") != "high_confidence_talk_matches"
            or current.get("already_published")
        ):
            candidate_id = str(current["candidate_id"])
            decisions[candidate_id] = self._decision_entry(
                current,
                decisions.get(candidate_id, {}),
                "held",
                reason="candidate is no longer an unpublished high-confidence match",
            )
            write_decisions(self.decisions_path, decisions)
            current = None
        elif surfaced_id and current is None:
            stale_id = surfaced_id
            stale = decisions.get(stale_id, {})
            decisions[stale_id] = {
                **stale,
                "status": "held",
                "updated_at": self.now().isoformat(),
                "reason": "candidate is no longer present in the local report",
            }
            write_decisions(self.decisions_path, decisions)
        return candidates, decisions, current

    def _eligible_unsurfaced(
        self,
        candidates: list[dict[str, object]],
        decisions: dict[str, dict[str, object]],
    ) -> list[dict[str, object]]:
        return [
            candidate
            for candidate in candidates
            if candidate.get("classification") == "high_confidence_talk_matches"
            and self._effective_status(candidate, decisions) == "awaiting_approval"
            and not decisions.get(str(candidate.get("candidate_id", "")), {}).get(
                "surfaced_at"
            )
        ]

    def review(self, candidate_id: str | None = None) -> dict[str, object] | None:
        candidates, decisions, current = self._review_queue()
        by_id = {
            str(candidate["candidate_id"]): candidate
            for candidate in candidates
            if candidate.get("candidate_id")
        }
        if candidate_id:
            return self._surface_explicit(candidate_id, by_id, decisions, current)
        if current:
            return current
        eligible = self._eligible_unsurfaced(candidates, decisions)
        if not eligible:
            return None
        candidate = max(eligible, key=scanner.review_priority)
        self._write_decision(candidate, decisions, "surfaced")
        candidate["status"] = "surfaced"
        return candidate

    def _surface_explicit(
        self,
        candidate_id: str,
        by_id: dict[str, dict[str, object]],
        decisions: dict[str, dict[str, object]],
        current: dict[str, object] | None,
    ) -> dict[str, object]:
        candidate = by_id.get(candidate_id)
        if candidate is None:
            raise PublicationError(f"Candidate {candidate_id} was not found; run a new scan.")
        status = self._effective_status(candidate, decisions)
        if status in {"rejected", "published"}:
            raise PublicationError(f"Candidate {candidate_id} is {status} and cannot be surfaced.")
        if candidate.get("classification") != "high_confidence_talk_matches":
            raise PublicationError("Only a high-confidence candidate can be surfaced.")
        if current and current.get("candidate_id") != candidate_id:
            current_id = str(current["candidate_id"])
            decisions[current_id] = self._decision_entry(
                current,
                decisions.get(current_id, {}),
                "awaiting_approval",
                reason="advanced to another review candidate",
            )
        decisions[candidate_id] = self._decision_entry(
            candidate,
            decisions.get(candidate_id, {}),
            "surfaced",
        )
        write_decisions(self.decisions_path, decisions)
        candidate["status"] = "surfaced"
        return candidate

    def next_candidate(
        self,
        candidate_id: str | None = None,
    ) -> dict[str, object] | None:
        candidates, decisions, current = self._review_queue()
        by_id = {
            str(candidate["candidate_id"]): candidate
            for candidate in candidates
            if candidate.get("candidate_id")
        }
        if candidate_id:
            return self._surface_explicit(candidate_id, by_id, decisions, current)
        eligible = self._eligible_unsurfaced(candidates, decisions)
        if not eligible:
            return current
        candidate = max(eligible, key=scanner.review_priority)
        return self._surface_explicit(
            str(candidate["candidate_id"]),
            by_id,
            decisions,
            current,
        )

    def reject(self, candidate_id: str | None) -> PublicationResult:
        candidate, decisions = self._select_candidate(candidate_id, require_awaiting=False)
        status = self._effective_status(candidate, decisions)
        if status == "published":
            raise PublicationError("A published candidate must be unpublished before rejection.")
        if status == "rejected":
            return PublicationResult(str(candidate["candidate_id"]), False, "Already rejected.")
        self._write_decision(candidate, decisions, "rejected")
        return PublicationResult(str(candidate["candidate_id"]), True, "Candidate rejected locally.")

    def hold(self, candidate_id: str | None) -> PublicationResult:
        candidate, decisions = self._select_candidate(candidate_id, require_awaiting=False)
        status = self._effective_status(candidate, decisions)
        if status == "published":
            raise PublicationError("A published candidate cannot be held.")
        if status == "held":
            return PublicationResult(str(candidate["candidate_id"]), False, "Candidate is already held.")
        self._write_decision(candidate, decisions, "held")
        return PublicationResult(str(candidate["candidate_id"]), True, "Candidate held locally.")

    def reset(self, candidate_id: str) -> PublicationResult:
        candidate, decisions = self._select_candidate(candidate_id, require_awaiting=False)
        if candidate.get("classification") != "high_confidence_talk_matches" or candidate.get(
            "ambiguous_candidate_match"
        ):
            raise PublicationError("Only an unambiguous high-confidence candidate can be reset.")
        candidate_id_value = str(candidate["candidate_id"])
        decisions[candidate_id_value] = self._decision_entry(
            candidate,
            {},
            "awaiting_approval",
            reason="explicitly reset for review",
        )
        write_decisions(self.decisions_path, decisions)
        return PublicationResult(candidate_id_value, True, "Candidate reset to awaiting approval.")

    def _load_website_talks(self) -> list[TalkEvent]:
        calendar = load_cached_calendar_talks(self.repo_root / TALKS_COMBINED_PATH)
        history = load_history_talks(self.repo_root / HISTORY_PATH)
        talks = deduplicate_talks(calendar + history)
        validate_no_duplicate_talks(talks)
        return talks

    def _unique_talk_for_candidate(
        self,
        candidate: dict[str, object],
        talks: list[TalkEvent],
    ) -> tuple[int, TalkEvent]:
        if candidate.get("classification") != "high_confidence_talk_matches":
            raise PublicationError("Only a high-confidence candidate may be published.")
        if candidate.get("ambiguous_candidate_match"):
            raise PublicationError("Candidate talk match is ambiguous; run a new scan.")
        entry = TalkAssetManifestEntry(
            date_label=str(candidate.get("talk_date", "")),
            title=str(candidate.get("talk_title", "")),
            event=str(candidate.get("talk_event", "")),
            aliases=(),
            assets=(),
        )
        try:
            match_index = match_talk_asset_entry(entry, talks)
        except RuntimeError as error:
            raise PublicationError(str(error)) from None
        if match_index is None:
            raise PublicationError("Candidate has no unique website talk record; run a new scan.")
        return match_index, talks[match_index]

    def _resolve_source_root(self) -> Path:
        if self.source_root is None:
            try:
                self.source_root = scanner.resolve_mytalk_directory()
            except RuntimeError as error:
                raise PublicationError(str(error)) from None
        return self.source_root

    def _validate_source(
        self,
        candidate: dict[str, object],
        talk: TalkEvent,
    ) -> Path:
        relative_value = str(
            candidate.get("relative_source_path") or candidate.get("relative_path") or ""
        )
        relative_path = Path(relative_value)
        if not relative_value or relative_path.is_absolute() or ".." in relative_path.parts:
            raise PublicationError("Candidate source path is unsafe; run a new scan.")
        source_root = self._resolve_source_root().resolve()
        source = (source_root / relative_path).resolve()
        if not source.is_relative_to(source_root) or source.is_symlink():
            raise PublicationError("Candidate source is outside MYTALK_DIR or is a symlink.")
        if not source.exists():
            raise PublicationError("Candidate source PDF is missing; run a new scan.")
        if not source.is_file() or source.suffix.lower() != ".pdf":
            raise PublicationError("Candidate source is not a PDF.")
        if scanner.is_hard_excluded(source) or scanner.is_draft_file(source):
            raise PublicationError("Temporary, backup, or draft-like PDFs cannot be published.")

        stat = source.stat()
        expected_size = int(candidate.get("source_size", -1))
        if stat.st_size != expected_size:
            raise PublicationError("Candidate source size changed; run a new scan.")
        if stat.st_size > self.max_public_bytes:
            size_mb = stat.st_size / (1024 * 1024)
            limit_mb = self.max_public_bytes / (1024 * 1024)
            raise PublicationError(
                f"Candidate PDF is {size_mb:.1f} MB, above the {limit_mb:g} MB public limit."
            )
        expected_hash = str(candidate.get("source_sha256", "")).lower()
        if not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
            raise PublicationError("Candidate source hash is invalid; run a new scan.")
        if sha256_file(source) != expected_hash:
            raise PublicationError("Candidate source hash changed; run a new scan.")
        with source.open("rb") as file:
            if file.read(5) != b"%PDF-":
                raise PublicationError("Candidate source failed PDF signature validation.")

        try:
            if scanner.fitz is not None:
                extracted = scanner.pymupdf_extract(source)
            elif scanner.PdfReader is not None:
                extracted = scanner.pypdf_extract(source)
            else:
                raise PublicationError(scanner.pdf_dependency_instruction())
        except PublicationError:
            raise
        except Exception as error:
            raise PublicationError(
                f"Candidate PDF validation failed ({type(error).__name__})."
            ) from None
        cover_candidates = tuple(extracted.get("cover_title_candidates", ()))
        best_cover_score = max(
            (
                scanner.title_evidence_score(talk.title, str(cover_title), 80)[0]
                for cover_title in cover_candidates
            ),
            default=0,
        )
        if best_cover_score < 40:
            raise PublicationError(
                "First-page cover title is no longer compatible with the website talk; "
                "run a new scan."
            )
        return source

    def _find_manifest_entry(
        self,
        manifest_path: Path,
        payload: list[dict[str, object]],
        talks: list[TalkEvent],
        target_index: int,
    ) -> int | None:
        try:
            entries = load_talk_asset_manifest(manifest_path)
        except RuntimeError as error:
            raise PublicationError(str(error)) from None
        matches: list[int] = []
        for index, entry in enumerate(entries):
            try:
                match_index = match_talk_asset_entry(entry, talks)
            except RuntimeError as error:
                raise PublicationError(str(error)) from None
            if match_index == target_index:
                matches.append(index)
        if len(matches) > 1:
            raise PublicationError("Multiple manifest entries target this talk.")
        if len(entries) != len(payload):
            raise PublicationError("Manifest validation did not preserve entry count.")
        return matches[0] if matches else None

    def _prepare_manifest(
        self,
        candidate: dict[str, object],
        talk: TalkEvent,
        target_index: int,
        talks: list[TalkEvent],
        public_url: str,
    ) -> tuple[list[dict[str, object]], bool]:
        manifest_path = self.repo_root / TALK_ASSETS_PATH
        payload = load_manifest_payload(manifest_path)
        entry_index = self._find_manifest_entry(
            manifest_path,
            payload,
            talks,
            target_index,
        )
        if entry_index is None:
            payload.append(
                {
                    "date": date_label_for_dedup(talk),
                    "title": talk.title,
                    "event": talk.event,
                    "aliases": [],
                    "assets": [],
                }
            )
            entry_index = len(payload) - 1
        assets = payload[entry_index].setdefault("assets", [])
        if not isinstance(assets, list):
            raise PublicationError("The target manifest assets field is invalid.")

        candidate_id = str(candidate["candidate_id"])
        source_hash = str(candidate["source_sha256"])
        for entry in payload:
            raw_assets = entry.get("assets", [])
            if not isinstance(raw_assets, list):
                raise PublicationError("A manifest assets field is invalid.")
            for raw_asset in raw_assets:
                if not isinstance(raw_asset, dict):
                    raise PublicationError("A manifest asset is invalid.")
                if raw_asset.get("candidate_id") == candidate_id:
                    if (
                        raw_asset.get("sha256") == source_hash
                        and raw_asset.get("url") == public_url
                        and raw_asset.get("public") is True
                    ):
                        return canonical_manifest_payload(payload), False
                    raise PublicationError("Candidate ID conflicts with an existing asset.")
                if raw_asset.get("url") == public_url and raw_asset.get("sha256") != source_hash:
                    raise PublicationError("Destination URL conflicts with an existing asset.")

        assets.append(
            {
                "type": "slides",
                "label": "Slides",
                "url": public_url,
                "public": True,
                "candidate_id": candidate_id,
                "sha256": source_hash,
                "approved_at": self.now().isoformat(),
            }
        )
        return canonical_manifest_payload(payload), True

    def _write_manifest(self, payload: list[dict[str, object]]) -> None:
        path = self.repo_root / TALK_ASSETS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            load_talk_asset_manifest(path)
        except RuntimeError as error:
            raise PublicationError(str(error)) from None

    def _run_talk_update(self) -> None:
        script = Path(__file__).with_name("update_talks_from_ics.py")
        environment = os.environ.copy()
        environment.setdefault("PYTHONPYCACHEPREFIX", str(Path("/tmp") / "talk-assets-pycache"))
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=self.repo_root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
            raise PublicationError(f"Talk-page regeneration failed: {detail}")

    def _validate_page_structure(self, source_root: Path | None) -> tuple[str, str]:
        index_path = self.repo_root / INDEX_PATH
        talks_path = self.repo_root / TALKS_PATH
        index_text = index_path.read_text(encoding="utf-8")
        talks_text = talks_path.read_text(encoding="utf-8")
        marker_expectations = (
            (index_text, INDEX_START, INDEX_PATH),
            (index_text, INDEX_END, INDEX_PATH),
            (talks_text, TALKS_START, TALKS_PATH),
            (talks_text, TALKS_END, TALKS_PATH),
        )
        for text, marker, path in marker_expectations:
            if text.count(marker) != 1:
                raise PublicationError(f"Expected exactly one generated marker in {path}.")
        if re.search(r"^(?:<<<<<<<|=======|>>>>>>>)", index_text + "\n" + talks_text, re.MULTILINE):
            raise PublicationError("Generated talk files contain merge-conflict markers.")
        for path in (index_path, talks_path, self.repo_root / TALK_ASSETS_PATH):
            for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
                content = line.rstrip("\r\n")
                trailing_match = re.search(r"[ \t]+$", content)
                if not trailing_match:
                    continue
                if path.suffix == ".md" and trailing_match.group() == "  ":
                    continue
                raise PublicationError(f"Trailing whitespace found in {path.name}.")

        private_fragments = {
            str(source_root) if source_root else "",
            str(Path.home() / "Library" / "CloudStorage" / "Dropbox"),
        }
        for path in (index_path, talks_path, self.repo_root / TALK_ASSETS_PATH):
            text = path.read_text(encoding="utf-8")
            if any(fragment and fragment in text for fragment in private_fragments):
                raise PublicationError("A local Dropbox path appeared in a tracked text file.")

        if (self.repo_root / ".git").exists():
            result = self._git(
                "diff",
                "--check",
                "--",
                INDEX_PATH,
                TALKS_PATH,
                str(TALK_ASSETS_PATH),
                check=False,
            )
            if result.returncode:
                raise PublicationError("git diff --check failed for generated talk files.")
        return index_text, talks_text

    def _validate_generated_pages(self, public_url: str, source_root: Path) -> None:
        index_text, talks_text = self._validate_page_structure(source_root)
        if talks_text.count(f"]({public_url})") != 1:
            raise PublicationError("talks.md does not contain exactly one approved Slides URL.")
        if public_url in index_text or "[Slides]" in index_text:
            raise PublicationError("index.md must not contain talk-slide links.")

    def _git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode:
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "git command failed"
            raise PublicationError(detail)
        return result

    def _ensure_git_scope_clean(
        self,
        paths: Sequence[Path],
        *,
        allow_untracked: Sequence[Path] = (),
    ) -> None:
        if not self.git_enabled:
            return
        branch = self._git("branch", "--show-current").stdout.strip()
        if branch != "main":
            raise PublicationError("Talk assets can only be published from the main branch.")
        staged = self._git("diff", "--cached", "--name-only").stdout.splitlines()
        if staged:
            raise PublicationError("Existing staged changes must be committed or unstaged first.")
        allowed_untracked = {path.resolve() for path in allow_untracked}
        for path in paths:
            relative = str(path.relative_to(self.repo_root))
            status = self._git("status", "--porcelain", "--", relative).stdout.strip()
            if status.startswith("??") and path.resolve() in allowed_untracked:
                continue
            if status:
                raise PublicationError(
                    f"Publication target {relative} already has local changes; finish them first."
                )

    def _commit_and_push(
        self,
        expected_paths: Sequence[Path],
        message: str,
    ) -> str:
        relative_paths = [str(path.relative_to(self.repo_root)) for path in expected_paths]
        self._git("add", "--", *relative_paths)
        staged = set(self._git("diff", "--cached", "--name-only").stdout.splitlines())
        expected = set(relative_paths)
        if not staged:
            return ""
        if not staged <= expected:
            raise PublicationError("Refusing to commit an unrelated staged file.")
        self._git("config", "user.name", "github-actions[bot]")
        self._git(
            "config",
            "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        )
        self._git("commit", "-m", message, "--", *sorted(staged))
        self._git("pull", "--rebase", "--autostash", "origin", "main")
        self._git("push", "origin", "main")
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _workflow_runs(self) -> list[dict[str, object]]:
        result = subprocess.run(
            [
                "gh",
                "run",
                "list",
                "--workflow",
                WORKFLOW_NAME,
                "--branch",
                "main",
                "--limit",
                "20",
                "--json",
                "databaseId,headSha,status,conclusion,url,createdAt",
            ],
            cwd=self.repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            raise PublicationError("Could not inspect the Update public talks workflow.")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise PublicationError("GitHub CLI returned invalid workflow data.") from None
        return payload if isinstance(payload, list) else []

    def _wait_for_workflow_run(self, commit_sha: str, timeout: int = 45) -> int | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for run in self._workflow_runs():
                if run.get("headSha") == commit_sha:
                    return int(run["databaseId"])
            time.sleep(3)
        return None

    def _monitor_workflow(self, commit_sha: str) -> None:
        if not self.workflow_enabled:
            return
        if shutil.which("gh") is None:
            raise PublicationError("GitHub CLI is required to verify Pages deployment.")
        run_id = self._wait_for_workflow_run(commit_sha)
        if run_id is None:
            dispatch = subprocess.run(
                [
                    "gh",
                    "workflow",
                    "run",
                    WORKFLOW_NAME,
                    "--ref",
                    "main",
                    "-f",
                    "deploy_pages=true",
                ],
                cwd=self.repo_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if dispatch.returncode:
                raise PublicationError("Update public talks did not start and dispatch failed.")
            run_id = self._wait_for_workflow_run(commit_sha)
        if run_id is None:
            raise PublicationError("Update public talks did not appear after dispatch.")
        watched = subprocess.run(
            ["gh", "run", "watch", str(run_id), "--exit-status"],
            cwd=self.repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if watched.returncode:
            raise PublicationError("Update public talks or Pages deployment failed.")

    def _public_talks_url(self) -> str:
        remote = self._git("remote", "get-url", "origin").stdout.strip()
        match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", remote)
        if not match:
            raise PublicationError("Could not derive the GitHub Pages URL from origin.")
        owner, repository = match.groups()
        if repository.lower() == f"{owner.lower()}.github.io":
            return f"https://{owner}.github.io/talks.html"
        return f"https://{owner}.github.io/{repository}/talks.html"

    def _confirm_public_page(self, public_url: str, *, present: bool = True) -> None:
        if not self.workflow_enabled:
            return
        deadline = time.monotonic() + 120
        page_url = self._public_talks_url()
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(page_url, timeout=15) as response:
                    body = response.read().decode("utf-8", errors="replace")
                if (public_url in body) is present:
                    return
            except OSError:
                pass
            time.sleep(5)
        expected = "observed" if present else "removed"
        raise PublicationError(
            f"Pages deployed, but the public Slides link was not {expected}."
        )

    def approve(self, candidate_id: str | None) -> PublicationResult:
        candidate, decisions = self._select_candidate(candidate_id, require_awaiting=True)
        candidate_id_value = str(candidate["candidate_id"])
        talks = self._load_website_talks()
        target_index, talk = self._unique_talk_for_candidate(candidate, talks)
        source = self._validate_source(candidate, talk)
        destination = self.repo_root / SLIDE_DIRECTORY / destination_filename(candidate)
        public_url = "/" + str(destination.relative_to(self.repo_root)).replace(os.sep, "/")
        manifest_path = self.repo_root / TALK_ASSETS_PATH
        talks_path = self.repo_root / TALKS_PATH
        index_path = self.repo_root / INDEX_PATH
        scope_paths = [manifest_path, talks_path, index_path, destination]
        self._ensure_git_scope_clean(scope_paths, allow_untracked=[destination])

        source_hash = str(candidate["source_sha256"])
        destination_existed = destination.exists()
        if destination_existed and sha256_file(destination) != source_hash:
            raise PublicationError("Destination PDF exists with different content; refusing to overwrite.")

        manifest_payload, manifest_changed = self._prepare_manifest(
            candidate,
            talk,
            target_index,
            talks,
            public_url,
        )
        before = {
            path: path.read_bytes() if path.exists() and path.is_file() else None
            for path in (manifest_path, talks_path, index_path)
        }
        copied = False
        try:
            if not destination_existed:
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(".pdf.tmp")
                shutil.copyfile(source, temporary)
                if sha256_file(temporary) != source_hash:
                    temporary.unlink(missing_ok=True)
                    raise PublicationError("Copied PDF failed hash verification.")
                os.replace(temporary, destination)
                copied = True
            if manifest_changed:
                self._write_manifest(manifest_payload)
            self._run_talk_update()
            self._validate_generated_pages(public_url, self._resolve_source_root())
        except Exception as error:
            for path, content in before.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            if copied:
                destination.unlink(missing_ok=True)
            self._write_decision(candidate, decisions, "failed", reason=str(error))
            if isinstance(error, PublicationError):
                raise
            raise PublicationError(type(error).__name__) from None

        changed_paths = [
            path
            for path in (destination, manifest_path, talks_path, index_path)
            if (
                (path == destination and copied)
                or (path != destination and before[path] != path.read_bytes())
            )
        ]
        if not changed_paths:
            self._write_decision(
                candidate,
                decisions,
                "published",
                public_url=public_url,
            )
            return PublicationResult(
                candidate_id_value,
                False,
                "Already published; no change needed.",
                public_url,
            )

        commit_sha = ""
        if self.git_enabled:
            try:
                commit_sha = self._commit_and_push(
                    changed_paths,
                    f"Publish slides for {talk.title}",
                )
                if commit_sha:
                    self._monitor_workflow(commit_sha)
                    self._confirm_public_page(public_url)
            except PublicationError as error:
                self._write_decision(
                    candidate,
                    decisions,
                    "failed",
                    reason=str(error),
                    public_url=public_url,
                )
                raise
        self._write_decision(
            candidate,
            decisions,
            "published",
            public_url=public_url,
        )
        return PublicationResult(
            candidate_id_value,
            True,
            "Slides published successfully.",
            public_url,
            commit_sha,
        )

    def unpublish(self, candidate_id: str, *, confirmed: bool) -> PublicationResult:
        if not confirmed:
            raise PublicationError("Unpublish requires --confirm.")
        manifest_path = self.repo_root / TALK_ASSETS_PATH
        payload = load_manifest_payload(manifest_path)
        removed_assets: list[dict[str, object]] = []
        removed_entry: dict[str, object] | None = None
        retained_entries: list[dict[str, object]] = []
        for entry in payload:
            assets = entry.get("assets", [])
            if not isinstance(assets, list):
                raise PublicationError("A manifest assets field is invalid.")
            retained_assets: list[dict[str, object]] = []
            for asset in assets:
                if isinstance(asset, dict) and asset.get("candidate_id") == candidate_id:
                    removed_assets.append(asset)
                    removed_entry = entry
                else:
                    retained_assets.append(asset)
            if retained_assets:
                updated_entry = dict(entry)
                updated_entry["assets"] = retained_assets
                retained_entries.append(updated_entry)
        if not removed_assets:
            return PublicationResult(candidate_id, False, "Already unpublished; no change needed.")
        if len(removed_assets) != 1:
            raise PublicationError("Candidate ID appears more than once in the manifest.")
        decisions = load_decisions(self.decisions_path)
        candidate = next(
            (
                item
                for item in self._load_candidates()
                if item.get("candidate_id") == candidate_id
            ),
            None,
        ) if self.report_path.exists() else None
        if candidate is None:
            assert removed_entry is not None
            candidate = {
                "candidate_id": candidate_id,
                "talk_date": str(removed_entry.get("date", "")),
                "talk_title": str(removed_entry.get("title", "")),
                "source_sha256": str(removed_assets[0].get("sha256", "")),
            }
        public_url = str(removed_assets[0].get("url", ""))
        if not public_url.startswith("/assets/talk-slides/"):
            raise PublicationError("Candidate asset URL is outside the talk-slide directory.")
        destination = (self.repo_root / public_url.lstrip("/")).resolve()
        slide_root = (self.repo_root / SLIDE_DIRECTORY).resolve()
        if not destination.is_relative_to(slide_root):
            raise PublicationError("Candidate asset path is unsafe.")
        still_referenced = any(
            isinstance(asset, dict) and asset.get("url") == public_url
            for entry in retained_entries
            for asset in entry.get("assets", [])
            if isinstance(entry.get("assets", []), list)
        )
        talks_path = self.repo_root / TALKS_PATH
        index_path = self.repo_root / INDEX_PATH
        scope_paths = [manifest_path, talks_path, index_path, destination]
        self._ensure_git_scope_clean(scope_paths)
        before = {
            path: path.read_bytes() if path.exists() and path.is_file() else None
            for path in (manifest_path, talks_path, index_path, destination)
        }
        try:
            self._write_manifest(canonical_manifest_payload(retained_entries))
            self._run_talk_update()
            if not still_referenced:
                destination.unlink(missing_ok=True)
            index_text, talks_text = self._validate_page_structure(self.source_root)
            if public_url in talks_text or public_url in index_text:
                raise PublicationError(
                    "Unpublish validation found a remaining Slides link."
                )
        except Exception as error:
            for path, content in before.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            self._write_decision(candidate, decisions, "failed", reason=str(error))
            if isinstance(error, PublicationError):
                raise
            raise PublicationError(type(error).__name__) from None
        changed_paths = [
            path
            for path in (destination, manifest_path, talks_path, index_path)
            if (path.read_bytes() if path.exists() and path.is_file() else None) != before[path]
        ]
        commit_sha = ""
        if self.git_enabled and changed_paths:
            try:
                commit_sha = self._commit_and_push(
                    changed_paths,
                    f"Unpublish slides for {candidate.get('talk_title', candidate_id)}",
                )
                if commit_sha:
                    self._monitor_workflow(commit_sha)
                    self._confirm_public_page(public_url, present=False)
            except PublicationError as error:
                self._write_decision(
                    candidate,
                    decisions,
                    "failed",
                    reason=str(error),
                )
                raise
        self._write_decision(candidate, decisions, "held", reason="unpublished")
        return PublicationResult(
            candidate_id,
            bool(changed_paths),
            "Slides unpublished successfully.",
            commit_sha=commit_sha,
        )


def approval_summary(candidate: dict[str, object]) -> str:
    destination = SLIDE_DIRECTORY / destination_filename(candidate)
    paths = [destination, TALK_ASSETS_PATH, Path(TALKS_PATH)]
    return "\n".join(
        [
            "Publication mutation:",
            f"- Candidate: {candidate['candidate_id']} / {candidate['candidate_filename']}",
            f"- Talk: {candidate['talk_date']} — {candidate['talk_title']}",
            f"- Destination: {destination}",
            "- Commit files: " + ", ".join(str(path) for path in paths),
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish an explicitly approved local talk-slide candidate.",
    )
    parser.add_argument(
        "action",
        choices=(
            "review",
            "next",
            "approve",
            "reject",
            "hold",
            "reset",
            "unpublish",
        ),
    )
    parser.add_argument("candidate_id", nargs="?")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="required for unpublish",
    )
    return parser


def safe_error_message(error: Exception) -> str:
    message = str(error)
    replacements = (
        (str(REPO_ROOT), "<repository>"),
        (str(Path.home()), "$HOME"),
        (os.environ.get("MYTALK_DIR", ""), "MYTALK_DIR"),
    )
    for value, replacement in replacements:
        if value:
            message = message.replace(value, replacement)
    return message


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        publisher = TalkAssetPublisher()
        if arguments.action in {"review", "next"}:
            candidate = (
                publisher.review(arguments.candidate_id)
                if arguments.action == "review"
                else publisher.next_candidate(arguments.candidate_id)
            )
            if candidate is None:
                print("No unsurfaced high-confidence talk-slide candidates found.")
            else:
                print(scanner.format_approval_card(candidate))
            return 0
        if arguments.action == "approve":
            candidate, _decisions = publisher._select_candidate(
                arguments.candidate_id,
                require_awaiting=True,
            )
            print(approval_summary(candidate))
            result = publisher.approve(arguments.candidate_id)
        elif arguments.action == "reject":
            result = publisher.reject(arguments.candidate_id)
        elif arguments.action == "hold":
            result = publisher.hold(arguments.candidate_id)
        elif arguments.action == "reset":
            if not arguments.candidate_id:
                raise PublicationError("reset requires a candidate ID.")
            result = publisher.reset(arguments.candidate_id)
        else:
            if not arguments.candidate_id:
                raise PublicationError("unpublish requires a candidate ID.")
            result = publisher.unpublish(
                arguments.candidate_id,
                confirmed=arguments.confirm,
            )
    except (PublicationError, OSError) as error:
        print(f"Error: {safe_error_message(error)}", file=sys.stderr)
        return 1
    print(result.message)
    if result.public_url:
        print(f"Public URL: {result.public_url}")
    if result.commit_sha:
        print(f"Commit: {result.commit_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
