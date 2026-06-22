import os
import time
import json
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import ntplib
from fastapi import FastAPI, Request, Depends, HTTPException, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
import uvicorn

from .models import engine, Base, get_db, PublishedAlert, AlertDraft, MonitoredTarget, AppConfig, User, ScanLog
from . import auth
from .tasks import scan_single_target

Base.metadata.create_all(bind=engine)
app = FastAPI(title="RedTape Radar")

@app.on_event("startup")
async def _init_time_sync():
    from .models import SessionLocal
    db = SessionLocal()
    try:
        cfg = {c.key: c.value for c in db.query(AppConfig).filter(AppConfig.key.in_(["use_ntp", "manual_time"])).all()}
    finally:
        db.close()
    if cfg.get("use_ntp") == "true":
        _sync_ntp()
    elif cfg.get("manual_time"):
        _apply_manual_time(cfg["manual_time"])

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# --- App-level NTP offset (seconds to add to utcnow() to get NTP-corrected time) ---
_ntp_offset: float = 0.0

def _sync_ntp() -> tuple[bool, str]:
    global _ntp_offset
    try:
        resp = ntplib.NTPClient().request('pool.ntp.org', version=3)
        _ntp_offset = resp.offset
        return True, ""
    except Exception as e:
        return False, str(e)[:200]

def _apply_manual_time(manual_time_str: str) -> tuple[bool, str]:
    global _ntp_offset
    try:
        manual_dt = datetime.strptime(manual_time_str, '%Y-%m-%d %H:%M:%S')
        _ntp_offset = (manual_dt - datetime.utcnow()).total_seconds()
        return True, ""
    except Exception as e:
        return False, str(e)[:200]

def _format_dt(dt: datetime, fmt: str = '24h', tz_name: str = 'UTC', include_seconds: bool = True) -> str:
    try:
        aware = dt.replace(tzinfo=dt_timezone.utc).astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, Exception):
        aware = dt
    time_part = ('%I:%M:%S %p' if include_seconds else '%I:%M %p') if fmt == '12h' else ('%H:%M:%S' if include_seconds else '%H:%M')
    return aware.strftime('%Y-%m-%d ' + time_part)

templates.env.filters['format_dt'] = _format_dt

# --- Constants ---
_SECRET_KEYS = {"openai_api_key", "claude_api_key", "gemini_api_key", "smtp_pass", "confluence_api_token"}
CERT_DIR = "certs"

# --- Rate limiter (failed login attempts per IP) ---
_login_attempts: dict = defaultdict(list)
_RATE_LIMIT_ATTEMPTS = 5
_RATE_LIMIT_WINDOW = 300  # 5 minutes

def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    cutoff = now - _RATE_LIMIT_WINDOW
    _login_attempts[ip] = [t for t in _login_attempts[ip] if t > cutoff]
    return len(_login_attempts[ip]) >= _RATE_LIMIT_ATTEMPTS

def _record_failed_login(ip: str):
    _login_attempts[ip].append(time.time())

