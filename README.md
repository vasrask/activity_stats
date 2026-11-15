# Activity Stats

Activity Stats collects activity for public GitHub repositories and generates a static dashboard with repository and release pages.

Default repositories:

- [Jenkins](https://github.com/jenkinsci/jenkins)
- [Woodpecker](https://github.com/woodpecker-ci/woodpecker)
- [Tekton Pipelines](https://github.com/tektoncd/pipeline)

For architecture, collection semantics, database tables, and limitations, see [DESIGN.md](DESIGN.md).

## Requirements

- Python 3.10 or later
- GitHub CLI 2.104.0 or later (optional)
- A GitHub token (optional)

No Python packages need to be installed; the collector uses the standard library.

## Run locally

From the project directory set a GitHub token, either manually from GitHub or using GitHub CLI. In PowerShell:

```powershell
gh auth login
$env:GITHUB_TOKEN = gh auth token
```

In Command Prompt, the equivalent is:

```cmd
gh auth login
for /f "delims=" %i in ('gh auth token') do set "GITHUB_TOKEN=%i"
```

On Linux, authenticate with GitHub CLI and export its token into the current shell:

```bash
gh auth login
export GITHUB_TOKEN="$(gh auth token)"
```

You can also set `GITHUB_TOKEN` using another secure method. A token is optional for public repositories, but helps avoid the low unauthenticated API rate limit.

Then, on Windows or Linux, collect data and write a deployable site to `docs/`:

```powershell
python activity_stats.py
```

The first run requests up to 400 days of activity by default. Later runs use the repository's saved sync position and recheck the previous day to avoid gaps at the boundary. This works whether you run the program daily, less often, or on demand. The lookback is a rolling limit: if the program is not run for longer than the configured window, activity older than that window will not be backfilled. The `activity_stats.db` database is in the project directory by default and preserves collected data and sync positions between runs. Generated HTML, CSS, and JavaScript go to `docs/` by default.

### Preview locally

After generating the site, start Python's built-in web server from the project directory:

```bash
python -m http.server 8000 --directory docs
```

Open [http://localhost:8000](http://localhost:8000) in your browser. The generated site can also be hosted on any static web server without GitHub Pages.

Useful options:

```powershell
# Collect a different repository and/or lookback period
python activity_stats.py --repo owner/repo --since-days 180 --output-dir docs

# Collect custom repositories; repeat --repo to collect more than one
python activity_stats.py --repo owner/first --repo owner/second --output-dir docs

# Rebuild the website from SQLite without calling GitHub
python activity_stats.py --render-only --output-dir docs

# Use a different database
python activity_stats.py --database activity_stats-local.sqlite3 --output-dir docs
```

If GitHub's rate limit is reached, collection saves any progress, reports a warning, and still generates the website from saved data. An incomplete sync does not advance its watermark, so the next run retries that interval. If there is no data yet, the generated overview says so.

Run tests with:

```bash
python -m unittest
```

## Continuous integration and GitHub Pages deployment

The workflow in `.github/workflows/pages.yml` runs the unit tests on pull
requests and pushes to `main`. After tests pass on a push to `main`, it deploys
the committed `docs/` directory to GitHub Pages. It does not collect fresh
GitHub activity; generate the site and commit the updated `docs/` when you
want to publish new statistics.

### One-time setup

1. Push this project to your GitHub repository.
2. In the repository, open **Settings → Pages** and set the build and
   deployment source to **GitHub Actions**.
3. Ensure the repository's Actions settings allow the workflow to run. The
   workflow needs the `pages: write` and `id-token: write` permissions declared
   in its deploy job.

### Publish an updated dashboard

From the project directory, regenerate the site, commit it, and push to `main`.
For example:

```bash
python activity_stats.py
git add docs
git commit -m "Update activity dashboard"
git push
```

Use `python3` instead of `python` on Linux. Tests run on GitHub before
deployment; a failed test prevents the deployment. You can also manually
trigger the deployment from the **Actions** tab or with GitHub CLI:

```bash
gh workflow run pages.yml --repo vasrask/activity_stats --ref main
```

The Pages deployment serves the already-generated static files; the SQLite
database is not uploaded and is not required by the hosted site. Keep the
database on the machine where you collect activity so future runs can continue
from the saved sync state.