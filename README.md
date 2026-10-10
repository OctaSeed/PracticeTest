# Lead Management and Automation System

A FastAPI lead-management service with HMAC-authenticated lead intake, SQLite persistence, duplicate protection, email-based upserts, deal-stage tracking, and Google Sheets sync using user OAuth.

## Features

- `POST /api/leads` accepts `id`, `name`, `email`, `company`, and `source`.
- HMAC-SHA256 signature verification using the `X-Lead-Signature: sha256=<hex>` header over the exact request body.
- Database uniqueness on partner lead ID and normalized email. Repeated payloads are no-ops; existing email matches update the record. Conflicting ID/email identities return HTTP 409.
- Deal stages: New, Qualified, Proposal, Won, Lost.
- Persistent SQLite storage and a dashboard protected by HTTP Basic authentication.
- Google OAuth offline access with a persisted refresh token, automatic access-token refresh, and Google Sheets upserts.
- Automated tests, Docker support, and a GitHub Actions test workflow.

## Quick start

Extract the project bundle, then run:

```bash
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
```

Set a strong `LEAD_SHARED_SECRET` and `ADMIN_PASSWORD` in `.env`, then launch:

```bash
uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
```

Open http://localhost:8000/ for the dashboard and http://localhost:8000/docs for the API docs.

## Tests

```bash
pytest -q
```

The test suite covers invalid signatures, duplicate submissions, updates by existing email, payload validation, stage changes, authentication, OAuth state replay protection, and conflicting identities.

## Google Sheets setup

Create a Google Cloud OAuth client of type **Web application**, enable the Google Sheets API, and add the redirect URI `http://localhost:8000/oauth/google/callback`. Save the client JSON as `credentials.json` (do not commit it), set `GOOGLE_CLIENT_SECRETS_FILE`, `GOOGLE_REDIRECT_URI`, and `GOOGLE_SHEETS_SPREADSHEET_ID` in `.env`, then use **Connect Google** from the dashboard. The app requests offline access and stores OAuth credentials in SQLite so it can refresh access tokens on future days without asking the user to sign in each time. Google can still revoke a refresh token, in which case the account must be reconnected.

## Security notes

- Never commit `.env`, `credentials.json`, `token.json`, or SQLite database files.
- Use unique secrets and HTTPS before exposing the service outside a trusted local environment.
- The partner website must sign the exact raw JSON bytes it sends; do not parse and reserialize the body before calculating its HMAC.
- SQLite is suitable for a small free deployment. For a multi-instance production deployment, consider managed PostgreSQL, a background job queue for reliable Sheets retries, structured audit logging, and encrypted credential storage.

See the detailed README inside the project bundle for the full API reference and integration walkthrough.
