from celery import Celery
from celery.schedules import crontab
from sqlalchemy.orm import Session
from bs4 import BeautifulSoup
import requests
import hashlib
import json
import difflib

from .models import SessionLocal, MonitoredTarget, AlertDraft, AppConfig

celery_app = Celery("redtape_tasks", broker="redis://localhost:6379/0")

# Schedule the weekly scan (Mondays at 8:00 AM)
celery_app.conf.beat_schedule = {
    'weekly-osha-scan': {
        'task': 'app.tasks.scan_all_targets',
        'schedule': crontab(hour=8, minute=0, day_of_week=1)
    }
}

def fetch_and_clean_text(url: str) -> str:
    """Scrapes the URL and strips out HTML/Scripts to get raw text."""
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
    """The main background worker that checks for updates and triggers the AI."""
    db = SessionLocal()
    try:
        targets = db.query(MonitoredTarget).filter(MonitoredTarget.is_active == True).all()
        
        # Get the Ollama URL (Default to localhost if not explicitly set in config)
        # Assuming Ollama is running on the same Linux host
        ollama_url = "http://localhost:11434/api/generate"
        
        for target in targets:
            try:
                # 1. Scrape the current webpage
                current_text = fetch_and_clean_text(target.url)
                current_hash = hashlib.sha256(current_text.encode('utf-8')).hexdigest()
                
                # 2. Check if the page has changed since last scan
                if target.last_hash and target.last_hash != current_hash:
                    # Logic to pull previous text would go here (omitted for brevity, 
                    # assuming you feed the current text to AI to extract dates/changes)
                    
                    # 3. Ask Local AI (Ollama) to extract the facts
                    prompt = f"""You are a regulatory research assistant. 
                    Analyze the following text from {target.url}.
                    Extract any newly proposed regulations, changes, or compliance dates into strictly formatted JSON.
                    
                    REQUIRED JSON SCHEMA:
                    {{
                        "Topic": "Extract the core topic (e.g., Heat Illness Prevention)",
                        "Summary": "Factual summary of exactly what was added, removed, or changed.",
                        "Explicit_Dates": "Any dates explicitly written in the text. (Leave blank if none)."
                    }}
                    
                    TEXT:
                    {current_text[:8000]}""" # Cap at 8k characters for processing speed
                    
                    ai_response = requests.post(
                        ollama_url,
                        json={"model": "llama3", "prompt": prompt, "stream": False, "format": "json"},
                        timeout=120
                    )
                    
                    if ai_response.status_code == 200:
                        ai_data = json.loads(ai_response.json()['response'])
                        
                        # 4. Save the AI's findings to the Triage Inbox
                        new_draft = AlertDraft(
                            target_id=target.id,
                            topic=ai_data.get('Topic', 'Pending Review'),
                            summary_raw=ai_data.get('Summary', 'No summary generated.'),
                            detected_dates=ai_data.get('Explicit_Dates', '')
                        )
                        db.add(new_draft)
                
                # Update the target's hash and timestamp
                target.last_hash = current_hash
                db.commit()
                
            except Exception as e:
                print(f"Error scanning {target.url}: {str(e)}")
                
    finally:
        db.close()