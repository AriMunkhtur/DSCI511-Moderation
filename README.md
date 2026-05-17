# Cross-Platform Content Moderation Dataset

DSCI-511 term project. A pipeline that collects social media posts from
Bluesky and Lemmy, records what each platform did to moderate them, then
asks a local LLM how Meta's and X's written policies would treat the same
content. The output is a dataset for studying where platform moderation
and policy-as-prompt judgments diverge.

## What is / is NOT in this repo

This repo contains **code and structure only**. The collected dataset,
images, and LLM outputs are intentionally absent — they contain real
user content (including moderated/removed posts) and are kept local for
privacy and platform-ToS reasons. Anyone with this repo regenerates the
data themselves by running the pipeline.

## Structure

```
schema.sql              SQLite schema. Build the DB from this.
smoke_test.py           Run first. Proves schema + environment work.
src/
  prompt_builder_v2.py  Builds LLM prompts from policy + post. Pure, tested.
prompts/
  template_text.txt     Prompt template, text-only posts.
  template_vision.txt    Prompt template, posts with images.
policies_v2/
  meta/  x/             6 policy JSONs (violence, hate_speech, spam).
.gitignore              Keeps data/content out of Git. Do not delete.
```

## Setup

1. Build the database:
   `sqlite3 moderation.db < schema.sql`
   (or run `python smoke_test.py` to verify everything works first)

2. Any code that connects to the DB must run
   `PRAGMA foreign_keys = ON` on every connection.

3. Post identity is the global `ap_id`, UNIQUE. Always insert posts with
   `INSERT OR IGNORE` — this dedupes federated Lemmy copies.

## Components

| Component | Status | Owner |
|-----------|--------|-------|
| Schema | Done | Ari |
| Prompt builder | Done, tested | Ari |
| Policy JSONs | Done | Ari |
| Collector (Bluesky + Lemmy) | Not built | Jayesh |
| LLM runner (Ollama) | Not built | Ari |

## Locked design decisions

- Platforms: Bluesky + Lemmy
- Categories: violence, hate_speech, spam (only)
- English only, 7-day collection window
- Storage: SQLite, single moderation.db
- policy_simulation is WIDE (meta + x columns, one model)
- Post dedup on global ap_id
