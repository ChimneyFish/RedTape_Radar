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
from onelogin.saml2.auth import OneLogin_Saml2_Auth
import uvicorn

from .models import engine, Base, get_db, PublishedAlert, AlertDraft, MonitoredTarget, AppConfig, User, ScanLog
from . import auth
from .migrations import run_migrations
from . import saml as saml_util
from .tasks import scan_single_target

run_migrations(engine, Base)
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


def _get_saml_config(db: Session) -> dict:
    rows = {c.key: c.value for c in db.query(AppConfig).filter(AppConfig.key.in_(saml_util.SAML_CONFIG_KEYS)).all()}
    return {**saml_util.SAML_CONFIG_DEFAULTS, **rows}


def _saml_enabled(db: Session) -> bool:
    cfg = db.query(AppConfig).filter(AppConfig.key == "saml_enabled").first()
    return bool(cfg and cfg.value == "true")


@app.get("/", response_class=HTMLResponse)
async def view_dashboard(request: Request, db: Session = Depends(get_db), current_user: User = Depends(auth.get_current_user)):
    alerts = db.query(PublishedAlert).order_by(PublishedAlert.published_at.desc()).limit(50).all()
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "user": current_user, "alerts": alerts,
    })


@app.get("/activity", response_class=HTMLResponse)
async def view_activity_logs(request: Request, db: Session = Depends(get_db), current_user: User = Depends(auth.get_current_user)):
    logs = db.query(ScanLog).order_by(ScanLog.timestamp.desc()).limit(200).all()
    time_cfg = _get_time_config(db)
    return templates.TemplateResponse(request=request, name="activity.html", context={
        "user": current_user, "logs": logs,
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
        **saml_util.SAML_CONFIG_DEFAULTS,
    }
    current_settings = {**defaults, **settings_dict}
    # Track which secrets are already saved, then strip their values from the context
    saved_secrets = {k for k in _SECRET_KEYS if settings_dict.get(k)}
    for k in _SECRET_KEYS:
        current_settings[k] = ""
    base_url = str(request.base_url).rstrip("/")
    return templates.TemplateResponse(request=request, name="settings.html", context={
        "user": admin_user, "settings": current_settings,
        "system_users": users, "targets": targets, "saved_secrets": saved_secrets,
        "sp_entity_id": f"{base_url}/saml/metadata", "sp_acs_url": f"{base_url}/saml/acs",
    })


@app.get("/local-login", response_class=HTMLResponse)
async def view_local_login(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request=request, name="login.html",
        context={"user": None, "error": None, "saml_enabled": _saml_enabled(db)})


# ─────────────────────────────────────────────────────────────────────────────
# Auth routes
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/local-login")
async def process_local_login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    client_ip = request.client.host
    saml_enabled = _saml_enabled(db)
    if _is_rate_limited(client_ip):
        return templates.TemplateResponse(request=request, name="login.html",
            context={"user": None, "error": "Too many failed login attempts. Please wait 5 minutes.", "saml_enabled": saml_enabled})

    user = db.query(User).filter(User.email == email).first()
    if not user or not user.hashed_password or not auth.verify_password(password, user.hashed_password):
        _record_failed_login(client_ip)
        return templates.TemplateResponse(request=request, name="login.html",
            context={"user": None, "error": "Invalid credentials", "saml_enabled": saml_enabled})

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
# SSO (SAML / Microsoft Entra ID)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/saml/metadata")
async def saml_sp_metadata(request: Request, db: Session = Depends(get_db)):
    config = _get_saml_config(db)
    base_url = str(request.base_url).rstrip("/")
    metadata, errors = saml_util.build_sp_metadata_xml(config, base_url)
    if errors:
        raise HTTPException(status_code=500, detail=f"Invalid SP metadata: {', '.join(errors)}")
    return Response(
        content=metadata, media_type="application/xml",
        headers={"Content-Disposition": "attachment; filename=redtape_radar_sp_metadata.xml"},
    )


@app.get("/saml/login")
async def saml_login(request: Request, db: Session = Depends(get_db)):
    config = _get_saml_config(db)
    if config.get("saml_enabled") != "true":
        raise HTTPException(status_code=404)
    req_data = await saml_util.prepare_fastapi_request(request)
    base_url = str(request.base_url).rstrip("/")
    saml_auth = OneLogin_Saml2_Auth(req_data, saml_util.build_saml_settings(config, base_url))
    return RedirectResponse(url=saml_auth.login(), status_code=303)


@app.post("/saml/acs")
async def saml_acs(request: Request, db: Session = Depends(get_db)):
    config = _get_saml_config(db)
    if config.get("saml_enabled") != "true":
        raise HTTPException(status_code=404)
    saml_enabled = True

    req_data = await saml_util.prepare_fastapi_request(request)
    base_url = str(request.base_url).rstrip("/")
    saml_auth = OneLogin_Saml2_Auth(req_data, saml_util.build_saml_settings(config, base_url))
    saml_auth.process_response()
    errors = saml_auth.get_errors()
    if errors or not saml_auth.is_authenticated():
        reason = saml_auth.get_last_error_reason() or ", ".join(errors) or "Unknown error"
        return templates.TemplateResponse(request=request, name="login.html",
            context={"user": None, "error": f"SSO login failed: {reason}", "saml_enabled": saml_enabled})

    email = saml_auth.get_nameid()
    if not email:
        return templates.TemplateResponse(request=request, name="login.html",
            context={"user": None, "error": "SSO login failed: no email/NameID returned by the identity provider.", "saml_enabled": saml_enabled})

    display_name = saml_util.extract_display_name(saml_auth.get_attributes())

    user = db.query(User).filter(User.email == email).first()
    if not user:
        if config.get("saml_auto_provision") != "true":
            return templates.TemplateResponse(request=request, name="login.html",
                context={"user": None, "error": f"No account provisioned for {email}. Contact your administrator.", "saml_enabled": saml_enabled})
        user = User(
            email=email, name=display_name or email, role=config.get("saml_default_role", "read_only"),
            is_local=False, is_active=True,
        )
        db.add(user)
    elif not user.is_active:
        return templates.TemplateResponse(request=request, name="login.html",
            context={"user": None, "error": "This account has been disabled.", "saml_enabled": saml_enabled})
    elif display_name and not user.name:
        user.name = display_name

    user.last_login = datetime.utcnow()
    db.commit()

    token = auth.create_local_token(user.email)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="local_session", value=token, httponly=True, max_age=86400)
    return response


