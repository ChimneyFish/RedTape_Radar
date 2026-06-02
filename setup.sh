#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

echo "===================================================="
echo "🚀 INITIATING REDTAPE RADAR ZERO-TOUCH DEPLOYMENT..."
echo "===================================================="

# 1. CAPTURE ADMIN CREDENTIALS INTERACTIVELY
echo "Please configure your Local 'Break-Glass' Admin Account."
read -p "Enter Admin Email (e.g., admin@domain.com): " ADMIN_EMAIL
read -s -p "Enter Admin Password: " ADMIN_PASSWORD
echo ""
read -s -p "Confirm Admin Password: " ADMIN_PASSWORD_CONFIRM
echo ""

if [ "$ADMIN_PASSWORD" != "$ADMIN_PASSWORD_CONFIRM" ]; then
    echo "❌ Error: Passwords do not match. Aborting setup."
    exit 1
fi

# Define paths
PROJECT_ROOT="$HOME/redtape_radar"
USER_NAME=$(logname || echo $SUDO_USER || whoami)

echo "----------------------------------------------------"
echo "[1/8] Installing System Dependencies (Redis & Python venv)..."
apt-get update -y
apt-get install -y python3-venv python3-pip redis-server

echo "[2/8] Constructing Project Directory Tree..."
mkdir -p "$PROJECT_ROOT/app/templates"
mkdir -p "$PROJECT_ROOT/app/static"

echo "[3/8] Writing Python Backend Files..."

# --- REQUIREMENTS.TXT ---
cat << 'EOF' > "$PROJECT_ROOT/requirements.txt"
fastapi>=0.110.0
uvicorn>=0.28.0
SQLAlchemy>=2.0.0
pydantic-settings>=2.2.0
jinja2>=3.1.0
fastapi-azure-auth>=4.2.0
requests>=2.31.0
beautifulsoup4>=4.12.0
jira>=3.6.0
celery>=5.3.0
redis>=5.0.0
passlib[bcrypt]>=1.7.4
python-jose[cryptography]>=3.3.0
python-multipart>=0.0.9
EOF

# --- APP/__INIT__.PY ---
cat << 'EOF' > "$PROJECT_ROOT/app/__init__.py"
__version__ = "1.0.0"
EOF

# --- APP/MODELS.PY ---
cat << 'EOF' > "$PROJECT_ROOT/app/models.py"
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship, declarative_base, sessionmaker
from sqlalchemy import create_engine
from datetime import datetime

DATABASE_URL = "sqlite:///./redtape_radar.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=True)
    role = Column(String(50), default="read_only")
    is_active = Column(Boolean, default=True)
    is_local = Column(Boolean, default=False)
    hashed_password = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)

class MonitoredTarget(Base):
    __tablename__ = "monitored_targets"
    id = Column(Integer, primary_key=True, index=True)
    resource = Column(String(50), nullable=False)
    url = Column(String(2048), unique=True, nullable=False)
    extraction_mode = Column(String(20), default="auto_clean")
    keyword_anchor = Column(String(100), nullable=True)
    last_scanned = Column(DateTime, default=datetime.utcnow)
    last_hash = Column(String(64), nullable=True)
    is_active = Column(Boolean, default=True)
    drafts = relationship("AlertDraft", back_populates="target", cascade="all, delete-orphan")

class AlertDraft(Base):
    __tablename__ = "alert_drafts"
    id = Column(Integer, primary_key=True, index=True)
    target_id = Column(Integer, ForeignKey("monitored_targets.id"), nullable=False)
    topic = Column(String(255), nullable=False)
    summary_raw = Column(Text, nullable=False)
    detected_dates = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_reviewed = Column(Boolean, default=False)
    target = relationship("MonitoredTarget", back_populates="drafts")

class PublishedAlert(Base):
    __tablename__ = "published_alerts"
    id = Column(Integer, primary_key=True, index=True)
    resource = Column(String(50), nullable=False)
    url = Column(String(2048), nullable=False)
    topic = Column(String(255), nullable=False)
    summary = Column(Text, nullable=False)
    actionable_steps = Column(Text, nullable=False)
    key_deadlines = Column(String(100), nullable=True)
    published_at = Column(DateTime, default=datetime.utcnow)
    confluence_page_id = Column(String(100), nullable=True)

class AppConfig(Base):
    __tablename__ = "app_config"
    key = Column(String(50), primary_key=True, index=True)
    value = Column(String(500), nullable=True)
    is_secret = Column(Boolean, default=False)
EOF

