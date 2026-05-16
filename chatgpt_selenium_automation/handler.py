import os
import json
import re
import socket
import time
import subprocess
from typing import Callable, Optional
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
import pyperclip


class ChatGPTAutomation:

    def __init__(
        self,
        chrome_path,
        chrome_driver_path,
        profile_dir=None,
        base_url="https://chatgpt.com",
        start_minimized=False,
        hide_offscreen=False,
        cookie_file_path=None,
        human_verification_callback=None,
        log_callback=None,
        pause_gate: Optional[Callable[[], None]] = None,
    ):
        """
        This constructor automates the following steps:
        1. Open a Chrome browser with remote debugging enabled at a specified URL.
        2. Prompt the user to complete the log-in/registration/human verification, if required.
        3. Connect a Selenium WebDriver to the browser instance after human verification is completed.

        :param chrome_path: file path to chrome.exe (ex. C:\\Users\\User\\...\\chromedriver.exe)
        :param chrome_driver_path: file path to chromedriver.exe (ex. C:\\Users\\User\\...\\chromedriver.exe)
        """

        self.chrome_path = chrome_path
        self.chrome_driver_path = chrome_driver_path
        self.profile_dir = profile_dir
        self.base_url = base_url
        self.start_minimized = start_minimized
        self.hide_offscreen = hide_offscreen
        self.cookie_file_path = cookie_file_path
        self.human_verification_callback = human_verification_callback
        self.log_callback = log_callback
        self._pause_gate = pause_gate or (lambda: None)
        self.chrome_process = None
        self.cookie_name = None

        url = self.base_url
        free_port = self.find_available_port()
        self._log(f"[init] base_url={url}")
        self._log(f"[init] profile_dir={self.profile_dir}")
        self._log(f"[init] remote_debug_port={free_port}")
        self.launch_chrome_with_remote_debugging(free_port, url)
        self._log("[init] Chrome launched, attaching WebDriver...")
        self.driver = self.setup_webdriver(free_port)
        self._log("[init] WebDriver attached.")
        if self.start_minimized:
            try:
                self.driver.minimize_window()
                self._log("[init] Window minimized.")
            except Exception:
                self._log("[init] Minimize failed (ignored).")

        # Primary persistence mechanism: Chrome profile (user-data-dir).
        # Cookie-file restore is optional (only if cookie_file_path is provided).
        cookie_restored = False
        if self.cookie_file_path:
            self._log(f"[init] Trying restore cookie from {self.cookie_file_path}")
            cookie_restored = self.try_restore_cookie(self.cookie_file_path, url)
            self._log(f"[init] cookie_restored={cookie_restored}")

        self._log("[init] Checking logged-in state...")
        logged_in = self.is_logged_in(url=url, timeout=8)
        self._log(f"[init] logged_in={logged_in}")

        if not cookie_restored and not logged_in:
            self._log("[init] Need human verification/login.")
            self.wait_for_human_verification()
        else:
            self._log("[init] Login appears ready; continuing.")

        # Optional: persist cookie token to file (best-effort) if enabled.
        if self.cookie_file_path:
            time.sleep(1.5)
            self.cookie = self.get_cookie()
            if self.cookie:
                self.save_cookie_to_file(self.cookie_file_path)
            else:
                self._log("Không lấy được cookie sau khi đăng nhập (cookie = None).")

    def _log(self, msg: str) -> None:
        if self.log_callback:
            try:
                self.log_callback(msg)
                return
            except Exception:
                pass
        print(msg)
        return

    def _call_pause_gate(self) -> None:
        """Hook for UI pause/resume — safe to call from any wait loop in the worker thread."""
        try:
            self._pause_gate()
        except Exception:
            pass

    @staticmethod
    def find_available_port():
        """ This function finds and returns an available port number on the local machine by creating a temporary
            socket, binding it to an ephemeral port, and then closing the socket. """

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            return s.getsockname()[1]

    def launch_chrome_with_remote_debugging(self, port, url, wait_seconds=30):
        """ Launches a new Chrome instance with remote debugging enabled on the specified port and navigates to the
            provided url """
        chrome_exe = str(self.chrome_path).strip('"')
        remote_profile_dir = os.path.abspath(self.profile_dir) if self.profile_dir else os.path.abspath("remote-profile")
        os.makedirs(remote_profile_dir, exist_ok=True)

        cmd = [
            chrome_exe,
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={remote_profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            url,
        ]
        if self.start_minimized:
            cmd.insert(-1, "--start-minimized")
        if self.hide_offscreen:
            cmd.insert(-1, "--window-position=-32000,-32000")
            cmd.insert(-1, "--window-size=800,600")
        self._log(f"[chrome] Launch: {' '.join(cmd)}")
        # Popen is non-blocking so we can continue to human verification immediately.
        # Keep handle so we can terminate Chrome later.
        try:
            self.chrome_process = subprocess.Popen(cmd, shell=False)
        except Exception as e:
            raise RuntimeError(f"Không mở được Chrome: {e}") from e

        # Wait until Chrome remote debugger accepts connections.
        start_time = time.time()
        while time.time() - start_time < wait_seconds:
            self._call_pause_gate()
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    self._log(f"[chrome] Remote debugging ready on port {port}")
                    return
            except OSError:
                # If Chrome exits immediately, we won't ever get the port.
                if self.chrome_process and self.chrome_process.poll() is not None:
                    code = self.chrome_process.returncode
                    self._log(f"[chrome] Chrome process exited early (code={code}).")
                    break
                time.sleep(0.2)
        self._log(f"[chrome] Remote debugging port not ready after {wait_seconds}s: {port}")
        raise RuntimeError(
            f"Chrome remote debugging không sẵn sàng (port={port}). "
            f"Hãy đảm bảo đã đóng hết Chrome đang chạy (Task Manager), hoặc đổi Profile name."
        )

    def setup_webdriver(self, port):
        """  Initializes a Selenium WebDriver instance, connected to an existing Chrome browser
             with remote debugging enabled on the specified port"""

        self._log(f"[webdriver] Connecting to debuggerAddress=127.0.0.1:{port}")
        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
        service = Service(executable_path=self.chrome_driver_path)
        driver = webdriver.Chrome(service=service, options=chrome_options)
        self._log("[webdriver] Connected.")
        return driver

    def get_cookie(self):
        """
        Get chat.openai.com cookie from the running chrome instance.
        """
        candidate_names = {
            "__Secure-next-auth.session-token",
            "next-auth.session-token",
            "__Host-next-auth.session-token",
        }

        # Retry because auth cookies may be set slightly after verification.
        for _ in range(10):
            self._call_pause_gate()
            try:
                cookies = self.driver.get_cookies()
                chosen = None

                for elem in cookies:
                    name = elem.get("name")
                    if name in candidate_names:
                        chosen = elem
                        break

                # Fallback: search by suffix if names differ.
                if not chosen:
                    for elem in cookies:
                        name = elem.get("name") or ""
                        if name.endswith("next-auth.session-token"):
                            chosen = elem
                            break

                if chosen:
                    self.cookie_name = chosen.get("name")
                    return chosen.get("value")

                cookie_names = [c.get("name") for c in cookies if c.get("name")]
                self.cookie_name = None
                self._log(f"Chưa thấy cookie session-token, thử lại... (sample: {cookie_names[:8]})")
            except Exception:
                pass

            time.sleep(0.5)

        return None

    def _composer_input_elements(self):
        try:
            return self.driver.find_elements(By.XPATH, self._chat_input_xpath())
        except Exception:
            return []

    def _wait_for_composer_presence(self, total_timeout: float) -> bool:
        deadline = time.monotonic() + float(total_timeout)
        while time.monotonic() < deadline:
            self._call_pause_gate()
            for el in self._composer_input_elements():
                try:
                    if el.is_displayed():
                        return True
                except Exception:
                    continue
            time.sleep(0.25)
        return False

    def is_logged_in(self, url=None, timeout=5):
        """Detect whether the main ChatGPT UI is accessible (textarea exists)."""
        try:
            target = url or self.base_url
            self.driver.get(target)
            self._log(f"[login] current_url={self.driver.current_url}")
            return self._wait_for_composer_presence(total_timeout=timeout)
        except Exception:
            return False

    @staticmethod
    def _chat_input_xpath() -> str:
        # ChatGPT UI has changed over time; support both textarea and contenteditable composer.
        return (
            '//textarea[contains(@id, "prompt-textarea")]'
            ' | //textarea[@id="prompt-textarea"]'
            ' | //*[@contenteditable="true" and (@role="textbox" or @data-testid="composer-text-input")]'
        )

    def open_chat(self, url: str, timeout: int = 30) -> None:
        """
        Open a ChatGPT page (main chat or custom GPT link) and wait until the prompt textarea is available.
        """
        self._log(f"[nav] Opening: {url}")
        self.driver.get(url)
        if not self._wait_for_composer_presence(total_timeout=float(timeout)):
            raise TimeoutException(f"Composer not ready within {timeout}s after open: {url}")
        # Best-effort: dismiss common overlays/modals that can block sending.
        self._best_effort_prepare_chat()
        self._log("[nav] Textarea ready.")

    def _best_effort_prepare_chat(self) -> None:
        """
        Best-effort UI prep:
        - Close popups/overlays
        - Accept cookie banners
        - Ensure composer is focusable
        This should be safe to call frequently.
        """
        try:
            self._dismiss_common_overlays()
        except Exception:
            pass
        try:
            comp = self._find_composer()
            self._prime_composer_focus(comp)
        except Exception:
            pass

    def _dismiss_common_overlays(self) -> None:
        """
        Click common modal buttons that may block the composer/send action.
        Uses conservative selectors/text matches to avoid destructive actions.
        """
        # Try a few rounds because overlays can appear after initial load.
        candidates = [
            # Cookies / consent
            "Accept",
            "I agree",
            "Agree",
            "OK",
            "Got it",
            "Continue",
            "Close",
            "Dismiss",
            # Vietnamese
            "Đồng ý",
            "Tôi đồng ý",
            "OK",
            "Tiếp tục",
            "Đóng",
            "Bỏ qua",
        ]

        def _click_by_text_once(label: str) -> bool:
            xps = [
                f'//button[normalize-space()="{label}"]',
                f'//button[contains(normalize-space(),"{label}")]',
                f'//*[@role="button" and normalize-space()="{label}"]',
                f'//*[@role="button" and contains(normalize-space(),"{label}")]',
            ]
            for xp in xps:
                try:
                    els = self.driver.find_elements(By.XPATH, xp)
                except Exception:
                    els = []
                for el in els:
                    try:
                        if not el.is_displayed():
                            continue
                    except Exception:
                        continue
                    try:
                        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                    except Exception:
                        pass
                    try:
                        el.click()
                        return True
                    except Exception:
                        try:
                            self.driver.execute_script("arguments[0].click();", el)
                            return True
                        except Exception:
                            continue
            return False

        for _ in range(3):
            self._call_pause_gate()
            clicked_any = False
            for lab in candidates:
                try:
                    if _click_by_text_once(lab):
                        clicked_any = True
                        time.sleep(0.15)
                except Exception:
                    continue
            if not clicked_any:
                break

    def try_restore_cookie(self, cookie_file_path, url):
        """Load cookie from file, inject into browser, then check if logged in."""
        try:
            if not cookie_file_path or not os.path.exists(cookie_file_path):
                return False

            with open(cookie_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            cookie_value = None
            cookie_name = "__Secure-next-auth.session-token"
            if isinstance(data, dict):
                # Allow file stored either as {"value": "..."} or as full cookie dict.
                cookie_value = data.get("value") or data.get("__Secure-next-auth.session-token")
                cookie_name = data.get("name") or cookie_name
            if not cookie_value:
                return False

            parsed = urlparse(url)
            domain = parsed.hostname or "chat.openai.com"

            # Navigate to domain before adding cookie.
            self.driver.get(url)

            cookie_dict = {
                "name": cookie_name,
                "value": cookie_value,
                "domain": domain,
                "path": "/",
                "secure": True,
                "httpOnly": True,
            }
            self.driver.add_cookie(cookie_dict)
            self.driver.refresh()
            return self.is_logged_in(url=url, timeout=5)
        except Exception:
            return False

    def save_cookie_to_file(self, cookie_file_path):
        """Persist current session cookie to a json file for next run."""
        try:
            cookie_value = self.cookie if self.cookie else self.get_cookie()
            if not cookie_value or not cookie_file_path:
                return

            cookie_dir = os.path.dirname(os.path.abspath(cookie_file_path))
            if cookie_dir:
                os.makedirs(cookie_dir, exist_ok=True)

            with open(cookie_file_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"name": self.cookie_name or "__Secure-next-auth.session-token", "value": cookie_value},
                    f,
                    ensure_ascii=False,
                )
            self._log(f"Đã lưu cookie vào: {cookie_file_path}")
        except Exception:
            # Cookie persistence is best-effort; automation can still work after manual login.
            self._log("Lưu cookie thất bại (sẽ tiếp tục chạy automation, nhưng lần sau có thể cần login lại).")

    def _assistant_messages(self):
        """
        Chỉ các bubble assistant gốc (không nằm trong bubble assistant khác).
        ChatGPT đôi khi lồng 2 node cùng role → innerText trùng → copy/ghép bị duplicate.
        """
        try:
            allm = self.driver.find_elements(
                By.CSS_SELECTOR, 'div[data-message-author-role="assistant"]'
            )
        except Exception:
            return []
        roots = []
        for el in allm:
            try:
                nested = self.driver.execute_script(
                    """
                    var el = arguments[0];
                    var p = el.parentElement;
                    while (p) {
                      if (p.getAttribute && p.getAttribute('data-message-author-role') === 'assistant')
                        return true;
                      p = p.parentElement;
                    }
                    return false;
                    """,
                    el,
                )
                if not nested:
                    roots.append(el)
            except Exception:
                roots.append(el)
        return roots

    def _user_messages(self):
        """Return user messages in the order they appear in the DOM."""
        return self.driver.find_elements(By.CSS_SELECTOR, 'div[data-message-author-role="user"]')

    def _is_generating(self) -> bool:
        """
        Best-effort: detect if the UI is currently generating a response.
        """
        selectors = [
            'button[data-testid="stop-button"]',
            'button[data-testid="stop-generating"]',
            'button[data-testid*="stop"]',
            'button[aria-label*="Stop"]',
            'button[aria-label*="Stop generating"]',
            'button[aria-label*="Dừng tạo"]',
            'button[aria-label*="Dừng"]',
            # Some UIs show "Cancel" instead of "Stop"
            'button[aria-label*="Cancel"]',
        ]
        for sel in selectors:
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    return True
            except Exception:
                continue
        return False

    def wait_until_idle(self, timeout: int = 60) -> None:
        """
        Wait until the UI is not generating anymore.
        This prevents confusing a previous in-flight generation with a new send.
        """
        start = time.time()
        while time.time() - start < timeout:
            self._call_pause_gate()
            try:
                if not self._is_generating():
                    return
            except Exception:
                return
            time.sleep(0.3)
        # Don't hard-fail; some UIs never show a stop button. Continue best-effort.
        self._log("[idle] Timeout waiting for idle; continuing best-effort.")

    def _find_composer(self):
        """
        Find the chat composer element (textarea or contenteditable).
        Returns a WebElement.
        """
        deadline = time.monotonic() + 30.0
        last_exc: Optional[Exception] = None
        while time.monotonic() < deadline:
            self._call_pause_gate()
            try:
                return WebDriverWait(self.driver, 1).until(
                    EC.element_to_be_clickable((By.XPATH, self._chat_input_xpath()))
                )
            except Exception as e:
                last_exc = e
            time.sleep(0.2)
        end_vis = time.monotonic() + 5.0
        while time.monotonic() < end_vis:
            self._call_pause_gate()
            try:
                return WebDriverWait(self.driver, 1).until(
                    EC.visibility_of_element_located((By.XPATH, self._chat_input_xpath()))
                )
            except Exception as e:
                last_exc = e
            time.sleep(0.15)
        raise TimeoutException("Composer not found") from (last_exc or e)

    def _prime_composer_focus(self, composer) -> None:
        """
        Best-effort: ensure the composer is focused and ready.
        Fresh sessions sometimes ignore the first JS text injection until focused.
        """
        if composer is None:
            return
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", composer)
        except Exception:
            pass
        try:
            ActionChains(self.driver).move_to_element(composer).click(composer).pause(0.12).perform()
        except Exception:
            try:
                composer.click()
            except Exception:
                pass
        try:
            self.driver.execute_script("arguments[0].focus();", composer)
        except Exception:
            pass
        time.sleep(0.08)

    def _message_role_count_js(self, role: str) -> int:
        """Đếm bubble theo role; assistant chỉ đếm node gốc (không lồng), khớp _assistant_messages()."""
        role = (role or "").strip().replace('"', "")
        try:
            if role == "assistant":
                n = self.driver.execute_script(
                    """
                    var all = document.querySelectorAll('[data-message-author-role="assistant"]');
                    var c = 0;
                    for (var i = 0; i < all.length; i++) {
                      var el = all[i];
                      var p = el.parentElement;
                      var nested = false;
                      while (p) {
                        if (p.getAttribute && p.getAttribute('data-message-author-role') === 'assistant') {
                          nested = true;
                          break;
                        }
                        p = p.parentElement;
                      }
                      if (!nested) c++;
                    }
                    return c;
                    """
                )
            else:
                n = self.driver.execute_script(
                    """
                    var r = arguments[0];
                    return document.querySelectorAll('[data-message-author-role="' + r + '"]').length;
                    """,
                    role,
                )
            return int(n) if n is not None else 0
        except Exception:
            return 0

    def _last_user_inner_text_js(self) -> str:
        try:
            t = self.driver.execute_script(
                """
                var nodes = document.querySelectorAll('[data-message-author-role="user"]');
                if (!nodes.length) return '';
                var el = nodes[nodes.length - 1];
                return (el.innerText || el.textContent || '').trim();
                """
            )
            return (t or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _normalize_message_for_compare(text: str) -> str:
        """Collapse whitespace for robust prompt/user-message matching."""
        return re.sub(r"\s+", " ", (text or "")).strip()

    @staticmethod
    def _message_matches_prompt(message: str, prompt: str) -> bool:
        msg = ChatGPTAutomation._normalize_message_for_compare(message)
        pr = ChatGPTAutomation._normalize_message_for_compare(prompt)
        if not pr:
            return bool(msg)
        if msg == pr or pr in msg:
            return True
        pre = pr[: min(240, len(pr))]
        return bool(pre and msg.startswith(pre))

    def _composer_inner_content_js(self, el) -> str:
        """Đọc nội dung ô soạn — textarea dùng value; contenteditable dùng innerText."""
        if el is None:
            return ""
        try:
            t = self.driver.execute_script(
                """
                var e = arguments[0];
                if (!e) return '';
                var tag = (e.tagName || '').toLowerCase();
                if (tag === 'textarea') return (e.value || '').trim();
                return (e.innerText || e.textContent || '').trim();
                """,
                el,
            )
            return (t or "").strip()
        except Exception:
            return ""

    def _find_send_button(self):
        """Find the Send button (paper plane) if present — ưu tiên nút hiển thị và không disabled."""
        selectors = [
            'button[data-testid="send-button"]',
            'button[data-testid="composer-send-button"]',
            'button[data-testid*="send"]',
            'button[aria-label="Send prompt"]',
            'button[aria-label="Send message"]',
            'button[aria-label="Gửi"]',
            'button[aria-label="Gửi tin nhắn"]',
        ]
        for sel in selectors:
            try:
                for btn in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if not btn.is_displayed():
                            continue
                    except Exception:
                        continue
                    dis = btn.get_attribute("disabled")
                    aria_dis = btn.get_attribute("aria-disabled")
                    if dis or aria_dis == "true":
                        continue
                    return btn
            except Exception:
                continue
        xps = [
            '//button[contains(@data-testid,"send") and not(@disabled)]',
            '//button[contains(@data-testid,"Send") and not(@disabled)]',
            '//button[contains(@aria-label,"Send") and not(@disabled)]',
            '//button[contains(@aria-label,"Gửi") and not(@disabled)]',
        ]
        for xp in xps:
            try:
                for btn in self.driver.find_elements(By.XPATH, xp):
                    try:
                        if not btn.is_displayed():
                            continue
                    except Exception:
                        continue
                    dis = btn.get_attribute("disabled")
                    aria_dis = btn.get_attribute("aria-disabled")
                    if dis or aria_dis == "true":
                        continue
                    return btn
            except Exception:
                continue
        return None

    def _try_submit_composer_keyboard(self, composer) -> None:
        """Gửi bằng phím: Enter / Ctrl+Enter khi nút Send không ăn."""
        try:
            self.driver.execute_script("arguments[0].focus();", composer)
        except Exception:
            pass
        try:
            composer.click()
        except Exception:
            pass
        try:
            composer.send_keys(Keys.ENTER)
            return
        except Exception:
            pass
        try:
            composer.send_keys(Keys.CONTROL, Keys.ENTER)
        except Exception:
            pass

    def _set_composer_text_js(self, composer, text: str) -> None:
        """
        Set composer value using JS and dispatch input events.
        Works for both textarea and contenteditable composer.
        """
        js = r"""
const el = arguments[0];
const text = arguments[1];
el.focus();
if (el.tagName && el.tagName.toLowerCase() === 'textarea') {
  el.value = text;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
} else {
  // contenteditable / rich text
  el.focus();
  el.textContent = text;
  el.innerText = text;
  el.dispatchEvent(new InputEvent('input', { bubbles: true, data: text, inputType: 'insertText' }));
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}
"""
        self.driver.execute_script(js, composer, text)

    def _click_send(self) -> bool:
        btn = self._find_send_button()
        if not btn:
            return False
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        except Exception:
            pass
        try:
            btn.click()
            return True
        except Exception:
            try:
                self.driver.execute_script("arguments[0].click();", btn)
                return True
            except Exception:
                return False

    def _last_assistant_message(self):
        msgs = self._assistant_messages()
        return msgs[-1] if msgs else None

    def _last_user_message_text(self) -> str:
        msgs = self._user_messages()
        if not msgs:
            return ""
        return (msgs[-1].text or "").strip()

    def _assistant_texts_since_js(self, start_idx: int) -> list[str]:
        """
        Lấy nội dung các bubble assistant (chỉ node gốc, không lồng) từ index start_idx.
        """
        try:
            out = self.driver.execute_script(
                """
                function topLevelAssistantNodes() {
                  var all = document.querySelectorAll('[data-message-author-role="assistant"]');
                  var top = [];
                  for (var i = 0; i < all.length; i++) {
                    var el = all[i];
                    var p = el.parentElement;
                    var nested = false;
                    while (p) {
                      if (p.getAttribute && p.getAttribute('data-message-author-role') === 'assistant') {
                        nested = true;
                        break;
                      }
                      p = p.parentElement;
                    }
                    if (!nested) top.push(el);
                  }
                  return top;
                }
                var s = Math.max(0, arguments[0]);
                var nodes = topLevelAssistantNodes();
                var res = [];
                for (var i = s; i < nodes.length; i++) {
                  var el = nodes[i];
                  var inner = el.querySelector(
                    '[data-message-content],[data-testid="markdown"],div[class*="markdown"],.prose,[class*="prose"]'
                  );
                  var t = '';
                  if (inner) {
                    t = (inner.textContent || inner.innerText || '').trim();
                  }
                  if (!t) {
                    t = (el.textContent || el.innerText || '').trim();
                  }
                  if (t) res.push(t);
                }
                return res;
                """,
                start_idx,
            )
            if isinstance(out, list):
                return [str(x).strip() for x in out if str(x).strip()]
        except Exception:
            pass
        return []

    def _assistant_texts_range_js(self, start_idx: int, end_idx_exclusive: int) -> list[str]:
        """
        Read assistant top-level bubbles for half-open index range [start, end).
        Keeps empty strings per slot (unlike _assistant_texts_since_js) so join aligns with bubble indices.
        """
        if end_idx_exclusive <= start_idx:
            return []
        try:
            out = self.driver.execute_script(
                """
                function topLevelAssistantNodes() {
                  var all = document.querySelectorAll('[data-message-author-role="assistant"]');
                  var top = [];
                  for (var i = 0; i < all.length; i++) {
                    var el = all[i];
                    var p = el.parentElement;
                    var nested = false;
                    while (p) {
                      if (p.getAttribute && p.getAttribute('data-message-author-role') === 'assistant') {
                        nested = true;
                        break;
                      }
                      p = p.parentElement;
                    }
                    if (!nested) top.push(el);
                  }
                  return top;
                }
                var s = Math.max(0, arguments[0]);
                var e = Math.max(s, arguments[1]);
                var nodes = topLevelAssistantNodes();
                var res = [];
                for (var i = s; i < e && i < nodes.length; i++) {
                  var el = nodes[i];
                  var inner = el.querySelector(
                    '[data-message-content],[data-testid="markdown"],div[class*="markdown"],.prose,[class*="prose"],pre'
                  );
                  var t = '';
                  if (inner) {
                    t = (inner.textContent || inner.innerText || '').trim();
                  }
                  if (!t) {
                    t = (el.textContent || el.innerText || '').trim();
                  }
                  res.push(t);
                }
                return res;
                """,
                start_idx,
                end_idx_exclusive,
            )
            if isinstance(out, list):
                return [str(x) for x in out]
        except Exception:
            pass
        return []

    # Written to output files when every recovery path fails (avoids silent empty exports).
    _EXPORT_EMPTY_PLACEHOLDER = (
        "[EXPORT_FAILED] Could not recover assistant text for this prompt. "
        "Check this turn in the browser and re-run if needed."
    )

    def _read_assistant_range_textcontent(self, ba: int, bb: int) -> str:
        """
        One-shot JS read of [ba, bb) top-level assistant bubbles using textContent.
        textContent bypasses CSS line-clamp/overflow-hidden that truncates innerText.
        """
        if bb <= ba:
            return ""
        try:
            result = self.driver.execute_script(
                """
                function topNodes() {
                  var all = document.querySelectorAll('[data-message-author-role="assistant"]');
                  var top = [];
                  for (var i = 0; i < all.length; i++) {
                    var el = all[i], p = el.parentElement, nested = false;
                    while (p) {
                      if (p.getAttribute && p.getAttribute('data-message-author-role') === 'assistant') {
                        nested = true; break;
                      }
                      p = p.parentElement;
                    }
                    if (!nested) top.push(el);
                  }
                  return top;
                }
                var nodes = topNodes();
                var s = Math.max(0, arguments[0]);
                var e = Math.min(nodes.length, arguments[1]);
                if (e <= s) return '';
                var parts = [];
                for (var i = s; i < e; i++) {
                  var el = nodes[i];
                  var inner = el.querySelector(
                    '[data-message-content],[data-testid="markdown"],div[class*="markdown"],.prose,[class*="prose"]'
                  );
                  var target = inner || el;
                  var t = (target.textContent || '').trim();
                  if (!t) t = (target.innerText || '').trim();
                  if (t) parts.push(t);
                }
                return parts.join('\n\n');
                """,
                ba,
                bb,
            )
            return (result or "").strip()
        except Exception:
            return ""

    def extract_response_stable(
        self,
        ba: int,
        bb: int,
        context: str = "",
        rounds: int = 3,
        interval: float = 0.45,
        timeout: float = 30.0,
    ) -> str:
        """
        Read assistant response [ba, bb) with stability verification.
        Polls textContent repeatedly; returns when content is identical for
        `rounds` consecutive reads — guarantees streaming has fully finished.
        Falls back to best-effort content on timeout.
        """
        if bb <= ba:
            return ""
        ctx = context or f"[{ba},{bb})"
        deadline = time.monotonic() + timeout
        last: Optional[str] = None
        stable = 0

        while time.monotonic() < deadline:
            self._call_pause_gate()
            current = self._read_assistant_range_textcontent(ba, bb)
            if not current:
                stable = 0
                time.sleep(interval)
                continue
            if current == last:
                stable += 1
                if stable >= rounds:
                    self._log(f"[stable] OK ({stable} rounds) for {ctx} — {len(current)} chars")
                    return current
            else:
                last = current
                stable = 1
            time.sleep(interval)

        if last:
            self._log(f"[stable] Timeout — best-effort {len(last)} chars for {ctx}")
            return last
        self._log(f"[stable] No content found within {timeout}s for {ctx}")
        return ""

    def _last_assistant_message_id(self) -> str:
        """
        Return data-message-id of the last top-level assistant bubble, '' if none.
        Used to anchor extraction to exactly the response for the current prompt.
        """
        try:
            result = self.driver.execute_script(
                """
                var all = document.querySelectorAll('[data-message-author-role="assistant"]');
                var tops = [];
                for (var i = 0; i < all.length; i++) {
                  var el = all[i], p = el.parentElement, nested = false;
                  while (p) {
                    if (p.getAttribute && p.getAttribute('data-message-author-role') === 'assistant') {
                      nested = true; break;
                    }
                    p = p.parentElement;
                  }
                  if (!nested) tops.push(el);
                }
                return tops.length ? (tops[tops.length - 1].getAttribute('data-message-id') || '') : '';
                """
            )
            return (result or "").strip()
        except Exception:
            return ""

    def _read_new_response_textcontent(self, prev_id: str, ba: int, bb: int = -1) -> str:
        """
        JS: read top-level assistant bubbles that appeared AFTER the one with prev_id.
        Falls back to count-based index ba if prev_id is empty or not found.
        Handles DOM virtualization: when old bubbles are removed, ba may exceed tops.length;
        in that case, use bb-ba (new bubble count) to read from the end of the array.
        Uses textContent (bypasses CSS line-clamp that truncates innerText).
        """
        try:
            result = self.driver.execute_script(
                """
                var prevId = arguments[0];
                var ba     = arguments[1];
                var bb     = arguments[2];

                var all = document.querySelectorAll('[data-message-author-role="assistant"]');
                var tops = [];
                for (var i = 0; i < all.length; i++) {
                  var el = all[i], p = el.parentElement, nested = false;
                  while (p) {
                    if (p.getAttribute && p.getAttribute('data-message-author-role') === 'assistant') {
                      nested = true; break;
                    }
                    p = p.parentElement;
                  }
                  if (!nested) tops.push(el);
                }

                if (tops.length === 0) return '';

                var startIdx;

                // 1. ID-based anchor (most accurate).
                if (prevId) {
                  for (var j = tops.length - 1; j >= 0; j--) {
                    if ((tops[j].getAttribute('data-message-id') || '') === prevId) {
                      startIdx = j + 1;
                      break;
                    }
                  }
                }

                // 2. Count-based fallback.
                if (startIdx === undefined) {
                  startIdx = ba;
                }

                // 3. DOM virtualization fallback: old bubbles removed, startIdx past end.
                //    Use bb-ba (new bubble count) to read from the tail of the array.
                if (startIdx >= tops.length) {
                  if (bb > ba && bb > 0) {
                    startIdx = tops.length - (bb - ba);
                    if (startIdx < 0) startIdx = 0;
                  } else {
                    return '';
                  }
                }

                var parts = [];
                for (var i = startIdx; i < tops.length; i++) {
                  var el = tops[i];
                  var inner = el.querySelector(
                    '[data-message-content],[data-testid="markdown"],div[class*="markdown"],.prose,[class*="prose"]'
                  );
                  var target = inner || el;
                  var t = (target.textContent || '').trim();
                  if (!t) t = (target.innerText || '').trim();
                  if (t) parts.push(t);
                }
                return parts.join('\n\n');
                """,
                prev_id,
                ba,
                bb,
            )
            return (result or "").strip()
        except Exception:
            return ""

    def extract_new_response_stable(
        self,
        prev_id: str,
        ba: int,
        bb: int = -1,
        context: str = "",
        rounds: int = 3,
        interval: float = 0.3,
        timeout: float = 30.0,
    ) -> str:
        """
        Read the assistant response that appeared after prev_id with stability check.
        - prev_id : data-message-id of the last bubble before this prompt was sent.
        - ba      : fallback count index if prev_id is empty or not found in DOM.
        - bb      : count after send — used when ba is past end (DOM virtualization).
        - Polls textContent until identical for `rounds` consecutive reads.
        - On timeout, returns the best-effort content seen so far.

        Multi-path read per cycle:
        1. _read_new_response_textcontent (ID/count-based with virtualization fallback)
        2. _assistant_texts_since_js(_last_turn_start_assistant_count) — proven reliable
        3. Last-resort: all assistant bubbles, take the last n_new
        """
        ctx = context or f"after:{prev_id[:8] if prev_id else 'start'}"
        no_count_growth = bb > 0 and bb <= ba
        if no_count_growth:
            self._log(f"[stable] No count growth for {ctx}; using only id-anchored reads.")
        deadline = time.monotonic() + timeout
        last: Optional[str] = None
        stable = 0
        n_new = max(1, bb - ba) if (bb > 0 and bb > ba) else 1
        # _last_turn_start_assistant_count is set inside send_prompt_to_chatgpt just before send.
        _since = getattr(self, "_last_turn_start_assistant_count", ba)
        if not isinstance(_since, int):
            _since = ba

        while time.monotonic() < deadline:
            self._call_pause_gate()

            # Path 1: ID/count-based with virtualization fallback.
            current = self._read_new_response_textcontent(prev_id, ba, -1 if no_count_growth else bb)

            # Path 2: use the count saved inside send_prompt_to_chatgpt (avoids ba_now drift).
            if not current and not no_count_growth:
                parts = self._assistant_texts_since_js(_since)
                if not parts:
                    # Path 3: virtualization removed old bubbles — read last n_new from full list.
                    all_parts = self._assistant_texts_since_js(0)
                    parts = all_parts[-n_new:] if all_parts else []
                if parts:
                    current = ChatGPTAutomation._join_unique_assistant_chunks(parts)

            if not current:
                stable = 0
                time.sleep(interval)
                continue
            if current == last:
                stable += 1
                if stable >= rounds:
                    self._log(f"[stable] OK ({rounds}×, {len(current)} chars) for {ctx}")
                    return current
            else:
                last = current
                stable = 1
            time.sleep(interval)

        if last:
            self._log(f"[stable] Timeout — best-effort {len(last)} chars for {ctx}")
            return last
        self._log(f"[stable] No content for {ctx}")
        return ""

    def collect_assistant_range(self, start_idx: int, end_idx_exclusive: int) -> str:
        """
        Lấy nội dung các bubble assistant top-level từ index [start_idx, end_idx_exclusive).
        Dùng sau khi đã gửi xong loạt prompt — không cần nút Copy (ổn định khi thao tác tay trên trình duyệt).
        """
        if end_idx_exclusive <= start_idx:
            return ""
        try:
            msgs = self._assistant_messages()
        except Exception:
            return ""
        n = len(msgs)
        if start_idx < 0 or start_idx >= n:
            self._log(f"[collect] start_idx={start_idx} ngoài phạm vi (có {n} bubble assistant).")
            return ""
        end_idx_exclusive = min(end_idx_exclusive, n)
        parts: list[str] = []
        for i in range(start_idx, end_idx_exclusive):
            try:
                parts.append(self._element_plain_text(msgs[i]))
            except StaleElementReferenceException:
                msgs = self._assistant_messages()
                if i >= len(msgs):
                    break
                parts.append(self._element_plain_text(msgs[i]))
        return ChatGPTAutomation._join_unique_assistant_chunks(parts)

    def _copy_assistant_message_via_clipboard(self, msg, timeout: int = 20, reacquire_msg=None):
        """
        Click Copy on a given assistant message element and return clipboard text.
        reacquire_msg: optional callable -> WebElement to re-find after stale references.
        """
        if not msg:
            return ""
        try:
            ActionChains(self.driver).move_to_element(msg).pause(0.25).perform()
        except Exception:
            pass

        copy_btn = self._find_copy_button_for_assistant(msg)
        if not copy_btn:
            self._log("[copy] Không tìm thấy nút Copy trong turn assistant, fallback DOM/ghép turn")
            return self._dom_fallback_last_turn_text(msg)

        before = ""
        for _r in range(3):
            try:
                before = pyperclip.paste() or ""
                break
            except Exception:
                time.sleep(0.1)

        clicked = False
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", copy_btn)
            copy_btn.click()
            clicked = True
        except StaleElementReferenceException:
            msg2 = reacquire_msg() if reacquire_msg else self._last_assistant_message()
            copy_btn = self._find_copy_button_for_assistant(msg2) if msg2 else None
            if copy_btn:
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", copy_btn)
                    self.driver.execute_script("arguments[0].click();", copy_btn)
                    clicked = True
                except Exception:
                    self._log("[copy] Click Copy thất bại (stale), fallback DOM/ghép turn")
            if not clicked and msg2:
                return self._dom_fallback_last_turn_text(msg2)
        except Exception:
            try:
                self.driver.execute_script("arguments[0].click();", copy_btn)
                clicked = True
            except Exception:
                self._log("[copy] Click Copy thất bại, fallback DOM/ghép turn")

        if not clicked:
            msg_fb = reacquire_msg() if reacquire_msg else self._last_assistant_message()
            if not msg_fb:
                msg_fb = msg
            return self._dom_fallback_last_turn_text(msg_fb) if msg_fb else ""

        start = time.time()
        while time.time() - start < timeout:
            self._call_pause_gate()
            now = ""
            for _r in range(3):
                try:
                    now = pyperclip.paste() or ""
                    break
                except Exception:
                    time.sleep(0.08)
            if now and now != before:
                return ChatGPTAutomation._normalize_clipboard_blocks(now)
            time.sleep(0.2)

        self._log("[copy] Clipboard không đổi sau khi bấm Copy, fallback DOM/ghép turn")
        msg_fb = reacquire_msg() if reacquire_msg else None
        if not msg_fb:
            msg_fb = msg
        if not msg_fb and not reacquire_msg:
            msg_fb = self._last_assistant_message()
        return self._dom_fallback_last_turn_text(msg_fb) if msg_fb else ""

    def copy_assistant_bubble_by_index(self, assistant_index: int, timeout: int = 22) -> str:
        """
        Copy a specific top-level assistant bubble by index (for export fallback).
        """
        self.wait_until_idle(timeout=55)
        try:
            msgs = self._assistant_messages()
        except Exception:
            return ""
        if assistant_index < 0 or assistant_index >= len(msgs):
            return ""
        msg = msgs[assistant_index]

        def _reacquire():
            try:
                m = self._assistant_messages()
            except Exception:
                return None
            if assistant_index < 0 or assistant_index >= len(m):
                return None
            return m[assistant_index]

        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", msg)
        except Exception:
            pass
        return self._copy_assistant_message_via_clipboard(msg, timeout=timeout, reacquire_msg=_reacquire)

    def collect_assistant_range_filled(self, start_idx: int, end_idx_exclusive: int, context: str = "") -> str:
        """
        Export assistant range with retries: DOM x2, JS range, then clipboard Copy x2 on last bubble.
        If all fail, returns _EXPORT_EMPTY_PLACEHOLDER so on-disk files are not silently empty.
        """
        if end_idx_exclusive <= start_idx:
            return ""
        ctx = context or f"[{start_idx},{end_idx_exclusive})"
        for round_i in range(2):
            self._call_pause_gate()
            t = (self.collect_assistant_range(start_idx, end_idx_exclusive) or "").strip()
            if t:
                if round_i:
                    self._log(f"[collect] DOM ok on retry {round_i + 1} for {ctx}")
                return t
            time.sleep(0.7)

        parts = self._assistant_texts_range_js(start_idx, end_idx_exclusive)
        t = ChatGPTAutomation._join_unique_assistant_chunks(parts)
        if (t or "").strip():
            self._log(f"[collect] Used JS range fallback for {ctx}")
            return t.strip()

        last_idx = end_idx_exclusive - 1

        def _reacquire_target():
            try:
                m = self._assistant_messages()
            except Exception:
                return None
            if last_idx < 0 or last_idx >= len(m):
                return None
            return m[last_idx]

        for copy_try in (1, 2):
            self._call_pause_gate()
            msg = _reacquire_target()
            if not msg:
                time.sleep(0.35)
                continue
            try:
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", msg)
            except Exception:
                pass
            try:
                ActionChains(self.driver).move_to_element(msg).pause(0.2).perform()
            except Exception:
                pass
            t = (self._copy_assistant_message_via_clipboard(msg, timeout=24, reacquire_msg=_reacquire_target) or "").strip()
            if t:
                self._log(f"[collect] Clipboard recovered content (try {copy_try}) for {ctx}")
                return t
            time.sleep(0.45)

        self._log(f"[collect] All recovery paths failed for {ctx}")
        return ChatGPTAutomation._EXPORT_EMPTY_PLACEHOLDER

    @staticmethod
    def _dedupe_consecutive_chunks(parts: list[str]) -> list[str]:
        """Bỏ đoạn trống và bỏ phần trùng liền kề (cùng nội dung)."""
        out: list[str] = []
        for p in parts:
            t = (p or "").strip()
            if not t:
                continue
            if out and out[-1] == t:
                continue
            out.append(t)
        return out

    @staticmethod
    def _join_unique_assistant_chunks(parts: list[str]) -> str:
        return "\n\n".join(ChatGPTAutomation._dedupe_consecutive_chunks(parts)).strip()

    @staticmethod
    def _normalize_clipboard_blocks(text: str) -> str:
        """Bỏ block văn bản liền kề trùng hệt (clipboard đôi khi dán 2 lần cùng khung)."""
        t = (text or "").rstrip()
        if not t:
            return text or ""
        half = len(t) // 2
        if half >= 40 and t[:half] == t[half:]:
            return t[:half].strip()
        blocks = t.split("\n\n")
        return "\n\n".join(ChatGPTAutomation._dedupe_consecutive_chunks(blocks)).strip()

    def _element_plain_text(self, el) -> str:
        """innerText / markdown con — dùng khi còn tham chiếu WebElement hợp lệ."""
        if el is None:
            return ""
        # Scroll into view — off-screen elements return empty el.text in Chrome.
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'nearest'});", el)
        except Exception:
            pass

        el_text = ""
        try:
            el_text = (el.text or "").strip()
        except StaleElementReferenceException:
            return ""
        except Exception:
            pass

        # textContent bypasses CSS line-clamp/overflow-hidden that can truncate innerText.
        # Use the inner content element to avoid picking up toolbar button labels.
        js_text = ""
        try:
            js_text = (self.driver.execute_script(
                """
                var e = arguments[0];
                var inner = e.querySelector(
                  '[data-message-content],[data-testid="markdown"],div[class*="markdown"],.prose,[class*="prose"]'
                );
                var target = inner || e;
                return (target.textContent || target.innerText || '').trim();
                """,
                el,
            ) or "").strip()
        except Exception:
            pass

        # Take whichever is longer — textContent wins when CSS clamping truncates el.text.
        t = js_text if len(js_text) >= len(el_text) else el_text
        if t:
            return t

        # Last resort: raw innerText of full element.
        try:
            t = (self.driver.execute_script(
                "var e=arguments[0]; return (e.innerText||e.textContent||'').trim();", el
            ) or "").strip()
            return t
        except Exception:
            pass
        return ""

    def _get_assistant_texts_since(self, start_assistant_count: int) -> list[str]:
        """
        Return assistant message texts from a given starting index (by count before sending).
        ChatGPT sometimes splits a single turn into multiple assistant message nodes; this helps avoid missing content.
        """
        if start_assistant_count < 0:
            start_assistant_count = 0
        js_first = self._assistant_texts_since_js(start_assistant_count)
        if js_first:
            return ChatGPTAutomation._dedupe_consecutive_chunks(js_first)
        try:
            msgs = self._assistant_messages()
        except Exception:
            return []
        out: list[str] = []
        for m in msgs[start_assistant_count:]:
            t = self._element_plain_text(m)
            if t:
                out.append(t)
        return ChatGPTAutomation._dedupe_consecutive_chunks(out)

    def _dom_fallback_last_turn_text(self, msg) -> str:
        """Join assistant chunks for the last user turn when clipboard/copy fails."""
        start_idx = getattr(self, "_last_turn_start_assistant_count", None)
        if isinstance(start_idx, int):
            parts = self._assistant_texts_since_js(start_idx)
            if not parts:
                self._log("[copy] Fallback DOM: no new assistant content after this prompt; skip stale last bubble.")
                return ""
            if not parts:
                # DOM lệch / đếm bubble không khớp: lấy ít nhất bubble assistant cuối
                all_parts = self._assistant_texts_since_js(0)
                if all_parts:
                    parts = all_parts[-1:]
            joined = ChatGPTAutomation._join_unique_assistant_chunks(parts)
            if joined:
                self._log("[copy] Fallback DOM: đã lấy nội dung từ bubble assistant (innerText/markdown).")
                return joined

        msg_fresh = self._last_assistant_message()
        for candidate in (msg_fresh, msg):
            t = self._element_plain_text(candidate)
            if t:
                self._log("[copy] Fallback DOM: đã lấy nội dung từ bubble cuối (element).")
                return t

        self._log("[copy] Fallback DOM: không đọc được nội dung (DOM rỗng).")
        return ""

    @staticmethod
    def _lower_attr(el, name: str) -> str:
        try:
            v = el.get_attribute(name)
            return (v or "").strip().lower()
        except Exception:
            return ""

    def _is_likely_copy_button(self, btn) -> bool:
        """True if this toolbar button is clearly Copy (avoid Regenerate / Good response / etc.)."""
        label = self._lower_attr(btn, "aria-label")
        title = self._lower_attr(btn, "title")
        testid = self._lower_attr(btn, "data-testid")
        if "copy" in label or "copy" in title or "copy" in testid:
            return True
        if "sao chép" in label or "sao chép" in title:
            return True
        if "chép" in label and "sao" in label:
            return True
        return False

    def _is_bad_toolbar_button(self, btn) -> bool:
        """Exclude actions that are never Copy."""
        label = self._lower_attr(btn, "aria-label")
        title = self._lower_attr(btn, "title")
        testid = self._lower_attr(btn, "data-testid")
        blob = f"{label} {title} {testid}"
        for bad in (
            "regenerate",
            "read aloud",
            "good response",
            "bad response",
            "share",
            "edit",
            "more",
            "thumbs",
            "like",
            "dislike",
            "dừng",
            "stop",
            "gửi",
            "send",
        ):
            if bad in blob:
                return True
        return False

    def _turn_container_for_assistant(self, msg):
        """
        One conversation turn (user or assistant). Copy hovers live in this subtree only —
        never use #thread section:last (có thể là turn user / turn cũ).
        """
        try:
            return self.driver.execute_script(
                """
                var el = arguments[0];
                return el.closest('[data-testid="conversation-turn"]')
                    || el.closest('section')
                    || el.closest('article')
                    || el;
                """,
                msg,
            )
        except Exception:
            return None

    def _find_copy_button_for_assistant(self, msg):
        """
        Chỉ tìm nút Copy trong turn của assistant message cuối — tránh copy nhầm turn khác.
        """
        if not msg:
            return None

        explicit_rel = [
            './/button[contains(@aria-label,"Copy")]',
            './/button[contains(translate(@aria-label,"COPY","copy"),"copy")]',
            './/button[contains(@title,"Copy")]',
            './/button[contains(@aria-label,"Sao chép")]',
            './/button[contains(@title,"Sao chép")]',
            './/button[contains(@data-testid,"copy")]',
            './/button[@data-testid="copy-turn-action-button"]',
        ]
        for xp in explicit_rel:
            try:
                b = msg.find_element(By.XPATH, xp)
                if b and b.is_displayed() and not self._is_bad_toolbar_button(b):
                    return b
            except Exception:
                continue

        # Toolbar hàng justify-start — ưu tiên nút có nhãn Copy rõ ràng
        try:
            toolbar_btns = msg.find_elements(
                By.XPATH,
                './/div[contains(@class,"justify-start")]//button',
            )
        except Exception:
            toolbar_btns = []
        for b in toolbar_btns:
            try:
                if not b.is_displayed():
                    continue
            except Exception:
                continue
            if self._is_bad_toolbar_button(b):
                continue
            if self._is_likely_copy_button(b):
                return b

        # Trong cùng turn container (section / conversation-turn), vẫn chỉ nhánh assistant này
        turn = self._turn_container_for_assistant(msg)
        if turn is not None and turn != msg:
            for xp in explicit_rel:
                try:
                    b = turn.find_element(By.XPATH, xp)
                    if b and b.is_displayed() and not self._is_bad_toolbar_button(b):
                        try:
                            in_assistant = self.driver.execute_script(
                                """
                                var t = arguments[0], btn = arguments[1];
                                var all = t.querySelectorAll('[data-message-author-role="assistant"]');
                                if (!all.length) return true;
                                var a = all[all.length - 1];
                                return a.contains(btn);
                                """,
                                turn,
                                b,
                            )
                        except Exception:
                            in_assistant = True
                        if in_assistant or self._is_likely_copy_button(b):
                            return b
                except Exception:
                    continue
            try:
                turn_toolbar = turn.find_elements(
                    By.XPATH,
                    './/div[contains(@class,"justify-start")]//button',
                )
            except Exception:
                turn_toolbar = []
            for b in turn_toolbar:
                try:
                    if not b.is_displayed():
                        continue
                except Exception:
                    continue
                if self._is_bad_toolbar_button(b):
                    continue
                if self._is_likely_copy_button(b):
                    return b

        # ChatGPT: trong hàng action, Copy thường là nút đầu — chỉ khi không có nút "xấu" trước nó
        if toolbar_btns:
            for b in toolbar_btns:
                try:
                    if b.is_displayed() and not self._is_bad_toolbar_button(b):
                        return b
                except Exception:
                    continue

        return None

    def copy_last_response(self, timeout=20) -> str:
        """
        Click the Copy button of the last assistant message and return clipboard text.
        This is more reliable than element.text (which can be truncated).
        """
        self.wait_until_idle(timeout=60)
        msg = self._last_assistant_message()
        if not msg:
            return ""

        def _reacquire():
            return self._last_assistant_message()

        return self._copy_assistant_message_via_clipboard(msg, timeout=timeout, reacquire_msg=_reacquire)

    def send_prompt_to_chatgpt(self, prompt):
        """Sends a message to ChatGPT and waits for the assistant response to finish."""
        prompt_norm = (prompt or "").strip()

        self.wait_until_idle(timeout=60)
        # Ensure popups/overlays are not blocking the composer.
        self._best_effort_prepare_chat()

        start_assistant_count = len(self._assistant_messages())
        start_user_js = self._message_role_count_js("user")
        start_assistant_js = self._message_role_count_js("assistant")
        last_user_before = self._last_user_inner_text_js()
        self._last_turn_start_assistant_count = start_assistant_count
        start_user_dom = 0
        try:
            start_user_dom = len(self._user_messages())
        except Exception:
            start_user_dom = 0

        def sent_signal(_driver):
            # Strongest positive signal.
            if self._is_generating():
                return True

            # Sometimes role-count JS lags; use last-user text too.
            lu = self._last_user_inner_text_js()
            if lu and lu != last_user_before:
                if ChatGPTAutomation._message_matches_prompt(lu, prompt_norm):
                    return True

            u_now = self._message_role_count_js("user")
            if u_now > start_user_js:
                try:
                    comp = self._find_composer()
                except Exception:
                    comp = None
                empty = self._composer_inner_content_js(comp) == "" if comp else False
                if lu and ChatGPTAutomation._message_matches_prompt(lu, prompt_norm) and (self._is_generating() or empty):
                    return True
                if (not prompt_norm) and (empty or self._is_generating()):
                    return True
            # DOM-based user count (independent from JS role count).
            try:
                if len(self._user_messages()) > start_user_dom and ChatGPTAutomation._message_matches_prompt(
                    self._last_user_message_text(), prompt_norm
                ):
                    return True
            except Exception:
                pass
            a_now = self._message_role_count_js("assistant")
            if a_now > start_assistant_js and self._is_generating():
                return True
            return False

        last_exc = None
        for attempt in range(1, 4):
            try:
                self._call_pause_gate()
                self._best_effort_prepare_chat()
                composer = self._find_composer()
                self._prime_composer_focus(composer)
                try:
                    composer.send_keys(Keys.CONTROL, "a")
                    composer.send_keys(Keys.BACKSPACE)
                except Exception:
                    pass
                self._set_composer_text_js(composer, prompt)
                time.sleep(0.06)
                # Verify text is actually in composer; some fresh sessions ignore JS input until focused.
                try:
                    cur = self._composer_inner_content_js(composer)
                except Exception:
                    cur = ""
                if prompt_norm and (not cur or (prompt_norm[: min(60, len(prompt_norm))] not in cur)):
                    try:
                        composer.send_keys(Keys.CONTROL, "a")
                        composer.send_keys(Keys.BACKSPACE)
                        composer.send_keys(prompt)
                    except Exception:
                        pass
                # Submit using multiple strategies; fresh sessions can be flaky.
                submitted = False
                try:
                    self._try_submit_composer_keyboard(composer)
                    submitted = True
                except Exception:
                    submitted = False
                if not self._is_generating():
                    try:
                        if self._click_send():
                            submitted = True
                    except Exception:
                        pass
                # Last-resort: try to fire Enter via ActionChains.
                if not submitted and composer is not None:
                    try:
                        ActionChains(self.driver).move_to_element(composer).click(composer).send_keys(Keys.ENTER).perform()
                        submitted = True
                    except Exception:
                        pass

                deadline = time.monotonic() + 70.0
                sent_ok = False
                while time.monotonic() < deadline:
                    self._call_pause_gate()
                    try:
                        # Popups can appear right after submit; keep clearing them.
                        self._dismiss_common_overlays()
                        if sent_signal(self.driver):
                            sent_ok = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.12)
                if not sent_ok:
                    raise TimeoutException("sent_signal not satisfied")
                last_exc = None
                break
            except TimeoutException as e:
                last_exc = e
                self._log(
                    f"[send] Lần {attempt}/3: chưa thấy tín hiệu đã gửi ({e.__class__.__name__}), thử lại..."
                )
                try:
                    self._best_effort_prepare_chat()
                    composer = self._find_composer()
                    self._prime_composer_focus(composer)
                    try:
                        composer.send_keys(Keys.CONTROL, "a")
                        composer.send_keys(Keys.BACKSPACE)
                    except Exception:
                        pass
                    self._set_composer_text_js(composer, prompt)
                    if not self._click_send():
                        self._try_submit_composer_keyboard(composer)
                except Exception as e2:
                    self._log(f"[send] Chuẩn bị retry thất bại: {e2!r}")

        if last_exc is not None:
            raise last_exc

        self.check_response_ended(start_assistant_count=start_assistant_count)

    def check_response_ended(self, start_assistant_count=None, timeout=90):
        """Wait until the latest assistant message stabilizes (text stops changing)."""
        if start_assistant_count is None:
            start_assistant_count = len(self._assistant_messages())

        start_time = time.time()
        last_text = ""
        stable_rounds = 0

        while time.time() - start_time < timeout:
            self._call_pause_gate()
            try:
                msgs = self._assistant_messages()
            except Exception:
                msgs = []

            if len(msgs) < start_assistant_count + 1:
                time.sleep(0.5)
                continue

            # Use textContent — not affected by CSS line-clamp that can truncate el.text prematurely.
            try:
                el = msgs[-1]
                current_text = self.driver.execute_script(
                    """
                    var e = arguments[0];
                    var inner = e.querySelector(
                      '[data-message-content],[data-testid="markdown"],div[class*="markdown"],.prose,[class*="prose"]'
                    );
                    var target = inner || e;
                    return (target.textContent || target.innerText || '').trim();
                    """,
                    el,
                ) or ""
            except StaleElementReferenceException:
                time.sleep(0.3)
                continue
            except Exception:
                time.sleep(0.3)
                continue

            if current_text.strip() and current_text == last_text:
                stable_rounds += 1
                if stable_rounds >= 3:
                    break
            else:
                last_text = current_text
                stable_rounds = 0

            time.sleep(0.5)

        # Small buffer to ensure UI finished re-rendering.
        time.sleep(0.8)

    def return_chatgpt_conversation(self):
        """
        :return: returns a list of items, even items are the submitted questions (prompts) and odd items are chatgpt response
        """

        return self.driver.find_elements(by=By.CSS_SELECTOR, value='div.text-base')

    def save_conversation(self, file_name):
        """
        It saves the full chatgpt conversation of the tab open in chrome into a text file, with the following format:
            prompt: ...
            response: ...
            delimiter
            prompt: ...
            response: ...

        :param file_name: name of the file where you want to save
        """

        directory_name = "conversations"
        if not os.path.exists(directory_name):
            os.makedirs(directory_name)

        delimiter = "|^_^|"
        with open(os.path.join(directory_name, file_name), "a", encoding="utf-8") as file:
            wrote_any = False
            message_elems = self.driver.find_elements(By.CSS_SELECTOR, 'div[data-message-author-role]')
            last_user_prompt = None

            for elem in message_elems:
                role = elem.get_attribute("data-message-author-role")
                text = (elem.text or "").strip()
                if not text:
                    continue

                if role == "user":
                    last_user_prompt = text
                elif role == "assistant" and last_user_prompt is not None:
                    file.write(f"prompt: {last_user_prompt}\nresponse: {text}\n\n{delimiter}\n\n")
                    wrote_any = True
                    last_user_prompt = None

            # Fallback for older/unexpected DOM.
            if not wrote_any:
                chatgpt_conversation = self.return_chatgpt_conversation()
                for i in range(0, len(chatgpt_conversation), 2):
                    if i + 1 >= len(chatgpt_conversation):
                        break
                    file.write(
                        f"prompt: {chatgpt_conversation[i].text}\nresponse: {chatgpt_conversation[i + 1].text}\n\n{delimiter}\n\n")

    def return_last_response(self):
        """ :return: the text of the last chatgpt response """

        msgs = self._assistant_messages()
        if msgs:
            return msgs[-1].text

        # Fallback if message-role attributes are not available.
        response_elements = self.driver.find_elements(by=By.CSS_SELECTOR, value='div.text-base')
        return response_elements[-1].text

    def wait_for_human_verification(self):
        """Wait for user to complete manual login/human verification."""
        if self.human_verification_callback:
            self._log("[login] Waiting for human verification (UI popup)...")
            self.human_verification_callback()
            # Ensure login actually completed before continuing.
            if not self.is_logged_in(url=self.base_url, timeout=180):
                # One more attempt: navigate and wait explicitly (UI can be slow).
                try:
                    self._log("[login] Re-checking by open_chat...")
                    self.open_chat(self.base_url, timeout=180)
                except Exception as e:
                    raise RuntimeError("Đăng nhập chưa xong (không thấy prompt textarea).") from e
            self._log("[login] Human verification complete.")
            return

        print("You need to manually complete the log-in or the human verification if required.")

        while True:
            user_input = input(
                "Enter 'y' if you have completed the log-in or the human verification, or 'n' to check again: "
            ).lower().strip()

            if user_input == 'y':
                print("Continuing with the automation process...")
                break
            elif user_input == 'n':
                print("Waiting for you to complete the human verification...")
                time.sleep(5)  # You can adjust the waiting time as needed
            else:
                print("Invalid input. Please enter 'y' or 'n'.")

    def quit(self):
        """ Closes the browser and terminates the WebDriver session."""
        # Save cookie again right before closing (token may refresh after login).
        if self.cookie_file_path:
            try:
                # Ensure last auth cookies are written before reading.
                time.sleep(0.8)
                self.cookie = self.get_cookie()
                if self.cookie:
                    self.save_cookie_to_file(self.cookie_file_path)
            except Exception:
                pass

        print("Closing the browser...")
        # Capture chromedriver pid (best-effort) before quitting driver.
        chromedriver_pid = None
        try:
            svc = getattr(self.driver, "service", None)
            proc = getattr(svc, "process", None) if svc else None
            chromedriver_pid = getattr(proc, "pid", None) if proc else None
        except Exception:
            chromedriver_pid = None
        try:
            self.driver.close()
        except Exception:
            pass
        try:
            self.driver.quit()
        except Exception:
            pass

        # Best-effort: ensure chromedriver process is not left behind on Windows.
        if os.name == "nt" and chromedriver_pid:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(chromedriver_pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                )
            except Exception:
                pass

        # Terminate the Chrome process started by this automation.
        if self.chrome_process:
            try:
                # On Windows, ensure we kill the full Chrome process tree spawned by this instance
                # without affecting the user's other Chrome windows.
                if os.name == "nt" and self.chrome_process.pid:
                    subprocess.run(
                        ["taskkill", "/PID", str(self.chrome_process.pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                    )
                else:
                    self.chrome_process.terminate()
                    self.chrome_process.wait(timeout=5)
            except Exception:
                try:
                    self.chrome_process.kill()
                except Exception:
                    pass