@app.get("/saml/sls")
async def saml_sls(request: Request, db: Session = Depends(get_db)):
    config = _get_saml_config(db)
    if config.get("saml_enabled") != "true":
        raise HTTPException(status_code=404)
    req_data = await saml_util.prepare_fastapi_request(request)
    base_url = str(request.base_url).rstrip("/")
    saml_auth = OneLogin_Saml2_Auth(req_data, saml_util.build_saml_settings(config, base_url))
    redirect_url = saml_auth.process_slo(delete_session_cb=lambda: None)
    response = RedirectResponse(url=redirect_url or "/local-login", status_code=303)
    response.delete_cookie("local_session")
    return response


@app.post("/api/settings/saml/import-metadata")
async def import_idp_metadata(
    idp_metadata_file: UploadFile = File(...),
    db: Session = Depends(get_db), admin: User = Depends(auth.require_admin),
):
    content = await idp_metadata_file.read()
    try:
        parsed = saml_util.parse_idp_metadata(content)
    except Exception as e:
        return RedirectResponse(url=f"/settings?error={urllib.parse.quote(f'Could not parse metadata XML: {e}')}", status_code=303)

    if not parsed.get("idp_sso_url") or not parsed.get("idp_x509_cert"):
        return RedirectResponse(
            url="/settings?error=Metadata+file+did+not+contain+a+SingleSignOnService+URL+or+signing+certificate.",
            status_code=303,
        )

    for key, value in parsed.items():
        cfg = db.query(AppConfig).filter(AppConfig.key == key).first()
        if cfg:
            cfg.value = value
        else:
            db.add(AppConfig(key=key, value=value, is_secret=False))
    db.commit()
    return RedirectResponse(url="/settings?success=Entra+ID+metadata+imported.", status_code=303)


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


