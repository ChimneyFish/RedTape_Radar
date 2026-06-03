#!/bin/bash

# Exit on error, but we will allow certain commands to fail gracefully if things are already deleted
set -e

echo "===================================================="
echo "⚠️  INITIATING REDTAPE RADAR UNINSTALLATION"
echo "===================================================="
echo "WARNING: This is a destructive action."
echo "This will COMPLETELY REMOVE the RedTape Radar application,"
echo "including the local SQLite database, all saved configurations,"
echo "and the systemd background services."
echo "----------------------------------------------------"

# SAFETY CATCH
read -p "Are you ABSOLUTELY sure you want to proceed? (Type 'YES' to confirm): " CONFIRM

if [ "$CONFIRM" != "YES" ]; then
    echo "Uninstallation aborted by user."
    exit 0
fi

# Determine the actual user's home directory (handling if run via sudo)
USER_NAME=$(logname || echo $SUDO_USER || whoami)
PROJECT_ROOT=$(eval echo ~$USER_NAME)/redtape_radar

echo ""
echo "[1/4] Stopping RedTape Radar system daemons..."
systemctl stop redtape-web || echo "-> Web service already stopped or missing."
systemctl stop redtape-celery || echo "-> Celery service already stopped or missing."

echo "[2/4] Disabling daemons and removing systemd files..."
systemctl disable redtape-web || true
systemctl disable redtape-celery || true
rm -f /etc/systemd/system/redtape-web.service
rm -f /etc/systemd/system/redtape-celery.service

echo "[3/4] Reloading Linux systemd manager..."
systemctl daemon-reload
systemctl reset-failed

echo "[4/4] Erasing application files, virtual environment, and database..."
if [ -d "$PROJECT_ROOT" ]; then
    rm -rf "$PROJECT_ROOT"
    echo "-> Directory $PROJECT_ROOT fully wiped."
else
    echo "-> Project directory not found. Skipping."
fi

echo "===================================================="
echo "✅ REDTAPE RADAR HAS BEEN COMPLETELY UNINSTALLED."
echo "===================================================="
echo ""
echo "Note: System packages like 'redis-server' and 'python3-venv'"
echo "were left intact as they may be utilized by other OS functions."
echo "If you wish to remove Redis entirely, you can run:"
echo "  sudo apt-get remove --purge redis-server -y"
echo "===================================================="