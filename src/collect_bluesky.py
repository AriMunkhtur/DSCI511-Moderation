"""Collect category-relevant posts and Ozone labels from Bluesky into database."""

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

# Initizaling the configuration

BSKY_HOST = "https://bsky.social"
XRPC = f"{BSKY_HOST}/xrpc"
RATE_LIMIT_SECS = 0.5

TOTAL_TARGET = 3000
CATEGORIES = ["violence", "hate_speech", "spam"]
PER_CATEGORY_TARGET = TOTAL_TARGET // len(CATEGORIES)

# Search queries per category - cycle through until bucket is full
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
    """
    Create a requests.Session preconfigured for this collector.

    Sets a descriptive User-Agent and a JSON Accept header that are reused
    across every request, including the auth token once login succeeds.
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": "moderation-research-collector/1.0 (DSCI-511)",
        "Accept": "application/json",
    })
    return s


def login(session: requests.Session, username: str, password: str) -> Optional[str]:
    """
    Create a Bluesky session and attach the access JWT to the session.

    Auth is optional for collection but lets some Ozone labelers expose label
    values that are hidden from anonymous callers. Failures are logged and
    no error thrown so collection proceeds publicly.
    Returns the token or None.
    """
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
        logger.warning("Bluesky auth failed: %s - continuing as public", e)
        return None


def fetch_search_posts(session: requests.Session, query: str,
                       limit: int = 100, cursor: Optional[str] = None) -> tuple[list, Optional[str]]:
    """
    Run a single page of a full-text post search.

    Returns a (posts, next_cursor) tuple; pass the cursor back in to page
    through results. On error returns ([], None) so the caller stops cleanly.
    Capped at 100 results per call (the API maximum).
    """
    url = f"{XRPC}/app.bsky.feed.searchPosts"
    params = {"q": query, "limit": min(limit, 100)}
    if cursor:   # continue from a previous page when a cursor is supplied
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
    """
    Re-fetch a post by its AT URI to see whether it's still live.

    Used by the optional tombstone check to spot posts that vanished after we
    collected them. Returns one of: 'visible', 'not_found' (deleted/removed),
    'blocked' (viewer-blocked), or 'error' (request failed).
    """
    url = f"{XRPC}/app.bsky.feed.getPosts"
    try:
        r = session.get(url, params={"uris": [at_uri]}, timeout=10)
        if r.status_code == 400:   # malformed/unknown URI: treat as gone
            return "not_found"
        r.raise_for_status()
        posts = r.json().get("posts", [])
        if not posts:   # API returned nothing for the URI
            return "not_found"
        record_type = posts[0].get("$type", "")
        if "notFound" in record_type:   # explicit tombstone marker
            return "not_found"
        if "blocked" in record_type:   # viewer block/mute, not a mod removal
            return "blocked"
        return "visible"   # post is still live
    except requests.RequestException as e:
        logger.debug("Status check failed for %s: %s", at_uri, e)
        return "error"


def build_at_uri(did: str, collection: str, rkey: str) -> str:
    """
    Assemble an AT URI from its parts: at://<did>/<collection>/<rkey>.
    """
    return f"at://{did}/{collection}/{rkey}"


def url_sha256(url: str) -> str:
    """
    Hash a media URL to use as the media table's dedup key.

    Image bytes aren't downloaded at collection time, so the URL hash stands in
    for a content hash - enough to avoid duplicate rows per post. Swap to a
    bytes hash later if media gets downloaded.
    """
    return hashlib.sha256(url.encode()).hexdigest()


def extract_media(record: dict) -> list[dict]:
    """
    Collect media URLs from a post view's embed, as [{source_url, mime}].

    Handles the embed shapes Bluesky uses: image sets (prefers fullsize over
    thumb), external link cards (thumbnail, else the linked URL), video
    (thumbnail), and recordWithMedia quote posts (recurses into the inner
    media).
    Returns an empty list when there's no embed.
    """
    media: list[dict] = []
    embed = record.get("embed")
    if not embed:   # nothing attached to this post
        return media

    embed_type = embed.get("$type", "")

    if "images" in embed_type:   # one or more inline images
        for img in embed.get("images", []):
            for key in ("fullsize", "thumb"):   # prefer fullsize, fall back to thumb
                url = img.get(key)
                if url:
                    media.append({"source_url": url, "mime": _guess_mime(url)})
                    break   # taking one URL for this image, move to the next

    elif "external" in embed_type:   # link card
        ext = embed.get("external", {})
        thumb = ext.get("thumb")
        uri = ext.get("uri")
        if thumb:   # use the card's preview image if present
            media.append({"source_url": thumb, "mime": _guess_mime(thumb)})
        elif uri:   # otherwise fall back to the linked URL itself
            media.append({"source_url": uri, "mime": _guess_mime(uri)})

    elif "video" in embed_type:   # store the video's poster frame
        thumb = embed.get("thumbnail")
        if thumb:
            media.append({"source_url": thumb, "mime": "image/jpeg"})

    elif "recordWithMedia" in embed_type:   # quote post that also has media
        inner = embed.get("media", {})
        media.extend(extract_media({"embed": inner}))   # recurse into the inner embed

    return media


def _guess_mime(url: str) -> Optional[str]:
    """
    Guess a MIME type from a URL's file extension.

    Guards against non-string input,strips any query string,
    then matches common image/video extensions.
    Returns None when unrecognised so the column stays null.
    """
    if not isinstance(url, str):
        return None
    url_lower = url.lower().split("?")[0]   # drop query string before matching
    for ext, mime in [
        (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
        (".png", "image/png"),  (".gif", "image/gif"),
        (".webp", "image/webp"), (".mp4", "video/mp4"),
        (".webm", "video/webm"),
    ]:
        if url_lower.endswith(ext):
            return mime     # first matching extension
    return None   # unknown extension


def extract_platform_labels(post_view: dict) -> list[dict]:
    """
    Extract the active Ozone moderation labels from a post view.

    Returns each label as {val, src, cts} - the label string, the labeler DID
    that applied it, and its timestamp. Negated (retracted) labels and empty
    values are filtered out, so only labels currently in force are returned.
    """
    raw_labels = post_view.get("labels", [])
    return [
        {"val": lbl.get("val", ""), "src": lbl.get("src", ""), "cts": lbl.get("cts")}
        for lbl in raw_labels
        # keep only labels that are still in force (not negated) and non-empty
        if not lbl.get("neg", False) and lbl.get("val")
    ]


def parse_post_view(post_view: dict, collecting_instance: str = "bsky.social") -> Optional[dict]:
    """
    Flatten a Bluesky post view into the fields the DB layer expects.

    Unwraps a feedViewPost wrapper if present, requires a valid at:// uri, and
    skips non-English posts. Bluesky has no titles or communities, so those are null and likeCount is used as the score.
    """
    # Search results are postViews, but feeds wrap them as feedViewPost
    if "post" in post_view and isinstance(post_view.get("post"), dict):
        post_view = post_view["post"]

    uri = post_view.get("uri", "")
    if not uri or not uri.startswith("at://"):   # need a valid AT URI as the id
        return None

    author = post_view.get("author", {})
    record = post_view.get("record", {})

    langs = record.get("langs", [])
    if langs and "en" not in langs:   # skip posts explicitly tagged as non-English
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
    """
    Drive a cursor-paginated fetcher until `target` items are gathered.

    `fetcher(cursor)` must return (items, next_cursor). Stops when the target
    count is reached, the fetcher returns no items, or there's no next cursor.
    Sleeps between pages to respect rate limits. Returns the accumulated items
    """
    all_items: list[dict] = []
    while len(all_items) < target:   # keep paging until we have enough
        items, cursor = fetcher(cursor)
        if not items:   # empty page = no more results
            break
        all_items.extend(items)
        logger.info("    Paginator: %d / %d (cursor: %s)",
                    len(all_items), target,
                    (cursor[:20] + "...") if cursor else "None")
        if not cursor:   # no cursor = last page reached
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
    """
    Persist one parsed post plus its media and moderation labels.

    Skips duplicates up front. After inserting the post it writes each media
    URL and turns every Ozone label into a moderation_observed row ('!hide' ->
    'removed', otherwise 'nsfw_labeled'). If check_tombstones is set and the
    post has no labels, it re-fetches the post and logs a 'deleted' event when
    it has vanished. Returns the new post_id, or None if it was a duplicate.
    Updates the shared `stats` counters in place.
    """
    if db.post_exists(parsed["ap_id"]):   # already stored, skip before any writes
        stats["posts_duplicate"] += 1
        return None

    post_id = db.upsert_post(
        ap_id=parsed["ap_id"],
        platform="bluesky",
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

    if post_id is None:   # lost a race to a concurrent insert: count as duplicate
        stats["posts_duplicate"] += 1
        return None
    stats["posts_new"] += 1

    for m in parsed.get("_media_urls", []):   # store each attached media item
        url = m.get("source_url", "")
        if url:   # skip entries with no usable URL
            db.insert_media(
                post_id=post_id,
                sha256=url_sha256(url),
                source_url=url,
                mime=m.get("mime"),
            )

    for lbl in parsed.get("_platform_labels", []):   # one mod event per Ozone label
        val = lbl["val"]
        # '!hide' is a hard takedown; any other label is a softer content flag
        action_type = "removed" if val == "!hide" else "nsfw_labeled"
        db.insert_mod_event(
            post_id=post_id,
            action_type=action_type,
            target_type="post",
            actor_instance=lbl.get("src") or "bsky.social",
            observed_at=lbl.get("cts"),
        )
        stats["mod_events"] += 1

    # Only bother re-fetching unlabeled posts when tombstone checking is on.
    if check_tombstones and not parsed.get("_platform_labels"):
        status = check_post_status(session, parsed["_uri"])
        time.sleep(0.2)
        if status == "not_found":   # post vanished after collection, log deletion
            db.insert_mod_event(
                post_id=post_id,
                action_type="deleted",
                target_type="post",
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
    """
    Collect posts for all three categories via keyword search.

    For each category it cycles through that category's search queries, paging
    each one until the category reaches per_category_target, then moves on.
    Every kept post is written with its media and labels via write_post.
    English-only posts are fetched.
    Returns a stats dict of counts for the run.
    """
    stats: dict[str, int] = {
        "posts_new": 0,
        "posts_duplicate": 0,
        "posts_skipped_lang": 0,
        "mod_events": 0,
    }
    category_counts: dict[str, int] = {c: 0 for c in CATEGORIES}

    for category in CATEGORIES:   # fill each category's quota in turn
        queries = CATEGORY_QUERIES[category]
        logger.info("=== Category: %s (target: %d) ===", category, per_category_target)

        for query in queries:   # try each search query until the bucket fills
            if category_counts[category] >= per_category_target:
                logger.info("  [%s] Target reached - moving to next category", category)
                break

            remaining = per_category_target - category_counts[category]
            logger.info("  [%s] Query: %r - need %d more posts", category, query, remaining)

            # q=query binds the current query into the lambda (avoids late-binding)
            fetcher = lambda cur, q=query: fetch_search_posts(session, q, cursor=cur)
            raw_items = collect_paginated(fetcher, target=remaining)

            for raw in raw_items:   # write each search hit
                if category_counts[category] >= per_category_target:
                    break   # bucket filled mid-page, stop early
                parsed = parse_post_view(raw)
                if parsed is None:   # non-English / invalid post
                    stats["posts_skipped_lang"] += 1
                    continue
                post_id = write_post(db, session, parsed, stats, check_tombstones)
                if post_id is not None:   # only count genuinely new posts
                    category_counts[category] += 1

            logger.info("  [%s] After query %r: %d / %d",
                        category, query, category_counts[category], per_category_target)
            time.sleep(RATE_LIMIT_SECS)

        logger.info("[%s] Final count: %d posts", category, category_counts[category])

    logger.info("Category totals: %s", category_counts)
    return stats


def main():
    """
    CLI entry point: parse args and run the Bluesky collection.

    Opens one shared DB connection, optionally logs in (credentials via flags
    or BSKY_USERNAME/BSKY_PASSWORD env vars), then runs collect_bluesky.
    The --check-tombstones flag enables the slower per-post deletion check.
    """
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

        if args.username and args.password:   # auth can reveal more Ozone labels
            login(session, args.username, args.password)
        else:
            logger.info(
                "No credentials - collecting as public. "
                "Note: some Ozone labelers require auth to expose label values."
            )

        try:
            stats = collect_bluesky(
                db=db,
                session=session,
                per_category_target=args.per_category,
                check_tombstones=args.check_tombstones,
            )
            logger.info("Bluesky collection done - %s", stats)
        except Exception as e:
            logger.error("Collection failed: %s", e)
            raise


if __name__ == "__main__":
    main()
