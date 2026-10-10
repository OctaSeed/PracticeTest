# Lead Management and Automation System

A FastAPI lead-management service with HMAC-authenticated lead intake, SQLite persistence, duplicate protection, email-based upserts, deal-stage tracking, and Google Sheets sync using user OAuth.

## Features

- \`POST /api/leads\` accepts \`id\`, \`name\`, \`email\`, \`company\`, and \`source\`.
- HMAC-SHA256 signature verification using the \`X-Lead-Signature: sha256=<hex>\` header over the exact request body.
- Database uniqueness on partner lead ID and normalized email. Repeated payloads are no-ops; existing email matches update the record. Conflicting ID/email identities return HTTP 409.
- Deal stages: New, Qualified, Proposal, Won, Lost.
- Persistent SQLite storage and a dashboard protected by HTTP Basic authentication.
- Google OAuth offline access with a persisted refresh token, automatic access-token refresh, and Google Sheets upserts.
- Automated tests, Docker support, and a GitHub Actions test workflow.

## Quick start

Clone the repo, then run:

\`\`\`bash
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
\`\`\`

Set a strong \`LEAD_SHARED_SECRET\` and \`ADMIN_PASSWORD\` in \`.env\`, then launch:

\`\`\`bash
uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
\`\`\`

Open http://localhost:8000/ for the dashboard, http://localhost:8000/docs for API docs, and http://localhost:8000/healthz for the health check.

## Send a signed lead

The signature is HMAC-SHA256 over the exact raw JSON request body. Example:

\`\`\`bash
export LEAD_SHARED_SECRET='replace-this-with-your-configured-secret'
python scripts/demo.py
\`\`\`

The script posts an example lead to the running local server. New records return HTTP 201; updates and accepted duplicate no-ops return HTTP 200. Invalid signatures return 401, invalid input returns 422, and conflicting IDs/emails return 409.

## API reference

| Method | Path | Authentication | Purpose |
| --- | --- | --- | --- |
| POST | \`/api/leads\` | HMAC signature | Receive or update an inbound lead |
| GET | \`/api/leads\` | HTTP Basic | List lead records |
| PATCH | \`/api/leads/{external_id}/stage\` | HTTP Basic | Change deal stage |
| GET | \`/api/integration/google\` | HTTP Basic | Read Google connection status |
| POST | \`/api/sheets/retry\` | HTTP Basic | Retry pending/failed sheet updates |
| GET | \`/oauth/google/start\` | HTTP Basic | Start Google user OAuth |
| GET | \`/oauth/google/callback\` | HTTP Basic + OAuth state | Complete OAuth sign-in |
| GET | \`/healthz\` | None | Service health check |

## Google Sheets OAuth setup

1. In Google Cloud Console, select/create a project and enable the **Google Sheets API**.
2. Configure the OAuth consent screen; add your account as a test user if the app is in testing mode.
3. Create an OAuth client ID of type **Web application** and add this exact authorized redirect URI: \`http://localhost:8000/oauth/google/callback\`.
4. Download the client-secret JSON into the project root as \`credentials.json\`. Do not commit this file.
5. Set \`GOOGLE_CLIENT_SECRETS_FILE=./credentials.json\`, \`GOOGLE_REDIRECT_URI=http://localhost:8000/oauth/google/callback\`, and \`GOOGLE_SHEETS_SPREADSHEET_ID\` in \`.env\`. The Google account needs edit access to the sheet.
6. Start the app and click **Connect Google** in the dashboard.

The first worksheet should have these column headers in row 1: \`Lead ID\`, \`Name\`, \`Email\`, \`Company\`, \`Source\`, \`Deal Stage\`, \`Created At\`, \`Updated At\`. Empty worksheets are initialized automatically. Each create/update/stage change synchronizes the matching row by canonical lead ID. If Google is disconnected or unavailable, lead intake still stores the record and records a pending/failed sync state.

OAuth requests offline access and persists the refresh token in SQLite. The service refreshes expired access tokens automatically, so normal next-day synchronization does not require another interactive sign-in. Google may revoke refresh tokens; reconnect if that happens. Keep the database file private because it holds OAuth credentials.

## Tests

\`\`\`bash
pytest -q
\`\`\`

The tests cover invalid signatures, duplicate submissions, updates by existing email, malformed input, stage changes, admin authentication, single-use OAuth state, and conflicting identities. GitHub Actions runs this suite on pushes and pull requests.

## Design choices and next improvements

SQLite keeps the project free and simple to run locally. HMAC over the raw body prevents unauthenticated lead injection, and database uniqueness constraints provide the final guard against duplicates. Leads are committed before attempting the external Sheets API so a temporary Google outage does not lose incoming data.

For production, I would add a durable retry queue with backoff, encrypted-at-rest OAuth secrets, structured audit logs, rate limiting, stronger admin identity management, managed PostgreSQL for concurrent/multi-instance deployment, and integration tests against a dedicated Google test sheet.

## Security

Never commit \`.env\`, \`credentials.json\`, token files, or local SQLite databases. Change the example secrets and passwords before running the app. Use HTTPS and a trusted reverse proxy before exposing the app beyond localhost. The separate demo recording is supplied with the submission.
