CREATE TABLE IF NOT EXISTS repositories (
    full_name TEXT PRIMARY KEY,
    html_url TEXT NOT NULL,
    description TEXT,
    created_at TEXT,
    default_branch TEXT,
    language TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repository_snapshots (
    full_name TEXT NOT NULL REFERENCES repositories(full_name),
    observed_on TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (full_name, observed_on, metric)
);

CREATE TABLE IF NOT EXISTS repository_sync_state (
    full_name TEXT PRIMARY KEY REFERENCES repositories(full_name),
    initial_since TEXT NOT NULL,
    last_successful_sync TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    full_name TEXT NOT NULL REFERENCES repositories(full_name),
    event_type TEXT NOT NULL,
    event_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    actor TEXT,
    details TEXT NOT NULL,
    PRIMARY KEY (full_name, event_type, event_id)
);

CREATE INDEX IF NOT EXISTS events_by_repository_date
    ON events(full_name, occurred_at);

CREATE TABLE IF NOT EXISTS contributors (
    full_name TEXT NOT NULL REFERENCES repositories(full_name),
    observed_on TEXT NOT NULL,
    login TEXT NOT NULL,
    contributions INTEGER NOT NULL,
    PRIMARY KEY (full_name, observed_on, login)
);

CREATE TABLE IF NOT EXISTS languages (
    full_name TEXT NOT NULL REFERENCES repositories(full_name),
    observed_on TEXT NOT NULL,
    language TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    PRIMARY KEY (full_name, observed_on, language)
);
