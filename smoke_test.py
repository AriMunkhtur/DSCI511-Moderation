"""
smoke_test.py  -  run this FIRST, before writing any collector code.

Proves: schema builds, FK enforcement works, dedup works, a full
post->author->moderation->media insert chain works on your machine.

    python smoke_test.py

Expected last line:  ALL CHECKS PASSED
If anything fails, your environment or the schema path is wrong -
fix that before touching the real collector.
"""

import sqlite3, os, hashlib
from datetime import datetime, timezone

DB = "moderation_smoketest.db"
if os.path.exists(DB):
    os.remove(DB)

con = sqlite3.connect(DB)
con.execute("PRAGMA foreign_keys = ON")
con.executescript(open("schema.sql").read())

now = datetime.now(timezone.utc).isoformat()

# 1. author
con.execute(
    "INSERT INTO authors (platform, author_handle, author_global_id, created_at) VALUES (?,?,?,?)",
    ("lemmy", "u/testuser", "https://lemmy.world/u/testuser", now),
)
author_id = con.execute("SELECT author_id FROM authors").fetchone()[0]

# 2. post
con.execute(
    """INSERT INTO posts
       (ap_id, platform, author_id, origin_instance, collecting_instance,
        community, title, body, lang, has_media, collected_at, created_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
    ("https://lemmy.world/post/9001", "lemmy", author_id, "lemmy.world",
     "hexbear.net", "news", "Test title", "Test body text", "en", 1, now, now),
)
post_id = con.execute("SELECT post_id FROM posts").fetchone()[0]

# 3. same post seen again from a different instance -> must NOT duplicate
con.execute(
    "INSERT OR IGNORE INTO posts (ap_id, platform, collected_at) VALUES (?,?,?)",
    ("https://lemmy.world/post/9001", "lemmy", now),
)
dup = con.execute(
    "SELECT COUNT(*) FROM posts WHERE ap_id=?",
    ("https://lemmy.world/post/9001",),
).fetchone()[0]
assert dup == 1, f"DEDUP FAILED: {dup} rows"

# 4. moderation event
con.execute(
    """INSERT INTO moderation_observed
       (post_id, action_type, target_type, self_deleted, actor_instance, observed_at)
       VALUES (?,?,?,?,?,?)""",
    (post_id, "REMOVAL", "post", 0, "lemmy.world", now),
)

# 5. media
fake_bytes = b"not-a-real-image"
con.execute(
    "INSERT INTO media (post_id, sha256, local_path, source_url, mime) VALUES (?,?,?,?,?)",
    (post_id, hashlib.sha256(fake_bytes).hexdigest(),
     "media/test.jpg", "https://example.com/x.jpg", "image/jpeg"),
)

# 6. FK enforcement: inserting media for a non-existent post must FAIL
fk_enforced = False
try:
    con.execute(
        "INSERT INTO media (post_id, sha256) VALUES (?,?)",
        (999999, "deadbeef"),
    )
except sqlite3.IntegrityError:
    fk_enforced = True
assert fk_enforced, "FK NOT ENFORCED: did you set PRAGMA foreign_keys = ON?"

con.commit()

print("schema built       : OK")
print("author/post/mod/media insert chain : OK")
print("dedup on ap_id     : OK (3 inserts -> 1 row)")
print("FK enforcement     : OK")
print("ALL CHECKS PASSED")

con.close()
os.remove(DB)
