from celery import Celery
from celery.schedules import crontab
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import requests
import hashlib
import json
import smtplib
from email.mime.text import MIMEText
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

from .models import SessionLocal, MonitoredTarget, AlertDraft, AppConfig, ScanLog

celery_app = Celery("redtape_tasks", broker="redis://localhost:6379/0")

celery_app.conf.beat_schedule = {
    'hourly-engine-tick': {
        'task': 'app.tasks.scan_all_targets',
        'schedule': crontab(minute=0)
    }
}

def extract_text(html_content: str, mode: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    if mode == "auto_clean":
        for script in soup(["script", "style", "nav", "footer", "header", "aside"]):
            script.decompose()
    else: 
        for script in soup(["script", "style"]):
            script.decompose()
    return "\n".join(line.strip() for line in soup.get_text(separator="\n").splitlines() if line.strip())

def scrape_with_depth(base_url: str, mode: str, recursive: bool) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(base_url, headers=headers, timeout=15)
        res.raise_for_status()
        master_text = extract_text(res.text, mode)
        
        if recursive:
            soup = BeautifulSoup(res.text, "html.parser")
            base_domain = urlparse(base_url).netloc
            links_visited = set([base_url])
            
            for a_tag in soup.find_all('a', href=True):
                if len(links_visited) > 5: break
                full_link = urljoin(base_url, a_tag['href'])
                if urlparse(full_link).netloc == base_domain and full_link not in links_visited:
                    links_visited.add(full_link)
                    try:
                        sub_res = requests.get(full_link, headers=headers, timeout=10)
                        master_text += f"\n\n--- Content from {full_link} ---\n"
                        master_text += extract_text(sub_res.text, mode)
                    except: pass
        return master_text[:15000]
    except Exception as e:
        return ""

def clean_json_response(raw_text: str) -> dict:
    """Strips markdown formatting that causes silent JSONDecodeErrors."""
    clean_text = raw_text.replace('```json', '').replace('```', '').strip()
    return json.loads(clean_text)

def call_llm(prompt: str, config: dict) -> dict:
    provider = config.get("llm_provider", "local")
    try:
        if provider == "openai":
            res = requests.post("https://api.openai.com/v1/chat/completions", headers={"Authorization": f"Bearer {config.get('openai_api_key')}"}, json={"model": "gpt-4o-mini", "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": prompt}]})
            return clean_json_response(res.json()['choices'][0]['message']['content'])
            
        elif provider == "claude":
            res = requests.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": config.get('claude_api_key'), "anthropic-version": "2023-06-01"}, json={"model": "claude-3-haiku-20240307", "max_tokens": 1000, "messages": [{"role": "user", "content": prompt + "\nRespond ONLY in valid JSON."}]})
            return clean_json_response(res.json()['content'][0]['text'])
            
        elif provider == "gemini":
            key = config.get("gemini_api_key")
            res = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={key}", json={"contents": [{"parts": [{"text": prompt + "\nRespond ONLY in valid JSON."}]}]})
            return clean_json_response(res.json()['candidates'][0]['content']['parts'][0]['text'])
            
        else: 
            model = config.get("local_model_name", "llama3")
            res = requests.post("http://localhost:11434/api/generate", json={"model": model, "prompt": prompt, "stream": False, "format": "json"}, timeout=180)
            return json.loads(res.json()['response'])
    except Exception as e:
        raise Exception(f"LLM Error ({provider}): {str(e)}")

def send_alert_email(config: dict, target_name: str, topic: str):
    if config.get("enable_emails") != "true": return
    try:
        msg = MIMEText(f"RedTape Radar detected a potential regulatory change on {target_name}.\n\nTopic: {topic}\n\nPlease log in to the Triage Inbox to review.")
        msg['Subject'] = f"[RedTape Alert] Change Detected: {target_name}"
        msg['From'] = config.get("smtp_user")
        msg['To'] = config.get("alert_email")
        
        server = smtplib.SMTP(config.get("smtp_server"), int(config.get("smtp_port", 587)))
        server.starttls()
        server.login(config.get("smtp_user"), config.get("smtp_pass"))
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"Failed to send email: {e}")

