"""
llm_lemmy.py - Lemmy-only LLM runner.

The main llm_runner.py used ORDER BY post_id ASC + LIMIT which picked only Bluesky
posts for the first 1000 post run (Bluesky post_ids are 1-3000, Lemmy is 3001-4164 because bluesky was run 1st).
This script targets Lemmy only to fix that gap.

Also uses a text bearing filter (body OR title non empty) instead of has_media=0,
so we don't skip Lemmy posts that have link thumbnails.

Usage:
    python src/llm_lemmy.py
    python src/llm_lemmy.py --limit 50
    python src/llm_lemmy.py --moderated-only   # just the platform-moderated ones (fast)
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# import the prompt builder
sys.path.insert(0, str(Path(__file__).parent))
from prompt_builder_v2 import build_prompt  # noqa: E402


# Config
DB_PATH = Path(__file__).parent.parent / "moderation.db"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gemma4:latest"
CATEGORIES = ["violence", "hate_speech", "spam"]
PLATFORMS = ["meta", "x"]
REQUEST_TIMEOUT = 120

REFUSAL_MARKERS = (
    "i can't help",
    "i cannot help",
    "i won't",
    "i will not",
    "i'm not able",
    "i am not able",
    "as an ai",
)


def call_ollama(prompt):
    """Send one prompt to Ollama. Returns (raw_text, parsed_json_or_None)."""
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "think": False,
        "stream": False,
    }
    response = requests.post(OLLAMA_URL, json=payload, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    raw = response.json().get("response", "").strip()

    # check for refusal phrases
    first_chunk = raw.lower()[:200]
    if any(marker in first_chunk for marker in REFUSAL_MARKERS):
        return raw, None

    # try parsing the response as JSON
    try:
        return raw, json.loads(raw)
    except json.JSONDecodeError:
        # sometimes JSON has trailing prose, grab the {...} block
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            try:
                return raw, json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                pass
        return raw, None


def call_with_retry(prompt):
    """One retry on failure, then return raw + None."""
    try:
        return call_ollama(prompt)
    except (requests.RequestException, ValueError):
        time.sleep(2)
        try:
            return call_ollama(prompt)
        except (requests.RequestException, ValueError) as e:
            return f"ERROR: {e}", None


def verdict_fields(parsed, raw):
    if parsed is None:
        return {
            "action": "REFUSED",
            "category": "none",
            "confidence": "low",
            "raw": raw,
        }
    return {
        "action": str(parsed.get("action", "REFUSED")).upper(),
        "category": str(parsed.get("category", "none")),
        "confidence": str(parsed.get("confidence", "low")),
        "raw": raw,
    }


def process_post(con, post):
    """Run 6 calls for one post and insert up to 3 rows."""
    title = post["title"] or ""
    body = post["body"] or ""
    rows_written = 0

    for category in CATEGORIES:
        existing = con.execute(
            "SELECT 1 FROM policy_simulation "
            "WHERE post_id = ? AND model = ? AND category_tested = ?",
            (post["post_id"], MODEL, category),
        ).fetchone()
        if existing:
            continue

        verdicts = {}
        truncated_flag = 0

        for platform in PLATFORMS:
            prompt, was_truncated = build_prompt(
                platform=platform,
                category=category,
                post_title=title,
                post_text=body,
                has_image=False,
            )
            if was_truncated:
                truncated_flag = 1

            raw, parsed = call_with_retry(prompt)
            verdicts[platform] = verdict_fields(parsed, raw)

        now = datetime.now(timezone.utc).isoformat()
        con.execute(
            """INSERT INTO policy_simulation (
                 post_id, model, category_tested,
                 meta_action, meta_category, meta_confidence,
                 x_action,    x_category,    x_confidence,
                 meta_raw_response, x_raw_response,
                 prompt_truncated, created_at
               ) VALUES (?,?,?, ?,?,?, ?,?,?, ?,?, ?,?)""",
            (
                post["post_id"], MODEL, category,
                verdicts["meta"]["action"], verdicts["meta"]["category"], verdicts["meta"]["confidence"],
                verdicts["x"]["action"], verdicts["x"]["category"], verdicts["x"]["confidence"],
                verdicts["meta"]["raw"], verdicts["x"]["raw"],
                truncated_flag, now,
            ),
        )
        con.commit()
        rows_written += 1

    return rows_written


def main(limit, moderated_only):
    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}")
        sys.exit(1)

    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    con.row_factory = sqlite3.Row

    if moderated_only:
        # only the moderated Lemmy posts, much faster
        query = """
            SELECT DISTINCT p.post_id, p.title, p.body
              FROM posts p
              JOIN moderation_observed m ON m.post_id = p.post_id
             WHERE p.platform = 'lemmy'
               AND ((p.body IS NOT NULL AND p.body != '')
                    OR (p.title IS NOT NULL AND p.title != ''))
               AND (SELECT COUNT(*) FROM policy_simulation s
                     WHERE s.post_id = p.post_id AND s.model = ?) < 3
             ORDER BY p.post_id
        """
        print("MODE: moderated Lemmy posts only")
    else:
        # all Lemmy text-bearing posts not yet fully judged
        query = """
            SELECT p.post_id, p.title, p.body
              FROM posts p
             WHERE p.platform = 'lemmy'
               AND ((p.body IS NOT NULL AND p.body != '')
                    OR (p.title IS NOT NULL AND p.title != ''))
               AND (SELECT COUNT(*) FROM policy_simulation s
                     WHERE s.post_id = p.post_id AND s.model = ?) < 3
             ORDER BY p.post_id
        """
        print("MODE: all Lemmy text-bearing posts")

    posts = con.execute(query, (MODEL,)).fetchall()

    if limit:
        posts = posts[:limit]

    total = len(posts)
    print(f"Found {total} Lemmy posts needing processing (model: {MODEL})")
    if total == 0:
        print("Nothing to do.")
        return

    start = time.perf_counter()
    for i, post in enumerate(posts, 1):
        t0 = time.perf_counter()
        try:
            written = process_post(con, post)
        except Exception as e:
            print(f"  post {post['post_id']}: UNEXPECTED ERROR: {e}")
            continue
        dt = time.perf_counter() - t0
        avg = (time.perf_counter() - start) / i
        eta_min = (total - i) * avg / 60
        print(f"  [{i}/{total}] post {post['post_id']}: +{written} rows in {dt:.1f}s "
              f"(avg {avg:.1f}s/post, ETA {eta_min:.0f} min)")

    con.close()
    elapsed = (time.perf_counter() - start) / 60
    print(f"\nDone. {total} Lemmy posts processed in {elapsed:.1f} min.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="process only N posts (smoke test)")
    parser.add_argument("--moderated-only", action="store_true",
                        help="only process posts that have a moderation_observed entry")
    args = parser.parse_args()
    main(args.limit, args.moderated_only)
