"""SQLite write layer. posts, moderation_observed, media."""

import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


class DB:
    """thin wrapper around sqlite3 for moderation.db writes."""

    def __init__(self, db_path="moderation.db"):
        self.path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        # FKs are off by default in sqlite, needed for CASCADE deletes
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._ensure_schema()
        logger.info("Connected to %s", db_path)

    def _ensure_schema(self):
        """idempotent, safe to call on every connection."""
        self.conn.executescript("""
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS posts (
                post_id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ap_id                TEXT NOT NULL UNIQUE,
                platform             TEXT NOT NULL,
                origin_instance      TEXT,
                collecting_instance  TEXT,
                community            TEXT,
                title                TEXT,
                body                 TEXT,
                lang                 TEXT,
                has_media            INTEGER DEFAULT 0,
                score                INTEGER,
                created_at           TEXT,
                collected_at         TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_posts_platform  ON posts(platform);
            CREATE INDEX IF NOT EXISTS idx_posts_community ON posts(community);

            CREATE TABLE IF NOT EXISTS moderation_observed (
                mod_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id         INTEGER NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                action_type     TEXT NOT NULL,
                target_type     TEXT,
                actor_instance  TEXT,
                observed_at     TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_mod_post   ON moderation_observed(post_id);
            CREATE INDEX IF NOT EXISTS idx_mod_action ON moderation_observed(action_type);

            CREATE TABLE IF NOT EXISTS media (
                media_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id     INTEGER NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                sha256      TEXT NOT NULL,
                source_url  TEXT,
                mime        TEXT,
                UNIQUE (post_id, sha256)
            );

            CREATE INDEX IF NOT EXISTS idx_media_post ON media(post_id);
        """)
        self.conn.commit()

    # posts

    def upsert_post(
        self,
        ap_id,
        platform,
        collected_at=None,
        origin_instance=None,
        collecting_instance=None,
        community=None,
        title=None,
        body=None,
        lang="en",
        has_media=0,
        score=None,
        created_at=None,
    ):
        """insert a post, keyed by ap_id. returns post_id or None if it already existed."""
        collected_at = collected_at or _utcnow()

        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO posts
                (ap_id, platform, origin_instance, collecting_instance,
                 community, title, body, lang, has_media, score, created_at, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ap_id, platform, origin_instance, collecting_instance,
                community, title, body, lang, has_media, score, created_at, collected_at,
            ),
        )
        self.conn.commit()

        # real insert sets lastrowid + rowcount=1, dup leaves rowcount=0
        if cur.lastrowid and cur.rowcount > 0:
            return cur.lastrowid
        return None  # duplicate

    def get_post_id(self, ap_id):
        row = self.conn.execute(
            "SELECT post_id FROM posts WHERE ap_id = ?", (ap_id,)
        ).fetchone()
        return row["post_id"] if row else None

    def post_exists(self, ap_id):
        # cheaper than upsert_post when we just want to skip
        row = self.conn.execute(
            "SELECT 1 FROM posts WHERE ap_id = ? LIMIT 1", (ap_id,)
        ).fetchone()
        return row is not None

    # moderation_observed

    def insert_mod_event(
        self,
        post_id,
        action_type,
        observed_at=None,
        target_type="post",
        actor_instance=None,
    ):
        """log one platform mod decision. always inserts (a post can have multiple events)."""
        observed_at = observed_at or _utcnow()
        cur = self.conn.execute(
            """
            INSERT INTO moderation_observed
                (post_id, action_type, target_type, actor_instance, observed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, action_type, target_type, actor_instance, observed_at),
        )
        self.conn.commit()
        return cur.lastrowid

    # media

    def insert_media(self, post_id, sha256, source_url=None, mime=None):
        """deduped on (post_id, sha256). returns media_id or None if dup."""
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO media
                (post_id, sha256, source_url, mime)
            VALUES (?, ?, ?, ?)
            """,
            (post_id, sha256, source_url, mime),
        )
        self.conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else None

    # lifecycle

    def close(self):
        self.conn.close()
        logger.info("DB connection closed.")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
