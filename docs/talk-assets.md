# Talk-slide candidate and publication operations

The dedicated public-talk calendar remains the source of truth for talk metadata. Dropbox/MyTalks remains the private source archive for presentation files. The repository receives a public PDF copy only after an explicit per-candidate approval.

```text
Dropbox/MyTalks
→ scan_talk_assets.py (local, read-only candidate detection)
→ explicit approval of one stable candidate ID
→ assets/talk-slides/<date>-<talk-title>.pdf
→ _data/talk_assets.json
→ talks.md
→ commit and push
→ Update public talks workflow
→ GitHub Pages
```

Discovery alone never publishes a file. The weekly scanner cannot copy PDFs, edit the manifest, commit, push, change Dropbox sharing, or deploy Pages.

## Local candidate scan

Install the page-one PDF extractor and run the scanner locally:

```bash
python3 -m pip install -r requirements-talk-assets.txt

MYTALK_DIR="$HOME/Library/CloudStorage/Dropbox/MyTalks" \
python3 scripts/scan_talk_assets.py
```

If `MYTALK_DIR` is absent, the scanner checks common Dropbox locations. It refuses to choose when multiple directories match.

The scanner reads the first page of each PDF, extracts embedded text without OCR, and combines visible cover-title evidence with date, event, location, directory, metadata, and weak filename evidence. Generic filenames such as `kmukaida.pdf` do not identify a talk. PDFs are preferred over nearby `.key`, `.pptx`, or `.odp` source files, while draft, backup, temporary, duplicate, and ambiguous files are kept out of automatic high-confidence approval cards.

Local reports are written to:

```text
.local/talk-asset-candidates.json
.local/talk-asset-candidates.md
.local/talk-asset-decisions.json
```

`.local/` is ignored by Git. Candidate reports may contain paths relative to `MYTALK_DIR` and extracted first-page text; none of that information is copied into tracked files.

## Stable candidate IDs and approval cards

Every matched PDF candidate receives an ID derived from the talk date, normalized talk title, and PDF SHA-256 identity:

```text
TA-2026-07-29-A4F2D8C1
```

The ID remains stable while those three values remain unchanged. A changed source PDF receives a different ID and requires a new scan and a new approval.

The local decision file implements a single-candidate review queue. Any number of candidates may remain `awaiting_approval`, but at most one may be `surfaced`. Only the surfaced candidate is actionable by a bare reply. The scan command and the `review` command surface the highest-scoring, then highest-identity-score unsurfaced high-confidence candidate when no candidate is currently surfaced.

The surfaced card includes the ID, talk, date, PDF filename, extracted cover title, score, and status. A direct reply of `o`, `ok`, or `OK` approves it; `n` rejects it; `hold` holds it; and `next` advances to the next unsurfaced candidate. These mappings apply only in direct response to the review card and are not inferred from an unrelated use of “okay.” Thirty-one awaiting candidates therefore do not make a bare `o` ambiguous: only one candidate is surfaced.

An ID may still be named explicitly:

```text
o TA-2026-07-29-A4F2D8C1
```

There is no bulk approval, `approve all`, timeout approval, or implicit approval.

## Approval, rejection, and hold commands

The underlying commands are:

```bash
python3 scripts/publish_talk_asset.py review
python3 scripts/publish_talk_asset.py next
python3 scripts/publish_talk_asset.py approve TA-2026-07-29-A4F2D8C1
python3 scripts/publish_talk_asset.py reject TA-2026-07-29-A4F2D8C1
python3 scripts/publish_talk_asset.py hold TA-2026-07-29-A4F2D8C1
python3 scripts/publish_talk_asset.py reset TA-2026-07-29-A4F2D8C1
```

`approve`, `reject`, and `hold` may omit the ID when one candidate is surfaced. `next` records the current candidate as previously surfaced and advances without rejecting or publishing it. Previously surfaced candidates are not automatically cycled back into the queue. Held candidates are skipped; explicitly request one with `next TA-...` or `review TA-...` before approving it. Rejected and published candidates cannot be surfaced again. `reject` and `hold` update only `.local/talk-asset-decisions.json`; they do not modify tracked files or the Dropbox source. `reset` explicitly returns an unambiguous high-confidence candidate to the unsurfaced awaiting state.

Before performing an approval, the assistant reports the exact candidate, destination PDF, target talk, and files to be committed. It then runs the complete operation without asking the user to edit files or create a Dropbox link.

## Publication validation and repository copy

Approval verifies all of the following before publication:

