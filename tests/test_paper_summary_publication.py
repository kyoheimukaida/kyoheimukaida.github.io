from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import update_recent_papers as recent


class RecentPapersTest(unittest.TestCase):
    def test_third_paper_and_summary_survive_homepage_generation(self):
        ids = ["2609.07467", "2609.05621", "2609.01480", "2603.06296"]
        hits = [{"metadata": {"titles": [{"title": aid}],
                              "arxiv_eprints": [{"value": aid}]}} for aid in ids]
        payload = json.dumps({"hits": {"hits": hits}}).encode()
        cache = recent.load_summary_cache(str(ROOT / "_data/paper_summaries.json"))
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / "index.md"
            page.write_text(f"before\n{recent.START}\nold\n{recent.END}\nafter\n")
            with patch.object(recent.urllib.request, "urlopen", return_value=io.BytesIO(payload)), \
                 patch.object(recent, "README_PATH", str(page)), \
                 patch.object(recent, "load_summary_cache", return_value=cache):
                recent.main()
            result = page.read_text()
            for aid in ids[:3]:
                self.assertIn(f"https://arxiv.org/abs/{aid}", result)
            self.assertNotIn(ids[3], result)
            self.assertIn(cache[ids[2]]["summary_en"], result)
            self.assertTrue(result.startswith("before\n"))
            self.assertTrue(result.endswith("\nafter\n"))


class DeploymentComparisonTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "index.md").write_text("Existing website\n")
        (self.repo / "_data").mkdir()
        (self.repo / "_data/publications_highlights.json").write_text("{}\n")
        self.base = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True).strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def changed(self, sha):
        return subprocess.check_output(
            [sys.executable, str(ROOT / "scripts/pages_content_changed.py"), sha],
            cwd=self.repo, text=True,
        ).strip()

    def test_paper_update_deploys_without_talk_changes(self):
        (self.repo / "index.md").write_text("New paper with its summary\n")
        self.commit()
        self.assertEqual(self.changed(self.base), "true")

    def test_citation_cache_refresh_does_not_redeploy(self):
        (self.repo / "_data/publications_highlights.json").write_text('{"citations": 42}\n')
        self.commit()
        self.assertEqual(self.changed(self.base), "false")

    def test_already_published_content_does_not_redeploy(self):
        self.assertEqual(self.changed(self.base), "false")

    def test_first_or_unavailable_deployment_gets_rebuilt(self):
        self.assertEqual(self.changed(""), "true")
        self.assertEqual(self.changed("0" * 40), "true")


if __name__ == "__main__":
    unittest.main()
