# Kyohei Mukaida

Source files for my personal academic website.

Website: https://kyoheimukaida.github.io/

## Paper summaries and website deployment

- The home page shows the three most recent arXiv papers from INSPIRE.
- English summaries are maintained in `_data/paper_summaries.json` by the weekly Codex task; GitHub Actions only reads the cache and needs no OpenAI API key.
- The **Deploy website** workflow publishes website changes pushed to `main`. It also runs after successful paper, publication, CV, or grant updater workflows, because their `GITHUB_TOKEN` commits do not trigger push workflows.
- Deployment compares website inputs with the last successful Pages deployment. Cache-only INSPIRE/citation refreshes and unchanged public pages do not rebuild or redeploy the site.
- To recover a missed deployment, run **Deploy website** manually. Check the published page after the workflow succeeds; a Git commit alone does not confirm publication.

## Public-talk calendar operations

The automated data flow is: dedicated public-website Google Calendar → private ICS feed → GitHub Actions → `scripts/update_talks_from_ics.py` → `index.md` and `talks.md` → GitHub Pages.

- **Live source of truth:** Only the dedicated public-website calendar is used for live and upcoming public talks. Do not use a primary, private-meeting, or internal schedule calendar.
- **Historical source:** `_data/talks_history.json` supplies historical/manual records. Calendar records take precedence when the same talk exists in both sources.
- **Repository secret:** `TALKS_ICS_URLS` must contain the dedicated calendar's private ICS URL. Never commit or log the URL.
- **Schedule:** Daily at 08:17 Asia/Tokyo.
- **Manual run:** Actions → Update public talks → Run workflow.
- **Generated files:** Only `index.md` and `talks.md` are staged by the updater.
- **No-change behavior:** The workflow creates no commit and deploys no Pages artifact.
- **Missing-secret behavior:** GitHub Actions fails before generation instead of silently using the local calendar cache fallback.
- **Pages setup:** In Settings → Pages → Build and deployment, select **GitHub Actions** as the source so the workflow can deploy the generated artifact.

For troubleshooting, check the `TALKS_ICS_URLS` secret, the Update public talks logs, the generated block markers, duplicate entries (especially calendar/history overlaps), and the GitHub Pages deployment status. The script's cached-calendar fallback is for local development only.

Local Dropbox candidate scanning, one-candidate approval, repository-hosted slide publication, and rollback are documented in [Talk asset operations](docs/talk-assets.md).
