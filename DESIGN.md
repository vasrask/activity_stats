# Activity Stats: design and collection logic

## Purpose

Activity Stats is a static-report workflow of GitHub repository activity. Python collects API responses into SQLite; Python mtemplates produce static HTML pages with Highcharts visualizations. The published website is read-only and does not call GitHub.

The default tracked repositories are `jenkinsci/jenkins`, `woodpecker-ci/woodpecker`, and `tektoncd/pipeline`.

## Domain model

- **Repository** holds GitHub repository metadata and its releases.
- **RepositoryActivity** represents the collected activity for a repository
  over a time interval and provides event and cumulative counts.
- **RepositoryRelease** represents a GitHub release. Releases divide activity
  into version-like intervals.
- **ActivityStatsDashboard** builds the overview, repository pages, and
  release pages from the stored data.
- **GitHubClient** handles REST API requests, authorization, errors, and the
  API's pagination links.

## Collection and refresh

The first collection for a repository requests activity from the configured lookback boundary (400 days by default). Each later run reads that repository's `last_successful_sync` from SQLite and requests data from that point, rechecking the previous day to avoid gaps at the boundary. Events are deduplicated by `(repository, event type, event ID)`. The configured lookback remains a rolling limit, so a repository not collected for longer than that window cannot be backfilled beyond the current window.

The sync watermark is written only after all collection endpoints finish for that repository. If a request fails partway through, already fetched rows may be committed, but the watermark is not advanced; a later run retries the unconfirmed interval. Repository runs are committed independently.

Collection includes:

- Commits, using commit author dates.
- Fork creation events.
- Published releases, as release/version boundaries.
- Issue and pull-request open/close events.
- Current repository totals: stars, watchers, forks, and open issues plus
  pull requests.
- Contributor counts and language byte totals as dated observations.

The GitHub API exposes current repository totals, contributors, and language composition rather than complete historical time series. Activity Stats records these values as snapshots when it runs; those histories begin with the first successful collection.

## Release intervals

Release pages group recorded activity after the preceding release through the selected release. The earliest collected release uses the configured lookback boundary as its lower bound. Counts for an interval are limited to events retained within the collection window. The release page shows individual event counts, recorded contributors, and a cumulative commit chart.

Since GitHub's release endpoint only returns published releases, tags that have no published release are not being considered.

## Database

The SQLite schema is maintained in [`schema.sql`](schema.sql). Its version is stored in SQLite's `PRAGMA user_version`; the schema is applied once to a new or unversioned database, not recreated on every run.

- `repositories`: current descriptive metadata and last fetch time.
- `events`: deduplicated timestamped commits, forks, releases, issues, and
  pull requests.
- `repository_snapshots`: dated repository-wide totals.
- `repository_sync_state`: initial lookback boundary and successful-sync
  watermark.
- `contributors`: dated contribution totals returned by GitHub.
- `languages`: dated language byte totals returned by GitHub.

The database is needed by the collector to retain collected records and sync positions between runs. Keep it on the machine that collects the data; without it, the collector starts without the saved sync position and may need to make more API requests. The database is not needed to view or serve an already generated website: those pages and their assets are self-contained static files.

## Generated pages

Templates live in `templates/`, and the dashboard stylesheet lives in `static/activity_stats.css`.

- `index.html` compares repositories and shows daily and cumulative activity.
- One repository page shows activity charts, repository snapshots, languages, contributors, recent events, and links to releases.
- One release page shows its interval activity and release metadata.

Generated files go to the directory passed to `--output-dir` (`docs/` by default). The renderer copies the required CSS and Highcharts runtime from the project's `static/` directory into the output directory, alongside the generated HTML. That output can be published by a static host such as GitHub Pages, or served locally for preview with Python's built-in web server (`python -m http.server 8000 --directory docs`).


## Limitations

- A first backfill can use many API requests. A token helps avoid the unauthenticated rate limit.
- GitHub returns only the events exposed by the relevant endpoints; this is not a complete audit log of every repository action.
- `open_issues_count` combines issues and pull requests in repository totals; collected activity events distinguish them.
- Historical stars, watchers, contributor totals, and language composition are unavailable before the first snapshot.
- Release interval counts include only activity collected within the configured rolling lookback (400 days by default). If a release interval began before that window, its page does not include events from before the window, so its counts are not necessarily lifetime totals for that release.