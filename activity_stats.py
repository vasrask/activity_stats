#!/usr/bin/env python3
"""Collect public GitHub repository activity and generate a static dashboard."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any, Iterator


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_REPOSITORIES = (
    "jenkinsci/jenkins",
    "woodpecker-ci/woodpecker",
    "tektoncd/pipeline",
)
API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
CHARTS_URL = "static/highcharts/code/highcharts.js"
EVENT_TYPES = (
    "commit",
    "fork",
    "issue_opened",
    "issue_closed",
    "pull_request_opened",
    "pull_request_closed",
)
ACTIVITY_EVENT_TYPES = EVENT_TYPES
EVENT_LABELS = {
    "commit": "Commits",
    "fork": "Forks",
    "release": "Releases",
    "issue_opened": "Issues opened",
    "issue_closed": "Issues closed",
    "pull_request_opened": "Pull requests opened",
    "pull_request_closed": "Pull requests closed",
}


class GitHubAPIError(RuntimeError):
    """Raised when GitHub cannot return a successful API response."""


class GitHubClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token

    def _get(
        self, url: str, params: dict[str, str] | None = None
    ) -> tuple[Any, str | None]:
        query = urllib.parse.urlencode(params or {})
        if not url.startswith(API_URL):
            url = f"{API_URL}{url}"
        if query:
            url = f"{url}?{query}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "activity-stats",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response), response.headers.get("Link")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            if error.code in (403, 429) and error.headers.get("X-RateLimit-Remaining") == "0":
                reset = error.headers.get("X-RateLimit-Reset", "unknown")
                raise GitHubAPIError(
                    f"GitHub API rate limit reached (reset timestamp: {reset}). "
                    "Set GITHUB_TOKEN to raise the limit."
                ) from error
            raise GitHubAPIError(
                f"GitHub API returned HTTP {error.code} for {url}: {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise GitHubAPIError(f"Could not reach the GitHub API: {error.reason}") from error

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        response, _ = self._get(path, params)
        return response

    def pages(
        self,
        path: str,
        params: dict[str, str] | None = None,
        stop_before: Any | None = None,
    ) -> Iterator[dict[str, Any]]:
        url = path
        page_params = dict(params or {})
        page_params["per_page"] = "100"
        while True:
            results, link_header = self._get(url, page_params)
            if not isinstance(results, list):
                raise GitHubAPIError(f"Expected a list response from {url}")
            if not results:
                return
            for result in results:
                if stop_before is not None and stop_before(result):
                    return
                yield result
            next_link = (
                re.search(r'<([^>]+)>;\s*rel="next"', link_header)
                if link_header
                else None
            )
            if next_link is None:
                return
            url = next_link.group(1)
            page_params = {}


def parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def timestamp_text(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class RepositoryRelease:
    event_id: str
    tag_name: str
    name: str
    published_at: dt.datetime
    author: str
    url: str
    prerelease: bool

    @classmethod
    def from_event(cls, row: sqlite3.Row) -> RepositoryRelease:
        details = json.loads(row["details"])
        published_at = parse_datetime(row["occurred_at"])
        if published_at is None:
            raise ValueError(f"Release {row['event_id']} has no publication date")
        tag_name = details.get("tag_name") or row["event_id"]
        return cls(
            event_id=row["event_id"],
            tag_name=tag_name,
            name=details.get("name") or tag_name,
            published_at=published_at,
            author=row["actor"] or "Unknown",
            url=details.get("html_url") or "",
            prerelease=bool(details.get("prerelease", False)),
        )

    @property
    def slug(self) -> str:
        safe_tag = re.sub(r"[^A-Za-z0-9._-]+", "_", self.tag_name).strip("._")
        return safe_tag or self.event_id


class Repository:
    def __init__(self, record: sqlite3.Row, connection: sqlite3.Connection) -> None:
        self.record = record
        self.connection = connection
        self.full_name = record["full_name"]
        self.slug = self.full_name.replace("/", "_")
        self.releases = self._load_releases()

    def _load_releases(self) -> list[RepositoryRelease]:
        rows = self.connection.execute(
            """
            SELECT event_id, occurred_at, actor, details FROM events
            WHERE full_name = ? AND event_type = 'release'
            ORDER BY occurred_at, event_id
            """,
            (self.full_name,),
        )
        return [RepositoryRelease.from_event(row) for row in rows]

    def activity(self, since: dt.datetime, until: dt.datetime) -> RepositoryActivity:
        return RepositoryActivity(self, since, until)


class RepositoryActivity:
    def __init__(
        self, repository: Repository, since: dt.datetime, until: dt.datetime
    ) -> None:
        self.repository = repository
        self.since = since
        self.until = until
        self.counts = event_counts(
            repository.connection, repository.full_name, since
        )
        self.daily = daily_event_series(
            repository.connection, repository.full_name, since, until
        )

    def total(self) -> int:
        return sum(self.counts.values())

    def events_between(
        self, start: dt.datetime, end: dt.datetime
    ) -> list[sqlite3.Row]:
        bounded_start = max(start, self.since)
        bounded_end = min(end, self.until)
        if bounded_start >= bounded_end:
            return []
        return self.repository.connection.execute(
            """
            SELECT event_type, occurred_at, actor, details FROM events
            WHERE full_name = ? AND event_type != 'release'
                AND occurred_at > ? AND occurred_at <= ?
            ORDER BY occurred_at, event_id
            """,
            (
                self.repository.full_name,
                timestamp_text(bounded_start),
                timestamp_text(bounded_end),
            ),
        ).fetchall()

    def event_counts_between(
        self, start: dt.datetime, end: dt.datetime
    ) -> Counter[str]:
        return Counter(
            row["event_type"] for row in self.events_between(start, end)
        )

    def commits_between(
        self, start: dt.datetime, end: dt.datetime
    ) -> list[sqlite3.Row]:
        return [
            row for row in self.events_between(start, end)
            if row["event_type"] == "commit"
        ]


def connect_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if schema_version < 1:
        schema_path = PROJECT_DIR / "schema.sql"
        schema = schema_path.read_text(encoding="utf-8")
        connection.executescript(
            f"BEGIN;\n{schema}\nPRAGMA user_version = 1;\nCOMMIT;"
        )
    return connection


def insert_event(
    connection: sqlite3.Connection,
    repository: str,
    event_type: str,
    event_id: str | int,
    occurred_at: str | None,
    actor: str | None,
    details: dict[str, Any],
) -> None:
    parsed = parse_datetime(occurred_at)
    if parsed is None:
        return
    connection.execute(
        """
        INSERT OR IGNORE INTO events
            (full_name, event_type, event_id, occurred_at, actor, details)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            repository,
            event_type,
            str(event_id),
            timestamp_text(parsed),
            actor,
            json.dumps(details, separators=(",", ":"), sort_keys=True),
        ),
    )


