import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class DB:
    """
    Thin wrapper around sqlite3 for moderation.db writes.

    Matches the 3-table collector schema:
        posts                — one row per unique post (ap_id is the identity)
        moderation_observed  — platform decisions; 0 rows = unmoderated
        media                — media URLs only (no bytes), deduped by sha256

    There is no authors / policy_simulation / collection_meta table in this
    schema — those are added later when the LLM runner needs them.
    """

    def __init__(self, db_path: str = "moderation.db"):
        self.path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._ensure_schema()
        logger.info("Connected to %s", db_path)

    def _ensure_schema(self) -> None:
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

    # ------------------------------------------------------------------
    # posts
    # ------------------------------------------------------------------

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
        """Insert post if ap_id is new. Returns post_id, or None if duplicate."""
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

        if cur.lastrowid and cur.rowcount > 0:
            return cur.lastrowid

        # rowcount == 0 means it already existed (duplicate)
        return None

    def get_post_id(self, ap_id: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT post_id FROM posts WHERE ap_id = ?", (ap_id,)
        ).fetchone()
        return row["post_id"] if row else None

    def post_exists(self, ap_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM posts WHERE ap_id = ? LIMIT 1", (ap_id,)
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # moderation_observed
    # ------------------------------------------------------------------

    def insert_mod_event(
        self,
        post_id: int,
        action_type: str,
        observed_at: Optional[str] = None,
        target_type: str = "post",
        actor_instance: Optional[str] = None,
    ) -> int:
        """Append a platform moderation event for a post."""
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

    # ------------------------------------------------------------------
    # media
    # ------------------------------------------------------------------

    def insert_media(
        self,
        post_id: int,
        sha256: str,
        source_url: Optional[str] = None,
        mime: Optional[str] = None,
    ) -> Optional[int]:
        """Insert a media row. Silently skips duplicates (same post + sha256)."""
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

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self.conn.close()
        logger.info("DB connection closed.")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
