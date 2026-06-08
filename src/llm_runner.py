"""
llm_runner.py - Runs posts through gemma4 under Meta and X policies, stores verdicts.

Each post gets 6 LLM calls (3 categories x 2 policies) -> 3 rows in policy_simulation.
Text-only in v1, skips posts with no body/title.

Usage:
    python src/llm_runner.py
    python src/llm_runner.py --limit 10   # smoke test

Requires Ollama running locally with gemma4:latest pulled.
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# import the prompt builder from the same folder
sys.path.insert(0, str(Path(__file__).parent))
from prompt_builder_v2 import build_prompt  # noqa: E402


# Config
DB_PATH = Path(__file__).parent.parent / "moderation.db"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gemma4:latest"
CATEGORIES = ["violence", "hate_speech", "spam"]
PLATFORMS = ["meta", "x"]
REQUEST_TIMEOUT = 120  # seconds, one call should be ~3-10s usually

# strings to detect when the model refuses to classify
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

    # check for refusal in the first chunk
    first_chunk = raw.lower()[:200]
    if any(marker in first_chunk for marker in REFUSAL_MARKERS):
        return raw, None

    # try to parse as JSON
    try:
        return raw, json.loads(raw)
    except json.JSONDecodeError:
        # sometimes the model wraps JSON in prose, find the first {...} block
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            try:
                return raw, json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                pass
        return raw, None


def call_with_retry(prompt):
    """One retry on failure, then give up."""
    try:
        return call_ollama(prompt)
    except (requests.RequestException, ValueError):
        time.sleep(2)
        try:
            return call_ollama(prompt)
        except (requests.RequestException, ValueError) as e:
            return f"ERROR: {e}", None


def verdict_fields(parsed, raw):
    """Normalize a parsed verdict into the columns we store in the DB."""
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
    """Run all 6 calls for one post, insert up to 3 rows. Returns rows inserted."""
    title = post["title"] or ""
    body = post["body"] or ""
    rows_written = 0

    for category in CATEGORIES:
        # skip if already done (idempotent re-runs)
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
                has_image=False,   # v1 is text-only
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


def main(limit):
    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}")
        print("Build it first with: sqlite3 moderation.db < schema.sql")
        sys.exit(1)

    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    con.row_factory = sqlite3.Row

    # pick text-bearing posts that don't have all 3 category rows yet
    posts = con.execute(
        """SELECT p.post_id, p.title, p.body
             FROM posts p
            WHERE (p.body IS NOT NULL AND p.body != '') 
               OR (p.title IS NOT NULL AND p.title != '')
              AND (
                    SELECT COUNT(*) FROM policy_simulation s
                     WHERE s.post_id = p.post_id AND s.model = ?
                  ) < 3
            ORDER BY p.post_id""",
        (MODEL,),
    ).fetchall()

    if limit:
        posts = posts[:limit]

    total = len(posts)
    print(f"Found {total} posts needing processing (text-only, model={MODEL})")
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
    print(f"\nDone. {total} posts processed in {elapsed:.1f} min.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="process only N posts (smoke test)")
    args = parser.parse_args()
    main(args.limit)
