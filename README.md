# Job Matcher

A local, zero-cost job matching service. Upload your CV as a PDF once. The service then pulls fresh postings from
[JobDataLake](https://www.jobdatalake.com/docs) and [Worklittle](https://docs.worklittle.com), ranks them
against your profile with a sentence-embedding model running on your CPU (`all-MiniLM-L6-v2`), remembers what it has
already seen in SQLite, and serves your **top 15 matches per day**.

No paid LLM APIs are involved. Without API keys, it runs end to end on bundled mock postings.

```
CV PDF ──► PyMuPDF text ──► profile (skills, roles, seniority, summary_blob) ──► embedding (once, stored)
                                                                                        │
JobDataLake / Worklittle / mock ──► RawJob ──► dedup vs SQLite ──► stage 1: score metadata
                                                                  ──► stage 2: fetch descriptions for top N, re-score
                                                                  ──► store all ──► GET /jobs/daily (top 15 ≥ threshold)
```

## Requirements

- Python 3.11+
- About 1.5 GB of disk for PyTorch and sentence-transformers. The model itself (about 90 MB) downloads from
  Hugging Face on first use, then runs offline.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # optional: add API keys, tune thresholds
```

## Run the API

```bash
uvicorn app.main:app --reload
```

Interactive docs are served at http://127.0.0.1:8000/docs.

```bash
# 1. Upload your CV (parsed, embedded, stored; later syncs reuse it)
curl -F "file=@cv.pdf" http://127.0.0.1:8000/api/v1/cv/upload

# 2. Fetch, score and store now (the in-app scheduler also does this every 24h)
curl -X POST http://127.0.0.1:8000/api/v1/jobs/sync

# 3. Today's top matches
curl http://127.0.0.1:8000/api/v1/jobs/daily

# 4. Triage a listing
curl -X PATCH http://127.0.0.1:8000/api/v1/jobs/<job-uuid> \
     -H "Content-Type: application/json" -d '{"is_dismissed": true}'    # or {"is_bookmarked": true}
```

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/cv/upload` | Multipart `file` (PDF, max 10 MB). Returns the extracted profile; `422` for non-PDF, scanned or corrupt files. |
| `GET` | `/api/v1/cv/profile` | The stored active profile (`404` before the first upload). |
| `POST` | `/api/v1/jobs/sync` | Runs the pipeline and returns `{status, fetched, new, matched, enriched, sources, …}`. Returns `409` if no CV has been uploaded or a sync is already running. |
| `GET` | `/api/v1/jobs/daily` | Top `DAILY_TARGET_COUNT` non-dismissed jobs with `relevance_score ≥ SIMILARITY_THRESHOLD` from the current 24h cycle. |
| `PATCH` | `/api/v1/jobs/{job_id}` | `{"is_dismissed": bool, "is_bookmarked": bool}` (either or both). |
| `GET` | `/health` | Liveness check. |

## CLI and external scheduling

```bash
python -m app.cli upload-cv ~/Documents/cv.pdf   # set the active profile without running the server
python -m app.cli sync                           # fetch → score → store, with a summary table
python -m app.cli daily                          # today's top matches
```

`sync` exits with `0` on success or partial success, `1` on an error (e.g. no CV uploaded), and `2` when every
source failed. That makes it safe to run from cron. If you schedule syncs externally, set
`SCHEDULER_ENABLED=false` for the server.

```cron
# crontab -e : every day at 07:00
0 7 * * * cd /path/to/job-tracker && .venv/bin/python -m app.cli sync >> sync.log 2>&1
```

On Windows Task Scheduler, use program `C:\path\to\job-tracker\.venv\Scripts\python.exe`, arguments
`-m app.cli sync`, and "Start in" set to the project folder.

## Configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./jobs.db` | |
| `JOBDATALAKE_API_KEY` / `WORKLITTLE_API_KEY` | empty | Each key enables its source. **With no keys, bundled mock data is used.** |
| `SIMILARITY_THRESHOLD` | `0.45` | Minimum cosine similarity (0–1) for a job to be surfaced. |
| `DAILY_TARGET_COUNT` | `15` | |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | If you change it, the stored profile is re-embedded on the next sync. |
| `REMOTE_ONLY` | `true` | Sent to the APIs as `remote_type=fully_remote` / `workplace_type=remote`. |
| `FILTER_BY_SENIORITY` | `false` | Sends the CV's estimated seniority as a hard API filter. |
| `FETCH_LOOKBACK_DAYS` | `2` | Only postings from this window are requested. |
| `MAX_JOBS_PER_SOURCE` | `150` | Split across up to 3 role queries derived from the CV. |
| `DETAIL_ENRICH_TOP_N` | `50` | Stage-2 detail calls per sync (see below). |
| `HTTP_MAX_RETRIES` | `4` | Retries for 429, 5xx and network errors. |
| `SCHEDULER_ENABLED` / `SYNC_INTERVAL_HOURS` / `SYNC_ON_STARTUP` | `true` / `24` / `false` | In-app APScheduler job. |

## How matching works

1. **CV parsing** is heuristic and fully local. PyMuPDF extracts text blocks, and two-column layouts are read column by
   column. The parser then detects sections and extracts:
   - **Skills:** matched against a taxonomy of about 200 skills with aliases (`k8s` → Kubernetes). Ambiguous words
     like *Go*, *C* and *R* only count inside a Skills section.
   - **Roles:** job titles are normalized, e.g. `Sr. Back-end Developer` → `Senior Backend Developer`.
   - **Years of experience:** overlapping employment date ranges are merged, and an explicit "N+ years" claim is used
     if larger. Seniority maps from years: junior <2, mid 2–5, senior 5–9, staff 9+. A senior/staff title can raise
     the level.

   These are compressed into `summary_blob`, which is embedded once and stored.
2. **Fetching** searches each configured source with the CV's roles, minus the level prefix. Requests are throttled
   below each vendor's documented limit (JobDataLake 10 req/s; Worklittle 60 req/min). Retries honour `Retry-After`,
   otherwise use exponential backoff with jitter. Worklittle's `QUOTA_EXCEEDED`, 401 and 402 are not retried. If one
   source fails, the others still run and the run is marked `partial`.
3. **Two-stage scoring.** Neither API returns descriptions in search results. Every new job is first scored on
   title, company, skills, seniority and location. Only the best `DETAIL_ENRICH_TOP_N` get a detail call, and those
   are re-scored on `title + description`. This keeps API usage bounded. Worklittle allows one request per second, so
   50 detail calls take about 50 seconds.
4. **Storage and dedup.** Every new job is stored, including those below the threshold, keyed by the unique
   `external_id = "<source>:<native id>"`. Later syncs therefore never re-score or re-insert a job, and "new" means
   genuinely unseen. The daily query applies the threshold and the dismissed flag. Its 24h window reaches back to the
   latest completed run, so a late scheduled run doesn't empty the digest.

## Tests

```bash
pytest              # fast: fake encoder, generated PDFs, mocked HTTP, temp SQLite (no network)
pytest -m slow      # also loads the real MiniLM model (downloads it on first run)
```

## Known limitations

- **Vendor schemas aren't fully documented.** The JobDataLake detail response and Worklittle's job object don't list
  their field names. The normalizers accept common variants (`description_text` / `description` /
  `description_html`, ISO or unix timestamps, and so on), and the tests use payloads shaped like the published
  examples. Run one real sync after adding a key. If a field comes back empty, adjust `normalize()` /
  `fetch_details()` in `app/services/job_fetcher.py`.
- Worklittle seniority values other than `entry` are guesses, which is another reason `FILTER_BY_SENIORITY` defaults
  to off.
- Scanned (image-only) CVs aren't supported, because there is no OCR.
- `SIMILARITY_THRESHOLD=0.45` is fairly strict for MiniLM on CV-vs-posting text. If the digest is sparse, lower it
  to about 0.35.
- **Generic CV prose can lift unrelated jobs.** With the real model and a sample full-stack CV, an "Account
  Executive" posting scored 0.51. The summary line "7+ years of experience shipping SaaS products" matched the
  posting's "3+ years of SaaS sales experience". Embedding only roles and skills (dropping the `Summary:` part in
  `build_summary_blob`) brought every non-engineering job below 0.45, though the closest was only 0.43. This comes
  from a single CV against mock postings, so check it against real feeds before changing the default.
- The free Worklittle tier returns 1,000 jobs per month.

## Project layout

```
app/
  api/            deps.py, v1/router.py, v1/endpoints/{cv,jobs}.py
  core/           config.py (pydantic-settings), database.py (async SQLModel/aiosqlite)
  models/         cv.py (CandidateProfile + stored record), job.py (Job, DailyRun, RawJob, schemas)
  services/       cv_parser.py, skills_taxonomy.py, matcher.py, job_fetcher.py, mock_jobs.json,
                  sync.py (pipeline + daily query), scheduler.py (APScheduler)
  cli.py          python -m app.cli {sync,daily,upload-cv}
  main.py         app factory + lifespan (DB init, model warm-up, scheduler)
tests/            test_cv_parser.py, test_matcher.py, test_job_fetcher.py, test_api.py
```
