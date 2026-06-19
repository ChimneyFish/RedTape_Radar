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

# Changed to run every 5 minutes
celery_app.conf.beat_schedule = {
    'engine-tick': {
        'task': 'app.tasks.scan_all_targets',
        'schedule': crontab(minute='*/5') 
    }
}

_AD_SELECTORS = [
    "[class*='ad-']", "[class*='-ad']", "[class*='ads-']", "[class*='-ads']",
    "[id*='ad-']", "[id*='-ad']", "[class*='banner']", "[id*='banner']",
    "[class*='sponsor']", "[id*='sponsor']", "[class*='promo']", "[id*='promo']",
    "[data-ad]", "[data-dfp]", "[data-google-ad]", "ins.adsbygoogle",
    ".advertisement", "#advertisement", "[class*='widget']",
]

def extract_text(html_content: str, mode: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    if mode == "auto_clean":
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
    elif mode == "body_no_ads":
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        for selector in _AD_SELECTORS:
            for el in soup.select(selector):
                el.decompose()
    else:  # full_page
        for tag in soup(["script", "style"]):
            tag.decompose()
    return "\n".join(line.strip() for line in soup.get_text(separator="\n").splitlines() if line.strip())

def _collect_same_domain_links(soup, base_url: str, base_domain: str, visited: set, limit: int) -> list:
    links = []
    for a_tag in soup.find_all('a', href=True):
        if len(visited) >= limit:
            break
        full_link = urljoin(base_url, a_tag['href'])
        if urlparse(full_link).netloc == base_domain and full_link not in visited:
            visited.add(full_link)
            links.append(full_link)
    return links

def scrape_with_depth(base_url: str, mode: str, recursive: bool) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(base_url, headers=headers, timeout=15)
        res.raise_for_status()
        master_text = extract_text(res.text, mode)
        if recursive:
            base_domain = urlparse(base_url).netloc
            visited = {base_url}

            # Depth 1: links on the base page (up to 5)
            base_soup = BeautifulSoup(res.text, "html.parser")
            depth1_links = _collect_same_domain_links(base_soup, base_url, base_domain, visited, limit=6)

            for link in depth1_links:
                try:
                    sub_res = requests.get(link, headers=headers, timeout=10)
                    master_text += f"\n\n--- Content from {link} ---\n"
                    master_text += extract_text(sub_res.text, mode)

                    # Depth 2: links on each depth-1 page (total cap: 20 pages)
                    sub_soup = BeautifulSoup(sub_res.text, "html.parser")
                    depth2_links = _collect_same_domain_links(sub_soup, link, base_domain, visited, limit=20)
                    for link2 in depth2_links:
                        try:
                            sub_res2 = requests.get(link2, headers=headers, timeout=10)
                            master_text += f"\n\n--- Content from {link2} ---\n"
                            master_text += extract_text(sub_res2.text, mode)
                        except:
                            pass
                except:
                    pass
        return master_text[:15000]
    except Exception:
        return ""

def clean_json_response(raw_text: str) -> dict:
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
            res_json = res.json()
            if "error" in res_json: raise Exception(res_json["error"].get("message", "Google API Error"))
            return clean_json_response(res_json['candidates'][0]['content']['parts'][0]['text'])
        else: 
            model = config.get("local_model_name", "llama3")
            res = requests.post("http://localhost:11434/api/generate", json={"model": model, "prompt": prompt, "stream": False, "format": "json"}, timeout=180)
            return json.loads(res.json()['response'])
    except Exception as e: raise Exception(f"LLM Error ({provider}): {str(e)}")

def send_alert_email(config: dict, target_name: str, topic: str):
    if config.get("enable_emails") != "true": return
    try:
        msg = MIMEText(f"RedTape Radar detected a change on {target_name}.\nTopic: {topic}\nLog in to Triage Inbox to review.")
        msg['Subject'] = f"[RedTape Alert] Change Detected: {target_name}"
        msg['From'] = config.get("smtp_user")
        msg['To'] = config.get("alert_email")
        server = smtplib.SMTP(config.get("smtp_server"), int(config.get("smtp_port", 587)))
        server.starttls()
        server.login(config.get("smtp_user"), config.get("smtp_pass"))
        server.send_message(msg)
        server.quit()
    except Exception: pass

def _process_target(target, db, config, now):
    """Core engine logic extracted for reuse by the scheduler and the instant-baseline task"""
    try:
        current_text = scrape_with_depth(target.url, target.extraction_mode, target.recursive)
        if not current_text:
            db.add(ScanLog(target_id=target.id, status_message="Failed to extract text from URL."))
            db.commit()
            return
        
        current_hash = hashlib.sha256(current_text.encode('utf-8')).hexdigest()
        
        if target.last_hash == current_hash:
            db.add(ScanLog(target_id=target.id, status_message="No Changes Detected."))
        else:
            old_text = target.last_text if target.last_text else "No historical text available (First Scan)."
            prompt = f"""You are an analyst comparing two versions of a webpage from {target.url}.
            Identify and summarize ALL meaningful content changes including text updates, new or removed information, policy changes, regulatory updates, pricing changes, personnel updates, or any other substantive modifications.
            Ignore purely cosmetic differences such as whitespace adjustments, punctuation, or HTML formatting with no semantic impact.
            If there are no meaningful content differences, set Topic to "NONE".
            --- OLD TEXT ---
            {old_text[:3500]}
            --- NEW TEXT ---
            {current_text[:3500]}
            REQUIRED JSON RESPONSE SCHEMA:
            {{ "Topic": "Short title of the change (or 'NONE')", "Summary": "Explain precisely what changed.", "Explicit_Dates": "Any dates or deadlines mentioned." }}"""

            try:
                ai_data = call_llm(prompt, config)
                if ai_data and ai_data.get('Topic') != "NONE":
                    raw_dates = ai_data.get('Explicit_Dates', '')
                    safe_dates = ", ".join(str(d) for d in raw_dates) if isinstance(raw_dates, list) else str(raw_dates)
                    db.add(AlertDraft(target_id=target.id, topic=str(ai_data.get('Topic')), summary_raw=str(ai_data.get('Summary')), detected_dates=safe_dates))
                    db.add(ScanLog(target_id=target.id, status_message=f"Change Detected: {ai_data.get('Topic')} sent to Triage."))
                    send_alert_email(config, target.resource, ai_data.get('Topic'))
                else:
                    db.add(ScanLog(target_id=target.id, status_message="Text changed, but AI found no meaningful content differences."))
            except Exception as e:
                db.rollback()
                db.add(ScanLog(target_id=target.id, status_message=f"LLM Diff Error: {str(e)[:240]}"))
        
        target.last_hash = current_hash
        target.last_text = current_text
        target.last_scanned = now 
        db.commit()
    except Exception as e:
        db.rollback()
        db.add(ScanLog(target_id=target.id, status_message=f"Fatal Scan Error: {str(e)[:200]}"))
        db.commit()

# --- The 5-Minute Scheduler Task ---
@celery_app.task
def scan_all_targets():
    db = SessionLocal()
    try:
        config = {cfg.key: cfg.value for cfg in db.query(AppConfig).all()}
        now = datetime.utcnow()
        targets = db.query(MonitoredTarget).filter(MonitoredTarget.is_active == True).all()
        for target in targets:
            freq = target.scan_frequency
            if freq == "5_min": threshold = now - timedelta(minutes=5)
            elif freq == "hourly": threshold = now - timedelta(hours=1)
            elif freq == "daily": threshold = now - timedelta(days=1)
            else: threshold = now - timedelta(days=7)

            if target.last_scanned and target.last_scanned > threshold:
                continue 
            _process_target(target, db, config, now)
    finally:
        db.close()

# --- NEW: The Instant Baseline Task ---
@celery_app.task
def scan_single_target(target_id: int):
    db = SessionLocal()
    try:
        target = db.query(MonitoredTarget).filter(MonitoredTarget.id == target_id).first()
        if not target: return
        config = {cfg.key: cfg.value for cfg in db.query(AppConfig).all()}
        _process_target(target, db, config, datetime.utcnow())
    finally:
        db.close()