import argparse
import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from db_writer import DB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

BSKY_HOST = "https://bsky.social"
XRPC = f"{BSKY_HOST}/xrpc"
RATE_LIMIT_SECS = 0.5

TOTAL_TARGET = 3000
CATEGORIES = ["violence", "hate_speech", "spam"]
PER_CATEGORY_TARGET = TOTAL_TARGET // len(CATEGORIES)

# Search queries per category — cycle through until bucket is full
CATEGORY_QUERIES: dict[str, list[str]] = {
    "violence": [
        "graphic violence", "violent threat", "violent content warning",
        "gore warning", "death threat", "self harm", "threatening",
        "violence", "#violentcontent", "#cw violence",
    ],
    "hate_speech": [
        "hate speech", "hate group", "racist harassment", "antisemitism",
        "islamophobia", "racial slur", "bigotry", "discrimination",
        "racism", "#hatespeech",
    ],
    "spam": [
        "spam account", "bot account", "phishing link", "scam message",
        "unsolicited promotion", "spam post", "spam bot", "buy followers",
        "spam", "#spam",
    ],
}

# Map Ozone label values to our categories
# Ref: https://docs.bsky.app/docs/advanced-guides/moderation
LABEL_TO_CATEGORY: dict[str, str] = {
    "violence":      "violence",
    "graphic-media": "violence",
    "self-harm":     "violence",
    "gore":          "violence",
    "hate":          "hate_speech",
    "intolerant":    "hate_speech",
    "spam":          "spam",
    "!hide":         "spam",
    "!warn":         "spam",
}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "moderation-research-collector/1.0 (DSCI-511)",
        "Accept": "application/json",
    })
    return s


def login(session: requests.Session, username: str, password: str) -> Optional[str]:
    url = f"{XRPC}/com.atproto.server.createSession"
    try:
        r = session.post(url, json={"identifier": username, "password": password})
        r.raise_for_status()
        data = r.json()
        token = data.get("accessJwt")
        if token:
            session.headers["Authorization"] = f"Bearer {token}"
            logger.info("Authenticated as %s (DID: %s)", username, data.get("did"))
        return token
    except requests.RequestException as e:
        logger.warning("Bluesky auth failed: %s — continuing as public", e)
        return None


def fetch_search_posts(session: requests.Session, query: str,
                       limit: int = 100, cursor: Optional[str] = None) -> tuple[list, Optional[str]]:
    url = f"{XRPC}/app.bsky.feed.searchPosts"
    params = {"q": query, "limit": min(limit, 100)}
    if cursor:
        params["cursor"] = cursor

    try:
        r = session.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data.get("posts", []), data.get("cursor")
    except requests.RequestException as e:
        logger.error("Search fetch error: %s", e)
        return [], None


def check_post_status(session: requests.Session, at_uri: str) -> str:
    """Re-fetch a post to detect removal. Returns: visible | not_found | blocked | error"""
    url = f"{XRPC}/app.bsky.feed.getPosts"
    try:
        r = session.get(url, params={"uris": [at_uri]}, timeout=10)
        if r.status_code == 400:
            return "not_found"
        r.raise_for_status()
        posts = r.json().get("posts", [])
        if not posts:
            return "not_found"
        record_type = posts[0].get("$type", "")
        if "notFound" in record_type:
            return "not_found"
        if "blocked" in record_type:
            return "blocked"
        return "visible"
    except requests.RequestException as e:
        logger.debug("Status check failed for %s: %s", at_uri, e)
        return "error"


def build_at_uri(did: str, collection: str, rkey: str) -> str:
    return f"at://{did}/{collection}/{rkey}"