@celery_app.task
def scan_all_targets():
    db = SessionLocal()
    try:
        config = {cfg.key: cfg.value for cfg in db.query(AppConfig).all()}
        now = datetime.utcnow()
        targets = db.query(MonitoredTarget).filter(MonitoredTarget.is_active == True).all()
        
        for target in targets:
            freq = target.scan_frequency
            if freq == "hourly": threshold = now - timedelta(hours=1)
            elif freq == "daily": threshold = now - timedelta(days=1)
            else: threshold = now - timedelta(days=7)

            if target.last_scanned and target.last_scanned > threshold:
                continue 
                
            try:
                # 1. Scrape the live website
                current_text = scrape_with_depth(target.url, target.extraction_mode, target.recursive)
                if not current_text:
                    db.add(ScanLog(target_id=target.id, status_message="Failed to extract text from URL."))
                    db.commit()
                    continue
                
                current_hash = hashlib.sha256(current_text.encode('utf-8')).hexdigest()
                
                # 2. Check if anything changed physically
                if target.last_hash == current_hash:
                    db.add(ScanLog(target_id=target.id, status_message="No Changes Detected."))
                else:
                    # If this is the very first scan, initialize old_text to match current_text
                    old_text = target.last_text if target.last_text else "No historical text available (First Scan)."
                    
                    # 3. Construct the Intelligent Diff Prompt
                    prompt = f"""You are a regulatory compliance auditor comparing two versions of a monitored text from {target.url}.
                    Your goal is to identify explicit changes in mandates, rules, requirements, deadlines, or compliance standards.
                    
                    Do NOT flag formatting changes, layout shifts, or non-regulatory modifications.
                    If no regulatory items were added, removed, or modified, set Topic to "NONE".
                    
                    --- PASTORAL/HISTORICAL TEXT VERSION ---
                    {old_text[:3500]}
                    
                    --- NEW/CURRENT TEXT VERSION ---
                    {current_text[:3500]}
                    
                    REQUIRED JSON RESPONSE SCHEMA:
                    {{
                        "Topic": "Name of the modified rule/mandate (or 'NONE')",
                        "Summary": "Explain precisely what changed. (e.g., 'High-visibility vest requirement added for warehouse personnel. Minimum penalty for non-compliance raised.')",
                        "Explicit_Dates": "Any new or modified deadlines stated in the text."
                    }}"""
                    
                    try:
                        ai_data = call_llm(prompt, config)
                        if ai_data and ai_data.get('Topic') != "NONE":
                            
                            # SAFETY CHECK: If AI returns a list of dates, join them into a string for the DB
                            raw_dates = ai_data.get('Explicit_Dates', '')
                            safe_dates = ", ".join(str(d) for d in raw_dates) if isinstance(raw_dates, list) else str(raw_dates)

                            db.add(AlertDraft(
                                target_id=target.id, 
                                topic=str(ai_data.get('Topic', 'Regulatory Shift Detected')), 
                                summary_raw=str(ai_data.get('Summary', 'No comparison summary provided.')), 
                                detected_dates=safe_dates
                            ))
                            db.add(ScanLog(target_id=target.id, status_message=f"Diff Engine Triggered: {ai_data.get('Topic')} sent to Triage."))
                            send_alert_email(config, target.resource, ai_data.get('Topic'))
                        else:
                            db.add(ScanLog(target_id=target.id, status_message="Text changed, but local AI found no regulatory significance in the diff."))
                    except Exception as e:
                        db.rollback() # <--- Clear the tainted database session
                        db.add(ScanLog(target_id=target.id, status_message=f"LLM Diff Error: {str(e)[:240]}"))
                
                # 4. Commit current state to history database columns
                target.last_hash = current_hash
                target.last_text = current_text
                target.last_scanned = now 
                db.commit()
                
            except Exception as e:
                db.rollback() # <--- Clear the tainted database session
                db.add(ScanLog(target_id=target.id, status_message=f"Fatal Scan Error: {str(e)[:200]}"))
                db.commit()
    finally:
        db.close()