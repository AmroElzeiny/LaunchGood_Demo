# ZapLance (Freelance Opportunities Alert Bot)

ZapLance monitors freelance opportunities, project briefs, contracts, consulting work, retainers, gigs, and mixed opportunity feeds. It discovers likely opportunity-detail links, extracts structured opportunity data with AI, and delivers relevant matches to Telegram subscribers.

## What It Does

- Runs in continuous 60-second cycles by default.
- Scans all active sources while `AGENT_COUNT` limits how many sources run at once.
- Opens each candidate link in a new browser tab, closes it, and returns to the main tab.
- Discovers project posts, contract boards, RFPs, consulting briefs, mixed opportunity feeds, and compatible job-style pages from mixed sources.
- Handles link deduplication and neglect tracking with SQLite.
- Stops scanning a source in the current cycle after 3 already-seen links in a row.
- Caps output to 3 opportunity cards per cycle globally.
- Supports subscriber delivery with per-user filter matching for role, location, budget/payment visibility, keywords, and selected sources.
- Neglects stale opportunities older than `MAX_JOB_POST_AGE_DAYS` using AI freshness validation.
- Supports a human-review queue for low-confidence AI decisions.
- Queues failed deliveries and retries them automatically.
- Supports NOWPayments payment links for 14-day, monthly, and quarterly subscriptions.

## Product Model

- The product is opportunity-first, not limited to traditional job posts.
- Relevant matches can be projects, contracts, freelance assignments, retainers, consulting briefs, gigs, RFPs, or job posts from mixed opportunity feeds.
- Success is measured as a relevant opportunity match, not a job match.
- Extraction targets support project-style fields such as client/requester, project title, scope summary, budget/rate/payment, duration, commitment, skills, deadline, start timeline, industry, engagement type, remote/location constraints, and proposal/contact URL.

## Source Strategy

- Freelance marketplaces:
  - Upwork
  - Contra
- Contract / project boards:
  - Dribbble
  - We Work Remotely
- Startup / client request boards:
  - Work at a Startup
  - Y Combinator Jobs
- Mixed opportunity boards:
  - Wellfound
  - Built In
  - FlexJobs

## Captcha + Fallback Strategy

- Fetch flow fallback order:
1. `StealthyFetcher` (`solve_cloudflare=True`)
2. `DynamicFetcher`
3. `Fetcher.get` with SSL verify from config
4. `Fetcher.get` with `verify=False` fallback

- AI decision flow: pass 1 -> pass 2 confirmation -> pass 3 final decision.
- If pass 2 decides a page is not a relevant opportunity detail page, the link is flagged as not-opportunity.
- Card limits are enforced: title max 5 words, description max 30 words.
- Telegram delivery is limited to users with an active free trial or paid subscription.

## Setup

1. Create and activate a venv:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

For exact environment reproduction, use `requirements-full.txt` instead of `requirements.txt`.
`requirements.txt` contains the direct runtime/test packages; `requirements-full.txt` is a full frozen dependency snapshot.

3. Configure:

- Copy `.env.example` to `.env`
- Fill:
  - `OPENAI_API_KEY`
  - `OPENAI_MODEL`
  - Optional split-model overrides:
    - `OPENAI_MODEL_WEBSITE_GUARD`
    - `OPENAI_MODEL_LINK_RANKING`
    - `OPENAI_MODEL_KEYWORD_EXPANSION`
    - `OPENAI_MODEL_EXTRACTION`
    - `OPENAI_MODEL_POST_AGE`
    - `OPENAI_MODEL_FILTER_MATCH`
  - `TELEGRAM_BOT_TOKEN`
  - `SCRAPE_SITES_FILE` path (default `links for UX UI.txt`)
  - `SPHERE_WEBSITES_FILE_PATH` path (default `sphere_websites.txt`)
  - Add your target source URLs inside `SCRAPE_SITES_FILE` as listing/feed pages, not single detail pages
  - `AGENT_COUNT=1`