# --- Confluence integration ---
def _post_to_confluence(config: dict, topic: str, summary: str, resource: str, url: str):
    base_url = config.get("confluence_url", "").rstrip("/")
    email = config.get("confluence_email")
    token = config.get("confluence_api_token")
    space_key = config.get("confluence_space_key")
    if not all([base_url, email, token, space_key]):
        return
    try:
        import requests as req
        body_html = (
            f"<h2>Source</h2><p><a href='{url}'>{resource}</a></p>"
            f"<h2>Summary</h2><p>{summary}</p>"
        )
        req.post(
            f"{base_url}/wiki/rest/api/content",
            auth=(email, token),
            json={
                "type": "page",
                "title": f"[RedTape Alert] {topic}",
                "space": {"key": space_key},
                "body": {"storage": {"value": body_html, "representation": "storage"}},
            },
            timeout=15,
        )
    except Exception as e:
        print(f"Confluence post failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Exception handlers
# ─────────────────────────────────────────────────────────────────────────────

@app.exception_handler(401)
async def unauthorized_redirect(request: Request, exc: HTTPException):
    return RedirectResponse(url="/local-login", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Page routes
# ─────────────────────────────────────────────────────────────────────────────

def _get_time_config(db: Session) -> dict:
    keys = ["timezone", "time_format"]
    rows = {c.key: c.value for c in db.query(AppConfig).filter(AppConfig.key.in_(keys)).all()}
    return {"timezone": rows.get("timezone", "UTC"), "time_format": rows.get("time_format", "24h")}


@app.get("/", response_class=HTMLResponse)
async def view_dashboard(request: Request, db: Session = Depends(get_db), current_user: User = Depends(auth.get_current_user)):
    alerts = db.query(PublishedAlert).order_by(PublishedAlert.published_at.desc()).limit(50).all()
    logs = db.query(ScanLog).order_by(ScanLog.timestamp.desc()).limit(20).all()
    time_cfg = _get_time_config(db)
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "user": current_user, "alerts": alerts, "logs": logs,
        "time_format": time_cfg["time_format"], "timezone": time_cfg["timezone"],
    })


@app.get("/triage", response_class=HTMLResponse)
async def view_triage_inbox(request: Request, db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)):
    drafts = db.query(AlertDraft).filter(AlertDraft.is_reviewed == False).all()
    return templates.TemplateResponse(request=request, name="triage.html", context={"user": admin_user, "drafts": drafts})


@app.get("/settings", response_class=HTMLResponse)
async def view_settings(request: Request, db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)):
    users = db.query(User).all()
    targets = db.query(MonitoredTarget).all()
    configs = db.query(AppConfig).all()
    settings_dict = {cfg.key: cfg.value for cfg in configs}
    defaults = {
        "llm_provider": "local", "local_model_name": "llama3",
        "openai_api_key": "", "gemini_api_key": "", "claude_api_key": "",
        "enable_emails": "false", "smtp_server": "", "smtp_port": "587",
        "smtp_user": "", "smtp_pass": "", "alert_email": "",
        "use_ntp": "true", "timezone": "UTC", "time_format": "24h",
        "confluence_url": "", "confluence_email": "",
        "confluence_api_token": "", "confluence_space_key": "",
    }
    current_settings = {**defaults, **settings_dict}
    # Track which secrets are already saved, then strip their values from the context
    saved_secrets = {k for k in _SECRET_KEYS if settings_dict.get(k)}
    for k in _SECRET_KEYS:
        current_settings[k] = ""
    return templates.TemplateResponse(request=request, name="settings.html", context={
        "user": admin_user, "settings": current_settings,
        "system_users": users, "targets": targets, "saved_secrets": saved_secrets,
    })


@app.get("/local-login", response_class=HTMLResponse)
async def view_local_login(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"user": None, "error": None})


# ─────────────────────────────────────────────────────────────────────────────
# Auth routes
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/local-login")
async def process_local_login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    client_ip = request.client.host
    if _is_rate_limited(client_ip):
        return templates.TemplateResponse(request=request, name="login.html",
            context={"user": None, "error": "Too many failed login attempts. Please wait 5 minutes."})

    user = db.query(User).filter(User.email == email).first()
    if not user or not auth.verify_password(password, user.hashed_password):
        _record_failed_login(client_ip)
        return templates.TemplateResponse(request=request, name="login.html", context={"user": None, "error": "Invalid credentials"})

    if user.must_change_password:
        return templates.TemplateResponse(request=request, name="reset_password.html", context={"email": user.email})

    token = auth.create_local_token(user.email)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="local_session", value=token, httponly=True, max_age=86400)
    return response


