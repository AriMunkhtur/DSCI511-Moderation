import argparse
import hashlib
import logging
import os
import time
from tqdm import tqdm
from datetime import datetime, timezone
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

API = "/api/v3"
RATE_LIMIT_SECS = 0.5

TOTAL_TARGET = 1000
CATEGORIES = ["violence", "hate_speech", "spam"]
PER_CATEGORY_TARGET = TOTAL_TARGET // len(CATEGORIES)

# Keywords matched against modlog removal reason — case-insensitive substring match
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


def classify_reason(reason: Optional[str]) -> Optional[str]:
    """Match a modlog reason string to a category. Priority: violence > hate_speech > spam."""
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


def login(session: requests.Session, instance: str,
          username: str, password: str) -> Optional[str]:
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
        logger.warning("[%s] Auth failed: %s — continuing unauthenticated", instance, e)
        return None


def fetch_posts(session: requests.Session, instance: str,
                community_name: Optional[str] = None,
                limit: int = 50, pages: int = 5) -> list[dict]:
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
            params["type_"] = "All"

        try:
            r = session.get(url, params=params, timeout=15)
            r.raise_for_status()
            posts = r.json().get("posts", [])
            if not posts:
                logger.info("[%s] No more posts at page %d", instance, page)
                break
            all_posts.extend(posts)
            logger.info("[%s] Page %d — fetched %d posts (total so far: %d)",
                        instance, page, len(posts), len(all_posts))
            time.sleep(RATE_LIMIT_SECS)
        except requests.RequestException as e:
            logger.error("[%s] Post fetch error on page %d: %s", instance, page, e)
            break

    return all_posts


def fetch_modlog_categorised(
    session: requests.Session,
    instance: str,
    community_name: Optional[str] = None,
    per_category_target: int = PER_CATEGORY_TARGET,
    page_size: int = 50,
) -> dict[str, list[dict]]:
    """Paginate modlog removals and bucket them by category."""
    url = f"https://{instance}{API}/modlog"
    buckets: dict[str, list] = {cat: [] for cat in CATEGORIES}
    buckets["unclassified"] = []

    page = 1
    while True:
        if all(len(buckets[c]) >= per_category_target for c in CATEGORIES):
            logger.info("[%s] All category buckets full — stopping modlog fetch", instance)
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

            if category and len(buckets[category]) >= per_category_target:
                continue

            post = entry.get("post", {})
            moderator = entry.get("moderator") or {}
            actor_instance = (
                extract_instance_from_ap_id(moderator.get("actor_id", ""))
                or instance
            )

            buckets[bucket].append({
                "ap_id": post.get("ap_id", ""),
                "_lemmy_post_id": post.get("id"),
                "action_type": "removed" if action.get("removed", True) else "restored",
                "actor_instance": actor_instance,
                "observed_at": action.get("when_"),
                "target_type": "post",
                "reason": reason,
                "category": category,
            })

        totals = {c: len(buckets[c]) for c in CATEGORIES}
        logger.info("[%s] Modlog page %d — category totals so far: %s", instance, page, totals)
        page += 1
        time.sleep(RATE_LIMIT_SECS)

    return buckets


