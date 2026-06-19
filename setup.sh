#!/bin/bash
set -e

echo "===================================================="
echo "🚀 CONFIGURING REDTAPE RADAR INFRASTRUCTURE..."
echo "===================================================="

read -p "Enter Admin Email (e.g., admin@domain.com): " ADMIN_EMAIL
read -s -p "Enter Admin Password: " ADMIN_PASSWORD
echo ""

PROJECT_ROOT="$HOME/RedTape_Radar"
BIND_IP=$(hostname -I | awk '{print $1}')

echo "----------------------------------------------------"
echo "Installing System Dependencies..."
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip redis-server curl openssl

echo "Installing Ollama AI Engine..."
if ! command -v ollama &> /dev/null; then
    curl -fsSL https://ollama.com/install.sh | sh
    sudo systemctl enable ollama
    sudo systemctl start ollama
    echo "Pulling Default AI Model (llama3) - This may take a few minutes..."
    ollama pull llama3
else
    echo "Ollama is already installed. Skipping..."
fi

echo "Creating Python Virtual Environment..."
cd "$PROJECT_ROOT"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip > /dev/null 2>&1
pip install -r requirements.txt

echo "Wiping legacy database to apply new architectural schema..."
rm -f "$PROJECT_ROOT/redtape_radar.db"

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

echo "Generating TLS Certificate..."
mkdir -p "$PROJECT_ROOT/certs"
if [ ! -f "$PROJECT_ROOT/certs/server.crt" ]; then
    openssl req -x509 -newkey rsa:4096 \
        -keyout "$PROJECT_ROOT/certs/server.key" \
        -out "$PROJECT_ROOT/certs/server.crt" \
        -days 365 -nodes \
        -subj "/C=US/ST=Local/L=Local/O=RedTape Radar/CN=$BIND_IP"
    chmod 600 "$PROJECT_ROOT/certs/server.key"
    echo "Self-signed certificate generated (valid 365 days). Replace via Settings > TLS Certificate."
else
    echo "Existing certificate found. Skipping generation."
fi

echo "Configuring Systemd Daemons..."

sudo bash -c "cat << EOF > /etc/systemd/system/redtape-web.service
[Unit]
Description=RedTape Radar FastAPI Web Server
After=network.target

[Service]
User=root
WorkingDirectory=$PROJECT_ROOT
Environment=\"PATH=$PROJECT_ROOT/venv/bin\"
ExecStart=$PROJECT_ROOT/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8443 --ssl-keyfile $PROJECT_ROOT/certs/server.key --ssl-certfile $PROJECT_ROOT/certs/server.crt
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
echo "Access point:   https://$BIND_IP:8443/local-login"
echo "Login with:     $ADMIN_EMAIL"
echo ""
echo "NOTE: Your browser will show a certificate warning for the"
echo "self-signed cert. Accept the exception to proceed, or upload"
echo "a CA-signed cert via Settings > TLS Certificate."
echo "===================================================="
