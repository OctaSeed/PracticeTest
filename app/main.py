from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

STAGES = ("New", "Qualified", "Proposal", "Won", "Lost")
HEADERS = ["Lead ID", "Name", "Email", "Company", "Source", "Deal Stage", "Created At", "Updated At"]
SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/spreadsheets"]
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class LeadIn(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    company: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=200)

    @field_validator("id", "name", "email", "company", "source")
    @classmethod
    def trim_strings(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Must not be blank")
        return value

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_RE.fullmatch(value):
            raise ValueError("Must be a valid email")
        return value


class StageUpdate(BaseModel):
    stage: Literal["New", "Qualified", "Proposal", "Won", "Lost"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(db(path)) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
          pk INTEGER PRIMARY KEY AUTOINCREMENT, external_id TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL, email TEXT NOT NULL, email_normalized TEXT NOT NULL UNIQUE,
          company TEXT NOT NULL, source TEXT NOT NULL,
          stage TEXT NOT NULL DEFAULT 'New' CHECK(stage IN ('New','Qualified','Proposal','Won','Lost')),
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          sheets_sync_status TEXT NOT NULL DEFAULT 'pending' CHECK(sheets_sync_status IN ('pending','synced','failed')),
          sheets_sync_error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_leads_updated ON leads(updated_at DESC);
        CREATE TABLE IF NOT EXISTS google_credentials (
          singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
          credentials_json TEXT NOT NULL, connected_email TEXT, connected_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS oauth_states (state TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        """)
        c.commit()


def store_oauth_state(path: str, state: str) -> None:
    stamp = now()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    with closing(db(path)) as c:
        c.execute("DELETE FROM oauth_states WHERE created_at < ?", (cutoff,))
        c.execute("INSERT INTO oauth_states(state,created_at) VALUES (?,?)", (state, stamp))
        c.commit()


def consume_oauth_state(path: str, state: str) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    with closing(db(path)) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT state FROM oauth_states WHERE state=? AND created_at>=?", (state, cutoff)).fetchone()
        if row is None:
            c.rollback()
            return False
        c.execute("DELETE FROM oauth_states WHERE state=?", (state,))
        c.commit()
        return True


def upsert(path: str, lead: LeadIn) -> tuple[dict[str, Any], str]:
    values = {
        "external_id": lead.id, "name": lead.name, "email": lead.email,
        "email_normalized": lead.email.lower(), "company": lead.company, "source": lead.source,
    }
    stamp = now()
    with closing(db(path)) as c:
        c.execute("BEGIN IMMEDIATE")
        by_id = c.execute("SELECT * FROM leads WHERE external_id=?", (lead.id,)).fetchone()
        by_email = c.execute("SELECT * FROM leads WHERE email_normalized=?", (lead.email.lower(),)).fetchone()
        if by_id and by_email and by_id["pk"] != by_email["pk"]:
            c.rollback()
            raise HTTPException(409, "Lead ID and email refer to different records.")
        existing = by_id or by_email
        if existing is None:
            c.execute("""INSERT INTO leads(external_id,name,email,email_normalized,company,source,stage,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,'New',?,?)""",
                      (lead.id, lead.name, lead.email, lead.email.lower(), lead.company, lead.source, stamp, stamp))
            pk, action = c.execute("SELECT last_insert_rowid()").fetchone()[0], "created"
        else:
            pk = existing["pk"]
            changed = any(existing[k] != v for k, v in values.items() if k != "external_id")
            if not changed:
                action = "duplicate"
            else:
                c.execute("""UPDATE leads SET name=?,email=?,email_normalized=?,company=?,source=?,updated_at=?,
                              sheets_sync_status='pending',sheets_sync_error=NULL WHERE pk=?""",
                          (lead.name, lead.email, lead.email.lower(), lead.company, lead.source, stamp, pk))
                action = "updated"
        c.commit()
        return dict(c.execute("SELECT * FROM leads WHERE pk=?", (pk,)).fetchone()), action


def save_credentials(path: str, creds: Any, email: str | None) -> None:
    with closing(db(path)) as c:
        old = c.execute("SELECT credentials_json,connected_email,connected_at FROM google_credentials WHERE singleton_id=1").fetchone()
        if not creds.refresh_token and old:
            token = json.loads(old["credentials_json"]).get("refresh_token")
            if token:
                creds.refresh_token = token
        if not creds.refresh_token:
            raise ValueError("Google did not return a refresh token. Revoke the app in Google Account security and reconnect.")
        c.execute("""INSERT INTO google_credentials VALUES(1,?,?,?)
                     ON CONFLICT(singleton_id) DO UPDATE SET credentials_json=excluded.credentials_json,
                     connected_email=excluded.connected_email,connected_at=excluded.connected_at""",
                  (creds.to_json(), email or (old["connected_email"] if old else None),
                   old["connected_at"] if old else now()))
        c.commit()


def google_status(path: str) -> dict[str, Any]:
    with closing(db(path)) as c:
        row = c.execute("SELECT connected_email,connected_at FROM google_credentials WHERE singleton_id=1").fetchone()
    return {"connected": bool(row), "email": row["connected_email"] if row else None,
            "connected_at": row["connected_at"] if row else None}


def get_credentials(path: str):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleRequest
    with closing(db(path)) as c:
        row = c.execute("SELECT credentials_json FROM google_credentials WHERE singleton_id=1").fetchone()
    if not row:
        return None
    creds = Credentials.from_authorized_user_info(json.loads(row["credentials_json"]), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        save_credentials(path, creds, None)
    return creds


def sync_sheets(path: str, spreadsheet_id: str | None, lead: dict[str, Any]) -> dict[str, str]:
    if not spreadsheet_id or not google_status(path)["connected"]:
        return {"status": "pending", "reason": "Connect Google and configure a spreadsheet ID"}
    from googleapiclient.discovery import build
    api = build("sheets", "v4", credentials=get_credentials(path), cache_discovery=False).spreadsheets().values()
    result = api.get(spreadsheetId=spreadsheet_id, range="A:H").execute()
    rows = result.get("values", [])
    if not rows:
        api.update(spreadsheetId=spreadsheet_id, range="A1:H1", valueInputOption="RAW",
                   body={"values": [HEADERS]}).execute()
        rows = [HEADERS]
    elif rows[0] != HEADERS:
        raise ValueError("The first worksheet row must contain the expected headers; see README.")
    row_num = next((n for n, row in enumerate(rows[1:], start=2) if row and str(row[0]) == str(lead["external_id"])), None)
    values = [[lead["external_id"], lead["name"], lead["email"], lead["company"], lead["source"],
               lead["stage"], lead["created_at"], lead["updated_at"]]]
    if row_num:
        api.update(spreadsheetId=spreadsheet_id, range=f"A{row_num}:H{row_num}", valueInputOption="RAW",
                   body={"values": values}).execute()
    else:
        api.append(spreadsheetId=spreadsheet_id, range="A:H", valueInputOption="RAW",
                   insertDataOption="INSERT_ROWS", body={"values": values}).execute()
    return {"status": "synced"}


HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lead Desk</title><style>
body{font:15px system-ui;margin:0;background:#f5f7fb;color:#172033}header{background:#111b31;color:white;padding:24px 6%;font-size:22px;font-weight:700}
main{max-width:1100px;margin:28px auto;padding:0 18px}.cards{display:flex;gap:14px;margin:20px 0}.card,section{background:white;border:1px solid #e1e6ef;border-radius:12px;padding:18px}.card{flex:1}.muted{color:#697589;font-size:13px}.num{font-size:28px;font-weight:700}section{padding:0;overflow:auto}h1{font-size:25px}table{width:100%;border-collapse:collapse;min-width:760px}th,td{text-align:left;padding:14px;border-bottom:1px solid #edf0f5}th{background:#fafbfc;color:#687386}button,a{background:#2855d9;color:white;border:0;border-radius:8px;padding:9px 13px;text-decoration:none;cursor:pointer}select{padding:7px;border:1px solid #ddd;border-radius:7px}.status{font-size:12px;color:#17684d}.integration{float:right}@media(max-width:600px){.cards{display:block}.card{margin:10px 0}.integration{float:none}}
</style></head><body><header>Lead Desk <span class="muted">/ Inbound lead management</span></header><main>
<div class="integration"><span id="gstatus">Google status loading</span> <a href="/oauth/google/start">Connect Google</a></div><h1>Pipeline overview</h1><p class="muted">Review inbound leads and update their deal stage.</p>
<div class="cards"><div class="card">Total leads<div class="num" id="total">—</div></div><div class="card">Open pipeline<div class="num" id="open">—</div></div><div class="card">Won<div class="num" id="won">—</div></div></div>
<section><table><thead><tr><th>Lead</th><th>Company</th><th>Source</th><th>Deal stage</th><th>Sheets sync</th></tr></thead><tbody id="rows"></tbody></table></section>
<p class="muted">Partner ingestion: POST /api/leads · HMAC-SHA256 signature required.</p></main><script>
const stages=['New','Qualified','Proposal','Won','Lost'];
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function load(){try{const a=await fetch('/api/leads'),b=await fetch('/api/integration/google');if(!a.ok||!b.ok)throw 0;const leads=await a.json(),g=await b.json();document.getElementById('total').textContent=leads.length;document.getElementById('open').textContent=leads.filter(x=>!['Won','Lost'].includes(x.stage)).length;document.getElementById('won').textContent=leads.filter(x=>x.stage==='Won').length;document.getElementById('gstatus').textContent=g.connected?'Google connected: '+(g.email||'account'):'Google not connected';document.getElementById('rows').innerHTML=leads.map(x=>'<tr><td><b>'+esc(x.name)+'</b><br><small>'+esc(x.email)+' · '+esc(x.external_id)+'</small></td><td>'+esc(x.company)+'</td><td>'+esc(x.source)+'</td><td><select aria-label="Deal stage for '+esc(x.name)+'" onchange="stage(\\''+encodeURIComponent(x.external_id)+'\\',this.value)">'+stages.map(s=>'<option '+(x.stage===s?'selected':'')+'>'+s+'</option>').join('')+'</select></td><td><span class="status">'+esc(x.sheets_sync_status)+'</span></td></tr>').join('')}catch(e){document.getElementById('rows').innerHTML='<tr><td colspan="5">Unable to load leads. Verify admin credentials.</td></tr>'}}
async function stage(id,value){await fetch('/api/leads/'+id+'/stage',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({stage:value})});load()}load();
</script></body></html>"""


def create_app(db_path: str | None = None, signing_secret: str | None = None,
               admin_username: str | None = None, admin_password: str | None = None,
               google_client_secrets_file: str | None = None, spreadsheet_id: str | None = None,
               google_redirect_uri: str | None = None) -> FastAPI:
    db_path = db_path or os.getenv("DATABASE_PATH", "./data/leads.db")
    signing_secret = signing_secret if signing_secret is not None else os.getenv("LEAD_SHARED_SECRET", "local-development-only-change-me")
    admin_username = admin_username if admin_username is not None else os.getenv("ADMIN_USERNAME", "admin")
    admin_password = admin_password if admin_password is not None else os.getenv("ADMIN_PASSWORD", "change-this-password")
    google_client_secrets_file = google_client_secrets_file or os.getenv("GOOGLE_CLIENT_SECRETS_FILE", "./credentials.json")
    spreadsheet_id = spreadsheet_id or os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
    google_redirect_uri = google_redirect_uri or os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/oauth/google/callback")
    init_db(db_path)
    app = FastAPI(title="Lead Management API", version="1.0.0")
    app.state.db_path, app.state.signing_secret = db_path, signing_secret.encode()
    app.state.admin_username, app.state.admin_password = admin_username, admin_password
    app.state.google_client_secrets_file, app.state.spreadsheet_id = google_client_secrets_file, spreadsheet_id
    app.state.google_redirect_uri = google_redirect_uri
    app.state.sheets_sync = lambda lead: sync_sheets(db_path, spreadsheet_id, lead)
    basic = HTTPBasic(auto_error=False)

    def require_admin(creds: HTTPBasicCredentials | None = Depends(basic)) -> str:
        user_ok = creds is not None and secrets.compare_digest(creds.username, app.state.admin_username)
        pass_ok = creds is not None and secrets.compare_digest(creds.password, app.state.admin_password)
        if not (user_ok and pass_ok):
            raise HTTPException(401, "Admin authentication required", headers={"WWW-Authenticate": "Basic"})
        return creds.username

    def sync_lead(lead: dict[str, Any]) -> dict[str, str]:
        try:
            result = app.state.sheets_sync(lead)
            state, error = result.get("status", "pending"), result.get("reason") or result.get("error")
        except Exception as exc:
            state, error = "failed", type(exc).__name__ + ": " + str(exc)
        with closing(db(app.state.db_path)) as c:
            c.execute("UPDATE leads SET sheets_sync_status=?,sheets_sync_error=? WHERE external_id=?",
                      (state, error[:500] if error and state == "failed" else None, lead["external_id"]))
            c.commit()
        return {"status": state, **({"reason": error} if error and state == "pending" else {})}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
    def dashboard():
        return HTML

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/api/leads", status_code=201)
    async def receive(request: Request, response: Response,
                      signature: str | None = Header(default=None, alias="X-Lead-Signature")):
        raw = await request.body()
        expected = "sha256=" + hmac.new(app.state.signing_secret, raw, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            raise HTTPException(401, "Invalid or missing lead signature")
        try:
            payload = LeadIn.model_validate_json(raw)
        except Exception as exc:
            raise HTTPException(422, "Invalid lead payload") from exc
        lead, action = upsert(app.state.db_path, payload)
        response.status_code = 201 if action == "created" else 200
        sync = {"status": lead["sheets_sync_status"]}
        if action != "duplicate":
            sync = sync_lead(lead)
            with closing(db(app.state.db_path)) as c:
                row = c.execute("SELECT * FROM leads WHERE pk=?", (lead["pk"],)).fetchone()
                lead = dict(row)
        return {"action": action, "lead": lead, "sheets_sync": sync}

    @app.get("/api/leads", dependencies=[Depends(require_admin)])
    def list_leads():
        with closing(db(app.state.db_path)) as c:
            return [dict(row) for row in c.execute("SELECT * FROM leads ORDER BY updated_at DESC,pk DESC").fetchall()]

    @app.patch("/api/leads/{external_id}/stage", dependencies=[Depends(require_admin)])
    def update_stage(external_id: str, body: StageUpdate):
        with closing(db(app.state.db_path)) as c:
            row = c.execute("SELECT * FROM leads WHERE external_id=?", (external_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Lead not found")
            if row["stage"] == body.stage:
                return {"action": "unchanged", "lead": dict(row)}
            c.execute("UPDATE leads SET stage=?,updated_at=?,sheets_sync_status='pending',sheets_sync_error=NULL WHERE external_id=?",
                      (body.stage, now(), external_id))
            c.commit()
            lead = dict(c.execute("SELECT * FROM leads WHERE external_id=?", (external_id,)).fetchone())
        sync = sync_lead(lead)
        with closing(db(app.state.db_path)) as c:
            lead = dict(c.execute("SELECT * FROM leads WHERE external_id=?", (external_id,)).fetchone())
        return {"action": "updated", "lead": lead, "sheets_sync": sync}

    @app.get("/api/integration/google", dependencies=[Depends(require_admin)])
    def integration_status():
        return google_status(app.state.db_path)

    def make_flow(state: str | None = None):
        from google_auth_oauthlib.flow import Flow
        path = Path(app.state.google_client_secrets_file)
        if not path.is_file():
            raise HTTPException(503, "Google OAuth client file not found: " + str(path))
        flow = Flow.from_client_secrets_file(str(path), scopes=SCOPES, state=state)
        flow.redirect_uri = app.state.google_redirect_uri
        return flow

    @app.get("/oauth/google/start", dependencies=[Depends(require_admin)])
    def oauth_start():
        if not app.state.spreadsheet_id:
            raise HTTPException(503, "Set GOOGLE_SHEETS_SPREADSHEET_ID before connecting Google Sheets")
        flow = make_flow()
        url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
        store_oauth_state(app.state.db_path, state)
        return RedirectResponse(url)

    @app.get("/oauth/google/callback", dependencies=[Depends(require_admin)])
    def oauth_callback(request: Request, state: str | None = None, error: str | None = None):
        if error:
            raise HTTPException(400, "Google OAuth was not completed: " + error)
        if not state or not consume_oauth_state(app.state.db_path, state):
            raise HTTPException(400, "OAuth state is invalid, expired, or already used. Restart Google sign-in.")
        flow = make_flow(state)
        flow.fetch_token(authorization_response=str(request.url))
        creds, email = flow.credentials, None
        try:
            from googleapiclient.discovery import build
            email = build("oauth2", "v2", credentials=creds, cache_discovery=False).userinfo().get().execute().get("email")
        except Exception:
            pass
        try:
            save_credentials(app.state.db_path, creds, email)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        retry_sync()
        return RedirectResponse("/", status_code=303)

    @app.post("/api/sheets/retry", dependencies=[Depends(require_admin)])
    def retry_sync():
        with closing(db(app.state.db_path)) as c:
            rows = c.execute("SELECT * FROM leads WHERE sheets_sync_status!='synced' ORDER BY pk").fetchall()
        counts = {"synced": 0, "failed": 0, "pending": 0}
        for row in rows:
            counts[sync_lead(dict(row))["status"]] += 1
        return counts

    return app


app = create_app()
