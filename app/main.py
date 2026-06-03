from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import uvicorn
from .models import engine, Base, get_db, PublishedAlert, AlertDraft, MonitoredTarget, AppConfig, User
from . import auth

Base.metadata.create_all(bind=engine)
app = FastAPI(title="RedTape Radar")
templates = Jinja2Templates(directory="app/templates")

@app.exception_handler(401)
async def unauthorized_redirect(request: Request, exc: HTTPException):
    return RedirectResponse(url="/local-login", status_code=303)

@app.get("/", response_class=HTMLResponse)
async def view_dashboard(request: Request, db: Session = Depends(get_db), current_user: User = Depends(auth.get_current_user)):
    alerts = db.query(PublishedAlert).order_by(PublishedAlert.published_at.desc()).limit(50).all()
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": current_user, "alerts": alerts})

@app.get("/triage", response_class=HTMLResponse)
async def view_triage_inbox(request: Request, db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)):
    drafts = db.query(AlertDraft).filter(AlertDraft.is_reviewed == False).all()
    return templates.TemplateResponse("triage.html", {"request": request, "user": admin_user, "drafts": drafts})

@app.get("/settings", response_class=HTMLResponse)
async def view_settings(request: Request, db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)):
    configs = db.query(AppConfig).all()
    settings_dict = {cfg.key: cfg.value for cfg in configs}
    defaults = {"entra_tenant_id": "", "entra_client_id": "", "entra_admin_group_id": "", "confluence_url": "", "confluence_email": "", "confluence_api_token": ""}
    current_settings = {**defaults, **settings_dict}
    return templates.TemplateResponse("settings.html", {"request": request, "user": admin_user, "settings": current_settings})

@app.get("/local-login", response_class=HTMLResponse)
async def view_local_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "user": None, "error": None})

@app.post("/api/local-login")
async def process_local_login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email, User.is_local == True).first()
    if not user or not auth.verify_password(password, user.hashed_password):
        return templates.TemplateResponse("login.html", {"request": request, "user": None, "error": "Invalid credentials"})
    token = auth.create_local_token(user.email)
    response = RedirectResponse(url="/settings", status_code=303)
    response.set_cookie(key="local_admin_session", value=token, httponly=True, max_age=3600)
    return response

@app.post("/api/settings/update")
async def update_settings(request: Request, db: Session = Depends(get_db), admin_user: User = Depends(auth.require_admin)):
    form_data = await request.form()
    for key, value in form_data.items():
        config_item = db.query(AppConfig).filter(AppConfig.key == key).first()
        if config_item: config_item.value = str(value)
        else:
            new_item = AppConfig(key=key, value=str(value), is_secret=("token" in key or "secret" in key))
            db.add(new_item)
    db.commit()
    return RedirectResponse(url="/settings?success=true", status_code=303)

@app.post("/api/drafts/{draft_id}/approve")
async def approve_ai_draft(
    draft_id: int, 
    actionable_steps: str = Form(...), 
    key_deadlines: str = Form(""),
    db: Session = Depends(get_db), 
    admin_user: User = Depends(auth.require_admin)
):
    # 1. Fetch the Draft
    draft = db.query(AlertDraft).filter(AlertDraft.id == draft_id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # 2. Save to Local Dashboard
    published = PublishedAlert(
        resource=draft.target.resource, 
        url=draft.target.url,
        topic=draft.topic, 
        summary=draft.summary_raw,
        actionable_steps=actionable_steps, 
        key_deadlines=key_deadlines
    )
    db.add(published)
    draft.is_reviewed = True
    db.commit()

    # 3. Push to Confluence
    # Pull dynamic settings configured in the Web UI
    config = {cfg.key: cfg.value for cfg in db.query(AppConfig).all()}
    conf_url = config.get("confluence_url")
    conf_email = config.get("confluence_email")
    conf_token = config.get("confluence_api_token")

    if conf_url and conf_email and conf_token:
        try:
            import requests
            
            # Create the HTML table row for Confluence
            new_row = f"""
            <tr>
                <td>{published.resource}</td>
                <td><a href="{published.url}">Link</a></td>
                <td>{published.topic}</td>
                <td>{published.summary}</td>
                <td>{published.actionable_steps} - Due: {published.key_deadlines}</td>
            </tr>
            """
            
            # Note: You will need to specify the exact Page ID in your settings or hardcode it here
            PAGE_ID = "YOUR_CONFLUENCE_PAGE_ID" 
            api_endpoint = f"{conf_url.rstrip('/')}/wiki/rest/api/content/{PAGE_ID}"
            auth_tuple = (conf_email, conf_token)
            
            # Fetch current page, inject row, and push update (Standard Atlassian API flow)
            current_page = requests.get(f"{api_endpoint}?expand=body.storage", auth=auth_tuple).json()
            if 'body' in current_page:
                current_html = current_page['body']['storage']['value']
                updated_html = current_html.replace('<tbody>', f'<tbody>\n{new_row}')
                
                payload = {
                    "id": current_page['id'],
                    "type": "page",
                    "title": current_page['title'],
                    "space": {"key": current_page['space']['key']},
                    "body": {"storage": {"value": updated_html, "representation": "storage"}},
                    "version": {"number": current_page['version']['number'] + 1}
                }
                requests.put(api_endpoint, json=payload, auth=auth_tuple)
        except Exception as e:
            print(f"Failed to push to Confluence: {e}")

    return RedirectResponse(url="/triage", status_code=303)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)