def fetch_single_post(session: requests.Session, instance: str,
                      lemmy_post_id: int) -> Optional[dict]:
    """Fetch a single post_view by numeric ID. Used for modlog-referenced posts we don't have yet."""
    url = f"https://{instance}{API}/post"
    try:
        r = session.get(url, params={"id": lemmy_post_id}, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("post_view")
    except requests.RequestException as e:
        logger.debug("[%s] Could not fetch post %d: %s", instance, lemmy_post_id, e)
        return None


def extract_instance_from_ap_id(ap_id: str) -> str:
    try:
        return urlparse(ap_id).netloc
    except Exception:
        return ""


def url_sha256(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def parse_post(raw: dict, collecting_instance: str) -> dict:
    """Flatten a Lemmy post_view into DB-ready fields."""
    post = raw.get("post", {})
    creator = raw.get("creator", {})
    community = raw.get("community", {})
    counts = raw.get("counts", {})

    ap_id = post.get("ap_id", "")
    origin_instance = extract_instance_from_ap_id(ap_id)

    media_urls: list[dict] = []
    post_url = post.get("url")
    thumb_url = post.get("thumbnail_url")
    if post_url:
        media_urls.append({"source_url": post_url, "mime": _guess_mime(post_url)})
    if thumb_url and thumb_url != post_url:
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


def _guess_mime(url: str) -> Optional[str]:
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


def infer_mod_action(parsed: dict) -> Optional[str]:
    """
    Return the most severe current platform mod state, or None if unmoderated.
    User self-deletions (post.deleted) are NOT platform decisions, so they
    are not recorded in moderation_observed under this schema.
    """
    if parsed.get("_removed"):
        return "removed"
    if parsed.get("_locked"):
        return "locked"
    if parsed.get("_nsfw"):
        return "nsfw_labeled"
    return None


def write_media(db: DB, post_id: int, media_urls: list[dict]) -> int:
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
        if result:
            inserted += 1
    return inserted


def write_modlog_entry(
    db: DB,
    session: requests.Session,
    instance: str,
    entry: dict,
    stats: dict,
) -> None:
    """Ensure the post is in the DB, then write the mod event."""
    ap_id = entry["ap_id"]
    if not ap_id:
        return

    post_id = db.get_post_id(ap_id)

    if post_id is None:
        # Post was already removed before our sweep — fetch it individually
        lemmy_id = entry.get("_lemmy_post_id")
        if not lemmy_id:
            return
        raw = fetch_single_post(session, instance, lemmy_id)
        if raw is None:
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


def collect_instance(
    db: DB,
    session: requests.Session,
    instance: str,
    communities: list[str],
    per_category_target: int = PER_CATEGORY_TARGET,
) -> dict[str, int]:
    stats = {
        "posts_new": 0,
        "posts_duplicate": 0,
        "mod_events": 0,
        "posts_skipped_lang": 0,
    }

    targets = communities if communities else [None]

    for community in targets:
        label = community or "<local feed>"
        logger.info("[%s] === Community: %s ===", instance, label)

        # Pass 1 — modlog: category-labelled removals
        logger.info("[%s/%s] Pass 1: fetching categorised modlog...", instance, label)
        buckets = fetch_modlog_categorised(
            session, instance, community,
            per_category_target=per_category_target,
        )

        for category in CATEGORIES:
            entries = buckets[category]
            logger.info("[%s/%s] Category '%s': %d modlog entries",
                        instance, label, category, len(entries))
            for entry in tqdm(entries):
                write_modlog_entry(db, session, instance, entry, stats)

        # Pass 2 — community feed sweep to fill remaining quota
        total_collected = stats["posts_new"]
        remaining = max(0, TOTAL_TARGET - total_collected)
        if remaining == 0:
            logger.info("[%s/%s] Target reached via modlog — skipping feed sweep", instance, label)
            continue

        page_size = 50
        pages_needed = -(-remaining // page_size)  # ceiling division
        logger.info(
            "[%s/%s] Pass 2: feed sweep for ~%d more posts (%d pages)",
            instance, label, remaining, pages_needed,
        )

        raw_posts = _fetch_posts_paged(
            session, instance, community,
            page_size=page_size, max_pages=pages_needed,
        )
        logger.info("[%s/%s] Processing %d post_views from feed", instance, label, len(raw_posts))

        for raw in raw_posts:
            parsed = parse_post(raw, collecting_instance=instance)

            # language_id 37 = English, 0 = undetermined
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


def _fetch_posts_paged(
    session: requests.Session,
    instance: str,
    community_name: Optional[str],
    page_size: int = 50,
    max_pages: int = 60,
) -> list[dict]:
    all_posts: list[dict] = []
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
            logger.info("[%s] Feed page %d — %d posts (total: %d)",
                        instance, page, len(posts), len(all_posts))
            time.sleep(RATE_LIMIT_SECS)
        except requests.RequestException as e:
            logger.error("[%s] Feed fetch error (page %d): %s", instance, page, e)
            break

    return all_posts


def main():
    parser = argparse.ArgumentParser(description="Collect Lemmy posts into moderation.db")
    parser.add_argument("--instances", nargs="+", default=["lemmy.world"],
                        help="Lemmy instance hostnames to collect from")
    parser.add_argument("--communities", nargs="*", default=[],
                        help="Community names to target (omit for local feed)")
    parser.add_argument("--per-category", type=int, default=PER_CATEGORY_TARGET,
                        help=f"Posts to collect per category (default: {PER_CATEGORY_TARGET})")
    parser.add_argument("--username", default=os.getenv("LEMMY_USERNAME"),
                        help="Lemmy username for auth (optional but recommended)")
    parser.add_argument("--password", default=os.getenv("LEMMY_PASSWORD"),
                        help="Lemmy password for auth (optional but recommended)")
    parser.add_argument("--db", default="moderation.db",
                        help="Path to SQLite database file")
    args = parser.parse_args()

    with DB(args.db) as db:
        session = make_session()

        for instance in args.instances:
            logger.info("=== Starting collection: %s (target: %d per category) ===",
                        instance, args.per_category)

            if args.username and args.password:
                login(session, instance, args.username, args.password)
            else:
                logger.info(
                    "[%s] No credentials — collecting as anonymous. "
                    "Note: some instances hide removal reasons without auth.",
                    instance,
                )

            try:
                stats = collect_instance(
                    db=db,
                    session=session,
                    instance=instance,
                    communities=args.communities,
                    per_category_target=args.per_category,
                )
                logger.info("[%s] Done — %s", instance, stats)
            except Exception as e:
                logger.error("[%s] Collection failed: %s", instance, e)
                raise

            session.headers.pop("Authorization", None)


if __name__ == "__main__":
    main()
