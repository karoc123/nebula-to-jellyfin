CREATE TABLE update_cache (
    component TEXT PRIMARY KEY CHECK(component IN ('application', 'yt-dlp')),
    installed TEXT NOT NULL,
    latest TEXT,
    status TEXT NOT NULL DEFAULT 'unknown' CHECK(status IN ('unknown', 'current', 'available')),
    attempted_at TEXT NOT NULL,
    succeeded_at TEXT,
    error TEXT
);