@app.post("/api/users/{target_id}/edit")
async def edit_user(
    target_id: int, name: str = Form(...), email: str = Form(...), role: str = Form(...),
    db: Session = Depends(get_db), admin: User = Depends(auth.require_admin),
):
    if role not in ("read_only", "editor", "admin"):
        raise HTTPException(status_code=400, detail="Invalid role.")
    target = db.query(User).filter(User.id == target_id).first()
    if not target:
        return RedirectResponse(url="/settings", status_code=303)

    email = email.strip()
    if email != target.email and db.query(User).filter(User.email == email, User.id != target.id).first():
        return RedirectResponse(url="/settings?error=Another+account+already+uses+that+email.", status_code=303)

    if target.id == admin.id and role != "admin":
        remaining_admins = db.query(User).filter(User.role == "admin", User.id != target.id, User.is_active == True).count()
        if remaining_admins == 0:
            return RedirectResponse(
                url="/settings?error=Can't+remove+admin+role+from+the+only+active+administrator.",
                status_code=303,
            )

    target.name = name.strip()
    target.email = email
    target.role = role
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/api/users/{target_id}/deactivate")
async def deactivate_user(target_id: int, db: Session = Depends(get_db), admin: User = Depends(auth.require_admin)):
    target = db.query(User).filter(User.id == target_id).first()
    if not target:
        return RedirectResponse(url="/settings", status_code=303)

    if target.role == "admin":
        remaining_admins = db.query(User).filter(User.role == "admin", User.id != target.id, User.is_active == True).count()
        if remaining_admins == 0:
            return RedirectResponse(
                url="/settings?error=Can't+disable+the+only+active+administrator.",
                status_code=303,
            )

    target.is_active = False
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/api/users/{target_id}/reactivate")
async def reactivate_user(target_id: int, db: Session = Depends(get_db), admin: User = Depends(auth.require_admin)):
    target = db.query(User).filter(User.id == target_id).first()
    if target:
        target.is_active = True
        db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/api/users/{target_id}/delete")
