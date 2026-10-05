#!/usr/bin/env python3
"""Compare rendered-site inputs with the last successful Pages deployment."""

from __future__ import annotations

import subprocess
import sys


# INSPIRE/citation caches change daily but do not directly render on the site.
# The generators write the public content to the Markdown pages below.
SITE_PATHS = (
    "*.md", "*.html", "_config.yml", "_layouts", "_includes", "_sass",
    "assets", "_data", "CNAME", "Gemfile", "Gemfile.lock",
    ":(exclude)README.md", ":(exclude)docs/**",
    ":(exclude)_data/publications_inspire.json",
    ":(exclude)_data/publications_highlights.json",
)


def content_changed(deployed_sha: str) -> bool:
    if not deployed_sha:
        return True
    # A missing historical commit must not prevent a fresh deployment.
    if subprocess.run(
        ["git", "cat-file", "-e", f"{deployed_sha}^{{commit}}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode:
        return True
    result = subprocess.run(
        ["git", "diff", "--quiet", deployed_sha, "HEAD", "--", *SITE_PATHS],
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("Could not compare Pages content with the deployed commit.")
    return result.returncode == 1


if __name__ == "__main__":
    print("true" if content_changed(sys.argv[1] if len(sys.argv) > 1 else "") else "false")
