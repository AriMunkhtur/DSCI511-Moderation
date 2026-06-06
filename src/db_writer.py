"""SQLite write layer for database (posts, moderation_observed, media)."""

import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    """Current UTC time as an ISO8601 string."""
    return datetime.now(timezone.utc).isoformat()


class DB:
    """
    Thin wrapper around sqlite3 for moderation.db writes.
    """

    def __init__(self, db_path: str = "moderation.db"):
        """
        Open a connection to the SQLite file and prepare it for writing.

        Enables foreign keys (off by default in SQLite, and required for the
        CASCADE deletes), switches to WAL journal mode for better write
        concurrency, then ensures the schema exists.
        """
        self.path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._ensure_schema()
        logger.info("Connected to %s", db_path)

    def _ensure_schema(self) -> None:
        """
        Create the three tables and their indexes if they don't exist.

        Ensures idempotency, so it's safe to call on every connection.
        Foreign keys use ON DELETE CASCADE: deleting a post also removes
        its moderation_observed and media rows.
        """
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
        logger.debug("Schema verified / created.")

    # Posts table methods

    def upsert_post(
        self,
        ap_id: str,
        platform: str,
        collected_at: Optional[str] = None,
        origin_instance: Optional[str] = None,
        collecting_instance: Optional[str] = None,
        community: Optional[str] = None,
        title: Optional[str] = None,
        body: Optional[str] = None,
        lang: Optional[str] = "en",
        has_media: int = 0,
        score: Optional[int] = None,
        created_at: Optional[str] = None,
    ) -> Optional[int]:
        """
        Insert a post, keyed by its globally-unique ap_id.

        Uses INSERT OR IGNORE so a post already in the table is left untouched
        rather than overwritten. `collected_at` defaults to now (UTC) if omitted.

        Returns:
            The new post_id on insert, or None if the ap_id already existed
        """
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

        # A real insert sets lastrowid and rowcount=1; an ignored duplicate
        # leaves rowcount=0, so this distinguishes "inserted" from "already there".
        if cur.lastrowid and cur.rowcount > 0:
            return cur.lastrowid

        # rowcount == 0 means it already existed (duplicate)
        return None

    def get_post_id(self, ap_id: str) -> Optional[int]:
        """
        Look up the internal post_id for a post's ap_id.

        Used when a moderation event references a post that may already be
        stored (e.g. the Lemmy modlog pass linking back to a collected post).
        Returns None if the post isn't in the table.
        """
        row = self.conn.execute(
            "SELECT post_id FROM posts WHERE ap_id = ?", (ap_id,)
        ).fetchone()
        return row["post_id"] if row else None

    def post_exists(self, ap_id: str) -> bool:
        """
        Fast existence check for a post's ap_id.

        Cheaper than upsert_post when the caller only needs to know whether to
        skip a post — the collectors call this before doing any parsing work.
        """
        row = self.conn.execute(
            "SELECT 1 FROM posts WHERE ap_id = ? LIMIT 1", (ap_id,)
        ).fetchone()
        return row is not None

    # Moderation Observed table methods

    def insert_mod_event(
        self,
        post_id: int,
        action_type: str,
        observed_at: Optional[str] = None,
        target_type: str = "post",
        actor_instance: Optional[str] = None,
    ) -> int:
        """
        Record a platform moderation decision against a post.

        A post can have multiple events (e.g. labeled then removed), so this
        always inserts a new row rather than deduping.

        Returns the new mod_id.
        """
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

    # Media table methods

    def insert_media(
        self,
        post_id: int,
        sha256: str,
        source_url: Optional[str] = None,
        mime: Optional[str] = None,
    ) -> Optional[int]:
        """
        Record one media item attached to a post.

        Deduped on (post_id, sha256) via INSERT OR IGNORE, so the same image on
        the same post is only stored once. sha256 is currently the hash of the source URL.

        Returns the new media_id, or None if it was a duplicate.
        """
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

    # Database lifecyle methods

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self.conn.close()
        logger.info("DB connection closed.")

    def __enter__(self):
        """Context-manager entry: return self."""
        return self

    def __exit__(self, *_):
        """Context-manager exit: close the connection."""
        self.close()
