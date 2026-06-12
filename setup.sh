#!/bin/bash
set -e

echo "===================================================="
echo "🚀 CONFIGURING REDTAPE RADAR INFRASTRUCTURE..."
echo "===================================================="

# 1. Capture Admin Credentials
read -p "Enter Admin Email (e.g., admin@domain.com): " ADMIN_EMAIL
read -s -p "Enter Admin Password: " ADMIN_PASSWORD
echo ""

PROJECT_ROOT="$HOME/redtape_radar"
BIND_IP=$(hostname -I | awk '{print $1}')

echo "----------------------------------------------------"
echo "Installing System Dependencies (Redis & Python venv)..."
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip redis-server

echo "Creating Python Virtual Environment and installing packages..."
cd "$PROJECT_ROOT"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip > /dev/null 2>&1
pip install -r requirements.txt

echo "Executing Database Build and Injecting Admin Credentials..."
cat << 'EOF' > "$PROJECT_ROOT/create_admin.py"
import sys
from app.models import SessionLocal, engine, Base, User
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

echo "Configuring Systemd Daemons to run securely..."

sudo bash -c "cat << EOF > /etc/systemd/system/redtape-web.service
[Unit]
Description=RedTape Radar FastAPI Web Server
After=network.target

[Service]
User=root
WorkingDirectory=$PROJECT_ROOT
Environment=\"PATH=$PROJECT_ROOT/venv/bin\"
ExecStart=$PROJECT_ROOT/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF"

sudo bash -c "cat << EOF > /etc/systemd/system/redtape-celery.service
[Unit]
Description=RedTape Radar Celery Worker & Scheduler
After=network.target redis-server.service

[Service]
User=root
WorkingDirectory=$PROJECT_ROOT
Environment=\"PATH=$PROJECT_ROOT/venv/bin\"
ExecStart=$PROJECT_ROOT/venv/bin/celery -A app.tasks worker -B --loglevel=info
Restart=always

[Install]
WantedBy=multi-user.target
EOF"

echo "Booting Systems into Existence..."
sudo systemctl daemon-reload
sudo systemctl enable redtape-web redtape-celery redis-server
sudo systemctl restart redtape-web redtape-celery redis-server

echo "===================================================="
echo "✅ INFRASTRUCTURE READY! REDTAPE RADAR IS LIVE!"
echo "===================================================="
echo "Access point:   http://$BIND_IP:8000/local-login"
echo "Login with:     $ADMIN_EMAIL"
echo "===================================================="