# --- APP/AUTH.PY ---
cat << 'EOF' > "$PROJECT_ROOT/app/auth.py"
from fastapi import Request, Depends, HTTPException, Security
from sqlalchemy.orm import Session
from fastapi_azure_auth import SingleTenantAzureAuthorizationCodeBearer
from passlib.context import CryptContext
from jose import jwt
from datetime import datetime
from .models import get_db, User, AppConfig

azure_scheme = SingleTenantAzureAuthorizationCodeBearer(
    app_client_id="00000000-0000-0000-0000-000000000000",
    tenant_id="00000000-0000-0000-0000-000000000000",
    scopes={"api://00000000-0000-0000-0000-000000000000/user_impersonation": "Access API"}
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET_KEY = "DEPLOYMENT_SECRET_KEY_REPLACE_LATER"
ALGORITHM = "HS256"

def verify_password(plain_password, hashed_password): return pwd_context.verify(plain_password, hashed_password)
def get_password_hash(password): return pwd_context.hash(password)
def create_local_token(email: str): return jwt.encode({"sub": email, "type": "local_admin"}, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(request: Request, db: Session = Depends(get_db)):
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        try:
            token_payload = await azure_scheme(request)
            email = token_payload.claims.get('preferred_username') or token_payload.claims.get('upn')
            if email:
                user = db.query(User).filter(User.email == email, User.is_active == True).first()
                if not user:
                    user = User(email=email, name=token_payload.claims.get('name', 'SSO User'), role="read_only")
                    db.add(user)
                    db.commit()
                    db.refresh(user)
                return user
        except Exception: pass 

    local_token = request.cookies.get("local_admin_session")
    if local_token:
        try:
            payload = jwt.decode(local_token, SECRET_KEY, algorithms=[ALGORITHM])
            email = payload.get("sub")
            user = db.query(User).filter(User.email == email, User.is_local == True, User.is_active == True).first()
            if user: return user
        except Exception: pass

    raise HTTPException(status_code=401, detail="Authentication required.")

async def require_admin(current_user: User = Depends(get_current_user)):
    if current_user.role != 'admin': raise HTTPException(status_code=403, detail="Admin required.")
    return current_user
EOF

# --- APP/MAIN.PY ---
cat << 'EOF' > "$PROJECT_ROOT/app/main.py"
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
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/api/local-login")
async def process_local_login(email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email, User.is_local == True).first()
    if not user or not auth.verify_password(password, user.hashed_password):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})
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

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
EOF

# --- APP/TASKS.PY ---
cat << 'EOF' > "$PROJECT_ROOT/app/tasks.py"
from celery import Celery
from celery.schedules import crontab
celery_app = Celery("redtape_tasks", broker="redis://localhost:6379/0")
celery_app.conf.beat_schedule = {
    'weekly-osha-scan': {'task': 'app.tasks.scan_all_targets', 'schedule': crontab(hour=8, minute=0, day_of_week=1)}
}
@celery_app.task
def scan_all_targets():
    print("Background scan executed via Celery.")
EOF

echo "[4/8] Writing Web UI Templates..."

