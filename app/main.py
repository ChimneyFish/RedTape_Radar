from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
import uvicorn
import subprocess
from .models import engine, Base, get_db, PublishedAlert, AlertDraft, MonitoredTarget, AppConfig, User, ScanLog
from . import auth
from .tasks import scan_single_target
import json
from fastapi import FastAPI, Request, Depends, HTTPException, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response

Base.metadata.create_all(bind=engine)
app = FastAPI(title="RedTape Radar")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

@app.exception_handler(401)
async def unauthorized_redirect(request: Request, exc: HTTPException):
    return RedirectResponse(url="/local-login", status_code=303)

@app.get("/", response_class=HTMLResponse)
async def view_dashboard(request: Request, db: Session = Depends(get_db), current_user: User = Depends(auth.get_current_user)):
    alerts = db.query(PublishedAlert).order_by(PublishedAlert.published_at.desc()).limit(50).all()
    logs = db.query(ScanLog).order_by(ScanLog.timestamp.desc()).limit(20).all() # Fetch latest logs
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"user": current_user, "alerts": alerts, "logs": logs})

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
        "llm_provider": "local", "local_model_name": "llama3", "openai_api_key": "", "gemini_api_key": "", "claude_api_key": "",
        "enable_emails": "false", "smtp_server": "", "smtp_port": "587", "smtp_user": "", "smtp_pass": "", "alert_email": ""
    }
    current_settings = {**defaults, **settings_dict}
    return templates.TemplateResponse(request=request, name="settings.html", context={"user": admin_user, "settings": current_settings, "system_users": users, "targets": targets})

@app.get("/local-login", response_class=HTMLResponse)
async def view_local_login(request: Request): return templates.TemplateResponse(request=request, name="login.html", context={"user": None, "error": None})

@app.post("/api/local-login")
async def process_local_login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user or not auth.verify_password(password, user.hashed_password):
        return templates.TemplateResponse(request=request, name="login.html", context={"user": None, "error": "Invalid credentials"})
    
    # Intercept for forced password reset
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

@app.post("/api/users/{target_id}/force-reset")
async def force_user_reset(target_id: int, db: Session = Depends(get_db), admin: User = Depends(auth.require_admin)):
    target = db.query(User).filter(User.id == target_id).first()
    if target:
        target.must_change_password = True
        db.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/local-login", status_code=303)
    response.delete_cookie("local_session")
    return response

@app.post("/api/users/create")
async def create_user(email: str = Form(...), name: str = Form(...), role: str = Form("read_only"), password: str = Form(...), db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)):
    if not db.query(User).filter(User.email == email).first():
        db.add(User(email=email, name=name, role=role, hashed_password=auth.get_password_hash(password), is_local=True))
        db.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/api/settings/update")
async def update_settings(request: Request, db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)):
    form_data = await request.form()
    for key, value in form_data.items():
        if key in ["use_ntp", "manual_time", "timezone"]: continue # Skip OS time variables
        config_item = db.query(AppConfig).filter(AppConfig.key == key).first()
        if config_item: config_item.value = str(value)
        else: db.add(AppConfig(key=key, value=str(value), is_secret=("key" in key or "token" in key or "pass" in key)))
    db.commit()
    return RedirectResponse(url="/settings?success=true", status_code=303)

# NEW: OS Level Time Controller
@app.post("/api/system/time")
async def update_system_time(
    use_ntp: bool = Form(False), manual_time: str = Form(""), timezone: str = Form("UTC"),
    admin_user: User = Depends(auth.require_admin)
):
    try:
        subprocess.run(["timedatectl", "set-timezone", timezone], check=True)
        if use_ntp:
            subprocess.run(["timedatectl", "set-ntp", "true"], check=True)
        else:
            subprocess.run(["timedatectl", "set-ntp", "false"], check=True)
            if manual_time: 
                # Requires Format: "YYYY-MM-DD HH:MM:SS"
                subprocess.run(["timedatectl", "set-time", manual_time], check=True)
    except Exception as e:
        print(f"Failed to set system time: {e}")
    return RedirectResponse(url="/settings?success=true", status_code=303)

@app.post("/api/targets")
async def add_monitored_target(url: str = Form(...), resource: str = Form(...), mode: str = Form("auto_clean"), frequency: str = Form("weekly"), recursive: bool = Form(False), db: Session = Depends(get_db), editor_user: User = Depends(auth.require_editor)):
    new_target = MonitoredTarget(url=url, resource=resource, extraction_mode=mode, scan_frequency=frequency, recursive=recursive)
    db.add(new_target)
    db.commit()
    scan_single_target.delay(new_target.id) # <--- Triggers the Instant Baseline!
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/api/targets/{target_id}/delete")
async def delete_monitored_target(target_id: int, db: Session = Depends(get_db), editor_user: User = Depends(auth.require_editor)):
    target = db.query(MonitoredTarget).filter(MonitoredTarget.id == target_id).first()
    if target: db.delete(target); db.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/api/drafts/{draft_id}/approve")
async def approve_ai_draft(draft_id: int, actionable_steps: str = Form(...), key_deadlines: str = Form(""), db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)):
    draft = db.query(AlertDraft).filter(AlertDraft.id == draft_id).first()
    if not draft: raise HTTPException(status_code=404)
    db.add(PublishedAlert(resource=draft.target.resource, url=draft.target.url, topic=draft.topic, summary=draft.summary_raw, actionable_steps=actionable_steps, key_deadlines=key_deadlines))
    draft.is_reviewed = True
    db.commit()
    return RedirectResponse(url="/triage", status_code=303)

@app.get("/api/settings/export")
async def export_settings(db: Session = Depends(get_db), admin: User = Depends(auth.require_admin)):
    configs = db.query(AppConfig).all()
    targets = db.query(MonitoredTarget).all()
    
    export_data = {
        "config": {c.key: c.value for c in configs},
        "targets": [
            {
                "resource": t.resource,
                "url": t.url,
                "extraction_mode": t.extraction_mode,
                "scan_frequency": t.scan_frequency,
                "recursive": t.recursive
            } for t in targets
        ]
    }
    
    return Response(
        content=json.dumps(export_data, indent=4),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=redtape_backup.json"}
    )

@app.post("/api/settings/import")
async def import_settings(backup_file: UploadFile = File(...), db: Session = Depends(get_db), admin: User = Depends(auth.require_admin)):
    content = await backup_file.read()
    try:
        data = json.loads(content)
        
        # 1. Restore API Keys and Configurations
        if "config" in data:
            for key, value in data["config"].items():
                cfg = db.query(AppConfig).filter(AppConfig.key == key).first()
                if cfg: cfg.value = value
                else: db.add(AppConfig(key=key, value=value, is_secret=("key" in key or "token" in key or "pass" in key)))
        
        # 2. Restore Monitored URLs (Ignoring duplicates)
        if "targets" in data:
            for t in data["targets"]:
                existing = db.query(MonitoredTarget).filter(MonitoredTarget.url == t.get("url")).first()
                if not existing:
                    db.add(MonitoredTarget(
                        resource=t.get("resource"), url=t.get("url"),
                        extraction_mode=t.get("extraction_mode", "auto_clean"),
                        scan_frequency=t.get("scan_frequency", "weekly"),
                        recursive=t.get("recursive", False)
                    ))
        db.commit()
    except Exception as e:
        print(f"Import failed: {e}")
        
    return RedirectResponse(url="/settings?success=true", status_code=303)