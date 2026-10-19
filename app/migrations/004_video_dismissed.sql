-- Hides finished downloads from the download list without forgetting they were downloaded.
ALTER TABLE videos ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0 CHECK(dismissed IN (0, 1));
