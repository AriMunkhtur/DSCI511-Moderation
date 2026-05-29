-- ===================================================================
-- moderation.db  schema
-- ===================================================================
-- Design decisions:
--   * policy_simulation is WIDE (2 policies, 1 model, no extension)
--   * post identity = global ap_id, UNIQUE, dedup at write time
--   * raw LLM response stored as text (for debugging garbage output)
--   * provenance kept minimal: see collection_meta + README
--
-- Run once:   sqlite3 moderation.db < schema.sql
-- Verify:     sqlite3 moderation.db ".tables"
-- ===================================================================

PRAGMA foreign_keys = ON;   -- must be set every connection (see README)

-- -------------------------------------------------------------------
-- authors : one row per unique author
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS authors (
    author_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    platform         TEXT NOT NULL,              -- 'bluesky' | 'lemmy'
    author_handle    TEXT,                       -- @name / u/name
    author_global_id TEXT NOT NULL UNIQUE,       -- DID (bsky) / actor URL (lemmy)
    created_at       TEXT                        -- ISO8601, account creation if known
);

-- -------------------------------------------------------------------
-- posts : one row per UNIQUE post (dedup key = ap_id)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS posts (
    post_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ap_id               TEXT NOT NULL UNIQUE,    -- GLOBAL id. dedup happens here.
    platform            TEXT NOT NULL,           -- 'bluesky' | 'lemmy'
    author_id           INTEGER,                 -- FK -> authors
    origin_instance     TEXT,                    -- where post was ORIGINALLY made
    collecting_instance TEXT,                    -- which instance WE fetched it from
    community           TEXT,                    -- subreddit-equivalent (lemmy) / feed
    title               TEXT,                    -- lemmy has titles; bsky usually null
    body                TEXT,                    -- post text (strip markdown before LLM)
    lang                TEXT,                    -- 'en' only per locked decision
    has_media           INTEGER DEFAULT 0,       -- 0/1 boolean
    score               INTEGER,                 -- upvotes/likes if available
    created_at          TEXT,                    -- when the post was authored (ISO8601)
    collected_at        TEXT NOT NULL,           -- when WE pulled it (ISO8601)
    FOREIGN KEY (author_id) REFERENCES authors(author_id)
);

-- -------------------------------------------------------------------
-- moderation_observed : what the PLATFORM actually did to the post
-- one post can have multiple events over time -> separate table
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS moderation_observed (
    mod_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id       INTEGER NOT NULL,              -- FK -> posts
    action_type   TEXT NOT NULL,                 -- see allowed values in README
    target_type   TEXT,                          -- 'post' | 'comment' | 'account'
    self_deleted  INTEGER DEFAULT 0,             -- 1 = user removed it (NOT a mod action)
    actor_instance TEXT,                          -- which instance issued the action
    observed_at   TEXT NOT NULL,                 -- when WE saw this state (ISO8601)
    FOREIGN KEY (post_id) REFERENCES posts(post_id)
);

-- -------------------------------------------------------------------
-- media : images attached to posts. sha256 = dedup identical images.
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media (
    media_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL,                -- FK -> posts
    sha256      TEXT NOT NULL,                   -- hash of the image bytes
    local_path  TEXT,                            -- where the file is saved on disk
    source_url  TEXT,                            -- original media URL
    mime        TEXT,                            -- 'image/jpeg' etc
    FOREIGN KEY (post_id) REFERENCES posts(post_id),
    UNIQUE (post_id, sha256)                     -- same image once per post
);

-- -------------------------------------------------------------------
-- policy_simulation : the LLM verdicts. WIDE: meta + x in one row.
-- one row per post (per model). class scope = one model, so 1 row/post.
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS policy_simulation (
    sim_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id           INTEGER NOT NULL,          -- FK -> posts
    model             TEXT NOT NULL,             -- 'qwen2.5vl:7b'
    category_tested   TEXT NOT NULL,             -- 'violence'|'hate_speech'|'spam'

    meta_action       TEXT,                      -- REMOVE|KEEP|LABEL|REFUSED
    meta_category     TEXT,                      -- model's category call under Meta
    meta_confidence   TEXT,                      -- high|medium|low

    x_action          TEXT,
    x_category        TEXT,
    x_confidence      TEXT,

    meta_raw_response TEXT,                       -- raw model output, for debugging
    x_raw_response    TEXT,
    prompt_truncated  INTEGER DEFAULT 0,          -- the flag from build_prompt()
    created_at        TEXT NOT NULL,
    FOREIGN KEY (post_id) REFERENCES posts(post_id),
    UNIQUE (post_id, model, category_tested)      -- no accidental re-runs
);

-- -------------------------------------------------------------------
-- collection_meta : minimal provenance. one row per collection run.
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collection_meta (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_started_at    TEXT NOT NULL,
    run_ended_at      TEXT,
    platform          TEXT,
    instances         TEXT,                       -- comma list of lemmy instances
    window_start      TEXT,                        -- collection window covered
    window_end        TEXT,
    policy_versions   TEXT,                        -- e.g. 'meta=2026-05-16;x=2025'
    notes             TEXT
);

-- -------------------------------------------------------------------
-- indexes : speed up the queries you will actually run
-- -------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_posts_platform   ON posts(platform);
CREATE INDEX IF NOT EXISTS idx_posts_collected  ON posts(collected_at);
CREATE INDEX IF NOT EXISTS idx_mod_post         ON moderation_observed(post_id);
CREATE INDEX IF NOT EXISTS idx_sim_post         ON policy_simulation(post_id);
CREATE INDEX IF NOT EXISTS idx_sim_actions      ON policy_simulation(meta_action, x_action);
