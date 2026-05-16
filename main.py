import os
import json
import threading
import queue
import traceback
import time
import shutil
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

from chatgpt_selenium_automation.handler import ChatGPTAutomation


WORKSPACE_ROOT = os.path.dirname(os.path.abspath(__file__))

chrome_driver_path = os.path.join(WORKSPACE_ROOT, "chromedriver.exe")
chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CONFIG_PATH = os.path.join(WORKSPACE_ROOT, "config.json")
PROFILES_ROOT = os.path.join(WORKSPACE_ROOT, "profiles")
LOGS_DIR = os.path.join(WORKSPACE_ROOT, "logs")
DEFAULT_BOT_URL = "https://chatgpt.com/g/g-6890c1f20ccc819184debf18401e6f19-gpt-family-u60"


def _safe_mkdir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _move_to_prompt_backup(src_path: str, prompt_dir: str, log_cb) -> None:
    """
    Move a processed prompt .txt file into PromptBackup/ under prompt_dir.
    Avoid overwriting by appending a timestamp if needed.
    """
    try:
        backup_dir = os.path.join(prompt_dir, "PromptBackup")
        _safe_mkdir(backup_dir)
        base = os.path.basename(src_path)
        dst_path = os.path.join(backup_dir, base)
        if os.path.abspath(dst_path) == os.path.abspath(src_path):
            return
        if os.path.exists(dst_path):
            stem, ext = os.path.splitext(base)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dst_path = os.path.join(backup_dir, f"{stem}_{ts}{ext}")
        shutil.move(src_path, dst_path)
        log_cb(f"[backup] Moved prompt file to: {dst_path}")
    except Exception as e:
        log_cb(f"[backup] Failed to move prompt file: {src_path} ({e!r})")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ChatGPT Selenium Automation")
        self.geometry("920x680")
        self.minsize(520, 420)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.worker_thread = None
        # When clear: worker blocks at pause gates (Pause). When set: run (Resume).
        self._worker_resume = threading.Event()
        self._worker_resume.set()
        self._run_log_lines: list[str] = []
        self._capture_run_log = False
        self._run_end_pending = False
        self._run_started_mono = 0.0

        self.prompt_dir_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.bot_url_var = tk.StringVar(value=DEFAULT_BOT_URL)
        self.profile_name_var = tk.StringVar(value="default")
        self.run_hidden_var = tk.BooleanVar(value=True)
        self.hide_offscreen_var = tk.BooleanVar(value=False)
        self.merge_ranges_var = tk.StringVar()
        self.merge_dir_var = tk.StringVar()
        self.delay_seconds_var = tk.StringVar(value="0")

        self._apply_theme()
        self._build_ui()
        self._load_config()
        self.after(200, self._drain_log_queue)

    def _apply_theme(self):
        # Modern-ish dark theme for ttk + tk widgets.
        self.COLORS = {
            "bg": "#0b1220",
            "panel": "#0f1a2b",
            "panel2": "#0c1526",
            "text": "#e5e7eb",
            "muted": "#9ca3af",
            "accent": "#3b82f6",
            "accent2": "#22c55e",
            "danger": "#ef4444",
            "border": "#22304a",
        }

        base_font = ("Segoe UI", 10)
        self.FONT = {
            "base": base_font,
            "title": ("Segoe UI Semibold", 13),
            "label": ("Segoe UI", 10),
            "small": ("Segoe UI", 9),
            "mono": ("Consolas", 10),
        }

        self.configure(bg=self.COLORS["bg"])
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(
            ".",
            background=self.COLORS["bg"],
            foreground=self.COLORS["text"],
            font=self.FONT["base"],
        )
        style.configure(
            "TFrame",
            background=self.COLORS["bg"],
        )
        style.configure(
            "Card.TLabelframe",
            background=self.COLORS["panel"],
            foreground=self.COLORS["text"],
            bordercolor=self.COLORS["border"],
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=self.COLORS["panel"],
            foreground=self.COLORS["text"],
            font=("Segoe UI Semibold", 12),
        )
        style.configure(
            "TLabel",
            background=self.COLORS["bg"],
            foreground=self.COLORS["text"],
            font=self.FONT["label"],
        )
        style.configure(
            "Hint.TLabel",
            background=self.COLORS["panel"],
            foreground=self.COLORS["muted"],
            font=self.FONT["small"],
        )
        style.configure(
            "TEntry",
            fieldbackground=self.COLORS["panel2"],
            foreground=self.COLORS["text"],
            bordercolor=self.COLORS["border"],
            lightcolor=self.COLORS["border"],
            darkcolor=self.COLORS["border"],
            padding=4,
        )
        style.configure(
            "TCheckbutton",
            background=self.COLORS["panel"],
            foreground=self.COLORS["text"],
        )
        style.configure(
            "TButton",
            padding=(10, 6),
            background=self.COLORS["panel2"],
            foreground=self.COLORS["text"],
            bordercolor=self.COLORS["border"],
        )
        style.map(
            "TButton",
            background=[("active", "#16233b")],
        )
        style.configure(
            "Primary.TButton",
            background=self.COLORS["accent"],
            foreground="#ffffff",
            bordercolor=self.COLORS["accent"],
            font=("Segoe UI Semibold", 10),
            padding=(10, 6),
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#2563eb")],
        )
        style.configure(
            "Danger.TButton",
            background=self.COLORS["danger"],
            foreground="#ffffff",
            bordercolor=self.COLORS["danger"],
            font=("Segoe UI Semibold", 10),
            padding=(10, 6),
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#dc2626")],
        )

    @staticmethod
    def _parse_merge_ranges(text: str):
        """
        Parse merge spec into sorted non-overlapping (start, end) segments.
        Mỗi dòng / token:
          - Khoảng: 4-12  → gộp file 4.txt … 12.txt thành một file 4-12.txt
          - Một file: 16 → chỉ copy/ghép 16.txt → 16.txt trong thư mục merge
        """
        raw = (
            text.replace(",", "\n")
            .replace(";", "\n")
            .replace(" ", "\n")
            .splitlines()
        )
        items = []
        for token in raw:
            token = token.strip()
            if not token:
                continue
            if "-" in token:
                a, b = token.split("-", 1)
                start = int(a.strip())
                end = int(b.strip())
            else:
                start = end = int(token)
            if start < 1 or end < 1 or start > end:
                raise ValueError(f"Range không hợp lệ: '{token}'")
            items.append((start, end))

        items.sort()
        for i in range(1, len(items)):
            prev_s, prev_e = items[i - 1]
            s, e = items[i]
            if s <= prev_e:
                raise ValueError(f"Range bị chồng nhau: {prev_s}-{prev_e} và {s}-{e}")
        return items

    @staticmethod
    def _merge_output_filename(stem: str, s: int, e: int) -> str:
        """Tên file trong thư mục merge: mục 14 → mota.txt; còn lại {stem}n hoặc {stem}a-b."""
        if s == e == 14:
            return "mota.txt"
        if s == e:
            return f"{stem}{s}.txt"
        return f"{stem}{s}-{e}.txt"

    @staticmethod
    def _post_merge_range_files(out_folder: str, merge_root: str, stem: str, file_ranges: list, log_put, flat: bool = False) -> None:
        """
        Read {stem}{i}.txt in out_folder and write merged files.
        flat=True (1 range configured): write directly into merge_root.
        flat=False (multiple ranges): write into merge_root/stem/.
        Mục 14 đơn → mota.txt. Các range khác → {stem}a-b.txt.
        """
        if not file_ranges or not merge_root:
            return
        if flat:
            dest_dir = merge_root
        else:
            dest_dir = os.path.join(merge_root, stem)
        _safe_mkdir(dest_dir)
        for s, e in file_ranges:
            chunks = []
            missing = []
            for i in range(s, e + 1):
                fp = os.path.join(out_folder, f"{stem}{i}.txt")
                if not os.path.isfile(fp):
                    missing.append(fp)
                    continue
                try:
                    with open(fp, "r", encoding="utf-8") as inf:
                        content = inf.read()
                    if content is not None:
                        chunks.append(content.rstrip())
                except Exception as ex:
                    log_put(f"[merge] Không đọc được {fp}: {ex!r}")
            if missing:
                for m in missing:
                    log_put(f"[merge] Thiếu file (bỏ qua nội dung): {m}")
            out_name = App._merge_output_filename(stem, s, e)
            out_path = os.path.join(dest_dir, out_name)
            try:
                merged = "\n\n".join(c for c in chunks if c)
                if chunks and not (merged or "").strip():
                    merged = ChatGPTAutomation._EXPORT_EMPTY_PLACEHOLDER
                    log_put(f"[merge] Nội dung gộp rỗng — ghi placeholder vào {out_path}")
                with open(out_path, "w", encoding="utf-8") as out_f:
                    out_f.write(merged)
                label = f"{s}" if s == e else f"{s}-{e}"
                log_put(f"[merge] Đã gộp {label} -> {out_path} ({len(chunks)} phần)")
            except Exception as ex:
                log_put(f"[merge] Lỗi ghi {out_path}: {ex!r}")

    @staticmethod
    def _widget_is_descendant_of(widget, ancestor) -> bool:
        while widget is not None:
            try:
                if widget == ancestor:
                    return True
                widget = widget.master
            except tk.TclError:
                break
        return False

    def _on_settings_mousewheel(self, event):
        if self._settings_canvas is None or self._settings_scroll_outer is None:
            return
        try:
            w = self.winfo_containing(event.x_root, event.y_root)
        except tk.TclError:
            return
        if w is None or not App._widget_is_descendant_of(w, self._settings_scroll_outer):
            return
        self._settings_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _build_scrollable_settings(self, parent):
        """Cột cài đặt cuộn dọc khi cửa sổ thấp — không mất control."""
        bg = self.COLORS["bg"]
        outer = ttk.Frame(parent)
        self._settings_scroll_outer = outer
        canvas = tk.Canvas(
            outer,
            highlightthickness=0,
            bg=bg,
            bd=0,
        )
        self._settings_canvas = canvas
        vsb = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner_win = canvas.create_window((0, 0), window=inner, anchor=tk.NW)
        canvas.configure(yscrollcommand=vsb.set)

        def _sync_scroll(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        inner.bind("<Configure>", _sync_scroll)

        def _canvas_resize(event):
            # Inner frame rộng bằng viewport để không bị tràn ngang.
            try:
                canvas.itemconfigure(inner_win, width=max(1, event.width))
            except tk.TclError:
                pass
            _sync_scroll()

        canvas.bind("<Configure>", _canvas_resize)

        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        outer.grid_rowconfigure(0, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        self.bind_all("<MouseWheel>", self._on_settings_mousewheel)
        return outer, inner

    def _build_ui(self):
        self._settings_canvas = None
        self._settings_scroll_outer = None

        root = ttk.Frame(self)
        root.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_columnconfigure(0, weight=0)
        header.grid_columnconfigure(1, weight=1)
        ttk.Label(header, text="ChatGPT Selenium Automation", font=self.FONT["title"]).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="Profile riêng • Bot link • Merge output • Copy button",
            style="Hint.TLabel",
            wraplength=280,
        ).grid(row=0, column=1, sticky="ew", padx=(12, 0))

        left_outer, left = self._build_scrollable_settings(root)
        left_outer.grid(row=1, column=0, sticky="nsew", padx=(0, 10), pady=(10, 0))
        right = ttk.Frame(root)
        right.grid(row=1, column=1, sticky="nsew", pady=(10, 0))

        root.grid_columnconfigure(0, weight=3, minsize=240)
        root.grid_columnconfigure(1, weight=5, minsize=200)
        root.grid_rowconfigure(1, weight=1)

        # Left: settings cards
        io_card = ttk.Labelframe(left, text="Inputs", style="Card.TLabelframe")
        io_card.grid(row=0, column=0, sticky="we", pady=(0, 12))
        io_card.grid_columnconfigure(0, weight=1)

        ttk.Label(io_card, text="Prompt directory").grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))
        self._add_dir_row(io_card, 1, self.prompt_dir_var, "Browse", self._choose_prompt_dir, pad_x=12)
        ttk.Label(io_card, text="Chứa nhiều file .txt, mỗi dòng là 1 prompt", style="Hint.TLabel").grid(
            row=2, column=0, sticky="w", padx=12, pady=(0, 10)
        )

        ttk.Label(io_card, text="Bot GPT link").grid(row=3, column=0, sticky="w", padx=12, pady=(0, 4))
        ttk.Entry(io_card, textvariable=self.bot_url_var).grid(row=4, column=0, sticky="we", padx=12, pady=(0, 10))

        out_card = ttk.Labelframe(left, text="Output", style="Card.TLabelframe")
        out_card.grid(row=1, column=0, sticky="we", pady=(0, 12))
        out_card.grid_columnconfigure(0, weight=1)

        ttk.Label(out_card, text="Output directory").grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))
        self._add_dir_row(out_card, 1, self.output_dir_var, "Browse", self._choose_output_dir, pad_x=12)

        ttk.Label(out_card, text="Merge ranges (mỗi dòng 1 range)").grid(
            row=2, column=0, sticky="w", padx=12, pady=(10, 4)
        )
        self.merge_text = tk.Text(
            out_card,
            height=2,
            bg=self.COLORS["panel2"],
            fg=self.COLORS["text"],
            insertbackground=self.COLORS["text"],
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=self.COLORS["border"],
            font=self.FONT["mono"],
        )
        self.merge_text.grid(row=3, column=0, sticky="we", padx=12, pady=(0, 6))
        ttk.Label(
            out_card,
            text="Mỗi dòng: khoảng 4-12, mục 14 → mota.txt. Không chồng range.",
            style="Hint.TLabel",
            wraplength=260,
        ).grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 6))

        ttk.Label(out_card, text="Merge output directory").grid(
            row=5, column=0, sticky="w", padx=12, pady=(0, 4)
        )
        self._add_dir_row(out_card, 6, self.merge_dir_var, "Browse", self._choose_merge_dir, pad_x=12)
        ttk.Label(
            out_card,
            text="1 range: file ghi thẳng vào thư mục này. Nhiều range: tạo <tên_stem>/ bên trong.",
            style="Hint.TLabel",
            wraplength=260,
        ).grid(row=7, column=0, sticky="ew", padx=12, pady=(0, 10))

        ttk.Label(out_card, text="Delay sau mỗi prompt (giây)").grid(
            row=8, column=0, sticky="w", padx=12, pady=(0, 4)
        )
        ttk.Entry(out_card, textvariable=self.delay_seconds_var).grid(
            row=9, column=0, sticky="we", padx=12, pady=(0, 12)
        )

        opt_card = ttk.Labelframe(left, text="Options", style="Card.TLabelframe")
        opt_card.grid(row=2, column=0, sticky="we")
        opt_card.grid_columnconfigure(0, weight=1)

        ttk.Label(opt_card, text="Profile name (Chrome profile riêng)").grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 4)
        )
        ttk.Entry(opt_card, textvariable=self.profile_name_var).grid(
            row=1, column=0, sticky="we", padx=12, pady=(0, 10)
        )

        cb_row = ttk.Frame(opt_card)
        cb_row.grid(row=2, column=0, sticky="w", padx=12, pady=(0, 12))
        ttk.Checkbutton(cb_row, text="Minimize window", variable=self.run_hidden_var).pack(side=tk.LEFT)
        ttk.Checkbutton(cb_row, text="Hide offscreen", variable=self.hide_offscreen_var).pack(side=tk.LEFT, padx=14)

        left.grid_columnconfigure(0, weight=1)

        # Right: actions + logs
        action_card = ttk.Labelframe(right, text="Run", style="Card.TLabelframe")
        action_card.grid(row=0, column=0, sticky="we", pady=(0, 12))
        action_card.grid_columnconfigure(0, weight=1)

        btns = ttk.Frame(action_card)
        btns.grid(row=0, column=0, sticky="we", padx=12, pady=12)
        btns.grid_columnconfigure(0, weight=1)
        btns.grid_columnconfigure(1, weight=1)

        self.start_btn = ttk.Button(btns, text="Start", command=self._on_start, style="Primary.TButton")
        self.start_btn.grid(row=0, column=0, sticky="we")
        self.quit_btn = ttk.Button(btns, text="Quit", command=self._on_quit, style="Danger.TButton")
        self.quit_btn.grid(row=0, column=1, sticky="we", padx=(10, 0))
        self.pause_btn = ttk.Button(
            btns, text="Pause", command=self._on_toggle_pause, state=tk.DISABLED
        )
        self.pause_btn.grid(row=1, column=0, columnspan=2, sticky="we", pady=(8, 0))

        log_card = ttk.Labelframe(right, text="Log", style="Card.TLabelframe")
        log_card.grid(row=1, column=0, sticky="nsew")
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        log_card.grid_rowconfigure(0, weight=1)
        log_card.grid_columnconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_card,
            bg="#050a14",
            fg=self.COLORS["text"],
            insertbackground=self.COLORS["text"],
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=self.COLORS["border"],
            font=self.FONT["mono"],
        )
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=(12, 0), pady=12)
        scrollbar = ttk.Scrollbar(log_card, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 12), pady=12)
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _add_dir_row(self, parent, row, var: tk.StringVar, btn_text: str, choose_fn, pad_x=0):
        row_frame = ttk.Frame(parent)
        row_frame.grid(row=row, column=0, sticky="we", padx=pad_x, pady=(0, 4))
        row_frame.grid_columnconfigure(0, weight=1)
        ttk.Entry(row_frame, textvariable=var).grid(row=0, column=0, sticky="we")
        ttk.Button(row_frame, text=btn_text, command=choose_fn).grid(row=0, column=1, sticky="e", padx=(10, 0))

    def _choose_prompt_dir(self):
        p = filedialog.askdirectory(title="Chọn thư mục prompt")
        if p:
            self.prompt_dir_var.set(p)

    def _choose_output_dir(self):
        p = filedialog.askdirectory(title="Chọn thư mục output")
        if p:
            self.output_dir_var.set(p)

    def _choose_merge_dir(self):
        p = filedialog.askdirectory(title="Chọn thư mục lưu file merge (gộp theo range)")
        if p:
            self.merge_dir_var.set(p)

    def _load_config(self) -> None:
        try:
            if not os.path.exists(CONFIG_PATH):
                return
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.prompt_dir_var.set(data.get("prompt_dir", "") or "")
            self.output_dir_var.set(data.get("output_dir", "") or "")
            self.merge_dir_var.set(data.get("merge_dir", "") or "")
            self.bot_url_var.set(data.get("bot_url", DEFAULT_BOT_URL) or DEFAULT_BOT_URL)
            self.profile_name_var.set(data.get("profile_name", "default") or "default")
            self.run_hidden_var.set(bool(data.get("run_hidden", True)))
            self.hide_offscreen_var.set(bool(data.get("hide_offscreen", False)))
            merge_ranges = data.get("merge_ranges", "") or ""
            self.merge_ranges_var.set(merge_ranges)
            try:
                self.merge_text.delete("1.0", tk.END)
                self.merge_text.insert(tk.END, merge_ranges)
            except Exception:
                pass
            self.delay_seconds_var.set(str(data.get("delay_seconds", "0")))
        except Exception:
            # Config is best-effort; ignore errors and keep UI defaults.
            pass

    def _save_config(self, show_message: bool = False) -> None:
        merge_text = ""
        try:
            merge_text = self.merge_text.get("1.0", tk.END).strip()
        except Exception:
            merge_text = self.merge_ranges_var.get().strip()

        data = {
            "prompt_dir": self.prompt_dir_var.get().strip(),
            "output_dir": self.output_dir_var.get().strip(),
            "merge_dir": self.merge_dir_var.get().strip(),
            "bot_url": self.bot_url_var.get().strip(),
            "profile_name": self.profile_name_var.get().strip() or "default",
            "run_hidden": bool(self.run_hidden_var.get()),
            "hide_offscreen": bool(self.hide_offscreen_var.get()),
            "merge_ranges": merge_text,
            "delay_seconds": self.delay_seconds_var.get().strip(),
        }

        try:
            config_dir = os.path.dirname(os.path.abspath(CONFIG_PATH))
            if config_dir:
                os.makedirs(config_dir, exist_ok=True)

            tmp_path = CONFIG_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            os.replace(tmp_path, CONFIG_PATH)
            self.log_queue.put(f"Đã lưu config: {CONFIG_PATH}")
            if show_message:
                messagebox.showinfo("Config", f"Đã lưu cấu hình vào:\n{CONFIG_PATH}")
        except Exception as e:
            try:
                self.log_queue.put(f"Lưu config thất bại: {e!r}")
                if show_message:
                    messagebox.showerror("Config", f"Lưu cấu hình thất bại:\n{e}\n\nĐường dẫn:\n{CONFIG_PATH}")
            except Exception:
                pass

    def _append_log(self, msg: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        self.log_text.insert(tk.END, line + "\n")
        self.log_text.see(tk.END)
        if self._capture_run_log:
            self._run_log_lines.append(line)

    def _persist_run_log_file(self) -> None:
        self._capture_run_log = False
        lines = self._run_log_lines
        self._run_log_lines = []
        if not lines:
            return
        _safe_mkdir(LOGS_DIR)
        fname = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        path = os.path.join(LOGS_DIR, fname)
        duration = time.monotonic() - getattr(self, "_run_started_mono", time.monotonic())
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
                f.write(
                    f"\n\n--- Kết thúc: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"Thời gian chạy (monotonic): {duration:.2f}s ---\n"
                )
        except Exception as e:
            try:
                self.log_text.insert(
                    tk.END,
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Lưu file log thất bại: {e!r}\n",
                )
            except Exception:
                pass

    def _drain_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        if self._run_end_pending and self.log_queue.empty():
            self._run_end_pending = False
            self._persist_run_log_file()
        self.after(200, self._drain_log_queue)

    def _on_quit(self):
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno("Đang chạy", "Automation đang chạy. Bạn có chắc muốn thoát?"):
                return
        try:
            self.unbind_all("<MouseWheel>")
        except Exception:
            pass
        # Auto-save config on exit (no popup)
        self._save_config(show_message=False)
        self.destroy()

    def _worker_pause_gate(self) -> None:
        if self._worker_resume.is_set():
            return
        self.log_queue.put("[pause] Tạm dừng — bấm Resume để tiếp tục.")
        while not self._worker_resume.is_set():
            self._worker_resume.wait(timeout=0.35)
        self.log_queue.put("[pause] Đã tiếp tục.")

    def _on_toggle_pause(self) -> None:
        if not (self.worker_thread and self.worker_thread.is_alive()):
            return
        if self._worker_resume.is_set():
            self._worker_resume.clear()
            self.pause_btn.config(text="Resume")
        else:
            self._worker_resume.set()
            self.pause_btn.config(text="Pause")

    def _on_start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Đang chạy", "Automation đang chạy rồi.")
            return

        prompt_dir = self.prompt_dir_var.get().strip()
        output_dir = self.output_dir_var.get().strip()
        bot_url = self.bot_url_var.get().strip()
        profile_name = (self.profile_name_var.get().strip() or "default").replace("\\", "_").replace("/", "_")
        profile_dir = os.path.join(PROFILES_ROOT, profile_name)
        run_hidden = bool(self.run_hidden_var.get())
        hide_offscreen = bool(self.hide_offscreen_var.get())
        merge_ranges_text = ""
        try:
            merge_ranges_text = self.merge_text.get("1.0", tk.END).strip()
        except Exception:
            merge_ranges_text = ""
        try:
            merge_ranges = self._parse_merge_ranges(merge_ranges_text)
        except Exception as e:
            messagebox.showerror("Merge ranges", f"Lỗi cấu hình merge ranges:\n{e}")
            return

        try:
            delay_seconds = float((self.delay_seconds_var.get() or "0").strip())
            if delay_seconds < 0:
                raise ValueError("delay_seconds phải >= 0")
        except Exception as e:
            messagebox.showerror("Delay", f"Lỗi cấu hình delay:\n{e}")
            return

        if not prompt_dir or not os.path.isdir(prompt_dir):
            messagebox.showerror("Thiếu prompt dir", "Vui lòng chọn đúng thư mục prompt.")
            return
        if not output_dir:
            messagebox.showerror("Thiếu output dir", "Vui lòng chọn thư mục output.")
            return

        merge_dir = self.merge_dir_var.get().strip()
        if merge_ranges_text.strip() and not merge_dir:
            messagebox.showerror(
                "Merge directory",
                "Bạn đã nhập merge ranges — vui lòng chọn thư mục Merge output directory.",
            )
            return

        if not os.path.isfile(chrome_driver_path):
            messagebox.showerror("Thiếu chromedriver", f"Không thấy `chromedriver.exe` tại:\n{chrome_driver_path}")
            return
        if not os.path.isfile(chrome_path):
            messagebox.showerror("Thiếu Chrome", f"Không thấy Chrome tại:\n{chrome_path}")
            return

        _safe_mkdir(output_dir)
        if merge_dir:
            _safe_mkdir(merge_dir)

        # Disable while running
        self.start_btn.config(state=tk.DISABLED)
        self._worker_resume.set()
        self.pause_btn.config(state=tk.NORMAL, text="Pause")
        self._run_log_lines = []
        self._capture_run_log = True
        self._run_started_mono = time.monotonic()
        self.log_text.delete("1.0", tk.END)
        # Auto-save config so next run doesn't require re-entering (có timestamp trong log file).
        self._save_config(show_message=False)
        self.log_queue.put("Bắt đầu automation...")

        def human_verification_callback():
            ev = threading.Event()

            def show():
                self.log_queue.put("[ui] Popup đăng nhập đã mở. Hãy đăng nhập xong rồi bấm OK.")
                messagebox.showinfo(
                    "Đăng nhập ChatGPT",
                    "Vui lòng đăng nhập ChatGPT và hoàn tất human verification (nếu có).\n"
                    "Sau khi xong, bấm OK để tiếp tục.",
                )
                self.log_queue.put("[ui] Bạn đã bấm OK. Tiếp tục kiểm tra đăng nhập...")
                ev.set()

            # Ensure messagebox executes in the Tk main thread.
            self.after(0, show)
            ev.wait()

        def worker():
            def _sleep_chunked(total_sec: float) -> None:
                if total_sec <= 0:
                    return
                end = time.monotonic() + total_sec
                while time.monotonic() < end:
                    self._worker_pause_gate()
                    rem = end - time.monotonic()
                    if rem <= 0:
                        break
                    time.sleep(min(0.35, rem))

            try:
                txt_files = sorted(
                    [f for f in os.listdir(prompt_dir) if f.lower().endswith(".txt")],
                    key=lambda x: x.lower(),
                )
                if not txt_files:
                    self.log_queue.put("Không tìm thấy file .txt trong prompt directory.")
                    return

                n_files = len(txt_files)
                for file_i, txt_name in enumerate(txt_files, start=1):
                    autom = None
                    try:
                        self._worker_pause_gate()
                        self.log_queue.put(
                            f"[worker] ===== File {file_i}/{n_files}: {txt_name} — phiên Chrome mới (1 file = 1 phiên bot) ====="
                        )
                        autom = ChatGPTAutomation(
                            chrome_path,
                            chrome_driver_path,
                            profile_dir=profile_dir,
                            base_url="https://chatgpt.com",
                            start_minimized=run_hidden,
                            hide_offscreen=hide_offscreen,
                            human_verification_callback=human_verification_callback,
                            log_callback=self.log_queue.put,
                            pause_gate=self._worker_pause_gate,
                        )
                        self.log_queue.put("[worker] Đã gắn Selenium với Chrome.")

                        if bot_url:
                            self.log_queue.put(f"[worker] Mở bot GPT: {bot_url}")
                            autom.open_chat(bot_url)

                        in_path = os.path.join(prompt_dir, txt_name)
                        stem = os.path.splitext(txt_name)[0]
                        out_folder = os.path.join(output_dir, stem)
                        _safe_mkdir(out_folder)

                        with open(in_path, "r", encoding="utf-8-sig") as f:
                            lines = [line.rstrip("\n") for line in f.readlines()]

                        prompts = [ln.strip() for ln in lines if ln.strip()]
                        self.log_queue.put(f"[worker] Đang chạy: {txt_name} ({len(prompts)} prompts)")

                        # Prepare merge range plan for this file.
                        # Only apply ranges fully within [1..len(prompts)].
                        file_ranges = []
                        for s, e in merge_ranges:
                            if s <= len(prompts) and e <= len(prompts):
                                file_ranges.append((s, e))
                            else:
                                self.log_queue.put(
                                    f"[merge] Bỏ qua range {s}-{e} vì vượt quá số prompt ({len(prompts)}) trong {txt_name}"
                                )
                        file_ranges.sort()

                        # Gửi từng prompt → copy ngay (clipboard ưu tiên) → lưu file → thử lại nếu rỗng.
                        _MAX_RETRY = 2
                        for idx, p in enumerate(prompts, start=1):
                            self._worker_pause_gate()
                            out_file = os.path.join(out_folder, f"{stem}{idx}.txt")
                            saved = False
                            t_after_send = time.monotonic()

                            for attempt in range(1, _MAX_RETRY + 2):
                                if attempt > 1:
                                    self.log_queue.put(
                                        f"[worker] [{txt_name}] Prompt {idx}/{len(prompts)} — gửi lại lần {attempt}/{_MAX_RETRY + 1}..."
                                    )
                                else:
                                    self.log_queue.put(
                                        f"[worker] [{txt_name}] Prompt {idx}/{len(prompts)} — gửi..."
                                    )

                                # Ghi nhận ID và count TRƯỚC khi gửi để xác định đúng bubble mới.
                                prev_id = autom._last_assistant_message_id()
                                ba_now = autom._message_role_count_js("assistant")
                                autom.send_prompt_to_chatgpt(p)
                                bb_now = autom._message_role_count_js("assistant")
                                wait_deadline = time.monotonic() + max(8.0, min(30.0, delay_seconds))
                                while bb_now <= ba_now and time.monotonic() < wait_deadline:
                                    self._worker_pause_gate()
                                    time.sleep(0.35)
                                    bb_now = autom._message_role_count_js("assistant")
                                t_after_send = time.monotonic()

                                if bb_now <= ba_now:
                                    self.log_queue.put(
                                        f"[worker] [{txt_name}] Prompt {idx} xong nhưng không thấy bubble assistant mới."
                                    )
                                else:
                                    self.log_queue.put(
                                        f"[worker] [{txt_name}] Prompt {idx} xong ({bb_now - ba_now} bubble)."
                                    )

                                # 1. ID-based + stability: timeout = delay_seconds (min 5s), gộp chung vào delay.
                                resp = autom.extract_new_response_stable(
                                    prev_id, ba_now, bb=bb_now,
                                    context=f"{txt_name} prompt {idx}/{len(prompts)}",
                                    timeout=max(5.0, delay_seconds)
                                )
                                # 2. Multi-method range fallback.
                                if not resp and bb_now > ba_now:
                                    resp = (autom.collect_assistant_range_filled(
                                        ba_now, bb_now,
                                        context=f"{txt_name} prompt {idx}/{len(prompts)}"
                                    ) or "").strip()
                                # 3. Multi-method fallback cuối cùng.
                                if (not resp or resp == ChatGPTAutomation._EXPORT_EMPTY_PLACEHOLDER) and bb_now > ba_now:
                                    resp = (autom.copy_assistant_bubble_by_index(bb_now - 1) or "").strip()

                                is_empty = (not resp) or resp == ChatGPTAutomation._EXPORT_EMPTY_PLACEHOLDER
                                if not is_empty:
                                    try:
                                        with open(out_file, "w", encoding="utf-8") as out_f:
                                            out_f.write(resp)
                                        self.log_queue.put(f"[worker] [{txt_name}] Đã lưu: {out_file}")
                                        saved = True
                                    except Exception as fe:
                                        self.log_queue.put(f"[worker] Lỗi ghi file {out_file}: {fe!r}")
                                    break

                                if attempt <= _MAX_RETRY:
                                    self.log_queue.put(
                                        f"[worker] [{txt_name}] Prompt {idx} nội dung rỗng — thử lại (gửi lại prompt)..."
                                    )
                                else:
                                    self.log_queue.put(
                                        f"[worker] [{txt_name}] Prompt {idx} hết lần thử — ghi placeholder."
                                    )

                            if not saved:
                                try:
                                    with open(out_file, "w", encoding="utf-8") as out_f:
                                        out_f.write(ChatGPTAutomation._EXPORT_EMPTY_PLACEHOLDER)
                                except Exception:
                                    pass

                            if delay_seconds > 0 and idx < len(prompts):
                                elapsed_since_send = time.monotonic() - t_after_send
                                remaining = delay_seconds - elapsed_since_send
                                if remaining > 0.05:
                                    self.log_queue.put(f"[delay] Nghỉ {remaining:.2f}s...")
                                    _sleep_chunked(remaining)
                                else:
                                    self.log_queue.put(f"[delay] Đã chờ {elapsed_since_send:.1f}s — bỏ qua delay.")

                        if file_ranges and merge_dir:
                            self.log_queue.put(
                                f"[merge] [{txt_name}] Gộp {len(file_ranges)} range vào thư mục merge..."
                            )
                            App._post_merge_range_files(
                                out_folder, merge_dir, stem, file_ranges, self.log_queue.put,
                                flat=(len(merge_ranges) == 1),
                            )

                        # After finishing this prompt file (including merge), move it to PromptBackup.
                        _move_to_prompt_backup(in_path, prompt_dir, self.log_queue.put)

                        self.log_queue.put(
                            f"[worker] [{txt_name}] Hoàn thành — đóng Chrome (file {file_i}/{n_files})."
                        )
                    finally:
                        if autom:
                            try:
                                autom.quit()
                            except Exception:
                                pass
                        # Give Windows a moment to release resources before starting next session.
                        _sleep_chunked(1.0)

                self.log_queue.put("[worker] Đã xử lý hết các file prompt — kết thúc.")
            except Exception as e:
                self.log_queue.put(f"Lỗi: {e!r}")
                self.log_queue.put(traceback.format_exc())
                try:
                    self.after(0, lambda: messagebox.showerror("Lỗi", f"{e}"))
                except Exception:
                    pass
            finally:
                def _worker_finished_ui():
                    self.start_btn.config(state=tk.NORMAL)
                    self._worker_resume.set()
                    self.pause_btn.config(state=tk.DISABLED, text="Pause")
                    self._run_end_pending = True

                self.after(0, _worker_finished_ui)

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()


if __name__ == "__main__":
    App().mainloop()
