from celery import Celery
from celery.schedules import crontab
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import requests
import hashlib
import json
from bs4 import BeautifulSoup

from .models import SessionLocal, MonitoredTarget, AlertDraft, AppConfig

celery_app = Celery("redtape_tasks", broker="redis://localhost:6379/0")

celery_app.conf.beat_schedule = {
    'frequency-check-scan': {
        'task': 'app.tasks.scan_all_targets',
        'schedule': crontab(minute=0)
    }
}

def fetch_and_clean_text(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for script in soup(["script", "style", "nav", "footer"]):
        script.decompose()
    text = soup.get_text(separator="\n")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())

@celery_app.task
def scan_all_targets():
    db = SessionLocal()
    try:
        config = {cfg.key: cfg.value for cfg in db.query(AppConfig).all()}
        freq_setting = config.get("scan_frequency", "weekly")
        
        now = datetime.utcnow()
        if freq_setting == "hourly":
            threshold = now - timedelta(hours=1)
        elif freq_setting == "daily":
            threshold = now - timedelta(days=1)
        else:
            threshold = now - timedelta(days=7)

        targets = db.query(MonitoredTarget).filter(MonitoredTarget.is_active == True).all()
        ollama_url = "http://localhost:11434/api/generate"
        
        for target in targets:
            if target.last_scanned and target.last_scanned > threshold:
                continue 
                
            try:
                current_text = fetch_and_clean_text(target.url)
                current_hash = hashlib.sha256(current_text.encode('utf-8')).hexdigest()
                
                if target.last_hash != current_hash:
                    prompt = f"""You are a regulatory research assistant. Analyze the text from {target.url}.
                    Extract proposed regulations, changes, or compliance dates into strictly formatted JSON.
                    REQUIRED JSON SCHEMA:
                    {{
                        "Topic": "Extract the core topic",
                        "Summary": "Factual summary of exactly what was added, removed, or changed.",
                        "Explicit_Dates": "Explicit dates (Leave blank if none)."
                    }}
                    TEXT: {current_text[:8000]}"""
                    
                    ai_response = requests.post(
                        ollama_url,
                        json={"model": "llama3", "prompt": prompt, "stream": False, "format": "json"},
                        timeout=120
                    )
                    
                    if ai_response.status_code == 200:
                        ai_data = json.loads(ai_response.json()['response'])
                        new_draft = AlertDraft(
                            target_id=target.id,
                            topic=ai_data.get('Topic', 'Pending Review'),
                            summary_raw=ai_data.get('Summary', 'No summary generated.'),
                            detected_dates=ai_data.get('Explicit_Dates', '')
                        )
                        db.add(new_draft)
                
                target.last_hash = current_hash
                target.last_scanned = now 
                db.commit()
                
            except Exception as e:
                print(f"Error scanning {target.url}: {str(e)}")
                
    finally:
        db.close()