- the candidate exists and is surfaced, or was named explicitly while awaiting approval;
- the candidate maps uniquely to one current website talk;
- the match remains high-confidence and unambiguous;
- the relative source path remains inside `MYTALK_DIR`;
- the source is a non-symlink PDF, not a draft, backup, or temporary file;
- scan-time size and SHA-256 still match;
- the PDF signature and first-page cover title remain valid and compatible;
- the PDF is within `MAX_PUBLIC_SLIDE_MB` (25 MB by default);
- an existing destination is either byte-identical or treated as a conflict.

Oversized PDFs are not compressed or altered. A hash, size, source, cover, or match change stops publication and requires a new scan.

The destination is deterministic, lowercase, ASCII-safe, and repository-local:

```text
assets/talk-slides/2026-07-29-listening-for-dark-waves-light-dark-matter-high-frequency-gws.pdf
```

The manifest stores a site-relative URL and audit metadata, never a Dropbox path or URL:

```json
[
  {
    "date": "2026-07-29",
    "title": "Listening for Dark Waves: Light Dark Matter & High-Frequency GWs",
    "event": "QUP–IPNS Synergy Workshop",
    "assets": [
      {
        "type": "slides",
        "label": "Slides",
        "url": "/assets/talk-slides/2026-07-29-listening-for-dark-waves-light-dark-matter-high-frequency-gws.pdf",
        "public": true,
        "candidate_id": "TA-2026-07-29-A4F2D8C1",
        "sha256": "...",
        "approved_at": "2026-07-19T01:02:03+00:00"
      }
    ]
  }
]
```

Only `public: true` assets with a valid repository slide path or public `https://` URL are rendered. Relative slide URLs must stay below `/assets/talk-slides/` and end in `.pdf`. Local paths, `file://`, local/LAN hosts, credentials, private ICS feeds, Dropbox folders, and malformed URLs are rejected.

Slides links appear only in `talks.md`, never in the upcoming block in `index.md`.

## Commit, workflow, and deployment

After copying and updating the manifest, the approval command regenerates the talk pages from cached calendar data when `TALKS_ICS_URLS` is absent. It checks markers, conflict markers, duplicate link count, trailing whitespace, `git diff --check`, and local-path leakage.

Only the approved PDF, `_data/talk_assets.json`, `talks.md`, and a legitimately changed `index.md` may be staged. Existing staged changes or pre-existing changes to those target files stop publication. Unrelated unstaged changes are neither committed nor discarded. The command commits, rebases from `origin/main` with Git autostash support, pushes `main`, monitors **Update public talks**, dispatches it if the push did not start it, and verifies that Pages contains the Slides URL.

The workflow deploys Pages when either generated talk content changes or a pushed commit changes `_data/talk_assets.json` or `assets/talk-slides/`. Scheduled scans with no generated change still create neither a commit nor a Pages deployment.

The local machine needs authenticated `git` push access and authenticated GitHub CLI access (`gh auth status`). The repository still needs the `TALKS_ICS_URLS` Actions secret for workflow validation and calendar synchronization.

Repeated approval is idempotent:

```text
Already published; no change needed.
```

It does not duplicate the PDF, manifest asset, Slides link, commit, or deployment.

## Rollback

Unpublishing requires an explicit candidate ID and confirmation flag:

```bash
python3 scripts/publish_talk_asset.py unpublish TA-2026-07-29-A4F2D8C1 --confirm
```

The command removes the matching manifest asset, regenerates `talks.md`, and removes the repository PDF only when no other manifest asset references its URL. It commits, pushes, and monitors the workflow using the same safety rules. It never deletes or changes the Dropbox source. The local candidate is left held after rollback; use `reset` before a later re-approval.

## Weekly Codex Automation

Recommended name: **Talk Slide Candidate Scan**

Recommended schedule: **Weekly, Monday, 08:45 Asia/Tokyo, Dedicated worktree**

```text
Scan the local Dropbox/MyTalks directory for presentation files that may correspond to talks in kyoheimukaida.github.io.

Use MYTALK_DIR if configured.

Run:
python3 scripts/scan_talk_assets.py

Rules:
- Candidate detection only.
- Do not run publish_talk_asset.py.
- Do not modify _data/talk_assets.json.
- Do not create Dropbox shared links.
- Do not mark any asset public.
- Do not commit or push.
- Do not copy local files into the repository.
- Do not expose local absolute paths outside the local candidate report.
- Keep at most one candidate surfaced for review.
- If none is surfaced, surface the highest-priority unsurfaced high-confidence candidate.
- Present the surfaced candidate with its stable candidate ID and approval card.
- If no unsurfaced candidates exist, report:
  No unsurfaced high-confidence talk-slide candidates found.
```
