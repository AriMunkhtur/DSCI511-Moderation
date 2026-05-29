import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class DB:
    """Thin wrapper around sqlite3 for moderation.db writes."""

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

            CREATE TABLE IF NOT EXISTS authors (
                author_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                platform         TEXT NOT NULL,
                author_handle    TEXT,
                author_global_id TEXT NOT NULL UNIQUE,
                created_at       TEXT
            );

            CREATE TABLE IF NOT EXISTS posts (
                post_id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ap_id               TEXT NOT NULL UNIQUE,
                platform            TEXT NOT NULL,
                author_id           INTEGER,
                origin_instance     TEXT,
                collecting_instance TEXT,
                community           TEXT,
                title               TEXT,
                body                TEXT,
                lang                TEXT,
                has_media           INTEGER DEFAULT 0,
                score               INTEGER,
                created_at          TEXT,
                collected_at        TEXT NOT NULL,
                FOREIGN KEY (author_id) REFERENCES authors(author_id)
            );

            CREATE TABLE IF NOT EXISTS moderation_observed (
                mod_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id        INTEGER NOT NULL,
                action_type    TEXT NOT NULL,
                target_type    TEXT,
                self_deleted   INTEGER DEFAULT 0,
                actor_instance TEXT,
                observed_at    TEXT NOT NULL,
                FOREIGN KEY (post_id) REFERENCES posts(post_id)
            );

            CREATE TABLE IF NOT EXISTS media (
                media_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id     INTEGER NOT NULL,
                sha256      TEXT NOT NULL,
                local_path  TEXT,
                source_url  TEXT,
                mime        TEXT,
                FOREIGN KEY (post_id) REFERENCES posts(post_id),
                UNIQUE (post_id, sha256)
            );

            CREATE TABLE IF NOT EXISTS policy_simulation (
                sim_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id           INTEGER NOT NULL,
                model             TEXT NOT NULL,
                category_tested   TEXT NOT NULL,
                meta_action       TEXT,
                meta_category     TEXT,
                meta_confidence   TEXT,
                x_action          TEXT,
                x_category        TEXT,
                x_confidence      TEXT,
                meta_raw_response TEXT,
                x_raw_response    TEXT,
                prompt_truncated  INTEGER DEFAULT 0,
                created_at        TEXT NOT NULL,
                FOREIGN KEY (post_id) REFERENCES posts(post_id),
                UNIQUE (post_id, model, category_tested)
            );

            CREATE TABLE IF NOT EXISTS collection_meta (
                run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_started_at    TEXT NOT NULL,
                run_ended_at      TEXT,
                platform          TEXT,
                instances         TEXT,
                window_start      TEXT,
                window_end        TEXT,
                policy_versions   TEXT,
                notes             TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_posts_platform  ON posts(platform);
            CREATE INDEX IF NOT EXISTS idx_posts_collected ON posts(collected_at);
            CREATE INDEX IF NOT EXISTS idx_mod_post        ON moderation_observed(post_id);
            CREATE INDEX IF NOT EXISTS idx_sim_post        ON policy_simulation(post_id);
            CREATE INDEX IF NOT EXISTS idx_sim_actions     ON policy_simulation(meta_action, x_action);
        """)
        self.conn.commit()
        logger.debug("Schema verified / created.")

    def upsert_author(self, platform: str, author_global_id: str,
                      author_handle: Optional[str] = None,
                      created_at: Optional[str] = None) -> int:
        """Insert author if not present; return author_id either way."""
        self.conn.execute(
            """
            INSERT OR IGNORE INTO authors
                (platform, author_global_id, author_handle, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (platform, author_global_id, author_handle, created_at),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT author_id FROM authors WHERE author_global_id = ?",
            (author_global_id,),
        ).fetchone()
        return row["author_id"]

    def upsert_post(
        self,
        ap_id: str,
        platform: str,
        collected_at: Optional[str] = None,
        author_id: Optional[int] = None,
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
        """Insert post if ap_id is new. Returns post_id or None if duplicate."""
        collected_at = collected_at or _utcnow()

        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO posts
                (ap_id, platform, author_id, origin_instance, collecting_instance,
                 community, title, body, lang, has_media, score, created_at, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ap_id, platform, author_id, origin_instance, collecting_instance,
                community, title, body, lang, has_media, score, created_at, collected_at,
            ),
        )
        self.conn.commit()

        if cur.lastrowid and cur.lastrowid > 0:
            if cur.rowcount == 0:
                return None  # duplicate
            return cur.lastrowid

        row = self.conn.execute(
            "SELECT post_id FROM posts WHERE ap_id = ?", (ap_id,)
        ).fetchone()
        return row["post_id"] if row else None

    def get_post_id(self, ap_id: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT post_id FROM posts WHERE ap_id = ?", (ap_id,)
        ).fetchone()
        return row["post_id"] if row else None

    def insert_mod_event(
        self,
        post_id: int,
        action_type: str,
        observed_at: Optional[str] = None,
        target_type: str = "post",
        self_deleted: int = 0,
        actor_instance: Optional[str] = None,
    ) -> int:
        observed_at = observed_at or _utcnow()
        cur = self.conn.execute(
            """
            INSERT INTO moderation_observed
                (post_id, action_type, target_type, self_deleted,
                 actor_instance, observed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (post_id, action_type, target_type, self_deleted,
             actor_instance, observed_at),
        )
        self.conn.commit()
        return cur.lastrowid

    def insert_media(
        self,
        post_id: int,
        sha256: str,
        local_path: Optional[str] = None,
        source_url: Optional[str] = None,
        mime: Optional[str] = None,
    ) -> Optional[int]:
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO media
                (post_id, sha256, local_path, source_url, mime)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, sha256, local_path, source_url, mime),
        )
        self.conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else None

    def insert_simulation(
        self,
        post_id: int,
        model: str,
        category_tested: str,
        meta_action: Optional[str] = None,
        meta_category: Optional[str] = None,
        meta_confidence: Optional[str] = None,
        x_action: Optional[str] = None,
        x_category: Optional[str] = None,
        x_confidence: Optional[str] = None,
        meta_raw_response: Optional[str] = None,
        x_raw_response: Optional[str] = None,
        prompt_truncated: int = 0,
        created_at: Optional[str] = None,
    ) -> Optional[int]:
        """Insert LLM verdict. Skips if (post_id, model, category_tested) already exists."""
        created_at = created_at or _utcnow()
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO policy_simulation
                (post_id, model, category_tested,
                 meta_action, meta_category, meta_confidence,
                 x_action, x_category, x_confidence,
                 meta_raw_response, x_raw_response,
                 prompt_truncated, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_id, model, category_tested,
                meta_action, meta_category, meta_confidence,
                x_action, x_category, x_confidence,
                meta_raw_response, x_raw_response,
                prompt_truncated, created_at,
            ),
        )
        self.conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else None

    def start_run(
        self,
        platform: str,
        instances: Optional[str] = None,
        window_start: Optional[str] = None,
        window_end: Optional[str] = None,
        policy_versions: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> int:
        """Open a collection run. Returns run_id to pass to end_run()."""
        cur = self.conn.execute(
            """
            INSERT INTO collection_meta
                (run_started_at, platform, instances,
                 window_start, window_end, policy_versions, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (_utcnow(), platform, instances,
             window_start, window_end, policy_versions, notes),
        )
        self.conn.commit()
        return cur.lastrowid

    def end_run(self, run_id: int) -> None:
        self.conn.execute(
            "UPDATE collection_meta SET run_ended_at = ? WHERE run_id = ?",
            (_utcnow(), run_id),
        )
        self.conn.commit()

    def post_exists(self, ap_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM posts WHERE ap_id = ? LIMIT 1", (ap_id,)
        ).fetchone()
        return row is not None

    def close(self) -> None:
        self.conn.close()
        logger.info("DB connection closed.")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