@app.post("/api/reset-password")
async def execute_password_reset(request: Request, email: str = Form(...), old_password: str = Form(...), new_password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user or not auth.verify_password(old_password, user.hashed_password):
        return templates.TemplateResponse(request=request, name="reset_password.html", context={"email": email, "error": "Old password incorrect."})

    user.hashed_password = auth.get_password_hash(new_password)
    user.must_change_password = False
    db.commit()

    token = auth.create_local_token(user.email)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="local_session", value=token, httponly=True, max_age=86400)
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/local-login", status_code=303)
    response.delete_cookie("local_session")
    return response


# ─────────────────────────────────────────────────────────────────────────────
# User management
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/users/create")
async def create_user(email: str = Form(...), name: str = Form(...), role: str = Form("read_only"), password: str = Form(...), db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)):
    if not db.query(User).filter(User.email == email).first():
        db.add(User(email=email, name=name, role=role, hashed_password=auth.get_password_hash(password), is_local=True))
        db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/api/users/{target_id}/force-reset")
async def force_user_reset(target_id: int, db: Session = Depends(get_db), admin: User = Depends(auth.require_admin)):
    target = db.query(User).filter(User.id == target_id).first()
    if target:
        target.must_change_password = True
        db.commit()
    return RedirectResponse(url="/settings", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/settings/update")
async def update_settings(request: Request, db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)):
    form_data = await request.form()
    for key, value in form_data.items():
        if key in ["use_ntp", "manual_time", "timezone", "time_format"]:
            continue  # Managed by the separate time form
        if key in _SECRET_KEYS and not value:
            continue  # Preserve existing secret when field left blank
        config_item = db.query(AppConfig).filter(AppConfig.key == key).first()
        if config_item:
            config_item.value = str(value)
        else:
            db.add(AppConfig(key=key, value=str(value), is_secret=(key in _SECRET_KEYS)))
    db.commit()
    return RedirectResponse(url="/settings?success=true", status_code=303)


# App-level time controller (no OS/timedatectl dependency)
@app.post("/api/system/time")
async def update_system_time(
    use_ntp: str = Form("false"), manual_time: str = Form(""), timezone: str = Form("UTC"),
    time_format: str = Form("24h"),
    db: Session = Depends(get_db),
    admin_user: User = Depends(auth.require_admin),
):
    ntp_enabled = use_ntp == "true"
    error_msg = None

    if ntp_enabled:
        ok, err = _sync_ntp()
        if not ok:
            error_msg = f"NTP sync failed: {err}"
    elif manual_time:
        ok, err = _apply_manual_time(manual_time)
        if not ok:
            error_msg = f"Invalid manual time: {err}"

    for key, value in [
        ("use_ntp", "true" if ntp_enabled else "false"),
        ("timezone", timezone),
        ("time_format", time_format),
        ("manual_time", manual_time),
    ]:
        cfg = db.query(AppConfig).filter(AppConfig.key == key).first()
        if cfg:
            cfg.value = value
        else:
            db.add(AppConfig(key=key, value=value, is_secret=False))
    db.commit()

    if error_msg:
        return RedirectResponse(url=f"/settings?error={urllib.parse.quote(error_msg)}", status_code=303)
    return RedirectResponse(url="/settings?success=true", status_code=303)


@app.get("/api/clock-config")
async def get_clock_config(db: Session = Depends(get_db)):
    keys = ["timezone", "time_format"]
    configs = {cfg.key: cfg.value for cfg in db.query(AppConfig).filter(AppConfig.key.in_(keys)).all()}
    return {"timezone": configs.get("timezone", "UTC"), "time_format": configs.get("time_format", "24h")}


@app.get("/api/settings/export")
async def export_settings(db: Session = Depends(get_db), admin: User = Depends(auth.require_admin)):
    configs = db.query(AppConfig).all()
    targets = db.query(MonitoredTarget).all()
    export_data = {
        "config": {c.key: c.value for c in configs},
        "targets": [
            {"resource": t.resource, "url": t.url, "extraction_mode": t.extraction_mode,
             "scan_frequency": t.scan_frequency, "recursive": t.recursive}
            for t in targets
        ],
    }
    return Response(
        content=json.dumps(export_data, indent=4),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=redtape_backup.json"},
    )


