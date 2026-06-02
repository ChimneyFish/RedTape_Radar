import customtkinter as ctk
import threading
import schedule
import time
import requests
from bs4 import BeautifulSoup
import hashlib
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
import pystray
from PIL import Image, ImageDraw
import os
import subprocess
import json
from urllib.parse import urljoin, urlparse
import difflib

__author__ = "Jim Clemmensen"
__copyright__ = "Copyright 2026, Jim Clemmensen"
__credits__ = ["Jim Clemmensen"]
__license__ = "Proprietary" 
__version__ = "1.0.0"
__maintainer__ = "Jim Clemmensen"
__email__ = "jimclem13@gmail.com"

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class RedTapeRadarApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("RedTape Radar")
        self.geometry("750x700") 
        self.protocol("WM_DELETE_WINDOW", self.hide_window)
        
        self.monitored_urls = {}

        self.config_dir = os.path.join(os.environ.get('LOCALAPPDATA', ''), "RedTapeRadar")
        self.config_path = os.path.join(self.config_dir, "config.json")
        os.makedirs(self.config_dir, exist_ok=True)

        self.ai_process = None
        self.start_local_ai_server()

        self.tabview = ctk.CTkTabview(self, width=710, height=660)
        self.tabview.pack(padx=20, pady=20)
        
        self.tab_dashboard = self.tabview.add("Monitor Dashboard")
        self.tab_settings = self.tabview.add("Settings Configuration")

        self.setup_dashboard_tab()
        self.setup_settings_tab()

        self.load_config()

        self.scheduler_thread = threading.Thread(target=self.run_scheduler, daemon=True)
        self.scheduler_thread.start()

    # --- Persistence ---
    def save_config(self):
        config = {
            "monitored_urls": self.monitored_urls,
            "ai_url": self.ollama_url.get(),
            "ai_model": self.ollama_model.get(),
            "smtp_server": self.smtp_server.get(),
            "smtp_port": self.smtp_port.get(),
            "sender_email": self.sender_email.get(),
            "sender_password": self.sender_password.get(),
            "recipient_email": self.recipient_email.get()
        }
        try:
            with open(self.config_path, "w") as f:
                json.dump(config, f, indent=4)
        except Exception as e:
            self.log(f"Error saving config: {e}")

    def load_config(self):
        if not os.path.exists(self.config_path):
            return
        try:
            with open(self.config_path, "r") as f:
                config = json.load(f)

            self.ollama_url.delete(0, 'end'); self.ollama_url.insert(0, config.get("ai_url", "http://localhost:8080"))
            self.ollama_model.delete(0, 'end'); self.ollama_model.insert(0, config.get("ai_model", "phi3"))
            self.smtp_server.delete(0, 'end'); self.smtp_server.insert(0, config.get("smtp_server", ""))
            self.smtp_port.delete(0, 'end'); self.smtp_port.insert(0, config.get("smtp_port", "587"))
            self.sender_email.delete(0, 'end'); self.sender_email.insert(0, config.get("sender_email", ""))
            self.sender_password.delete(0, 'end'); self.sender_password.insert(0, config.get("sender_password", ""))
            self.recipient_email.delete(0, 'end'); self.recipient_email.insert(0, config.get("recipient_email", ""))

            saved_urls = config.get("monitored_urls", {})
            for url, data in saved_urls.items():
                self.monitored_urls[url] = data
                freq = data.get("freq", "Daily")
                
                if freq == "Hourly":
                    schedule.every(1).hours.do(self.check_website, url=url).tag(url)
                elif freq == "Daily":
                    schedule.every(1).days.do(self.check_website, url=url).tag(url)
                elif freq == "Weekly":
                    schedule.every(1).weeks.do(self.check_website, url=url).tag(url)
                    
            self.refresh_url_list_ui()
            self.log("Previous configuration loaded successfully.")
        except Exception as e:
            self.log(f"Error loading configuration: {e}")

    # --- Local AI ---
    def start_local_ai_server(self):
        try:
            local_app_data = os.environ.get('LOCALAPPDATA')
            server_path = os.path.join(local_app_data, "LlamaMonitor", "llama-server.exe")
            model_path = os.path.join(local_app_data, "LlamaMonitor", "phi3.gguf")
            if os.path.exists(server_path) and os.path.exists(model_path):
                self.ai_process = subprocess.Popen([server_path, "-m", model_path, "--port", "8080"], creationflags=0x08000000)
                print("Local AI Engine initiated on port 8080.")
        except Exception as e:
            print(f"Failed to start AI Engine: {e}")

    # --- System Tray ---
    def create_tray_icon(self):
        image = Image.new('RGB', (64, 64), color=(30, 30, 30))
        dc = ImageDraw.Draw(image)
        dc.rectangle([(16, 16), (48, 48)], fill=(40, 150, 255))
        return image

    def hide_window(self):
        self.withdraw()
        menu = pystray.Menu(pystray.MenuItem('Show Dashboard', self.show_window), pystray.MenuItem('Quit Completely', self.quit_app))
        self.tray_icon = pystray.Icon("RedTapeRadar", self.create_tray_icon(), "RedTape Radar Active", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def show_window(self):
        self.tray_icon.stop()
        self.after(0, self.deiconify)

    def quit_app(self):
        # The Failsafe: Nuke the AI if the user forces the app to close mid-scan
        try:
            subprocess.run(
                ["taskkill", "/f", "/im", "llama-server.exe", "/t"], 
                capture_output=True, 
                creationflags=0x08000000
            )
        except Exception:
            pass
            
        if hasattr(self, 'ai_process') and self.ai_process:
            try:
                self.ai_process.terminate()
            except:
                pass
                
        # Destroy the tray icon and close the UI
        self.tray_icon.stop()
        self.destroy()

    # --- UI Setup ---
    def setup_dashboard_tab(self):
        input_frame = ctk.CTkFrame(self.tab_dashboard)
        input_frame.pack(pady=10, padx=10, fill="x")

        # Top Row: URL and Frequency
        top_row = ctk.CTkFrame(input_frame, fg_color="transparent")
        top_row.pack(fill="x", pady=5)
        self.url_entry = ctk.CTkEntry(top_row, placeholder_text="https://...", width=380)
        self.url_entry.pack(side="left", padx=10)
        self.freq_var = ctk.StringVar(value="Daily")
        freq_dropdown = ctk.CTkOptionMenu(top_row, variable=self.freq_var, values=["5 Minutes", "Hourly", "Daily", "Weekly"], width=100)
        freq_dropdown.pack(side="left", padx=5)
        add_button = ctk.CTkButton(top_row, text="Add Monitor", command=self.add_monitor, width=110)
        add_button.pack(side="left", padx=10)

        # Bottom Row: Target Specifics and Recursion
        bottom_row = ctk.CTkFrame(input_frame, fg_color="transparent")
        bottom_row.pack(fill="x", pady=5)
        
        self.target_mode_var = ctk.StringVar(value="Entire Page")
        mode_dropdown = ctk.CTkOptionMenu(bottom_row, variable=self.target_mode_var, values=["Entire Page", "Specific Element (CSS)"], width=150, command=self.toggle_ui_elements)
        mode_dropdown.pack(side="left", padx=10)

        self.selector_entry = ctk.CTkEntry(bottom_row, placeholder_text="e.g., #content or .main-article", width=180)
        self.selector_entry.pack(side="left", padx=5)
        self.selector_entry.configure(state="disabled", fg_color="gray20")

        self.recursive_var = ctk.BooleanVar(value=False)
        self.recursive_switch = ctk.CTkSwitch(bottom_row, text="Recursive Scrape (1 Layer - Index Pages)", variable=self.recursive_var)
        self.recursive_switch.pack(side="left", padx=20)

        self.active_frame = ctk.CTkScrollableFrame(self.tab_dashboard, height=100, label_text="Actively Monitored URLs")
        self.active_frame.pack(pady=5, padx=10, fill="x")

        self.log_box = ctk.CTkTextbox(self.tab_dashboard, width=670, height=200, font=ctk.CTkFont(family="Courier", size=12))
        self.log_box.pack(pady=10, padx=10, fill="both", expand=True)
        self.log("RedTape Radar initialized.")

    def toggle_ui_elements(self, choice):
        if choice == "Specific Element (CSS)":
            self.selector_entry.configure(state="normal", fg_color=["#F9F9FA", "#343638"])
            self.recursive_switch.deselect()
            self.recursive_switch.configure(state="disabled")
        else:
            self.selector_entry.delete(0, 'end')
            self.selector_entry.configure(state="disabled", fg_color="gray20")
            self.recursive_switch.configure(state="normal")

    def setup_settings_tab(self):
        # --- AI Configuration Section ---
        ai_frame = ctk.CTkLabel(self.tab_settings, text="Local AI Configuration (llama.cpp)", font=ctk.CTkFont(weight="bold"))
        ai_frame.pack(anchor="w", padx=20, pady=(10, 5))

        ctk.CTkLabel(self.tab_settings, text="Local AI Endpoint URL:").pack(anchor="w", padx=20, pady=(5, 0))
        self.ollama_url = ctk.CTkEntry(self.tab_settings, width=630)
        self.ollama_url.pack(padx=20, pady=(0, 5))

        ctk.CTkLabel(self.tab_settings, text="Model Name:").pack(anchor="w", padx=20, pady=(5, 0))
        self.ollama_model = ctk.CTkEntry(self.tab_settings, width=630)
        self.ollama_model.pack(padx=20, pady=(0, 5))

        separator = ctk.CTkFrame(self.tab_settings, height=2, fg_color="gray")
        separator.pack(fill="x", padx=20, pady=10)

        # --- Email Configuration Section ---
        email_label = ctk.CTkLabel(self.tab_settings, text="Email Notification Settings (SMTP)", font=ctk.CTkFont(weight="bold"))
        email_label.pack(anchor="w", padx=20, pady=(0, 5))

        ctk.CTkLabel(self.tab_settings, text="SMTP Server:").pack(anchor="w", padx=20, pady=(5, 0))
        self.smtp_server = ctk.CTkEntry(self.tab_settings, width=630)
        self.smtp_server.pack(padx=20, pady=(0, 5))

        ctk.CTkLabel(self.tab_settings, text="Port:").pack(anchor="w", padx=20, pady=(5, 0))
        self.smtp_port = ctk.CTkEntry(self.tab_settings, width=180)
        self.smtp_port.pack(anchor="w", padx=20, pady=(0, 5))

        ctk.CTkLabel(self.tab_settings, text="Sender Email Address:").pack(anchor="w", padx=20, pady=(5, 0))
        self.sender_email = ctk.CTkEntry(self.tab_settings, width=630)
        self.sender_email.pack(padx=20, pady=(0, 5))

        ctk.CTkLabel(self.tab_settings, text="Sender Password / App Password:").pack(anchor="w", padx=20, pady=(5, 0))
        self.sender_password = ctk.CTkEntry(self.tab_settings, show="*", width=630)
        self.sender_password.pack(padx=20, pady=(0, 5))

        ctk.CTkLabel(self.tab_settings, text="Recipient Email Address:").pack(anchor="w", padx=20, pady=(5, 0))
        self.recipient_email = ctk.CTkEntry(self.tab_settings, width=630)
        self.recipient_email.pack(padx=20, pady=(0, 5))

        # --- Action Buttons ---
        btn_frame = ctk.CTkFrame(self.tab_settings, fg_color="transparent")
        btn_frame.pack(pady=10)
        save_btn = ctk.CTkButton(btn_frame, text="Save Settings", command=self.save_config)
        save_btn.pack(side="left", padx=10)
        test_btn = ctk.CTkButton(btn_frame, text="Test Email", command=self.test_email, fg_color="green", hover_color="darkgreen")
        test_btn.pack(side="left", padx=10)

    # --- Logic ---
    def log(self, message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_box.insert("end", f"[{timestamp}] {message}\n")
        self.log_box.see("end")

    def refresh_url_list_ui(self):
        for widget in self.active_frame.winfo_children():
            widget.destroy()
        for url, data in self.monitored_urls.items():
            row = ctk.CTkFrame(self.active_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            
            # Display mode info
            mode_text = f"[{data.get('mode', 'Page')}]"
            lbl = ctk.CTkLabel(row, text=f"{mode_text} {url[:40]}...", width=400, anchor="w")
            lbl.pack(side="left", padx=5)

            del_btn = ctk.CTkButton(row, text="Remove", width=60, fg_color="darkred", hover_color="red", command=lambda u=url: self.remove_monitor(u))
            del_btn.pack(side="right", padx=5)

    def remove_monitor(self, url):
        if url in self.monitored_urls:
            del self.monitored_urls[url]
            schedule.clear(url) 
            self.log(f"Removed monitor for: {url}")
            self.refresh_url_list_ui()
            self.save_config()

    def add_monitor(self):
        url = self.url_entry.get().strip()
        freq = self.freq_var.get()
        mode = self.target_mode_var.get()
        selector = self.selector_entry.get().strip()
        is_recursive = self.recursive_var.get()

        # 1. Basic Validation
        if not url.startswith("http"):
            self.log("Error: Please specify a valid HTTP/HTTPS URL path.")
            return

        # 2. Prevent duplicate background schedules
        import schedule
        for job in schedule.get_jobs():
            if url in job.tags:
                self.log(f"Notice: {url} is already being monitored.")
                self.url_entry.delete(0, 'end')
                return

        # 3. Save to your internal dictionary (Crucial for your recursive logic)
        self.monitored_urls[url] = {
            'hash': None, 
            'freq': freq, 
            'mode': mode, 
            'selector': selector,
            'recursive': is_recursive
        }
        
        # 4. Schedule the background job
        if freq == "5 Minutes": schedule.every(5).minutes.do(self.check_website, url=url).tag(url)
        elif freq == "Hourly": schedule.every(1).hours.do(self.check_website, url=url).tag(url)
        elif freq == "Daily": schedule.every(1).days.do(self.check_website, url=url).tag(url)
        elif freq == "Weekly": schedule.every(1).weeks.do(self.check_website, url=url).tag(url)

        # 5. UI Cleanup and persistence
        self.log(f"Configured {freq} tracker for: {url}")
        self.url_entry.delete(0, 'end')
        self.refresh_url_list_ui()
        self.save_config()
        
        # 6. Fetch baseline in the background so the UI doesn't freeze
        import threading
        threading.Thread(target=self.check_website, args=(url,), daemon=True).start()

    def test_email(self):
        self.log("Connecting to SMTP server...")
        
        # 1. Grab all the settings from the UI boxes BEFORE threading
        smtp_srv = self.smtp_server.get().strip()
        smtp_prt = self.smtp_port.get().strip()
        sender = self.sender_email.get().strip()
        password = self.sender_password.get().strip()
        recipient = self.recipient_email.get().strip()

        # 2. Define the background worker function
        def background_send():
            try:
                from email.mime.text import MIMEText
                from email.mime.multipart import MIMEMultipart
                import smtplib

                msg = MIMEMultipart()
                msg['From'] = sender
                msg['To'] = recipient
                msg['Subject'] = "RedTape Radar - Test Email"

                body = "If you are reading this, your SMTP configuration is perfect and RedTape Radar is ready to send alerts."
                msg.attach(MIMEText(body, 'plain'))

                server = smtplib.SMTP(smtp_srv, int(smtp_prt))
                server.starttls()
                server.login(sender, password)
                server.send_message(msg)
                server.quit()

                self.log("SUCCESS: Test email sent! Check your inbox.")

            except Exception as e:
                self.log(f"Test Failed: Check your SMTP settings. Error: {e}")

        # 3. Launch the worker in the background (daemon=True ensures it dies if you close the app)
        threading.Thread(target=background_send, daemon=True).start()

    # --- Scraping Engine ---
    def scrape_url(self, url, mode, selector, is_recursive, current_depth=0, visited_urls=None, base_domain=None):
        """Fetches URL content. Handles specific selectors and strict 1-layer recursion."""
        if visited_urls is None: visited_urls = set()
        if base_domain is None: base_domain = urlparse(url).netloc
        
        # Hard limit: If depth is greater than 1, or we already checked this exact URL, stop.
        if current_depth > 1 or url in visited_urls: 
            return ""

        visited_urls.add(url)
        combined_text = ""
        
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            response = requests.get(url, headers=headers, timeout=15)
            soup = BeautifulSoup(response.text, 'html.parser')

            # --- Extract Text ---
            if mode == "Specific Element (CSS)" and selector:
                target_element = soup.select_one(selector)
                if target_element: 
                    combined_text += target_element.get_text(separator=' ', strip=True) + "\n"
            else:
                combined_text += soup.get_text(separator=' ', strip=True) + "\n"

            # --- Handle 1-Layer Recursion ---
            # If recursion is on, AND we are currently on the main index page (depth 0)...
            if is_recursive and current_depth < 1:
                for link in soup.find_all('a', href=True):
                    next_url = urljoin(url, link['href'])
                    
                    # Security check: Only scrape links that share the exact same domain
                    if urlparse(next_url).netloc == base_domain:
                        # Call the scraper on the found link, increasing the depth to 1
                        combined_text += self.scrape_url(next_url, mode, selector, True, current_depth + 1, visited_urls, base_domain)

        except Exception as e:
            pass # Silently skip broken sub-links so they don't crash the whole scan
            
        return combined_text

    def check_website(self, url):
        self.log(f"Scanning target: {url}")
        
        url_data = self.monitored_urls.get(url, {})
        if not url_data: return

        mode = url_data.get('mode', 'Entire Page')
        selector = url_data.get('selector', '')
        is_recursive = url_data.get('recursive', False)

        page_text = self.scrape_url(url, mode, selector, is_recursive)

        if not page_text.strip():
            self.log(f"Error: No content found for {url}. Check selector or connection.")
            return

        current_hash = hashlib.sha256(page_text.encode('utf-8')).hexdigest()
        previous_hash = url_data.get('hash')

        # Create a safe filename for this specific URL to store its text
        safe_filename = hashlib.md5(url.encode('utf-8')).hexdigest() + ".txt"
        cache_file = os.path.join(self.config_dir, safe_filename)

        if previous_hash is None:
            self.monitored_urls[url]['hash'] = current_hash
            # Save the baseline text to disk
            with open(cache_file, "w", encoding="utf-8") as f:
                f.write(page_text)
            self.log(f"Baseline signature saved for: {url}")
            self.save_config()
            
        elif previous_hash != current_hash:
            self.monitored_urls[url]['hash'] = current_hash
            
            # 1. Load the old text
            old_text = ""
            if os.path.exists(cache_file):
                with open(cache_file, "r", encoding="utf-8") as f:
                    old_text = f.read()
            
            # 2. Overwrite with the new text for next time
            with open(cache_file, "w", encoding="utf-8") as f:
                f.write(page_text)
            self.save_config()

            # 3. Use Python to find the exact differences
            diff = difflib.ndiff(old_text.splitlines(), page_text.splitlines())
            
            # Filter out lines that didn't change. Keep only Additions (+) and Deletions (-)
            changes_list = [line for line in diff if line.startswith('+ ') or line.startswith('- ')]
            changes_text = "\n".join(changes_list)

            # Clean up empty lines
            if not changes_text.strip():
                self.log(f"Scan complete. Minor invisible formatting changed, ignoring.")
                return

            self.log(f"ALERT DETECTED: Significant modification on {url}")
            self.trigger_alert(url, changes_text) # Pass the DIFF, not the whole page
            
        else:
            self.log(f"Scan complete. No changes detected.")

    def trigger_alert(self, url, changes_text):
        ai_url = self.ollama_url.get().strip()
        ai_model = self.ollama_model.get().strip()
        
        # New, strict prompt tailored for text differences
        prompt = f"""You are an automated regulatory compliance assistant. 
        Below is a raw list of text additions (+) and deletions (-) that just occurred on a regulatory webpage ({url}).
        
        Your ONLY job is to tell me what changed. 
        - If it is just a minor typo fix, formatting update, or date change, simply say "Minor administrative update: [explain briefly]."
        - If it is a substantive policy, safety, or structural change, summarize exactly what was added or removed.
        - Be extremely brief and direct. Do not write a long essay unless the changes are massive.
        
        RAW CHANGES:
        {changes_text[:6000]}"""
        
        threading.Thread(target=self._process_ai_and_email, args=(ai_url, ai_model, prompt, url), daemon=True).start()

    def _process_ai_and_email(self, ai_url, ai_model, prompt, url):
        self.log("ALERT: Change detected. Waking up AI Engine...")
        
        try:
            # --- 1. START THE AI SERVER ---
            # Define paths based on where the batch script installed the engine
            engine_dir = os.path.join(os.getenv('LOCALAPPDATA'), 'LlamaMonitor')
            model_path = os.path.join(engine_dir, ai_model)
            server_path = os.path.join(engine_dir, 'llama-server.exe')

            # Start it completely invisibly (no command prompt window)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            self.ai_process = subprocess.Popen(
                [server_path, "-m", model_path, "--port", "8080", "-c", "4096"],
                startupinfo=startupinfo,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            
            # Give the heavy model 15 seconds to fully load into RAM
            time.sleep(15) 
            self.log(f"AI Engine booted. Analyzing changes...")

            # --- 2. GET THE AI SUMMARY ---
            payload = {
                "prompt": prompt,
                "n_predict": 500,
                "temperature": 0.1
            }
            # Using a 300-second (5 min) timeout to give the local VM time to think
            response = requests.post(f"{ai_url}/completion", json=payload, timeout=300)
            response.raise_for_status()
            
            ai_summary = response.json().get('content', '').strip()
            self.log("AI Summary generated. Preparing email...")

            # --- 3. SEND THE EMAIL ---
            from email.mime.multipart import MIMEMultipart # Fallback import
            
            msg = MIMEMultipart()
            msg['From'] = self.sender_email.get().strip()
            msg['To'] = self.recipient_email.get().strip()
            msg['Subject'] = f"RedTape Radar Alert: Change Detected on Monitored URL"

            body = f"RedTape Radar detected modifications on the following URL:\n{url}\n\n"
            body += "--- AI Summary of Changes ---\n"
            body += f"{ai_summary}\n\n"
            body += "Please review the URL for complete details."

            msg.attach(MIMEText(body, 'plain'))

            # Connect to Gmail SMTP
            server = smtplib.SMTP(self.smtp_server.get().strip(), int(self.smtp_port.get().strip()))
            server.starttls()
            server.login(self.sender_email.get().strip(), self.sender_password.get().strip())
            server.send_message(msg)
            server.quit()

            self.log("SUCCESS: Notification email dispatched.")

        except requests.exceptions.RequestException as e:
            self.log(f"AI Connection Error: Verify llama-server configuration. {e}")
        except smtplib.SMTPException as e:
            self.log(f"Email Error: Check your SMTP settings/App Password. {e}")
        except Exception as e:
            self.log(f"Processing Error: {e}")

        finally:
            # --- 4. THE GARBAGE COLLECTOR ---
            # This executes instantly after the email sends, or if an error occurs above
            self.log("Shutting down AI Engine to free memory...")
            
            try:
                subprocess.run(
                    ["taskkill", "/f", "/im", "llama-server.exe", "/t"], 
                    capture_output=True, 
                    creationflags=0x08000000
                )
            except Exception:
                pass
                
            if hasattr(self, 'ai_process') and self.ai_process:
                try:
                    self.ai_process.terminate()
                except:
                    pass
            
            self.log("Memory cleared. Resuming background monitoring.")
    def _send_email_notification(self, target_url, body_content):
        try:
            msg = MIMEText(f"Automated scan found updates on tracked page:\n{target_url}\n\nAnalysis Summary:\n{body_content}")
            msg['Subject'] = f"RedTape Radar Alert: Policy Change Detected"
            msg['From'] = self.sender_email.get()
            msg['To'] = self.recipient_email.get()

            server = smtplib.SMTP(self.smtp_server.get(), int(self.smtp_port.get()))
            server.starttls()
            server.login(self.sender_email.get(), self.sender_password.get())
            server.send_message(msg)
            server.quit()
            self.log("Notification email dispatched successfully.")
        except Exception as e:
            self.log(f"Email Dispatch Failure: {e}")

    def run_scheduler(self):
        while True:
            schedule.run_pending()
            time.sleep(1)

if __name__ == "__main__":
    app = RedTapeRadarApp()
    app.mainloop()