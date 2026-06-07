# Cross-Platform Content Moderation Dataset

DSCI-511 term project. A pipeline that collects social media posts from
Bluesky and Lemmy, records what each platform did to moderate them, then
asks a local LLM how Meta's and X's written policies would treat the same
content. The output is a dataset for studying where platform moderation
and policy-as-prompt judgments diverge.

## What is / is NOT in this repo

This repo contains **code and structure only**. The collected dataset,
images, and LLM outputs are intentionally absent.

Code is explicitly typed to avoid errors during development.

## Structure

```
schema.sql              SQLite schema. Build the DB from this.
smoke_test.py           Run first. Proves schema + environment work.
src/
  prompt_builder_v2.py  Builds LLM prompts from policy + post. Pure, tested.
  collect_bluesky.py    Fetches data using the BlueSky API and stores it in DB.
  collect_lemmy.py      Fetches data using the Lemmy Open API and stores it in DB.
  db_writer.py          A wrapper to interact with the SQLite3 database under the hood.
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

2. Any code that connects to the DB must run
   `PRAGMA foreign_keys = ON` on every connection.

3. Post identity is the global `ap_id`, UNIQUE. Always insert posts with
   `INSERT OR IGNORE` — this dedupes federated Lemmy copies.

## Components

| Component                   | Status       | Owner  |
| --------------------------- | ------------ | ------ |
| Schema                      | Done         | Ari    |
| Prompt builder              | Done, tested | Ari    |
| Policy JSONs                | Done         | Ari    |
| Collector (Bluesky + Lemmy) | Done         | Jayesh |
| LLM runner (Ollama)         | Done         | Ari    |

## Locked design decisions

- Platforms: Bluesky + Lemmy
- Categories: violence, hate_speech, spam (only)
- English only
- Storage: SQLite, single moderation.db
- policy_simulation is WIDE (meta + x columns, one model)
- Post deduplication on global ap_id