# --- TEMPLATES/BASE.HTML ---
cat << 'EOF' > "$PROJECT_ROOT/app/templates/base.html"
<!DOCTYPE html>
<html>
<head>
    <title>{% block title %}RedTape Radar{% endblock %}</title>
    <style>
        body { font-family: system-ui, sans-serif; margin: 0; background: #f4f7f6; color: #333; }
        .navbar { background: #0b2b40; color: white; padding: 15px 20px; display: flex; justify-content: space-between; }
        .navbar a { color: white; text-decoration: none; margin-left: 20px; font-weight: 500; }
        .container { max-width: 1200px; margin: 40px auto; padding: 0 20px; }
        .card { background: white; border-radius: 8px; padding: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; }
        button { background: #0263e0; color: white; border: none; padding: 10px 15px; border-radius: 4px; cursor: pointer; }
    </style>
</head>
<body>
    <nav class="navbar">
        <div class="logo"><strong>RedTape Radar</strong></div>
        <div class="nav-links">
            <a href="/">Dashboard</a>
            {% if user and user.role == 'admin' %}<a href="/triage">Triage Inbox</a><a href="/settings">Settings</a>{% endif %}
        </div>
    </nav>
    <div class="container">{% block content %}{% endblock %}</div>
</body>
</html>
EOF

# --- TEMPLATES/LOGIN.HTML ---
cat << 'EOF' > "$PROJECT_ROOT/app/templates/login.html"
{% extends "base.html" %}
{% block content %}
<div class="card" style="max-width: 400px; margin: 0 auto; text-align: center;">
    <h2>Local Admin Login</h2>
    {% if error %}<p style="color: red;">{{ error }}</p>{% endif %}
    <form action="/api/local-login" method="post">
        <input type="email" name="email" placeholder="Admin Email" required style="width: 90%; padding: 10px; margin-bottom: 15px;"><br>
        <input type="password" name="password" placeholder="Password" required style="width: 90%; padding: 10px; margin-bottom: 15px;"><br>
        <button type="submit" style="width: 95%;">Authenticate</button>
    </form>
</div>
{% endblock %}
EOF

# --- TEMPLATES/SETTINGS.HTML ---
cat << 'EOF' > "$PROJECT_ROOT/app/templates/settings.html"
{% extends "base.html" %}
{% block content %}
<h1>System Configuration</h1>
<form action="/api/settings/update" method="post">
    <div class="card">
        <h2>Microsoft Entra ID (SSO)</h2>
        <label>Tenant ID:</label><br><input type="text" name="entra_tenant_id" value="{{ settings.entra_tenant_id }}" size="50"><br><br>
        <label>Client ID:</label><br><input type="text" name="entra_client_id" value="{{ settings.entra_client_id }}" size="50"><br><br>
        <label>Admin Security Group Object ID:</label><br><input type="text" name="entra_admin_group_id" value="{{ settings.entra_admin_group_id }}" size="50">
    </div>
    <div class="card">
        <h2>Confluence Integration</h2>
        <label>Workspace URL:</label><br><input type="text" name="confluence_url" value="{{ settings.confluence_url }}" size="50"><br><br>
        <label>Service Email:</label><br><input type="text" name="confluence_email" value="{{ settings.confluence_email }}" size="50"><br><br>
        <label>API Token:</label><br><input type="password" name="confluence_api_token" value="{{ settings.confluence_api_token }}" size="50">
    </div>
    <button type="submit" style="background-color: #28a745;">Save Configuration</button>
</form>
{% endblock %}
EOF

# --- TEMPLATES/DASHBOARD.HTML & TRIAGE.HTML (Stubs for completion) ---
cat << 'EOF' > "$PROJECT_ROOT/app/templates/dashboard.html"
{% extends "base.html" %}{% block content %}<h1>Dashboard</h1><p>Welcome, {{ user.name }}</p>{% endblock %}
EOF
cat << 'EOF' > "$PROJECT_ROOT/app/templates/triage.html"
{% extends "base.html" %}{% block content %}<h1>Triage Inbox</h1><p>Pending drafts will appear here.</p>{% endblock %}
EOF

echo "[5/8] Creating Python Virtual Environment and compiling dependencies..."
cd "$PROJECT_ROOT"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip > /dev/null 2>&1
pip install -r requirements.txt > /dev/null 2>&1

echo "[6/8] Executing Database Build and Injecting Admin Credentials..."
# Create the python script to inject the admin, run it, and delete it so credentials aren't left in a file.
cat << 'EOF' > "$PROJECT_ROOT/create_admin.py"
import sys
from app.database import SessionLocal, engine, Base
from app.models import User
from app.auth import get_password_hash

Base.metadata.create_all(bind=engine)
db = SessionLocal()
email = sys.argv[1]
password = sys.argv[2]

if not db.query(User).filter(User.email == email).first():
    admin = User(email=email, name="System Administrator", role="admin", is_local=True, hashed_password=get_password_hash(password))
    db.add(admin)
    db.commit()
db.close()
EOF

python3 create_admin.py "$ADMIN_EMAIL" "$ADMIN_PASSWORD"
rm "$PROJECT_ROOT/create_admin.py"

echo "[7/8] Writing Systemd Daemons for FastAPI and Celery..."

cat << EOF > /etc/systemd/system/redtape-web.service
[Unit]
Description=RedTape Radar FastAPI Web Server
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$PROJECT_ROOT
Environment="PATH=$PROJECT_ROOT/venv/bin"
ExecStart=$PROJECT_ROOT/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

cat << EOF > /etc/systemd/system/redtape-celery.service
[Unit]
Description=RedTape Radar Celery Worker
After=network.target redis-server.service

[Service]
User=$USER_NAME
WorkingDirectory=$PROJECT_ROOT
Environment="PATH=$PROJECT_ROOT/venv/bin"
ExecStart=$PROJECT_ROOT/venv/bin/celery -A app.tasks worker --loglevel=info
Restart=always

[Install]
WantedBy=multi-user.target
EOF

echo "[8/8] Booting Systems into Existence..."
systemctl daemon-reload
systemctl enable redtape-web redtape-celery redis-server
systemctl restart redtape-web redtape-celery redis-server

echo "===================================================="
echo "✅ REDTAPE RADAR IS LIVE!"
echo "===================================================="
echo "Navigate to: http://$(hostname -I | awk '{print $1}'):8000/local-login"
echo "Login with: $ADMIN_EMAIL"
echo "===================================================="