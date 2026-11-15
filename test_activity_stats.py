import datetime as dt
import io
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import activity_stats


class FakeGitHubClient:
    def __init__(self) -> None:
        self.now = dt.datetime.now(dt.timezone.utc)
        def recent(days):
            return (self.now - dt.timedelta(days=days)).isoformat()
        self.repository = {
            "full_name": "owner/repo",
            "html_url": "https://github.com/owner/repo",
            "description": "<b>Project</b>",
            "created_at": "2020-01-01T00:00:00Z",
            "default_branch": "main",
            "language": "Python",
            "stargazers_count": 123,
            "subscribers_count": 7,
            "forks_count": 8,
            "open_issues_count": 9,
        }
        self.items = {
            "/repos/owner/repo/commits": [
                {
                    "sha": "abc123",
                    "commit": {
                        "author": {"date": recent(2), "name": "Commit Author"},
                        "message": "Add feature",
                    },
                    "author": {"login": "contributor"},
                }
            ],
            "/repos/owner/repo/forks": [
                {
                    "id": 10,
                    "created_at": recent(3),
                    "full_name": "forker/repo",
                    "owner": {"login": "forker"},
                }
            ],
            "/repos/owner/repo/releases": [
                {
                    "id": 11,
                    "created_at": recent(4),
                    "published_at": recent(1),
                    "tag_name": "v1.0",
                    "name": "First release",
                    "author": {"login": "contributor"},
                    "html_url": "https://github.com/owner/repo/releases/tag/v1.0",
                }
            ],
            "/repos/owner/repo/issues": [
                {
                    "id": 12,
                    "number": 2,
                    "title": "Issue <script>alert(1)</script>",
                    "html_url": "https://github.com/owner/repo/issues/2",
                    "created_at": recent(5),
                    "closed_at": recent(1),
                    "updated_at": recent(1),
                    "user": {"login": "contributor"},
                }
            ],
            "/repos/owner/repo/contributors": [
                {"login": "contributor", "contributions": 42}
            ],
        }

    def get(self, path):
        if path == "/repos/owner/repo":
            return self.repository
        if path == "/repos/owner/repo/languages":
            return {"Python": 5000, "HTML": 1000}
        raise AssertionError(f"Unexpected API path: {path}")

    def pages(self, path, _params=None, stop_before=None):
        del _params
        for item in self.items.get(path, []):
            if stop_before is not None and stop_before(item):
                return
            yield item


class FakeCursorClient(activity_stats.GitHubClient):
    def __init__(self) -> None:
        super().__init__()
        self.requests = []

    def _get(self, url, params=None):
        self.requests.append((url, params))
        if len(self.requests) == 1:
            return (
                [{"id": 1}],
                '<https://api.github.com/repos/owner/repo/issues?after=cursor&page=2>; rel="next"',
            )
        return [{"id": 2}], None


class RateLimitedGitHubClient:
    def get(self, path):
        if path == "/repos/jenkinsci/jenkins":
            return {
                "full_name": "jenkinsci/jenkins",
                "html_url": "https://github.com/jenkinsci/jenkins",
                "description": "Jenkins",
                "created_at": "2011-02-02T00:00:00Z",
                "default_branch": "master",
                "language": "Java",
            }
        raise AssertionError(f"Unexpected API path: {path}")

    def pages(self, path, params=None, stop_before=None):
        del params, stop_before
        if path.endswith("/commits"):
            yield {
                "sha": "saved-before-limit",
                "commit": {
                    "author": {
                        "date": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "name": "Contributor",
                    },
                    "message": "Saved commit",
                },
                "author": {"login": "contributor"},
            }
            raise activity_stats.GitHubAPIError(
                "GitHub API rate limit reached (test)"
            )
        return


class ActivityStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "activity.db"
        self.connection = activity_stats.connect_database(self.database_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_parse_datetime_normalizes_to_utc(self) -> None:
        parsed = activity_stats.parse_datetime("2026-01-02T03:04:05Z")
        self.assertEqual(
            parsed,
            dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc),
        )

    def test_default_output_directory_is_docs(self) -> None:
        args = activity_stats.parse_args(["--render-only"])
        self.assertEqual(args.output_dir, Path("docs"))

    def test_collection_start_backfills_then_uses_incremental_watermark(self) -> None:
        now = dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc)
        first_start = activity_stats.collection_start(
            self.connection, "owner/repo", now, 400
        )
        self.assertEqual(first_start, now - dt.timedelta(days=400))

        self.connection.execute(
            """
            INSERT INTO repositories (full_name, html_url, fetched_at)
            VALUES ('owner/repo', 'https://github.com/owner/repo', ?)
            """,
            (activity_stats.timestamp_text(now),),
        )
        activity_stats.save_sync_success(
            self.connection,
            "owner/repo",
            first_start,
            now - dt.timedelta(hours=24),
        )
        next_start = activity_stats.collection_start(
            self.connection, "owner/repo", now, 400
        )
        self.assertEqual(next_start, now - dt.timedelta(days=2))

        expired_start = activity_stats.collection_start(
            self.connection,
            "owner/repo",
            now,
            400,
            overlap_days=500,
        )
        self.assertEqual(expired_start, now - dt.timedelta(days=400))

    def test_cumulative_series_accumulates_daily_counts(self) -> None:
        daily = {
            "commit": [[1000, 2], [2000, 0], [3000, 3]],
            "release": [[1000, 1], [2000, 1], [3000, 1]],
        }
        cumulative = activity_stats.cumulative_event_series(daily, ("commit",))
        self.assertEqual(cumulative["commit"], [[1000, 2], [2000, 2], [3000, 5]])
        self.assertNotIn("release", cumulative)

    def test_release_interval_counts_activity_but_not_release_events(self) -> None:
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 1, 3, tzinfo=dt.timezone.utc)
        self.connection.execute(
            """
            INSERT INTO repositories (full_name, html_url, fetched_at)
            VALUES ('owner/repo', 'https://github.com/owner/repo', ?)
            """,
            (activity_stats.timestamp_text(end),),
        )
        for event_type, event_id, occurred_at in (
            ("commit", "sha1", "2026-01-02T00:00:00+00:00"),
            ("fork", "fork1", "2026-01-02T12:00:00+00:00"),
            ("issue_opened", "issue1", "2026-01-02T13:00:00+00:00"),
            ("release", "release1", "2026-01-02T14:00:00+00:00"),
        ):
            activity_stats.insert_event(
                self.connection,
                "owner/repo",
                event_type,
                event_id,
                occurred_at,
                "contributor",
                {"message": "test"},
            )
        repository_row = self.connection.execute(
            "SELECT * FROM repositories WHERE full_name = 'owner/repo'"
        ).fetchone()
        repository = activity_stats.Repository(repository_row, self.connection)
        interval = repository.activity(start, end)
        counts = interval.event_counts_between(start, end)
        self.assertEqual(counts["commit"], 1)
        self.assertEqual(counts["fork"], 1)
        self.assertEqual(counts["issue_opened"], 1)
        self.assertNotIn("release", counts)
        self.assertEqual(len(interval.events_between(start, end)), 3)

    def test_schema_is_versioned_and_existing_databases_are_upgraded(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        legacy = sqlite3.connect(legacy_path)
        legacy.execute(
            """
            CREATE TABLE repositories (
                full_name TEXT PRIMARY KEY,
                html_url TEXT NOT NULL,
                description TEXT,
                created_at TEXT,
                default_branch TEXT,
                language TEXT,
                fetched_at TEXT NOT NULL
            )
            """
        )
        legacy.execute(
            """
            INSERT INTO repositories (full_name, html_url, fetched_at)
            VALUES ('owner/repo', 'https://github.com/owner/repo', '2026-01-01T00:00:00+00:00')
            """
        )
        legacy.commit()
        legacy.close()

        upgraded = activity_stats.connect_database(legacy_path)
        self.assertEqual(upgraded.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(
            upgraded.execute("SELECT COUNT(*) FROM repositories").fetchone()[0], 1
        )
        upgraded.close()

        with patch.object(activity_stats, "PROJECT_DIR", Path(self.temp_dir.name)):
            reopened = activity_stats.connect_database(legacy_path)
            self.assertEqual(reopened.execute("PRAGMA user_version").fetchone()[0], 1)
            reopened.close()

    def test_pagination_follows_github_cursor_link(self) -> None:
        client = FakeCursorClient()
        results = list(client.pages("/repos/owner/repo/issues", {"state": "all"}))
        self.assertEqual([item["id"] for item in results], [1, 2])
        self.assertEqual(
            client.requests[1][0],
            "https://api.github.com/repos/owner/repo/issues?after=cursor&page=2",
        )
        self.assertEqual(client.requests[1][1], {})

    def test_invalid_token_surfaces_http_error(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/repo",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"message":"Bad credentials"}'),
        )
        with patch("activity_stats.urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(
                activity_stats.GitHubAPIError, "HTTP 401"
            ):
                activity_stats.GitHubClient("not-a-token").get("/repos/owner/repo")

    def test_sync_and_render_repository_activity(self) -> None:
        client = FakeGitHubClient()
        since = client.now - dt.timedelta(days=30)
        activity_stats.sync_repository(client, self.connection, "owner/repo", since)
        self.connection.commit()

        event_types = {
            row["event_type"]
            for row in self.connection.execute(
                "SELECT event_type FROM events WHERE full_name = 'owner/repo'"
            )
        }
        self.assertEqual(
            event_types,
            {"commit", "fork", "release", "issue_opened", "issue_closed"},
        )
        self.assertEqual(
            activity_stats.latest_contributors(self.connection, "owner/repo")[0]["login"],
            "contributor",
        )
        self.assertEqual(
            activity_stats.latest_languages(self.connection, "owner/repo")[0]["language"],
            "Python",
        )
        sync_state = self.connection.execute(
            "SELECT last_successful_sync FROM repository_sync_state "
            "WHERE full_name = 'owner/repo'"
        ).fetchone()
        self.assertIsNotNone(sync_state)

        activity_stats.sync_repository(client, self.connection, "owner/repo", since)
        self.connection.commit()
        event_count = self.connection.execute(
            "SELECT COUNT(*) FROM events WHERE full_name = 'owner/repo'"
        ).fetchone()[0]
        self.assertEqual(event_count, 5)

        output_dir = Path(self.temp_dir.name) / "site"
        activity_stats.render_site(self.connection, output_dir, 30)
        home_page = (output_dir / "index.html").read_text(encoding="utf-8")
        repo_page = (output_dir / "owner_repo.html").read_text(encoding="utf-8")
        self.assertIn("owner/repo", home_page)
        self.assertIn("cumulative-comparison-chart", home_page)
        self.assertIn("&lt;b&gt;Project&lt;/b&gt;", home_page)
        self.assertIn("cumulative-activity-chart", repo_page)
        self.assertIn("languages-chart", repo_page)
        self.assertIn('"name":"Python","y":5000', repo_page)
        self.assertIn('"name":"Bytes","colorByPoint":true', repo_page)
        self.assertIn("Issue &lt;script&gt;alert(1)&lt;/script&gt;", repo_page)
        self.assertNotIn("<script>alert(1)</script>", repo_page)
        release_page = (output_dir / "owner_repo_release_v1.0.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("Activity statistics for this release", release_page)
        self.assertIn("Commit count: 1.", release_page)
        self.assertIn("<th>Forks</th><td>1</td>", release_page)
        self.assertNotIn('"name":"Releases"', release_page)
        self.assertTrue((output_dir / "static" / "activity_stats.css").exists())
        self.assertTrue(
            (output_dir / "static" / "highcharts" / "code" / "highcharts.js").exists()
        )
        self.assertIn("$repo_cards", (Path(activity_stats.PROJECT_DIR) / "templates" / "index_template.html").read_text(encoding="utf-8"))

    def test_daily_series_fills_days_without_events(self) -> None:
        day_one = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        day_three = dt.datetime(2026, 1, 3, tzinfo=dt.timezone.utc)
        self.connection.execute(
            """
            INSERT INTO repositories
                (full_name, html_url, fetched_at)
            VALUES ('owner/repo', 'https://github.com/owner/repo', ?)
            """,
            (activity_stats.timestamp_text(day_one),),
        )
        activity_stats.insert_event(
            self.connection,
            "owner/repo",
            "commit",
            "sha",
            activity_stats.timestamp_text(day_one),
            "contributor",
            {"message": "test"},
        )
        series = activity_stats.daily_event_series(
            self.connection, "owner/repo", day_one, day_three
        )
        self.assertEqual([point[1] for point in series["commit"]], [1, 0, 0])

    def test_rate_limit_saves_partial_data_and_still_generates_site(self) -> None:
        output_dir = Path(self.temp_dir.name) / "partial-site"
        with patch.object(
            activity_stats,
            "GitHubClient",
            return_value=RateLimitedGitHubClient(),
        ):
            result = activity_stats.main(
                [
                    "--database",
                    str(self.database_path),
                    "--output-dir",
                    str(output_dir),
                ]
            )

        self.assertEqual(result, 0)
        self.assertTrue((output_dir / "index.html").exists())
        repository_page = (
            output_dir / "jenkinsci_jenkins.html"
        ).read_text(encoding="utf-8")
        self.assertIn("Saved commit", repository_page)
        saved = self.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_id = 'saved-before-limit'"
        ).fetchone()[0]
        self.assertEqual(saved, 1)
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM repository_sync_state "
                "WHERE full_name = 'jenkinsci/jenkins'"
            ).fetchone()
        )

    def test_empty_database_still_generates_dashboard(self) -> None:
        output_dir = Path(self.temp_dir.name) / "empty-site"
        activity_stats.render_site(self.connection, output_dir, 400)
        page = (output_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn("No repository data has been collected yet", page)


if __name__ == "__main__":
    unittest.main()