@app.post("/api/settings/import")
async def import_settings(backup_file: UploadFile = File(...), db: Session = Depends(get_db), admin: User = Depends(auth.require_admin)):
    content = await backup_file.read()
    try:
        data = json.loads(content)
        if "config" in data:
            for key, value in data["config"].items():
                cfg = db.query(AppConfig).filter(AppConfig.key == key).first()
                if cfg:
                    cfg.value = value
                else:
                    db.add(AppConfig(key=key, value=value, is_secret=(key in _SECRET_KEYS)))
        if "targets" in data:
            for t in data["targets"]:
                existing = db.query(MonitoredTarget).filter(MonitoredTarget.url == t.get("url")).first()
                if not existing:
                    db.add(MonitoredTarget(
                        resource=t.get("resource"), url=t.get("url"),
                        extraction_mode=t.get("extraction_mode", "auto_clean"),
                        scan_frequency=t.get("scan_frequency", "weekly"),
                        recursive=t.get("recursive", False),
                    ))
        db.commit()
    except Exception as e:
        print(f"Import failed: {e}")
    return RedirectResponse(url="/settings?success=true", status_code=303)


@app.post("/api/settings/upload-cert")
async def upload_certificate(
    cert_file: UploadFile = File(...),
    key_file: UploadFile = File(...),
    admin: User = Depends(auth.require_admin),
):
    os.makedirs(CERT_DIR, exist_ok=True)
    for filename, upload in [("server.crt", cert_file), ("server.key", key_file)]:
        content = await upload.read()
        with open(os.path.join(CERT_DIR, filename), "wb") as f:
            f.write(content)
    return RedirectResponse(
        url="/settings?success=Certificate+uploaded.+Restart+the+service+to+apply+the+new+cert.",
        status_code=303,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Targets
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/targets")
async def add_monitored_target(
    url: str = Form(...), resource: str = Form(...), mode: str = Form("auto_clean"),
    frequency: str = Form("weekly"), recursive: str = Form("false"),
    db: Session = Depends(get_db), editor_user: User = Depends(auth.require_editor),
):
    new_target = MonitoredTarget(
        url=url, resource=resource, extraction_mode=mode,
        scan_frequency=frequency, recursive=(recursive == "true"),
    )
    db.add(new_target)
    db.commit()
    scan_single_target.delay(new_target.id)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/api/targets/{target_id}/delete")
async def delete_monitored_target(target_id: int, db: Session = Depends(get_db), editor_user: User = Depends(auth.require_editor)):
    target = db.query(MonitoredTarget).filter(MonitoredTarget.id == target_id).first()
    if target:
        db.delete(target)
        db.commit()
    return RedirectResponse(url="/settings", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Triage
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/drafts/{draft_id}/approve")
async def approve_ai_draft(
    draft_id: int, actionable_steps: str = Form(...), key_deadlines: str = Form(""),
    db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin),
):
    draft = db.query(AlertDraft).filter(AlertDraft.id == draft_id).first()
    if not draft:
        raise HTTPException(status_code=404)
    db.add(PublishedAlert(
        resource=draft.target.resource, url=draft.target.url,
        topic=draft.topic, summary=draft.summary_raw,
        actionable_steps=actionable_steps, key_deadlines=key_deadlines,
    ))
    draft.is_reviewed = True
    db.commit()
    config = {cfg.key: cfg.value for cfg in db.query(AppConfig).all()}
    _post_to_confluence(config, draft.topic, draft.summary_raw, draft.target.resource, draft.target.url)
    return RedirectResponse(url="/triage", status_code=303)


@app.post("/api/drafts/{draft_id}/dismiss")
async def dismiss_ai_draft(draft_id: int, db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)):
    draft = db.query(AlertDraft).filter(AlertDraft.id == draft_id).first()
    if not draft:
        raise HTTPException(status_code=404)
    draft.is_reviewed = True
    db.commit()
    return RedirectResponse(url="/triage", status_code=303)