async def delete_user(target_id: int, db: Session = Depends(get_db), admin: User = Depends(auth.require_admin)):
    target = db.query(User).filter(User.id == target_id).first()
    if not target:
        return RedirectResponse(url="/settings", status_code=303)

    if target.id == admin.id:
        return RedirectResponse(url="/settings?error=You+can't+delete+your+own+account+while+logged+in.", status_code=303)

    if target.role == "admin":
        remaining_admins = db.query(User).filter(User.role == "admin", User.id != target.id, User.is_active == True).count()
        if remaining_admins == 0:
            return RedirectResponse(url="/settings?error=Can't+delete+the+only+active+administrator.", status_code=303)

    db.delete(target)
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
    timezone = timezone.strip().replace(' ', '_')
    error_msg = None

    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, Exception):
        return RedirectResponse(
            url=f"/settings?error={urllib.parse.quote(f'Unknown timezone: {timezone!r}. Use IANA names like America/Los_Angeles or Europe/London.')}",
            status_code=303,
        )

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
    users = db.query(User).all()
    scan_logs = db.query(ScanLog).all()
    drafts = db.query(AlertDraft).all()
    published = db.query(PublishedAlert).all()

    cert_path = os.path.join(CERT_DIR, "server.crt")
    key_path = os.path.join(CERT_DIR, "server.key")
    tls_cert = open(cert_path).read() if os.path.exists(cert_path) else None
    tls_key = open(key_path).read() if os.path.exists(key_path) else None

    export_data = {
        "version": 2,
        "config": {c.key: c.value for c in configs},
        "users": [
            {"email": u.email, "name": u.name, "role": u.role, "is_active": u.is_active,
             "is_local": u.is_local, "hashed_password": u.hashed_password,
             "must_change_password": u.must_change_password}
            for u in users
        ],
        "targets": [
            {"resource": t.resource, "url": t.url, "extraction_mode": t.extraction_mode,
             "scan_frequency": t.scan_frequency, "recursive": t.recursive, "alert_email": t.alert_email,
             "alert_on_broken_link": t.alert_on_broken_link, "is_active": t.is_active,
             "last_hash": t.last_hash, "last_text": t.last_text,
             "last_scanned": t.last_scanned.isoformat() if t.last_scanned else None,
             "consecutive_failures": t.consecutive_failures, "is_broken": t.is_broken}
            for t in targets
        ],
        "scan_logs": [
            {"target_url": log.target.url, "timestamp": log.timestamp.isoformat(), "status_message": log.status_message}
            for log in scan_logs
        ],
        "alert_drafts": [
            {"target_url": d.target.url, "topic": d.topic, "summary_raw": d.summary_raw,
             "detected_dates": d.detected_dates, "created_at": d.created_at.isoformat(),
             "is_reviewed": d.is_reviewed}
            for d in drafts
        ],
        "published_alerts": [
            {"resource": p.resource, "url": p.url, "topic": p.topic, "summary": p.summary,
             "actionable_steps": p.actionable_steps, "key_deadlines": p.key_deadlines,
             "published_at": p.published_at.isoformat()}
            for p in published
        ],
        "tls_cert": tls_cert,
        "tls_key": tls_key,
    }
    return Response(
        content=json.dumps(export_data, indent=4),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=redtape_full_backup.json"},
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

        if "users" in data:
            for u in data["users"]:
                if not db.query(User).filter(User.email == u.get("email")).first():
                    db.add(User(
                        email=u.get("email"), name=u.get("name"), role=u.get("role", "read_only"),
                        is_active=u.get("is_active", True), is_local=u.get("is_local", False),
                        hashed_password=u.get("hashed_password"),
                        must_change_password=u.get("must_change_password", False),
                    ))

        target_by_url = {}
        if "targets" in data:
            for t in data["targets"]:
                target = db.query(MonitoredTarget).filter(MonitoredTarget.url == t.get("url")).first()
                if not target:
                    last_scanned = t.get("last_scanned")
                    target = MonitoredTarget(
                        resource=t.get("resource"), url=t.get("url"),
                        extraction_mode=t.get("extraction_mode", "auto_clean"),
                        scan_frequency=t.get("scan_frequency", "weekly"),
                        recursive=t.get("recursive", False),
                        alert_email=t.get("alert_email"),
                        alert_on_broken_link=t.get("alert_on_broken_link", True),
                        is_active=t.get("is_active", True),
                        last_hash=t.get("last_hash"),
                        last_text=t.get("last_text"),
                        last_scanned=datetime.fromisoformat(last_scanned) if last_scanned else None,
                        consecutive_failures=t.get("consecutive_failures", 0),
                        is_broken=t.get("is_broken", False),
                    )
                    db.add(target)
                    db.flush()  # assign an id so scan_logs/alert_drafts below can reference it
                target_by_url[t.get("url")] = target

        if "scan_logs" in data:
            for log in data["scan_logs"]:
                target = target_by_url.get(log.get("target_url"))
                if target:
                    db.add(ScanLog(
                        target_id=target.id, timestamp=datetime.fromisoformat(log["timestamp"]),
                        status_message=log.get("status_message", ""),
                    ))

        if "alert_drafts" in data:
            for d in data["alert_drafts"]:
                target = target_by_url.get(d.get("target_url"))
                if target:
                    db.add(AlertDraft(
                        target_id=target.id, topic=d.get("topic"), summary_raw=d.get("summary_raw"),
                        detected_dates=d.get("detected_dates"),
                        created_at=datetime.fromisoformat(d["created_at"]) if d.get("created_at") else datetime.utcnow(),
                        is_reviewed=d.get("is_reviewed", False),
                    ))

        if "published_alerts" in data:
            for p in data["published_alerts"]:
                db.add(PublishedAlert(
                    resource=p.get("resource"), url=p.get("url"), topic=p.get("topic"),
                    summary=p.get("summary"), actionable_steps=p.get("actionable_steps"),
                    key_deadlines=p.get("key_deadlines"),
                    published_at=datetime.fromisoformat(p["published_at"]) if p.get("published_at") else datetime.utcnow(),
                ))

        db.commit()

        if data.get("tls_cert") and data.get("tls_key"):
            os.makedirs(CERT_DIR, exist_ok=True)
            with open(os.path.join(CERT_DIR, "server.crt"), "w") as f:
                f.write(data["tls_cert"])
            with open(os.path.join(CERT_DIR, "server.key"), "w") as f:
                f.write(data["tls_key"])
            os.chmod(os.path.join(CERT_DIR, "server.key"), 0o600)
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
    frequency: str = Form("weekly"), recursive: str = Form("false"), alert_email: str = Form(""),
    alert_on_broken_link: str = Form("false"),
    db: Session = Depends(get_db), editor_user: User = Depends(auth.require_editor),
):
    new_target = MonitoredTarget(
        url=url, resource=resource, extraction_mode=mode,
        scan_frequency=frequency, recursive=(recursive == "true"),
        alert_email=(alert_email.strip() or None),
        alert_on_broken_link=(alert_on_broken_link == "true"),
    )
    db.add(new_target)
    db.commit()
    scan_single_target.delay(new_target.id)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/api/targets/{target_id}/update")
async def update_monitored_target(
    target_id: int, url: str = Form(...), resource: str = Form(...), mode: str = Form("auto_clean"),
    frequency: str = Form("weekly"), recursive: str = Form("false"), alert_email: str = Form(""),
    alert_on_broken_link: str = Form("false"),
    db: Session = Depends(get_db), editor_user: User = Depends(auth.require_editor),
):
    target = db.query(MonitoredTarget).filter(MonitoredTarget.id == target_id).first()
    if target:
        url_changed = target.url != url
        target.url = url
        target.resource = resource
        target.extraction_mode = mode
        target.scan_frequency = frequency
        target.recursive = (recursive == "true")
        target.alert_email = alert_email.strip() or None
        target.alert_on_broken_link = (alert_on_broken_link == "true")
        if url_changed:
            # Old hash/text belong to a different page now -- re-baseline instead
            # of diffing unrelated content on the next scan.
            target.last_hash = None
            target.last_text = None
            target.last_scanned = None
        db.commit()
        if url_changed:
            scan_single_target.delay(target.id)
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
