from celery import Celery
from celery.schedules import crontab
from sqlalchemy.orm import Session
import requests
import json
import hashlib

from .database import SessionLocal
from .models import MonitoredTarget, AlertDraft

# Connect Celery to your local Redis message broker
celery_app = Celery("redtape_tasks", broker="redis://localhost:6379/0")

# Schedule the clock: Run every Monday at 8:00 AM
celery_app.conf.beat_schedule = {
    'weekly-osha-scan': {
        'task': 'app.tasks.scan_all_targets',
        'schedule': crontab(hour=8, minute=0, day_of_week=1),
    },
}

@celery_app.task
def scan_all_targets():
    """Loops through the database and scrapes all active URLs."""
    db = SessionLocal()
    try:
        targets = db.query(MonitoredTarget).filter(MonitoredTarget.is_active == True).all()
        
        for target in targets:
            # 1. SCRAPE & DIFF
            # (Insert the BS4 web scraping and Difflib logic we built earlier here)
            scraped_text = "..." # Simulated extracted text
            current_hash = hashlib.sha256(scraped_text.encode('utf-8')).hexdigest()
            
            if target.last_hash != current_hash:
                target.last_hash = current_hash
                diff_text = "..." # Simulated differences
                
                # 2. CALL OLLAMA (Local AI)
                prompt = f"Extract facts to JSON. Topic, Summary, Dates.\n{diff_text}"
                ai_response = requests.post(
                    "http://localhost:11434/api/generate", # Default Ollama Linux port
                    json={"model": "llama3", "prompt": prompt, "stream": False, "format": "json"}
                )
                
                if ai_response.status_code == 200:
                    ai_data = json.loads(ai_response.json()['response'])
                    
                    # 3. SAVE DRAFT TO DATABASE FOR HUMAN REVIEW
                    new_draft = AlertDraft(
                        target_id=target.id,
                        topic=ai_data.get('Topic', 'Unknown'),
                        summary_raw=ai_data.get('Summary', 'No summary provided.'),
                        detected_dates=ai_data.get('Explicit_Dates', '')
                    )
                    db.add(new_draft)
                    db.commit()
                    
                    # TODO: Trigger email/Teams ping to PAAS team saying "New Draft in Triage"
    finally:
        db.close()