- Optional:
  - `TELEGRAM_SUBS_DB_PATH` (default: `state/telegram_subscriptions.db`)
  - `TELEGRAM_USER_LOG_DIR` (default: `state/user_logs`)
  - `USE_SCRAPLING_CLOUDFLARE_SOLVER` (`true|false`, default `false`)
  - `NEGLECT_POST_IF_FILTERS_MISS` (`true|false`, default `false`)
  - `ENABLE_HUMAN_REVIEW_QUEUE` (`true|false`, default `true`)
  - `HUMAN_REVIEW_CONFIDENCE_THRESHOLD` (`0..1`, default `0.62`)
  - `MAX_JOB_POST_AGE_DAYS` (integer >= 1, default `7`)
  - Recommended budget split:
    - `OPENAI_MODEL_WEBSITE_GUARD=gpt-5-nano`
    - `OPENAI_MODEL_LINK_RANKING=gpt-5-nano`
    - `OPENAI_MODEL_KEYWORD_EXPANSION=gpt-5-nano`
    - `OPENAI_MODEL_EXTRACTION=gpt-5-mini`
    - `OPENAI_MODEL_POST_AGE=gpt-5-mini`
    - `OPENAI_MODEL_FILTER_MATCH=gpt-5-mini`
  - `NOWPAYMENTS_API_KEY` (placeholder allowed; mock links/status are used if missing)
  - `NOWPAYMENTS_BASE_URL` (default: `https://api.nowpayments.io`)
  - `NOWPAY_EMAIL`, `NOWPAY_PASSWORD` (optional auth fallback if provider returns 401/403)

## Run

### Infinite loop

```powershell
.\.venv\Scripts\python main.py
```

### One cycle test

```powershell
.\.venv\Scripts\python main.py --once
```

### Watch browser actions

```powershell
.\.venv\Scripts\python main.py --headful
```

### Telegram subscription bot

```powershell
.\.venv\Scripts\python telegram_bot_main.py
```

### Analytics dashboard + human review queue

```powershell
.\.venv\Scripts\python run_dashboard.py
```

### AI prospect discovery CRM dashboard

```powershell
.\.venv\Scripts\python run_prospect_dashboard.py
```

This runs the separate `prospect_system/` Streamlit app and does not replace the Telegram/jobs workflow.

Required `.env` values for the full prospect flow:

- `PROSPECT_DEMO_WEBSITE_URL`
- `PROSPECT_DEFAULT_TARGET_CRITERIA`
- `PROSPECT_DEFAULT_OUTREACH_GOAL`
- `PROSPECT_MAX_PAGES_TO_SCRAPE`
- `PROSPECT_MANUAL_REVIEW_MINUTES`
- `PROSPECT_FIT_STATUS_RULES_JSON`
- `GOOGLE_SHEET_ID`
- `GOOGLE_SERVICE_ACCOUNT_JSON_PATH` or `GOOGLE_SERVICE_ACCOUNT_JSON`
- `GOOGLE_SERVICE_ACCOUNT_EMAIL`
- `GMAIL_SENDER_EMAIL`
- `PROSPECT_EMAIL_SEND_METHOD`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_SENDER_EMAIL`
- `SMTP_USE_TLS`
- `SMTP_USE_SSL`
- `OPENAI_API_KEY`
- `AI_MODEL`
- `SCRAPING_TIMEOUT_SECONDS`
- `CAPTCHA_END_MESSAGE`

Google Sheets setup:

1. Create a Google Cloud service account and enable the Google Sheets API.
2. Share your spreadsheet with the service account email from `GOOGLE_SERVICE_ACCOUNT_EMAIL`.
3. Create tabs named `Approved CRM`, `Rejected`, `Further Review`, `Analysis Logs`, `Errors`, and `Dashboard Metrics`.
4. Put the spreadsheet ID in `GOOGLE_SHEET_ID`.
5. Either save the service account JSON locally and set `GOOGLE_SERVICE_ACCOUNT_JSON_PATH`, or paste it into `GOOGLE_SERVICE_ACCOUNT_JSON`.

Email sending can use SMTP by setting `PROSPECT_EMAIL_SEND_METHOD=smtp`. For Gmail SMTP, use `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USE_TLS=true`, and put a Gmail app password in `SMTP_PASSWORD`. Gmail API sending remains available by setting `PROSPECT_EMAIL_SEND_METHOD=gmail_api`; Gmail API failures are shown in the dashboard and saved into the prospect state/logs.

### Reset dashboard analytics

```powershell
.\.venv\Scripts\python reset_dashboard_stats.py
```

## Output + State

- Text cards: `output/job_cards.txt`
- Dedup/state DB: `state/job_bot_state.db`
- Telegram subscriptions DB: `state/telegram_subscriptions.db`
- Per-user Telegram logs: `state/user_logs/{telegram_user_id}.txt`

## GitHub Safety

Do not commit `.env`, service account JSON files, local state databases, runtime logs, user logs, or generated output. The included `.gitignore` excludes those files. Keep `.env.example`, `state/.gitkeep`, and `output/.gitkeep` in the repository so fresh clones have the expected structure.

Each opportunity card can contain:

- title
- description
- scope summary
- client / requester
- location
- budget / rate / payment
- duration
- commitment level
- skills required
- proposal deadline
- start timeline
- engagement type
- industry / niche
- url
