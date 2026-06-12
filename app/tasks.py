from celery import Celery
from celery.schedules import crontab
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import requests
import hashlib
import json
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

from .models import SessionLocal, MonitoredTarget, AlertDraft, AppConfig

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
    else: # full_page
        for script in soup(["script", "style"]):
            script.decompose()
    return "\n".join(line.strip() for line in soup.get_text(separator="\n").splitlines() if line.strip())

def scrape_with_depth(base_url: str, mode: str, recursive: bool) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        res = requests.get(base_url, headers=headers, timeout=15)
        res.raise_for_status()
        master_text = extract_text(res.text, mode)
        
        if recursive:
            soup = BeautifulSoup(res.text, "html.parser")
            base_domain = urlparse(base_url).netloc
            links_visited = set([base_url])
            
            # Limit to 5 same-domain links to prevent massive memory spikes
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
                    
        return master_text[:15000] # Cap final payload
    except Exception as e:
        return ""

def call_llm(prompt: str, config: dict) -> dict:
    provider = config.get("llm_provider", "local")
    
    try:
        if provider == "openai":
            res = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {config.get('openai_api_key')}"},
                json={"model": "gpt-4o-mini", "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": prompt}]}
            )
            return json.loads(res.json()['choices'][0]['message']['content'])
            
        elif provider == "claude":
            res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": config.get('claude_api_key'), "anthropic-version": "2023-06-01"},
                json={"model": "claude-3-haiku-20240307", "max_tokens": 1000, "messages": [{"role": "user", "content": prompt + "\nRespond ONLY in valid JSON."}]}
            )
            return json.loads(res.json()['content'][0]['text'])
            
        elif provider == "gemini":
            key = config.get("gemini_api_key")
            res = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={key}",
                json={"contents": [{"parts": [{"text": prompt + "\nRespond ONLY in valid JSON."}]}]}
            )
            return json.loads(res.json()['candidates'][0]['content']['parts'][0]['text'])
            
        else: # Local Ollama
            model = config.get("local_model_name", "llama3")
            res = requests.post(
                "http://localhost:11434/api/generate",
                json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
                timeout=180
            )
            return json.loads(res.json()['response'])
    except Exception as e:
        print(f"LLM API Error ({provider}): {str(e)}")
        return {}

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
                current_text = scrape_with_depth(target.url, target.extraction_mode, target.recursive)
                if not current_text: continue
                
                current_hash = hashlib.sha256(current_text.encode('utf-8')).hexdigest()
                
                if target.last_hash != current_hash:
                    prompt = f"""You are a regulatory research assistant. Analyze the text from {target.url}.
                    Extract proposed regulations, changes, or compliance dates.
                    REQUIRED JSON SCHEMA:
                    {{
                        "Topic": "Extract the core topic",
                        "Summary": "Factual summary of exactly what was added, removed, or changed.",
                        "Explicit_Dates": "Explicit dates (Leave blank if none)."
                    }}
                    TEXT: {current_text}"""
                    
                    ai_data = call_llm(prompt, config)
                    
                    if ai_data:
                        db.add(AlertDraft(
                            target_id=target.id,
                            topic=ai_data.get('Topic', 'Pending Review'),
                            summary_raw=ai_data.get('Summary', 'No summary generated.'),
                            detected_dates=ai_data.get('Explicit_Dates', '')
                        ))
                
                target.last_hash = current_hash
                target.last_scanned = now 
                db.commit()
                
            except Exception as e:
                print(f"Error scanning {target.url}: {str(e)}")
    finally:
        db.close()