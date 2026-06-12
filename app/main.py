from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
import uvicorn
from .models import engine, Base, get_db, PublishedAlert, AlertDraft, MonitoredTarget, AppConfig, User
from . import auth

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
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"user": current_user, "alerts": alerts})

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
        "confluence_url": "", "confluence_email": "", "confluence_api_token": "",
        "llm_provider": "local", "local_model_name": "llama3",
        "openai_api_key": "", "gemini_api_key": "", "claude_api_key": ""
    }
    current_settings = {**defaults, **settings_dict}
    return templates.TemplateResponse(request=request, name="settings.html", context={"user": admin_user, "settings": current_settings, "system_users": users, "targets": targets})

@app.get("/local-login", response_class=HTMLResponse)
async def view_local_login(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"user": None, "error": None})

@app.post("/api/local-login")
async def process_local_login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user or not auth.verify_password(password, user.hashed_password):
        return templates.TemplateResponse(request=request, name="login.html", context={"user": None, "error": "Invalid credentials"})
    token = auth.create_local_token(user.email)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="local_session", value=token, httponly=True, max_age=86400)
    return response

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/local-login", status_code=303)
    response.delete_cookie("local_session")
    return response

@app.post("/api/users/create")
async def create_user(email: str = Form(...), name: str = Form(...), role: str = Form("read_only"), password: str = Form(...), db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)):
    if not db.query(User).filter(User.email == email).first():
        new_user = User(email=email, name=name, role=role, hashed_password=auth.get_password_hash(password), is_local=True)
        db.add(new_user)
        db.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/api/settings/update")
async def update_settings(request: Request, db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)):
    form_data = await request.form()
    for key, value in form_data.items():
        config_item = db.query(AppConfig).filter(AppConfig.key == key).first()
        if config_item: config_item.value = str(value)
        else:
            db.add(AppConfig(key=key, value=str(value), is_secret=("key" in key or "token" in key)))
    db.commit()
    return RedirectResponse(url="/settings?success=true", status_code=303)

@app.post("/api/targets")
async def add_monitored_target(
    url: str = Form(...), resource: str = Form(...), mode: str = Form("auto_clean"), 
    frequency: str = Form("weekly"), recursive: bool = Form(False),
    db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)
):
    new_target = MonitoredTarget(url=url, resource=resource, extraction_mode=mode, scan_frequency=frequency, recursive=recursive)
    db.add(new_target)
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/api/targets/{target_id}/delete")
async def delete_monitored_target(target_id: int, db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)):
    target = db.query(MonitoredTarget).filter(MonitoredTarget.id == target_id).first()
    if target:
        db.delete(target)
        db.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/api/drafts/{draft_id}/approve")
async def approve_ai_draft(draft_id: int, actionable_steps: str = Form(...), key_deadlines: str = Form(""), db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)):
    draft = db.query(AlertDraft).filter(AlertDraft.id == draft_id).first()
    if not draft: raise HTTPException(status_code=404, detail="Draft not found")

    published = PublishedAlert(resource=draft.target.resource, url=draft.target.url, topic=draft.topic, summary=draft.summary_raw, actionable_steps=actionable_steps, key_deadlines=key_deadlines)
    db.add(published)
    draft.is_reviewed = True
    db.commit()
    return RedirectResponse(url="/triage", status_code=303)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)