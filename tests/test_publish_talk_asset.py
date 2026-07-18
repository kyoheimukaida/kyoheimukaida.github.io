from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))

from publish_talk_asset import (  # noqa: E402
    PublicationError,
    TalkAssetPublisher,
    destination_filename,
)
import scan_talk_assets as scanner  # noqa: E402
from scan_talk_assets import stable_candidate_id  # noqa: E402


TALK_DATE = "2026-07-29"
TALK_TITLE = "Listening for Dark Waves: Light Dark Matter & High-Frequency GWs"
TALK_EVENT = "QUP–IPNS Synergy Workshop"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_pdf(path: Path, title: str = TALK_TITLE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    escaped_title = title.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = (
        "BT\n/F1 24 Tf\n48 300 Td\n"
        f"({escaped_title}) Tj\n"
        "0 -80 Td\n/F1 14 Tf\n(Kyohei Mukaida) Tj\nET\n"
    ).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 720 405] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"endstream",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode("ascii"))
        document.extend(body)
        document.extend(b"\nendobj\n")
    xref_offset = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    document.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(document)


class PublishTalkAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "site"
        self.source = self.root / "MyTalks"
        self.repo.mkdir()
        self.source.mkdir()
        (self.repo / "_data").mkdir()
        (self.repo / ".local").mkdir()

        (self.repo / "index.md").write_text(
            "# Home\n\n<!-- talks:start -->\nold\n<!-- talks:end -->\n",
            encoding="utf-8",
        )
        (self.repo / "talks.md").write_text(
            "# Talks\n\n<!-- talks-auto:start -->\nold\n<!-- talks-auto:end -->\n",
            encoding="utf-8",
        )
        (self.repo / "_data/talks_history.json").write_text("[]\n", encoding="utf-8")
        self.talk_record = {
            "source": "calendar",
            "date": TALK_DATE,
            "title": TALK_TITLE,
            "kind": "Invited talk",
            "event": TALK_EVENT,
            "location": "QUP Building, KEK",
            "url": "https://example.org/event",
        }
        self._write_talk_records([self.talk_record])
        (self.repo / "_data/talk_assets.json").write_text("[]\n", encoding="utf-8")

        self.source_pdf = self.source / "QUP-workshop" / "kmukaida.pdf"
        write_pdf(self.source_pdf)
        self.candidate = self._candidate_for(self.source_pdf)
        self._write_report([self.candidate])
        self.publisher = self._publisher()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _publisher(self, **overrides: object) -> TalkAssetPublisher:
        arguments: dict[str, object] = {
            "repo_root": self.repo,
            "source_root": self.source,
            "git_enabled": False,
            "workflow_enabled": False,
            "now": lambda: datetime(2026, 7, 19, 1, 2, 3, tzinfo=timezone.utc),
        }
        arguments.update(overrides)
        return TalkAssetPublisher(**arguments)

    def _candidate_for(
        self,
        source_pdf: Path,
        *,
        talk_date: str = TALK_DATE,
        talk_title: str = TALK_TITLE,
        classification: str = "high_confidence_talk_matches",
        ambiguous: bool = False,
    ) -> dict[str, object]:
        digest = file_sha256(source_pdf)
        relative = source_pdf.relative_to(self.source)
        candidate_id = stable_candidate_id(talk_date, talk_title, digest)
        stat = source_pdf.stat()
        return {
            "candidate_id": candidate_id,
            "status": "awaiting_approval",
            "talk_date": talk_date,
            "talk_title": talk_title,
            "talk_event": TALK_EVENT,
            "candidate_filename": source_pdf.name,
            "material_filename": source_pdf.name,
            "relative_path": str(relative),
            "relative_source_path": str(relative),
            "file_type": "pdf",
            "content_sha256": digest,
            "source_sha256": digest,
            "source_size": stat.st_size,
            "source_mtime": datetime.fromtimestamp(
                stat.st_mtime,
                tz=timezone.utc,
            ).isoformat(),
            "cover_title": talk_title,
            "extracted_cover_title_candidate": talk_title,
            "score": 109,
            "match_score": 109,
            "identity_score": 100,
            "confidence": "high",
            "classification": classification,
            "ambiguous_candidate_match": ambiguous,
            "already_published": False,
        }

    def _write_report(self, candidates: list[dict[str, object]]) -> None:
        payload = {
            "schema_version": 3,
            "source_directory": "MYTALK_DIR",
            "candidate_count": len(candidates),
            "candidates": candidates,
        }
        (self.repo / ".local/talk-asset-candidates.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _read_decisions(self) -> dict[str, dict[str, object]]:
        path = self.repo / ".local/talk-asset-decisions.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text())["decisions"]

    def _second_candidate(self, *, score: int = 90) -> dict[str, object]:
        candidate = dict(self.candidate)
        candidate["candidate_id"] = "TA-2026-07-29-ABCDEF12"
        candidate["score"] = score
        candidate["match_score"] = score
        candidate["identity_score"] = score
        return candidate

    def _write_talk_records(self, records: list[dict[str, object]]) -> None:
        (self.repo / "_data/talks_combined.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _destination(self, candidate: dict[str, object] | None = None) -> Path:
        selected = candidate or self.candidate
        return self.repo / "assets/talk-slides" / destination_filename(selected)

    def test_bare_approval_with_one_pending_candidate(self) -> None:
        surfaced = self.publisher.review()
        self.assertEqual(surfaced["candidate_id"], self.candidate["candidate_id"])
        result = self.publisher.approve(None)
        self.assertTrue(result.changed)
        self.assertEqual(result.candidate_id, self.candidate["candidate_id"])
        self.assertTrue(self._destination().exists())

    def test_bare_approval_with_multiple_pending_uses_only_surfaced(self) -> None:
        second = self._second_candidate(score=80)
        self._write_report([self.candidate, second])
        surfaced = self.publisher.review()
        self.assertEqual(surfaced["candidate_id"], self.candidate["candidate_id"])
        result = self.publisher.approve(None)
        self.assertEqual(result.candidate_id, self.candidate["candidate_id"])
        self.assertTrue(result.changed)

    def test_bare_action_without_surfaced_candidate_fails(self) -> None:
        with self.assertRaisesRegex(PublicationError, "surfaced candidate"):
            self.publisher.approve(None)

    def test_explicit_candidate_id_approval(self) -> None:
        second = self._second_candidate()
        self._write_report([self.candidate, second])
        result = self.publisher.approve(str(self.candidate["candidate_id"]))
        self.assertTrue(result.changed)

    def test_source_hash_changed_fails(self) -> None:
        data = bytearray(self.source_pdf.read_bytes())
        data[-1] = (data[-1] + 1) % 256
        self.source_pdf.write_bytes(data)
        with self.assertRaisesRegex(PublicationError, "hash changed"):
            self.publisher.approve(str(self.candidate["candidate_id"]))

    def test_missing_source_pdf_fails(self) -> None:
        self.source_pdf.unlink()
        with self.assertRaisesRegex(PublicationError, "missing"):
            self.publisher.approve(str(self.candidate["candidate_id"]))

    def test_ambiguous_match_fails(self) -> None:
        self.candidate["ambiguous_candidate_match"] = True
        self._write_report([self.candidate])
        with self.assertRaisesRegex(PublicationError, "ambiguous"):
            self.publisher.approve(str(self.candidate["candidate_id"]))

    def test_oversized_pdf_fails(self) -> None:
        publisher = self._publisher(max_public_bytes=self.source_pdf.stat().st_size - 1)
        with self.assertRaisesRegex(PublicationError, "public limit"):
            publisher.approve(str(self.candidate["candidate_id"]))

    def test_identical_destination_is_reused(self) -> None:
        destination = self._destination()
        destination.parent.mkdir(parents=True)
        shutil.copyfile(self.source_pdf, destination)
        result = self.publisher.approve(str(self.candidate["candidate_id"]))
        self.assertTrue(result.changed)
        self.assertEqual(file_sha256(destination), self.candidate["source_sha256"])

    def test_different_destination_content_conflicts(self) -> None:
        destination = self._destination()
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"%PDF-different")
        with self.assertRaisesRegex(PublicationError, "different content"):
            self.publisher.approve(str(self.candidate["candidate_id"]))

    def test_repeated_approval_is_idempotent(self) -> None:
        first = self.publisher.approve(str(self.candidate["candidate_id"]))
        second = self.publisher.approve(str(self.candidate["candidate_id"]))
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(second.message, "Already published; no change needed.")
        manifest = json.loads((self.repo / "_data/talk_assets.json").read_text())
        self.assertEqual(len(manifest), 1)
        self.assertEqual(len(manifest[0]["assets"]), 1)

    def test_rejected_candidate_is_not_published(self) -> None:
        manifest_before = (self.repo / "_data/talk_assets.json").read_bytes()
        self.publisher.reject(str(self.candidate["candidate_id"]))
        with self.assertRaisesRegex(PublicationError, "rejected"):
            self.publisher.approve(str(self.candidate["candidate_id"]))
        self.assertEqual((self.repo / "_data/talk_assets.json").read_bytes(), manifest_before)
        self.assertFalse(self._destination().exists())

    def test_held_candidate_is_not_published(self) -> None:
        self.publisher.hold(str(self.candidate["candidate_id"]))
        with self.assertRaisesRegex(PublicationError, "held"):
            self.publisher.approve(str(self.candidate["candidate_id"]))
        self.assertFalse(self._destination().exists())

    def test_review_surfaces_only_highest_priority_candidate(self) -> None:
        second = self._second_candidate(score=80)
        third = self._second_candidate(score=70)
        third["candidate_id"] = "TA-2026-07-29-1234ABCD"
        self._write_report([second, third, self.candidate])
        surfaced = self.publisher.review()
        self.assertEqual(surfaced["candidate_id"], self.candidate["candidate_id"])
        decisions = self._read_decisions()
        self.assertEqual(
            [key for key, value in decisions.items() if value["status"] == "surfaced"],
            [self.candidate["candidate_id"]],
        )

    def test_scanner_surfaces_one_candidate_when_queue_is_empty(self) -> None:
        second = self._second_candidate(score=80)
        records = [second, self.candidate]
        decisions_path = self.repo / ".local/scanner-decisions.json"
        surfaced = scanner.surface_review_candidate(records, {}, decisions_path)
        self.assertEqual(surfaced["candidate_id"], self.candidate["candidate_id"])
        decisions = json.loads(decisions_path.read_text())["decisions"]
        self.assertEqual(
            sum(decision["status"] == "surfaced" for decision in decisions.values()),
            1,
        )

    def test_next_surfaces_next_unsurfaced_candidate(self) -> None:
        second = self._second_candidate(score=80)
        self._write_report([self.candidate, second])
        first = self.publisher.review()
        following = self.publisher.next_candidate()
        self.assertEqual(first["candidate_id"], self.candidate["candidate_id"])
        self.assertEqual(following["candidate_id"], second["candidate_id"])
        decisions = self._read_decisions()
        self.assertEqual(decisions[str(first["candidate_id"])]["status"], "awaiting_approval")
        self.assertIn("surfaced_at", decisions[str(first["candidate_id"])])
        self.assertEqual(decisions[str(following["candidate_id"])]["status"], "surfaced")
        self.assertEqual(
            sum(decision["status"] == "surfaced" for decision in decisions.values()),
            1,
        )

    def test_bare_reject_applies_to_surfaced_candidate(self) -> None:
        second = self._second_candidate(score=80)
        self._write_report([self.candidate, second])
        surfaced = self.publisher.review()
        result = self.publisher.reject(None)
        self.assertEqual(result.candidate_id, surfaced["candidate_id"])
        self.assertEqual(
            self._read_decisions()[str(surfaced["candidate_id"])]["status"],
            "rejected",
        )

    def test_bare_hold_applies_to_surfaced_candidate(self) -> None:
        second = self._second_candidate(score=80)
        self._write_report([self.candidate, second])
        surfaced = self.publisher.review()
        result = self.publisher.hold(None)
        self.assertEqual(result.candidate_id, surfaced["candidate_id"])
        self.assertEqual(
            self._read_decisions()[str(surfaced["candidate_id"])]["status"],
            "held",
        )

    def test_held_candidate_is_skipped_but_can_be_explicitly_resurfaced(self) -> None:
        second = self._second_candidate(score=80)
        self._write_report([self.candidate, second])
        first = self.publisher.review()
        self.publisher.hold(None)
        following = self.publisher.review()
        self.assertEqual(following["candidate_id"], second["candidate_id"])
        resurfaced = self.publisher.next_candidate(str(first["candidate_id"]))
        self.assertEqual(resurfaced["candidate_id"], first["candidate_id"])
        decisions = self._read_decisions()
        self.assertEqual(decisions[str(first["candidate_id"])]["status"], "surfaced")
        self.assertEqual(
            sum(decision["status"] == "surfaced" for decision in decisions.values()),
            1,
        )

    def test_rejected_and_published_candidates_are_not_surfaced_again(self) -> None:
        second = self._second_candidate(score=80)
        third = self._second_candidate(score=70)
        third["candidate_id"] = "TA-2026-07-29-1234ABCD"
        self._write_report([self.candidate, second, third])
        rejected = self.publisher.review()
        self.publisher.reject(None)
        published = self.publisher.review()
        self.publisher.approve(None)
        following = self.publisher.review()
        self.assertEqual(rejected["candidate_id"], self.candidate["candidate_id"])
        self.assertEqual(published["candidate_id"], second["candidate_id"])
        self.assertEqual(following["candidate_id"], third["candidate_id"])

    def test_multiple_surfaced_decisions_fail_clearly(self) -> None:
        second = self._second_candidate(score=80)
        self._write_report([self.candidate, second])
        timestamp = "2026-07-19T01:02:03+00:00"
        decisions = {
            str(self.candidate["candidate_id"]): {
                "status": "surfaced",
                "updated_at": timestamp,
                "surfaced_at": timestamp,
            },
            str(second["candidate_id"]): {
                "status": "surfaced",
                "updated_at": timestamp,
                "surfaced_at": timestamp,
            },
        }
        (self.repo / ".local/talk-asset-decisions.json").write_text(
            json.dumps({"schema_version": 1, "decisions": decisions}, indent=2) + "\n"
        )
        with self.assertRaisesRegex(PublicationError, "Multiple candidates are surfaced"):
            self.publisher.review()

    def test_relative_url_is_added_only_to_talks_page(self) -> None:
        result = self.publisher.approve(str(self.candidate["candidate_id"]))
        talks = (self.repo / "talks.md").read_text()
        index = (self.repo / "index.md").read_text()
        self.assertEqual(talks.count(f"[Slides]({result.public_url})"), 1)
        self.assertNotIn("[Slides]", index)
        self.assertNotIn(result.public_url, index)

    def test_local_dropbox_path_is_not_written_to_tracked_files(self) -> None:
        self.publisher.approve(str(self.candidate["candidate_id"]))
        private_path = str(self.source)
        for relative in ("_data/talk_assets.json", "talks.md", "index.md"):
            self.assertNotIn(private_path, (self.repo / relative).read_text())

    def test_unpublish_removes_only_public_copy_and_link(self) -> None:
        approved = self.publisher.approve(str(self.candidate["candidate_id"]))
        destination = self._destination()
        result = self.publisher.unpublish(
            str(self.candidate["candidate_id"]),
            confirmed=True,
        )
        self.assertTrue(result.changed)
        self.assertTrue(self.source_pdf.exists())
        self.assertFalse(destination.exists())
        self.assertEqual(json.loads((self.repo / "_data/talk_assets.json").read_text()), [])
        self.assertNotIn(approved.public_url, (self.repo / "talks.md").read_text())

    def test_unpublish_requires_explicit_confirmation(self) -> None:
        self.publisher.approve(str(self.candidate["candidate_id"]))
        with self.assertRaisesRegex(PublicationError, "--confirm"):
            self.publisher.unpublish(
                str(self.candidate["candidate_id"]),
                confirmed=False,
            )

    def test_candidate_id_is_stable(self) -> None:
        first = stable_candidate_id(TALK_DATE, TALK_TITLE, str(self.candidate["source_sha256"]))
        second = stable_candidate_id(
            TALK_DATE,
            "LISTENING FOR DARK WAVES: LIGHT DARK MATTER & HIGH-FREQUENCY GWS",
            str(self.candidate["source_sha256"]),
        )
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
