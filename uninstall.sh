#!/bin/bash
set -e

echo "===================================================="
echo "🛑 REDTAPE RADAR UNINSTALLER"
echo "===================================================="

# Ensure script is run with sudo permissions
if [ "$EUID" -ne 0 ]; then
  echo "❌ Please run this script with sudo privileges:"
  echo "sudo ./uninstall.sh"
  exit 1
fi

PROJECT_ROOT="/root/redtape_radar"
# Fallback check if it's placed in a user home directory
if [ ! -d "$PROJECT_ROOT" ]; then
    PROJECT_ROOT="$(dirname "$(readlink -f "$0")")"
fi

echo "Stopping RedTape Radar systemd services..."
sudo systemctl stop redtape-web.service || true
sudo systemctl stop redtape-celery.service || true

echo "Disabling background daemons..."
sudo systemctl disable redtape-web.service || true
sudo systemctl disable redtape-celery.service || true

echo "Removing systemd configuration files..."
sudo rm -f /etc/systemd/system/redtape-web.service
sudo rm -f /etc/systemd/system/redtape-celery.service

echo "Reloading systemd manager configuration..."
sudo systemctl daemon-reload

echo "Purging active Celery/Redis task queues..."
if command -v redis-cli &> /dev/null; then
    redis-cli FLUSHALL || true
    echo "✓ Redis queue purged successfully."
fi

echo "----------------------------------------------------"
read -p "Do you want to delete the local SQLite database logs/history? (y/N): " DELETE_DB
if [[ "$DELETE_DB" =~ ^[Yy]$ ]]; then
    rm -f "$PROJECT_ROOT/redtape_radar.db"
    echo "✓ Database file removed."
else
    echo "✓ Database file preserved at $PROJECT_ROOT/redtape_radar.db"
fi

read -p "Do you want to delete the Python virtual environment? (y/N): " DELETE_VENV
if [[ "$DELETE_VENV" =~ ^[Yy]$ ]]; then
    rm -rf "$PROJECT_ROOT/venv"
    echo "✓ Virtual environment directory removed."
fi

echo "===================================================="
echo "✅ UNINSTALL COMPLETE!"
echo "===================================================="
echo "The application services and schedulers have been removed."
echo "Note: Shared infrastructure (Redis-Server and Ollama) were left intact."
echo "===================================================="