def save_snapshot(
    connection: sqlite3.Connection,
    repository: str,
    observed_on: str,
    metrics: dict[str, int | float | None],
) -> None:
    connection.executemany(
        """
        INSERT INTO repository_snapshots (full_name, observed_on, metric, value)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(full_name, observed_on, metric)
        DO UPDATE SET value = excluded.value
        """,
        [
            (repository, observed_on, metric, value)
            for metric, value in metrics.items()
            if value is not None
        ],
    )


def collection_start(
    connection: sqlite3.Connection,
    repository: str,
    now: dt.datetime,
    days: int,
    overlap_days: int = 1,
) -> dt.datetime:
    window_start = now - dt.timedelta(days=days)
    state = connection.execute(
        "SELECT last_successful_sync FROM repository_sync_state WHERE full_name = ?",
        (repository,),
    ).fetchone()
    if state is None:
        return window_start

    last_success = parse_datetime(state["last_successful_sync"])
    if last_success is None:
        return window_start
    return max(window_start, last_success - dt.timedelta(days=overlap_days))


def save_sync_success(
    connection: sqlite3.Connection,
    repository: str,
    initial_since: dt.datetime,
    completed_at: dt.datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO repository_sync_state
            (full_name, initial_since, last_successful_sync)
        VALUES (?, ?, ?)
        ON CONFLICT(full_name) DO UPDATE SET
            last_successful_sync = excluded.last_successful_sync
        """,
        (
            repository,
            timestamp_text(initial_since),
            timestamp_text(completed_at),
        ),
    )


def sync_repository(
    client: GitHubClient,
    connection: sqlite3.Connection,
    repository: str,
    since: dt.datetime,
) -> None:
    encoded_repository = urllib.parse.quote(repository, safe="/")
    endpoint = f"/repos/{encoded_repository}"
    info = client.get(endpoint)
    if not isinstance(info, dict) or info.get("full_name") is None:
        raise GitHubAPIError(f"Unexpected repository response for {repository}")

    full_name = str(info["full_name"])
    now = dt.datetime.now(dt.timezone.utc)
    observed_on = now.date().isoformat()
    since_text = timestamp_text(since)
    connection.execute(
        """
        INSERT INTO repositories
            (full_name, html_url, description, created_at, default_branch, language, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(full_name) DO UPDATE SET
            html_url = excluded.html_url,
            description = excluded.description,
            created_at = excluded.created_at,
            default_branch = excluded.default_branch,
            language = excluded.language,
            fetched_at = excluded.fetched_at
        """,
        (
            full_name,
            info.get("html_url", ""),
            info.get("description"),
            info.get("created_at"),
            info.get("default_branch"),
            info.get("language"),
            timestamp_text(now),
        ),
    )

    save_snapshot(
        connection,
        full_name,
        observed_on,
        {
            "stars": info.get("stargazers_count"),
            "watchers": info.get("subscribers_count"),
            "forks": info.get("forks_count"),
            "open_issues_and_prs": info.get("open_issues_count"),
        },
    )

    for commit in client.pages(
        f"{endpoint}/commits", {"since": since_text}
    ):
        commit_data = commit.get("commit", {})
        author = commit_data.get("author") or {}
        github_author = commit.get("author") or {}
        insert_event(
            connection,
            full_name,
            "commit",
            commit["sha"],
            author.get("date"),
            github_author.get("login") or author.get("name"),
            {"message": commit_data.get("message", "").splitlines()[0]},
        )

    for fork in client.pages(
        f"{endpoint}/forks",
        {"sort": "newest"},
        stop_before=lambda item: bool(item.get("created_at") and item["created_at"] < since_text),
    ):
        owner = fork.get("owner") or {}
        insert_event(
            connection,
            full_name,
            "fork",
            fork["id"],
            fork.get("created_at"),
            owner.get("login"),
            {"full_name": fork.get("full_name")},
        )

    for release in client.pages(
        f"{endpoint}/releases",
        stop_before=lambda item: bool(
            item.get("created_at") and item["created_at"] < since_text
        ),
    ):
        published_at = release.get("published_at")
        if published_at is None:
            continue
        author = release.get("author") or {}
        insert_event(
            connection,
            full_name,
            "release",
            release["id"],
            published_at,
            author.get("login"),
            {
                "name": release.get("name"),
                "tag_name": release.get("tag_name"),
                "prerelease": release.get("prerelease", False),
                "html_url": release.get("html_url"),
            },
        )

    for issue in client.pages(
        f"{endpoint}/issues",
        {"state": "all", "sort": "updated", "direction": "desc", "since": since_text},
        stop_before=lambda item: bool(
            item.get("updated_at") and item["updated_at"] < since_text
        ),
    ):
        issue_id = issue["id"]
        actor = (issue.get("user") or {}).get("login")
        is_pull_request = "pull_request" in issue
        prefix = "pull_request" if is_pull_request else "issue"
        issue_details = {
            "number": issue.get("number"),
            "title": issue.get("title"),
            "html_url": issue.get("html_url"),
        }
        created_at = issue.get("created_at")
        if created_at and created_at >= since_text:
            insert_event(
                connection,
                full_name,
                f"{prefix}_opened",
                issue_id,
                created_at,
                actor,
                issue_details,
            )
        closed_at = issue.get("closed_at")
        if closed_at and closed_at >= since_text:
            insert_event(
                connection,
                full_name,
                f"{prefix}_closed",
                f"{issue_id}:closed",
                closed_at,
                actor,
                issue_details,
            )

    for contributor in client.pages(f"{endpoint}/contributors"):
        author = contributor.get("login")
        if author:
            connection.execute(
                """
                INSERT INTO contributors (full_name, observed_on, login, contributions)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(full_name, observed_on, login)
                DO UPDATE SET contributions = excluded.contributions
                """,
                (
                    full_name,
                    observed_on,
                    author,
                    contributor.get("contributions", 0),
                ),
            )

    languages = client.get(f"{endpoint}/languages")
    if not isinstance(languages, dict):
        raise GitHubAPIError(f"Unexpected languages response for {repository}")
    connection.executemany(
        """
        INSERT INTO languages (full_name, observed_on, language, bytes)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(full_name, observed_on, language)
        DO UPDATE SET bytes = excluded.bytes
        """,
        [
            (full_name, observed_on, language, size)
            for language, size in languages.items()
        ],
    )
    save_sync_success(connection, full_name, since, dt.datetime.now(dt.timezone.utc))


def latest_contributors(
    connection: sqlite3.Connection, repository: str
) -> list[sqlite3.Row]:
    latest = connection.execute(
        "SELECT MAX(observed_on) FROM contributors WHERE full_name = ?",
        (repository,),
    ).fetchone()[0]
    if latest is None:
        return []
    return connection.execute(
        """
        SELECT login, contributions FROM contributors
        WHERE full_name = ? AND observed_on = ?
        ORDER BY contributions DESC, login COLLATE NOCASE
        LIMIT 20
        """,
        (repository, latest),
    ).fetchall()


def latest_languages(
    connection: sqlite3.Connection, repository: str
) -> list[sqlite3.Row]:
    latest = connection.execute(
        "SELECT MAX(observed_on) FROM languages WHERE full_name = ?",
        (repository,),
    ).fetchone()[0]
    if latest is None:
        return []
    return connection.execute(
        """
        SELECT language, bytes FROM languages
        WHERE full_name = ? AND observed_on = ?
        ORDER BY bytes DESC, language COLLATE NOCASE
        """,
        (repository, latest),
    ).fetchall()


def event_counts(
    connection: sqlite3.Connection,
    repository: str,
    since: dt.datetime,
) -> Counter[str]:
    rows = connection.execute(
        """
        SELECT event_type FROM events
        WHERE full_name = ? AND event_type != 'release' AND occurred_at >= ?
        """,
        (repository, timestamp_text(since)),
    )
    return Counter(row["event_type"] for row in rows)


def daily_event_series(
    connection: sqlite3.Connection,
    repository: str,
    since: dt.datetime,
    until: dt.datetime,
) -> dict[str, list[list[int]]]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    rows = connection.execute(
        """
        SELECT event_type, occurred_at FROM events
        WHERE full_name = ? AND occurred_at >= ?
        """,
        (repository, timestamp_text(since)),
    )
    for row in rows:
        occurred_at = parse_datetime(row["occurred_at"])
        if occurred_at is None or occurred_at > until:
            continue
        counts[row["event_type"]][occurred_at.date().isoformat()] += 1

    series: dict[str, list[list[int]]] = {}
    day = since.date()
    last_day = until.date()
    while day <= last_day:
        point_time = int(
            dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc).timestamp()
            * 1000
        )
        for event_type in (*EVENT_TYPES, "release"):
            series.setdefault(event_type, []).append(
                [point_time, counts[event_type].get(day.isoformat(), 0)]
            )
        day += dt.timedelta(days=1)
    return series


def cumulative_event_series(
    daily: dict[str, list[list[int]]],
    event_types: tuple[str, ...] = ACTIVITY_EVENT_TYPES,
) -> dict[str, list[list[int]]]:
    cumulative: dict[str, list[list[int]]] = {}
    for event_type in event_types:
        running_total = 0
        cumulative[event_type] = []
        for timestamp, daily_count in daily.get(event_type, []):
            running_total += daily_count
            cumulative[event_type].append([timestamp, running_total])
    return cumulative


def snapshot_series(
    connection: sqlite3.Connection, repository: str
) -> dict[str, list[list[int | float]]]:
    rows = connection.execute(
        """
        SELECT observed_on, metric, value FROM repository_snapshots
        WHERE full_name = ? ORDER BY observed_on
        """,
        (repository,),
    )
    series: dict[str, list[list[int | float]]] = defaultdict(list)
    for row in rows:
        day = dt.date.fromisoformat(row["observed_on"])
        timestamp = int(
            dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc).timestamp()
            * 1000
        )
        series[row["metric"]].append([timestamp, row["value"]])
    return dict(series)


def chart_block(
    chart_id: str,
    title: str,
    series: list[dict[str, Any]],
    chart_type: str = "line",
) -> str:
    payload = json.dumps(series, separators=(",", ":")).replace("<", "\\u003c")
    axes = (
        ""
        if chart_type == "pie"
        else "xAxis:{type:'datetime'},yAxis:{min:0,title:{text:null}},"
    )
    tooltip = (
        "tooltip:{pointFormat:'{series.name}: <b>{point.y:,} bytes</b> "
        "({point.percentage:.1f}%)'},"
        if chart_type == "pie"
        else ""
    )
    return (
        f'<section class="chart"><h2>{html.escape(title)}</h2>'
        f'<div id="{html.escape(chart_id, quote=True)}" class="chart-area"></div></section>'
        f"<script>Highcharts.chart({json.dumps(chart_id)},{{"
        f"chart:{{type:{json.dumps(chart_type)}}},title:{{text:null}},"
        f"{axes}{tooltip}legend:{{enabled:true}},credits:{{enabled:false}},"
        f"series:{payload}}});</script>"
    )


class ActivityStatsDashboard:
    def __init__(
        self, connection: sqlite3.Connection, output_dir: Path, days: int
    ) -> None:
        self.connection = connection
        self.output_dir = output_dir
        self.days = days
        self.now = dt.datetime.now(dt.timezone.utc)
        self.since = self.now - dt.timedelta(days=days)

    def _template(self, template_name: str, **values: str) -> str:
        template_path = PROJECT_DIR / "templates" / template_name
        return Template(template_path.read_text(encoding="utf-8")).substitute(values)

    def _repositories(self) -> list[Repository]:
        records = self.connection.execute(
            "SELECT * FROM repositories ORDER BY full_name COLLATE NOCASE"
        )
        return [Repository(record, self.connection) for record in records]

    def _navigation(self, repositories: list[Repository]) -> str:
        links = [
            f'<li><a href="{html.escape(repository.slug)}.html">'
            f"{html.escape(repository.full_name)}</a></li>"
            for repository in repositories
        ]
        return '<li><a href="index.html">Overview</a></li>' + "".join(links)

    def _render_index(self, repositories: list[Repository], navigation: str) -> None:
        cards: list[str] = []
        daily_series: list[dict[str, Any]] = []
        cumulative_series: list[dict[str, Any]] = []
        for repository in repositories:
            latest = snapshot_series(self.connection, repository.full_name)
            activity = repository.activity(self.since, self.now)
            cumulative = cumulative_event_series(activity.daily)

            def latest_value(metric: str) -> int:
                values = latest.get(metric, [])
                return int(values[-1][1]) if values else 0

            cards.append(
                f'<article class="repo-card"><h2><a href="{html.escape(repository.slug)}.html">'
                f"{html.escape(repository.full_name)}</a></h2>"
                f'<p>{html.escape(repository.record["description"] or "No description provided.")}</p>'
                '<div class="stats">'
                f"<span><b>{latest_value('stars'):,}</b> stars</span>"
                f"<span><b>{latest_value('forks'):,}</b> forks</span>"
                f"<span><b>{latest_value('watchers'):,}</b> watchers</span>"
                f"<span><b>{latest_value('open_issues_and_prs'):,}</b> open issues &amp; PRs</span>"
                f"<span><b>{activity.total():,}</b> events in the last {self.days} days</span>"
                "</div></article>"
            )
            daily_series.extend(
                {
                    "name": f"{repository.full_name} — {EVENT_LABELS[event_type]}",
                    "data": activity.daily[event_type],
                }
                for event_type in ACTIVITY_EVENT_TYPES
            )
            cumulative_series.extend(
                {
                    "name": f"{repository.full_name} — {EVENT_LABELS[event_type]}",
                    "data": cumulative[event_type],
                }
                for event_type in ACTIVITY_EVENT_TYPES
            )

        empty_state = (
            '<p class="empty-state">No repository data has been collected yet. '
            "The dashboard is ready; try collecting again after the GitHub API "
            "rate limit resets or configure GITHUB_TOKEN.</p>"
            if not repositories
            else ""
        )
        page = self._template(
            "index_template.html",
            nav_bars=navigation,
            repo_cards="".join(cards),
            empty_state=empty_state,
            daily_chart=chart_block(
                "daily-comparison-chart",
                f"Daily repository activity (last {self.days} days)",
                daily_series,
            ),
            cumulative_chart=chart_block(
                "cumulative-comparison-chart",
                f"Cumulative repository activity (last {self.days} days)",
                cumulative_series,
            ),
            days=str(self.days),
            generated_on=self.now.date().isoformat(),
            root="static",
            charts=CHARTS_URL,
        )
        (self.output_dir / "index.html").write_text(page, encoding="utf-8")

    def _render_repository(
        self,
        repository: Repository,
        navigation: str,
    ) -> None:
        record = repository.record
        activity = repository.activity(self.since, self.now)
        cumulative = cumulative_event_series(activity.daily)
        latest = snapshot_series(self.connection, repository.full_name)
        charts = [
            chart_block(
                "activity-chart",
                f"Activity over the last {self.days} days",
                [
                    {"name": EVENT_LABELS[event_type], "data": activity.daily[event_type]}
                    for event_type in ACTIVITY_EVENT_TYPES
                ],
            ),
            chart_block(
                "cumulative-activity-chart",
                f"Cumulative activity over the last {self.days} days",
                [
                    {"name": EVENT_LABELS[event_type], "data": cumulative[event_type]}
                    for event_type in ACTIVITY_EVENT_TYPES
                ],
            ),
            chart_block(
                "growth-chart",
                "Repository totals observed over time",
                [
                    {"name": label, "data": latest.get(metric, [])}
                    for metric, label in (
                        ("stars", "Stars"),
                        ("watchers", "Watchers"),
                        ("forks", "Forks"),
                        ("open_issues_and_prs", "Open issues & PRs"),
                    )
                    if latest.get(metric)
                ],
            ),
        ]
        languages = latest_languages(self.connection, repository.full_name)
        if languages:
            charts.append(
                chart_block(
                    "languages-chart",
                    "Programming languages (bytes)",
                    [
                        {
                            "name": "Bytes",
                            "colorByPoint": True,
                            "data": [
                                {"name": row["language"], "y": row["bytes"]}
                                for row in languages
                            ],
                        }
                    ],
                    "pie",
                )
            )

        contributors = latest_contributors(self.connection, repository.full_name)
        contributor_rows = "".join(
            f"<tr><td>{index}</td><td>{html.escape(row['login'])}</td>"
            f"<td>{row['contributions']:,}</td></tr>"
            for index, row in enumerate(contributors, 1)
        ) or '<tr><td colspan="3">No contributor data collected yet.</td></tr>'

        recent_events = self.connection.execute(
            """
            SELECT event_type, occurred_at, actor, details FROM events
            WHERE full_name = ? AND event_type != 'release'
            ORDER BY occurred_at DESC LIMIT 25
            """,
            (repository.full_name,),
        )
        event_rows: list[str] = []
        for row in recent_events:
            details = json.loads(row["details"])
            description = (
                details.get("title")
                or details.get("tag_name")
                or details.get("message")
                or ""
            )
            event_rows.append(
                f"<tr><td>{html.escape(row['occurred_at'][:10])}</td>"
                f"<td>{html.escape(EVENT_LABELS.get(row['event_type'], row['event_type']))}</td>"
                f"<td>{html.escape(row['actor'] or '—')}</td>"
                f"<td>{html.escape(description)}</td></tr>"
            )
        event_table = "".join(event_rows) or (
            '<tr><td colspan="4">No activity collected yet.</td></tr>'
        )

        release_rows: list[str] = []
        visible_releases = [
            release
            for release in repository.releases
            if release.published_at >= self.since
        ]
        for index, release in enumerate(repository.releases):
            if release not in visible_releases:
                continue
            previous_release = repository.releases[index - 1] if index > 0 else None
            start = previous_release.published_at if previous_release else self.since
            interval_events = activity.events_between(start, release.published_at)
            interval_counts = Counter(
                row["event_type"] for row in interval_events
            )
            contributor_count = len(
                {row["actor"] for row in interval_events if row["actor"]}
            )
            release_file = (
                f"{repository.slug}_release_{release.slug}.html"
            )
            release_rows.append(
                f'<tr><td><a href="{html.escape(release_file)}">'
                f"{html.escape(release.name)}</a></td>"
                f"<td>{html.escape(release.tag_name)}</td>"
                f"<td>{release.published_at.date().isoformat()}</td>"
                f"<td>{interval_counts['commit']:,}</td>"
                f"<td>{interval_counts['fork']:,}</td>"
                f"<td>{interval_counts['issue_opened']:,} / "
                f"{interval_counts['issue_closed']:,}</td>"
                f"<td>{interval_counts['pull_request_opened']:,} / "
                f"{interval_counts['pull_request_closed']:,}</td>"
                f"<td>{contributor_count:,}</td></tr>"
            )
            self._render_release(
                repository,
                release,
                start,
                interval_counts,
                interval_events,
                navigation,
                release_file,
                repository.releases.index(release),
            )
        releases_table = "".join(release_rows) or (
            '<tr><td colspan="4">No releases found in this collection period.</td></tr>'
        )
        page = self._template(
            "repository_template.html",
            nav_bars=navigation,
            name=html.escape(repository.full_name),
            repository_url=html.escape(record["html_url"], quote=True),
            description=html.escape(record["description"] or "No description provided."),
            created_at=html.escape((record["created_at"] or "unknown")[:10]),
            default_branch=html.escape(record["default_branch"] or "unknown"),
            language=html.escape(record["language"] or "not detected"),
            updated_on=html.escape(record["fetched_at"][:10]),
            activity_charts="".join(charts),
            releases_table=releases_table,
            contributor_rows=contributor_rows,
            event_rows=event_table,
            days=str(self.days),
            root="static",
            charts=CHARTS_URL,
        )
        (self.output_dir / f"{repository.slug}.html").write_text(
            page, encoding="utf-8"
        )

    def _render_release(
        self,
        repository: Repository,
        release: RepositoryRelease,
        start: dt.datetime,
        activity_counts: Counter[str],
        events: list[sqlite3.Row],
        navigation: str,
        output_name: str,
        release_index: int,
    ) -> None:
        previous = repository.releases[release_index - 1] if release_index else None
        activity_rows = "".join(
            f"<tr><td>{html.escape(row['occurred_at'][:10])}</td>"
            f"<td>{html.escape(EVENT_LABELS[row['event_type']])}</td>"
            f"<td>{html.escape(row['actor'] or 'Unknown')}</td>"
            f"<td>{html.escape(json.loads(row['details']).get('message') or json.loads(row['details']).get('title') or json.loads(row['details']).get('full_name') or '')}</td></tr>"
            for row in events
        ) or '<tr><td colspan="4">No activity in this release interval.</td></tr>'
        metric_rows = "".join(
            f"<tr><th>{html.escape(EVENT_LABELS[event_type])}</th>"
            f"<td>{activity_counts[event_type]:,}</td></tr>"
            for event_type in ACTIVITY_EVENT_TYPES
        )
        contributor_count = len(
            {row["actor"] for row in events if row["actor"]}
        )
        metric_rows += (
            f"<tr><th>Contributors with recorded activity</th>"
            f"<td>{contributor_count:,}</td></tr>"
        )
        commit_events = [
            row for row in events if row["event_type"] == "commit"
        ]
        chart = chart_block(
            "release-commits",
            "Cumulative commits in this release interval",
            [
                {
                    "name": "Commits",
                    "data": [
                        [
                            int(parse_datetime(row["occurred_at"]).timestamp() * 1000),
                            count,
                        ]
                        for count, row in enumerate(commit_events, 1)
                    ],
                }
            ],
        )
        page = self._template(
            "release_template.html",
            nav_bars=navigation,
            name=html.escape(release.name),
            repository=html.escape(repository.full_name),
            repository_page=f"{repository.slug}.html",
            repository_url=html.escape(repository.record["html_url"], quote=True),
            tag_name=html.escape(release.tag_name),
            release_url=html.escape(release.url, quote=True),
            published_at=release.published_at.date().isoformat(),
            author=html.escape(release.author),
            prerelease="Pre-release" if release.prerelease else "Stable release",
            previous_release=(
                html.escape(previous.tag_name) if previous else "collection start"
            ),
            window_start=max(start, self.since).date().isoformat(),
            commit_count=f"{activity_counts['commit']:,}",
            commit_chart=chart,
            activity_rows=activity_rows,
            metric_rows=metric_rows,
            root="static",
            charts=CHARTS_URL,
        )
        (self.output_dir / output_name).write_text(page, encoding="utf-8")

    def render(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        repositories = self._repositories()
        navigation = self._navigation(repositories)
        self._render_index(repositories, navigation)
        for repository in repositories:
            self._render_repository(repository, navigation)

        static_dir = self.output_dir / "static"
        static_dir.mkdir(parents=True, exist_ok=True)
        css = PROJECT_DIR / "static" / "activity_stats.css"
        (static_dir / "activity_stats.css").write_text(
            css.read_text(encoding="utf-8"), encoding="utf-8"
        )
        charts = PROJECT_DIR / CHARTS_URL
        deployed_charts = static_dir / "highcharts" / "code" / "highcharts.js"
        deployed_charts.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(charts, deployed_charts)


def render_site(
    connection: sqlite3.Connection,
    output_dir: Path,
    days: int,
) -> None:
    ActivityStatsDashboard(connection, output_dir, days).render()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect GitHub repository activity and generate static HTML pages."
    )
    parser.add_argument(
        "--repo",
        action="append",
        dest="repositories",
        help="GitHub owner/repo to collect (repeatable); defaults to Jenkins, Woodpecker, and Tekton.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("activity_stats.db"),
        help="SQLite database path (default: activity_stats.db).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("docs"),
        help="Directory for generated HTML and CSS (default: docs).",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=400,
        help="Collect and graph activity from this many days (default: 400).",
    )
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="Generate pages from the existing database without calling GitHub.",
    )
    args = parser.parse_args(argv)
    if args.since_days < 1:
        parser.error("--since-days must be a positive integer")
    args.repositories = args.repositories or list(DEFAULT_REPOSITORIES)
    for repository in args.repositories:
        if repository.count("/") != 1 or any(part in ("", ".", "..") for part in repository.split("/")):
            parser.error(f"Invalid repository name: {repository!r}; use owner/repo")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        connection = connect_database(args.database)
        try:
            if not args.render_only:
                client = GitHubClient(os.environ.get("GITHUB_TOKEN"))
                for repository in args.repositories:
                    print(f"Collecting {repository}...")
                    sync_since = collection_start(
                        connection,
                        repository,
                        dt.datetime.now(dt.timezone.utc),
                        args.since_days,
                    )
                    print(
                        f"Fetching activity since "
                        f"{sync_since.astimezone(dt.timezone.utc).date().isoformat()}..."
                    )
                    try:
                        sync_repository(client, connection, repository, sync_since)
                    except GitHubAPIError as error:
                        connection.commit()
                        print(
                            f"Warning: collection stopped for {repository}: {error}",
                            file=sys.stderr,
                        )
                        if "rate limit" in str(error).lower():
                            break
                        continue
                    connection.commit()
            render_site(connection, args.output_dir, args.since_days)
        finally:
            connection.close()
    except (GitHubAPIError, OSError, sqlite3.Error, ValueError) as error:
        print(f"activity_stats: {error}", file=sys.stderr)
        return 1
    print(f"Dashboard written to {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
