"""Lemmy collector. Pulls posts + modlog removal events into the db."""

import argparse
import hashlib
import logging
import math
import os
import time
from tqdm import tqdm
from typing import Optional
from urllib.parse import urlparse

import requests

from db_writer import DB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# config
API = "/api/v3"
RATE_LIMIT_SECS = 0.5

# bump this to collect more
TOTAL_TARGET = 1000
CATEGORIES = ["violence", "hate_speech", "spam"]
PER_CATEGORY_TARGET = TOTAL_TARGET // len(CATEGORIES)

# case-insensitive substring match against modlog removal reasons
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "violence": [
        "violence", "violent", "threat", "threatening", "gore",
        "graphic", "harm", "assault", "weapon", "kill", "murder",
        "abuse", "self-harm", "self harm",
    ],
    "hate_speech": [
        "hate", "hate speech", "racist", "racism", "slur", "bigot",
        "discrimination", "antisemit", "homophob", "transphob",
        "sexist", "misogyn", "xenophob", "nazi", "extremis",
    ],
    "spam": [
        "spam", "bot", "advertisement", "advertising", "scam",
        "phishing", "unsolicited", "flood", "duplicate", "repost",
        "low effort", "low-effort", "off topic", "off-topic",
    ],
}


def classify_reason(reason):
    """maps a modlog reason string to one of the 3 categories. priority: violence > hate_speech > spam."""
    if not reason:
        return None
    lower = reason.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return category
    return None


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "moderation-research-collector/1.0 (DSCI-511)",
        "Accept": "application/json",
    })
    return s


def login(session, instance, username, password):
    """auth unlocks removal reasons that some instances hide from anon."""
    """ wasnt used because most of the instances require verification that takes a long time"""
    url = f"https://{instance}{API}/user/login"
    try:
        r = session.post(url, json={"username_or_email": username, "password": password})
        r.raise_for_status()
        token = r.json().get("jwt")
        if token:
            session.headers["Authorization"] = f"Bearer {token}"
            logger.info("[%s] Authenticated as %s", instance, username)
        return token
    except requests.RequestException as e:
        logger.warning("[%s] Auth failed: %s - continuing unauthenticated", instance, e)
        return None


def fetch_posts(session, instance, community_name=None, limit=50, pages=5):
    """recent posts, newest first. community feed if name given, else Local.
        This one wasnt also working, didnt get community stuff only got instances
    """
    all_posts = []
    url = f"https://{instance}{API}/post/list"

    for page in range(1, pages + 1):
        params = {
            "type_": "Local",
            "sort": "New",
            "limit": limit,
            "page": page,
        }
        if community_name:
            params["community_name"] = community_name
            params["type_"] = "All"  # All = include federated posts

        try:
            r = session.get(url, params=params, timeout=15)
            r.raise_for_status()
            posts = r.json().get("posts", [])
            if not posts:
                logger.info("[%s] No more posts at page %d", instance, page)
                break
            all_posts.extend(posts)
            logger.info("[%s] Page %d - fetched %d posts (total: %d)",
                        instance, page, len(posts), len(all_posts))
            time.sleep(RATE_LIMIT_SECS)
        except requests.RequestException as e:
            logger.error("[%s] Post fetch error on page %d: %s", instance, page, e)
            break

    return all_posts


