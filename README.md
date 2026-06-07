# Cross Platform Content Moderation Dataset

DSCI-511 term project. A pipeline that collects social media posts from Bluesky and Lemmy, records what each platform did to moderate them, and asks a local LLM how Meta's and X's written policies would treat the same content. Output is a SQLite dataset (`moderation.db`) for studying divergence between platform moderation and policy as prompt LLM judgments.

**Contributors:** Ari Munkhtur, Jayesh Bane


---

## How To Run

### Prerequisites
- Python 3.10+
- [Ollama](https://ollama.ai) with `gemma4:latest` pulled (`ollama pull gemma4:latest`)
- `pip install -r requirements.txt`

### Build the database
```bash
sqlite3 moderation.db < schema.sql
```

### Collect

**Bluesky**  credentials are optional but unlock more Ozone labels:
```bash
python src/collect_bluesky.py \
    --username <handle.bsky.social> \
    --password <app_password> \
    --db moderation.db
```

**Lemmy**  pass one or more instance hostnames:
```bash
python src/collect_lemmy.py \
    --instances lemmy.world lemmy.ml programming.dev \
    --username <username> \
    --password <password> \
    --db moderation.db
```

### Run the LLM judge

```bash
# main runner (Bluesky posts)
# Make you have Local LLM model running on localhost. Edit the python file according to your llm model and current hardware specs. 
# This might take a while depending on hardware specs. 
python src/llm_runner.py --limit 1000 --db moderation.db  

# Lemmy specific runner (see Challenges below for why this is separate)
python src/llm_lemmy.py --limit 1200 --db moderation.db
```

Each post produces 6 LLM calls (3 categories × 2 policies) =  3 verdict rows per post. ~17–25 seconds per post on local hardware. 
Processing power will be different on your hardware. 

---

## Database Schema

Four tables, foreign keys enforced via `PRAGMA foreign_keys = ON` on every connection.

- **`posts`** — one row per unique post. 
- **`moderation_observed`** — zero or more rows per post. Each row is a platform moderation event. 
- **`media`** — image or video URLs attached to posts. Bytes are NOT downloaded (only URLs and a SHA-256 hash of the URL (for duplication check)).
- **`policy_simulation`** — LLM verdicts. Wide schema: each row stores both Meta and X verdicts side by side. 3 rows per judged post.

See `schema.sql` for full column definitions.

---

## Challenges

### Lemmy modlog `community_name` parameter is silently ignored
The Lemmy modlog API documents a `community_name` filter parameter. **It does nothing.** Every sub-query returns the full instance modlog regardless of the filter. We caught this when the raw `moderation_observed` row count came in 4.6× higher than expected (182 rows reduced to 39 unique events after dedup).

**Workaround:** dedup at analysis time:
```sql
DELETE FROM moderation_observed 
WHERE mod_id NOT IN (
    SELECT MIN(mod_id) FROM moderation_observed 
    GROUP BY post_id, action_type
);
```

### Keyword filter dropped ~81% of Lemmy removals
Our v1 collector filters Lemmy modlog entries by matching the moderator's free text removal reason against category keywords. Mods often write reasons ("rule 3", "removed") that don't match. The filter rejected most available removals, leaving us with only 10 unique Lemmy moderation events in the final dataset.

**Fix for v2:** drop the keyword filter at collection; categorize at LLM judge time instead.

### Initial LLM run sampled only Bluesky
The first LLM judging run used `ORDER BY post_id ASC LIMIT 1000`. Bluesky post_ids (1–3000) were inserted before Lemmy post_ids (3001–4164), so the limit pulled exclusively Bluesky posts. Caught this by inspecting the verdict table, then wrote `llm_lemmy.py` to backfill Lemmy coverage. Both runners are included for reproducibility.

**Fix for v2:** stratified sampling that explicitly draws from each platform.


---

## Limitations

- **Single LLM tested** (`gemma4:latest`, 8B Q4). No inter-model robustness check. Findings may not generalize.
- **Text only.** Image policy enforcement deferred to v2. 3,542 media URLs were collected as metadata but bytes were never downloaded or analyzed.
- **52% dataset coverage.** 2,164 of 4,164 posts have LLM verdicts. Remaining 2000 posts would need ~2 additional days of processing the posts.
- **No hand labeled validation set.** All findings compare LLM verdicts to platform verdicts, not to human ground truth. Cannot compute precision/recall.
- **English only**, 7 day collection window.
- **Platform asymmetry.** Bluesky labels are 85% adult content according to their 2025 Transparency Report  
- **Media dedup is approximate** - hash of URL, not hash of bytes. The same image with different URLs stores as two rows.
- **LLM verdicts are not bit reproducible.** We use `think=False, stream=False` for stability, but exact verdict reproduction across runs is not guaranteed at temperature > 0.

---

## AI-Assisted Development

This project used AI coding assistants (Claude) for data analysis implementation. AI assistance was used for running LLM verdicts on already collected posts : 

- **LLM Runner** python files 

AI assistance is acknowledged here rather than obscured.

---

