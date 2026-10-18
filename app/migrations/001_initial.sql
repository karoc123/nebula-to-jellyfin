CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE channels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    directory TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    image_url TEXT,
    subscribed INTEGER NOT NULL DEFAULT 0 CHECK(subscribed IN (0, 1)),
    initialized INTEGER NOT NULL DEFAULT 0 CHECK(initialized IN (0, 1)),
    last_checked_at TEXT,
    last_error TEXT
);
CREATE TABLE videos (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id),
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    duration REAL,
    thumb_url TEXT,
    metadata_ready INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'available'
        CHECK(status IN ('available', 'queued', 'downloading', 'publishing', 'done', 'error')),
    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
    error TEXT,
    path_stem TEXT UNIQUE,
    target_path TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX videos_queue ON videos(status, updated_at);
CREATE INDEX videos_channel ON videos(channel_id, published_at);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('video', 'channel', 'check', 'cookie_test')),
    payload TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued', 'running', 'done', 'error')),
    error TEXT,
    result TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX jobs_active ON jobs(dedupe_key) WHERE status IN ('queued', 'running');