def fetch_modlog_categorised(
    session,
    instance,
    community_name=None,
    per_category_target=PER_CATEGORY_TARGET,
    page_size=50,
):
    """walks the post-removal modlog, buckets entries by category via classify_reason."""
    url = f"https://{instance}{API}/modlog"
    buckets: dict[str, list] = {cat: [] for cat in CATEGORIES}
    buckets["unclassified"] = []

    page = 1
    while True:
        # stop when every bucket is full
        if all(len(buckets[c]) >= per_category_target for c in CATEGORIES):
            logger.info("[%s] All buckets full, stopping modlog fetch", instance)
            break

        params: dict = {
            "limit": page_size,
            "page": page,
            "type_": "ModRemovePost",
        }
        if community_name:
            params["community_name"] = community_name

        try:
            r = session.get(url, params=params, timeout=15)
            r.raise_for_status()
            entries = r.json().get("removed_posts", [])
        except requests.RequestException as e:
            logger.error("[%s] Modlog fetch error (page %d): %s", instance, page, e)
            break

        if not entries:
            logger.info("[%s] Modlog exhausted at page %d", instance, page)
            break

        for entry in entries:
            action = entry.get("mod_remove_post", {})
            reason = action.get("reason") or ""
            category = classify_reason(reason)
            bucket = category if category else "unclassified"

            # skip if this category is full, keep filling others
            if category and len(buckets[category]) >= per_category_target:
                continue

            post = entry.get("post", {})
            moderator = entry.get("moderator") or {}
            # prefer moderator's home instance, fall back to this one
            actor_instance = (
                extract_instance_from_ap_id(moderator.get("actor_id", ""))
                or instance
            )

            buckets[bucket].append({
                "ap_id": post.get("ap_id", ""),
                "_lemmy_post_id": post.get("id"),
                # removed=True normal case, False = restore
                "action_type": "removed" if action.get("removed", True) else "restored",
                "actor_instance": actor_instance,
                "observed_at": action.get("when_"),
                "target_type": "post",
                "reason": reason,
                "category": category,
            })

        totals = {c: len(buckets[c]) for c in CATEGORIES}
        logger.info("[%s] Modlog page %d - totals: %s", instance, page, totals)
        page += 1
        time.sleep(RATE_LIMIT_SECS)

    return buckets


def fetch_single_post(session, instance, lemmy_post_id):
    """fetch one post by numeric id. used to recover removed posts that don't show in feed sweeps."""
    url = f"https://{instance}{API}/post"
    try:
        r = session.get(url, params={"id": lemmy_post_id}, timeout=15)
        if r.status_code == 404:
            # post is gone, not just removed
            return None
        r.raise_for_status()
        return r.json().get("post_view")
    except requests.RequestException as e:
        logger.debug("[%s] Could not fetch post %d: %s", instance, lemmy_post_id, e)
        return None


def extract_instance_from_ap_id(ap_id):
    # 'https://lemmy.world/post/123' -> 'lemmy.world'
    try:
        return urlparse(ap_id).netloc
    except Exception:
        return ""


def url_sha256(url):
    # not downloading bytes, so we hash the URL itself for dedup
    return hashlib.sha256(url.encode()).hexdigest()


def parse_post(raw, collecting_instance):
    """flattens a Lemmy post_view dict into the shape db_writer expects."""
    post = raw.get("post", {})
    creator = raw.get("creator", {})
    community = raw.get("community", {})
    counts = raw.get("counts", {})

    ap_id = post.get("ap_id", "")
    origin_instance = extract_instance_from_ap_id(ap_id)

    media_urls = []
    post_url = post.get("url")
    thumb_url = post.get("thumbnail_url")
    if post_url:
        media_urls.append({"source_url": post_url, "mime": _guess_mime(post_url)})
    if thumb_url and thumb_url != post_url:  # separate thumb, skip if same
        media_urls.append({"source_url": thumb_url, "mime": "image/jpeg"})

    return {
        "author_global_id": creator.get("actor_id", ""),
        "author_handle": creator.get("name", ""),
        "author_created_at": creator.get("published"),

        "ap_id": ap_id,
        "platform": "lemmy",
        "origin_instance": origin_instance,
        "collecting_instance": collecting_instance,
        "community": community.get("name"),
        "title": post.get("name"),
        "body": post.get("body"),
        "lang": post.get("language_id"),
        "has_media": 1 if media_urls else 0,
        "score": counts.get("score"),
        "created_at": post.get("published"),

        "_media_urls": media_urls,
        "_removed": post.get("removed", False),
        "_deleted": post.get("deleted", False),
        "_locked": post.get("locked", False),
        "_nsfw": post.get("nsfw", False),
    }


