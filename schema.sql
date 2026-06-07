
-- Only for the 2 collectors. Three tables. LLM judgement will be included later.
-- LLM tables get added later when the runner needs them.
--  
-- posts : the content 
-- moderation_observed : platform decision , if no decision then 0
-- media  : URLs of attached media


PRAGMA foreign_keys = ON;  -- turn FK on soemthing about fk not working each connection. 

-- posts: one row per unique post. ap_id is the global identity.

CREATE TABLE posts (
    post_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ap_id                TEXT NOT NULL UNIQUE,   -- Lemmy ap_id or Bluesky @
    platform             TEXT NOT NULL,           -- lemmy or bluesky
    origin_instance      TEXT,                    -- where the post originated
    collecting_instance  TEXT,                    -- where we observed it for lemmy
    community            TEXT,                    -- Lemmy community (NULL on Bluesky)
    title                TEXT,
    body                 TEXT,
    lang                 TEXT,
    has_media            INTEGER DEFAULT 0,       -- 0/1 
    score                INTEGER,
    created_at           TEXT,                    -- when created on platform
    collected_at         TEXT NOT NULL            -- when we collected it
);

CREATE INDEX idx_posts_platform ON posts(platform);
CREATE INDEX idx_posts_community ON posts(community);


-- moderation_observed: 0 rows for a post = unmoderated.
CREATE TABLE moderation_observed (
    mod_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         INTEGER NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    action_type     TEXT NOT NULL,    -- 'removed', 'restored', 'nsfw_labeled', etc. mostly lemmy 
    target_type     TEXT,             -- 'post' default
    actor_instance  TEXT,             -- instance/labeler DID that acted mostly bluesky 
    observed_at     TEXT NOT NULL
);

CREATE INDEX idx_mod_post ON moderation_observed(post_id);
CREATE INDEX idx_mod_action ON moderation_observed(action_type);

-- media: URLs only (no bytes) // this one is for just recognizing same images based on their hash. 
CREATE TABLE media (
    media_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    sha256      TEXT NOT NULL,        -- hash of source_url (bytes not downloaded)
    source_url  TEXT,
    mime        TEXT,
    UNIQUE(post_id, sha256)
);
-- media : actually saving the image // will use it for later when LLM is involved.  when we start downloading image bytes, REPLACE the media table above
--CREATE TABLE media (
--    media_id    INTEGER PRIMARY KEY AUTOINCREMENT,
--    post_id     INTEGER NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
--    sha256      TEXT NOT NULL,        -- hash of the actual image bytes
--    source_url  TEXT,                 -- where we downloaded it from
--    local_path  TEXT,                 -- where it lives on disk now
--    mime        TEXT,                 -- 'image/jpeg', 'video/mp4', etc.
--    UNIQUE(post_id, sha256)
--);
CREATE INDEX idx_media_post ON media(post_id);