def url_sha256(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def extract_media(record: dict) -> list[dict]:
    """Pull media URLs out of a post's embed field."""
    media: list[dict] = []
    embed = record.get("embed")
    if not embed:
        return media

    embed_type = embed.get("$type", "")

    if "images" in embed_type:
        for img in embed.get("images", []):
            for key in ("fullsize", "thumb"):
                url = img.get(key)
                if url:
                    media.append({"source_url": url, "mime": _guess_mime(url)})
                    break

    elif "external" in embed_type:
        ext = embed.get("external", {})
        thumb = ext.get("thumb")
        uri = ext.get("uri")
        if thumb:
            media.append({"source_url": thumb, "mime": _guess_mime(thumb)})
        elif uri:
            media.append({"source_url": uri, "mime": _guess_mime(uri)})

    elif "video" in embed_type:
        thumb = embed.get("thumbnail")
        if thumb:
            media.append({"source_url": thumb, "mime": "image/jpeg"})

    elif "recordWithMedia" in embed_type:
        inner = embed.get("media", {})
        media.extend(extract_media({"embed": inner}))

    return media


def _guess_mime(url: str) -> Optional[str]:
    if not isinstance(url, str):
        return None
    url_lower = url.lower().split("?")[0]
    for ext, mime in [
        (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
        (".png", "image/png"),  (".gif", "image/gif"),
        (".webp", "image/webp"), (".mp4", "video/mp4"),
        (".webm", "video/webm"),
    ]:
        if url_lower.endswith(ext):
            return mime
    return None


def extract_platform_labels(post_view: dict) -> list[dict]:
    """Return non-negated Ozone labels from a postView."""
    raw_labels = post_view.get("labels", [])
    return [
        {"val": lbl.get("val", ""), "src": lbl.get("src", ""), "cts": lbl.get("cts")}
        for lbl in raw_labels
        if not lbl.get("neg", False) and lbl.get("val")
    ]


def parse_post_view(post_view: dict, collecting_instance: str = "bsky.social") -> Optional[dict]:
    """Flatten a Bluesky postView into DB-ready fields. Returns None if non-English."""
    # Unwrap feedViewPost if needed
    if "post" in post_view and isinstance(post_view.get("post"), dict):
        post_view = post_view["post"]

    uri = post_view.get("uri", "")
    if not uri or not uri.startswith("at://"):
        return None

    author = post_view.get("author", {})
    record = post_view.get("record", {})

    langs = record.get("langs", [])
    if langs and "en" not in langs:
        return None

    did = author.get("did", "")
    handle = author.get("handle", "")
    text = record.get("text", "")
    score = post_view.get("likeCount", 0)
    media_urls = extract_media(post_view)
    has_media = 1 if media_urls else 0
    platform_labels = extract_platform_labels(post_view)

    return {
        "author_global_id": did,
        "author_handle": handle,
        "author_created_at": author.get("createdAt"),

        "ap_id": uri,
        "platform": "bluesky",
        "origin_instance": "bsky.social",
        "collecting_instance": collecting_instance,
        "community": None,
        "title": None,
        "body": text,
        "lang": "en",
        "has_media": has_media,
        "score": score,
        "created_at": record.get("createdAt"),

        "_uri": uri,
        "_media_urls": media_urls,
        "_platform_labels": platform_labels,
    }


def collect_paginated(fetcher, target: int, cursor: Optional[str] = None) -> list[dict]:
    """Generic cursor paginator. fetcher(cursor) -> (items, next_cursor)"""
    all_items: list[dict] = []
    while len(all_items) < target:
        items, cursor = fetcher(cursor)
        if not items:
            break
        all_items.extend(items)
        logger.info("    Paginator: %d / %d (cursor: %s)",
                    len(all_items), target,
                    (cursor[:20] + "...") if cursor else "None")
        if not cursor:
            break
        time.sleep(RATE_LIMIT_SECS)
    return all_items


def write_post(
    db: DB,
    session: requests.Session,
    parsed: dict,
    stats: dict,
    check_tombstones: bool = False,
) -> Optional[int]:
    """Write a parsed post + its labels to the DB. Returns post_id or None if duplicate."""
    if db.post_exists(parsed["ap_id"]):
        stats["posts_duplicate"] += 1
        return None

    author_id = db.upsert_author(
        platform="bluesky",
        author_global_id=parsed["author_global_id"],
        author_handle=parsed["author_handle"],
        created_at=parsed.get("author_created_at"),
    )
    stats["authors_seen"] += 1

    post_id = db.upsert_post(
        ap_id=parsed["ap_id"],
        platform="bluesky",
        author_id=author_id,
        origin_instance=parsed["origin_instance"],
        collecting_instance=parsed["collecting_instance"],
        community=parsed["community"],
        title=parsed["title"],
        body=parsed["body"],
        lang="en",
        has_media=parsed["has_media"],
        score=parsed["score"],
        created_at=parsed["created_at"],
    )

    if post_id is None:
        stats["posts_duplicate"] += 1
        return None
    stats["posts_new"] += 1

    for m in parsed.get("_media_urls", []):
        url = m.get("source_url", "")
        if url:
            db.insert_media(
                post_id=post_id,
                sha256=url_sha256(url),
                source_url=url,
                mime=m.get("mime"),
            )

    for lbl in parsed.get("_platform_labels", []):
        val = lbl["val"]
        action_type = "removed" if val == "!hide" else "nsfw_labeled"
        db.insert_mod_event(
            post_id=post_id,
            action_type=action_type,
            target_type="post",
            self_deleted=0,
            actor_instance=lbl.get("src") or "bsky.social",
            observed_at=lbl.get("cts"),
        )
        stats["mod_events"] += 1

    if check_tombstones and not parsed.get("_platform_labels"):
        status = check_post_status(session, parsed["_uri"])
        time.sleep(0.2)
        if status == "not_found":
            db.insert_mod_event(
                post_id=post_id,
                action_type="deleted",
                target_type="post",
                self_deleted=0,
                actor_instance="bsky.social",
            )
            stats["mod_events"] += 1

    return post_id


def collect_bluesky(
    db: DB,
    session: requests.Session,
    per_category_target: int = PER_CATEGORY_TARGET,
    check_tombstones: bool = False,
) -> dict[str, int]:
    stats: dict[str, int] = {
        "authors_seen": 0,
        "posts_new": 0,
        "posts_duplicate": 0,
        "posts_skipped_lang": 0,
        "mod_events": 0,
    }
    category_counts: dict[str, int] = {c: 0 for c in CATEGORIES}

    for category in CATEGORIES:
        queries = CATEGORY_QUERIES[category]
        logger.info("=== Category: %s (target: %d) ===", category, per_category_target)

        for query in queries:
            if category_counts[category] >= per_category_target:
                logger.info("  [%s] Target reached — moving to next category", category)
                break

            remaining = per_category_target - category_counts[category]
            logger.info("  [%s] Query: %r — need %d more posts", category, query, remaining)

            fetcher = lambda cur, q=query: fetch_search_posts(session, q, cursor=cur)
            raw_items = collect_paginated(fetcher, target=remaining)

            for raw in raw_items:
                if category_counts[category] >= per_category_target:
                    break
                parsed = parse_post_view(raw)
                if parsed is None:
                    stats["posts_skipped_lang"] += 1
                    continue
                post_id = write_post(db, session, parsed, stats, check_tombstones)
                if post_id is not None:
                    category_counts[category] += 1

            logger.info("  [%s] After query %r: %d / %d",
                        category, query, category_counts[category], per_category_target)
            time.sleep(RATE_LIMIT_SECS)

        logger.info("[%s] Final count: %d posts", category, category_counts[category])

    logger.info("Category totals: %s", category_counts)
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Collect Bluesky posts into moderation.db (3000 posts, 3 categories)"
    )
    parser.add_argument("--per-category", type=int, default=PER_CATEGORY_TARGET,
                        help=f"Posts to collect per category (default: {PER_CATEGORY_TARGET})")
    parser.add_argument("--check-tombstones", action="store_true",
                        help="Re-fetch each post to detect post-collection deletions (slow)")
    parser.add_argument("--username", default=os.getenv("BSKY_USERNAME"),
                        help="Bluesky handle for auth (optional)")
    parser.add_argument("--password", default=os.getenv("BSKY_PASSWORD"),
                        help="Bluesky app password for auth (optional)")
    parser.add_argument("--db", default="moderation.db",
                        help="Path to SQLite database file")
    args = parser.parse_args()

    with DB(args.db) as db:
        session = make_session()

        if args.username and args.password:
            login(session, args.username, args.password)
        else:
            logger.info(
                "No credentials — collecting as public. "
                "Note: some Ozone labelers require auth to expose label values."
            )

        run_id = db.start_run(
            platform="bluesky",
            instances="bsky.social",
            window_start=datetime.now(timezone.utc).isoformat(),
            notes=(
                f"categories={CATEGORIES} "
                f"per_category={args.per_category} "
                f"check_tombstones={args.check_tombstones}"
            ),
        )

        try:
            stats = collect_bluesky(
                db=db,
                session=session,
                per_category_target=args.per_category,
                check_tombstones=args.check_tombstones,
            )
            logger.info("Bluesky collection done — %s", stats)
        except Exception as e:
            logger.error("Collection failed: %s", e)
            raise
        finally:
            db.end_run(run_id)


if __name__ == "__main__":
    main()