def _guess_mime(url):
    if not isinstance(url, str):
        return None
    u = url.lower().split("?")[0]  # strip querystring
    for ext, mime in [
        (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
        (".png", "image/png"),  (".gif", "image/gif"),
        (".webp", "image/webp"), (".mp4", "video/mp4"),
        (".webm", "video/webm"),
    ]:
        if u.endswith(ext):
            return mime
    return None


def infer_mod_action(parsed):
    """most severe mod state currently visible, or None. user self-deletes aren't platform actions."""
    # check severe first
    if parsed.get("_removed"):
        return "removed"
    if parsed.get("_locked"):
        return "locked"
    if parsed.get("_nsfw"):
        return "nsfw_labeled"
    return None


def write_media(db, post_id, media_urls):
    inserted = 0
    for m in media_urls:
        url = m.get("source_url", "")
        if not url:
            continue
        result = db.insert_media(
            post_id=post_id,
            sha256=url_sha256(url),
            source_url=url,
            mime=m.get("mime"),
        )
        if result:  # None = duplicate
            inserted += 1
    return inserted


def write_modlog_entry(db, session, instance, entry, stats):
    """log one modlog removal. fetches the post first if we don't have it yet."""
    ap_id = entry["ap_id"]
    if not ap_id:
        return

    post_id = db.get_post_id(ap_id)

    if post_id is None:
        # post wasn't in the feed sweep (usually because it's removed). fetch by id.
        lemmy_id = entry.get("_lemmy_post_id")
        if not lemmy_id:
            return
        raw = fetch_single_post(session, instance, lemmy_id)
        if raw is None:
            # post is fully gone, give up
            return
        time.sleep(RATE_LIMIT_SECS)

        parsed = parse_post(raw, collecting_instance=instance)
        post_id = db.upsert_post(
            ap_id=parsed["ap_id"],
            platform="lemmy",
            origin_instance=parsed["origin_instance"],
            collecting_instance=instance,
            community=parsed["community"],
            title=parsed["title"],
            body=parsed["body"],
            lang="en",
            has_media=parsed["has_media"],
            score=parsed["score"],
            created_at=parsed["created_at"],
        )
        if post_id:
            write_media(db, post_id, parsed.get("_media_urls", []))
            stats["posts_new"] += 1
        else:
            # race condition, re-read
            post_id = db.get_post_id(ap_id)
            stats["posts_duplicate"] += 1

    if post_id is None:
        return

    db.insert_mod_event(
        post_id=post_id,
        action_type=entry["action_type"],
        target_type=entry["target_type"],
        actor_instance=entry["actor_instance"],
        observed_at=entry.get("observed_at"),
    )
    stats["mod_events"] += 1


def collect_instance(db, session, instance, communities, per_category_target=PER_CATEGORY_TARGET):
    """collect from one Lemmy instance. two passes per community: modlog, then feed sweep."""
    stats = {
        "posts_new": 0,
        "posts_duplicate": 0,
        "mod_events": 0,
        "posts_skipped_lang": 0,
    }

    # no communities = sweep local feed only
    targets = communities if communities else [None]

    for community in targets:
        label = community or "<local feed>"
        logger.info("[%s] === Community: %s ===", instance, label)

        # pass 1: modlog (categorised removals)
        logger.info("[%s/%s] Pass 1: modlog...", instance, label)
        buckets = fetch_modlog_categorised(
            session, instance, community,
            per_category_target=per_category_target,
        )

        for category in CATEGORIES:
            entries = buckets[category]
            logger.info("[%s/%s] Category '%s': %d entries",
                        instance, label, category, len(entries))
            for entry in tqdm(entries):
                write_modlog_entry(db, session, instance, entry, stats)

        # pass 2: feed sweep to top up
        total_collected = stats["posts_new"]
        remaining = max(0, TOTAL_TARGET - total_collected)
        if remaining == 0:
            logger.info("[%s/%s] Target hit via modlog, skipping feed", instance, label)
            continue

        page_size = 50
        pages_needed = math.ceil(remaining // page_size)
        logger.info("[%s/%s] Pass 2: feed sweep for ~%d more (%d pages)",
                    instance, label, remaining, pages_needed)

        raw_posts = _fetch_posts_paged(
            session, instance, community,
            page_size=page_size, max_pages=pages_needed,
        )
        logger.info("[%s/%s] Processing %d post_views", instance, label, len(raw_posts))

        for raw in raw_posts:
            parsed = parse_post(raw, collecting_instance=instance)

            # language_id: 37 = English, 0 = unknown
            lang_id = parsed.get("lang")
            if lang_id is not None and lang_id not in (0, 37):
                stats["posts_skipped_lang"] += 1
                continue

            if db.post_exists(parsed["ap_id"]):
                stats["posts_duplicate"] += 1
                continue

            post_id = db.upsert_post(
                ap_id=parsed["ap_id"],
                platform="lemmy",
                origin_instance=parsed["origin_instance"],
                collecting_instance=instance,
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
                continue
            stats["posts_new"] += 1

            write_media(db, post_id, parsed.get("_media_urls", []))
            action = infer_mod_action(parsed)
            if action:
                db.insert_mod_event(
                    post_id=post_id,
                    action_type=action,
                    target_type="post",
                    actor_instance=instance,
                )
                stats["mod_events"] += 1

    return stats


def _fetch_posts_paged(session, instance, community_name, page_size=50, max_pages=60):
    """like fetch_posts but with max_pages cap. used in the feed sweep stage."""
    all_posts = []
    url = f"https://{instance}{API}/post/list"

    for page in range(1, max_pages + 1):
        params: dict = {
            "type_": "All" if community_name else "Local",
            "sort": "New",
            "limit": page_size,
            "page": page,
        }
        if community_name:
            params["community_name"] = community_name

        try:
            r = session.get(url, params=params, timeout=15)
            r.raise_for_status()
            posts = r.json().get("posts", [])
            if not posts:
                break
            all_posts.extend(posts)
            logger.info("[%s] Feed page %d - %d posts (total: %d)",
                        instance, page, len(posts), len(all_posts))
            time.sleep(RATE_LIMIT_SECS)
        except requests.RequestException as e:
            logger.error("[%s] Feed fetch error (page %d): %s", instance, page, e)
            break

    return all_posts


def main():
    parser = argparse.ArgumentParser(description="Collect Lemmy posts into moderation.db")
    parser.add_argument("--instances", nargs="+", default=["lemmy.world"])
    parser.add_argument("--communities", nargs="*", default=[])
    parser.add_argument("--per-category", type=int, default=PER_CATEGORY_TARGET)
    parser.add_argument("--username", default=os.getenv("LEMMY_USERNAME"))
    parser.add_argument("--password", default=os.getenv("LEMMY_PASSWORD"))
    parser.add_argument("--db", default="moderation.db")
    args = parser.parse_args()

    with DB(args.db) as db:
        session = make_session()

        for instance in args.instances:
            logger.info("=== Collecting from %s (target: %d per category) ===",
                        instance, args.per_category)

            if args.username and args.password:
                login(session, instance, args.username, args.password)
            else:
                logger.info("[%s] No creds, running anonymous. Some removal reasons may be hidden.",
                            instance)

            try:
                stats = collect_instance(
                    db=db,
                    session=session,
                    instance=instance,
                    communities=args.communities,
                    per_category_target=args.per_category,
                )
                logger.info("[%s] Done - %s", instance, stats)
            except Exception as e:
                logger.error("[%s] Collection failed: %s", instance, e)
                raise

            # drop token before next instance
            session.headers.pop("Authorization", None)


if __name__ == "__main__":
    main()
