"""
Sweepstake Genie — GUI front-end built with customtkinter.

Run with:
    python gui.py
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

# Suppress runtime warnings — in a windowed exe they surface as Windows dialogs.
import warnings
warnings.filterwarnings("ignore")

# Must be called before any multiprocessing usage in a PyInstaller frozen exe.
multiprocessing.freeze_support()

import customtkinter as ctk
import yaml
from tkinter import ttk, messagebox

# ── Theme ─────────────────────────────────────────────────────────────────────

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ── Constants ─────────────────────────────────────────────────────────────────

PROFILE_PATH = Path("profile.yaml")
DEFAULT_DB   = "sweepstakes.db"

US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",
]

PROFILE_FIELDS = [
    ("first_name",    "First Name"),
    ("last_name",     "Last Name"),
    ("email",         "Email"),
    ("email_confirm", "Confirm Email"),
    ("phone",         "Phone"),
    ("address1",      "Address Line 1"),
    ("address2",      "Address Line 2"),
    ("city",          "City"),
    ("state",         "State"),
    ("zip",           "ZIP Code"),
    ("country",       "Country"),
    ("dob_month",     "Birth Month (MM)"),
    ("dob_day",       "Birth Day (DD)"),
    ("dob_year",      "Birth Year (YYYY)"),
    ("age",           "Age"),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts() -> str:
    """Return a timestamp string for log prefixes."""
    return datetime.now().strftime("%H:%M:%S")


def _run_in_thread(target_fn) -> threading.Thread:
    """Run *target_fn* in a daemon thread and return it."""
    t = threading.Thread(target=target_fn, daemon=True)
    t.start()
    return t


def _check_playwright_browsers() -> bool:
    """Return True if Playwright Chromium cache directory exists.

    Uses a filesystem check rather than spawning a subprocess so that a
    frozen PyInstaller exe doesn't re-launch itself.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        base = Path.home() / ".cache" / "ms-playwright"

    if not base.exists():
        return False
    return any("chromium" in entry.name.lower() for entry in base.iterdir())


def _install_playwright_browsers(log_q: queue.Queue) -> None:
    """Install Playwright Chromium in a background thread, posting progress to *log_q*."""
    log_q.put(f"[{_ts()}] Installing Playwright Chromium (one-time setup, ~150 MB)…")
    # When frozen as a PyInstaller exe, sys.executable is the exe itself — use
    # the 'playwright' CLI from PATH instead of '-m playwright'.
    if getattr(sys, "frozen", False):
        cmd = ["playwright", "install", "chromium"]
    else:
        cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            if line:
                log_q.put(f"[{_ts()}] {line}")
        proc.wait()
        if proc.returncode == 0:
            log_q.put(f"[{_ts()}] Chromium installed successfully.")
        else:
            log_q.put(f"[{_ts()}] WARNING: Playwright install exited with code {proc.returncode}.")
    except Exception as exc:
        log_q.put(f"[{_ts()}] ERROR installing Playwright: {exc}")


# ── Database history helper ────────────────────────────────────────────────────

def _load_history(db_path: str, limit: int = 100) -> list[dict[str, Any]]:
    """Return the last *limit* entries from *db_path* as a list of dicts."""
    import sqlite3

    path = Path(db_path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT title, url, status, entered_at
            FROM   sweepstakes
            ORDER  BY id DESC
            LIMIT  ?
            """,
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _load_stats(db_path: str) -> dict[str, int]:
    """Return status counts from *db_path*."""
    import sqlite3

    path = Path(db_path)
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM sweepstakes GROUP BY status"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS cnt FROM sweepstakes").fetchone()
        conn.close()
        stats: dict[str, int] = {}
        for row in rows:
            stats[row["status"]] = row["cnt"]
        stats["total"] = total["cnt"] if total else 0
        return stats
    except Exception:
        return {}


# ═════════════════════════════════════════════════════════════════════════════
# Main application window
# ═════════════════════════════════════════════════════════════════════════════

class SweepstakeGenieApp(ctk.CTk):

    def __init__(self) -> None:
        super().__init__()

        self.title("Sweepstake Genie 🎰")
        self.geometry("900x650")
        self.minsize(900, 650)

        # Communication queue between background threads and the UI
        self._log_queue: queue.Queue = queue.Queue()
        # Flag to signal running threads to stop
        self._stop_flag = threading.Event()
        self._running_thread: threading.Thread | None = None

        # DB path (updated from Settings tab)
        self._db_path: str = DEFAULT_DB

        # Build UI
        self._build_tabs()

        # Kick off queue polling
        self.after(100, self._poll_queue)

        # Auto-load profile on startup
        self.after(200, self._auto_load_profile)

        # Check Playwright browsers once the window is visible
        self.after(500, self._startup_browser_check)

    # ── Tab scaffolding ───────────────────────────────────────────────────────

    def _build_tabs(self) -> None:
        self._tabview = ctk.CTkTabview(self, anchor="nw")
        self._tabview.pack(fill="both", expand=True, padx=10, pady=10)

        self._tabview.add("Profile")
        self._tabview.add("Run")
        self._tabview.add("History")
        self._tabview.add("Settings")

        self._build_profile_tab(self._tabview.tab("Profile"))
        self._build_run_tab(self._tabview.tab("Run"))
        self._build_history_tab(self._tabview.tab("History"))
        self._build_settings_tab(self._tabview.tab("Settings"))

    # ─────────────────────────────────────────────────────────────────────────
    # Tab 1 — Profile
    # ─────────────────────────────────────────────────────────────────────────

    def _build_profile_tab(self, parent: ctk.CTkFrame) -> None:
        parent.columnconfigure(1, weight=1)

        self._profile_notice = ctk.CTkLabel(
            parent,
            text="",
            text_color="#aaaaaa",
            wraplength=700,
        )
        self._profile_notice.grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 4))

        self._profile_widgets: dict[str, ctk.CTkEntry | ctk.CTkComboBox] = {}

        for i, (key, label) in enumerate(PROFILE_FIELDS, start=1):
            lbl = ctk.CTkLabel(parent, text=label + ":", anchor="e", width=140)
            lbl.grid(row=i, column=0, sticky="e", padx=(10, 4), pady=3)

            if key == "state":
                widget: ctk.CTkEntry | ctk.CTkComboBox = ctk.CTkComboBox(
                    parent, values=US_STATES, width=220
                )
            else:
                widget = ctk.CTkEntry(parent, width=340)

            widget.grid(row=i, column=1, sticky="w", padx=(0, 10), pady=3)
            self._profile_widgets[key] = widget

        # Buttons row
        btn_row = len(PROFILE_FIELDS) + 1
        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.grid(row=btn_row, column=0, columnspan=2, pady=12)

        ctk.CTkButton(btn_frame, text="Load Profile", command=self._load_profile).pack(
            side="left", padx=6
        )
        ctk.CTkButton(btn_frame, text="Save Profile", command=self._save_profile).pack(
            side="left", padx=6
        )

        self._profile_status_lbl = ctk.CTkLabel(btn_frame, text="")
        self._profile_status_lbl.pack(side="left", padx=10)

    def _load_profile(self, path: Path | None = None, silent: bool = False) -> bool:
        """Load profile.yaml into the profile tab widgets. Returns True on success."""
        path = path or PROFILE_PATH
        if not path.exists():
            if not silent:
                self._profile_notice.configure(
                    text="No profile.yaml found. Fill in your details and click Save."
                )
            return False

        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception as exc:
            if not silent:
                self._profile_status_lbl.configure(
                    text=f"Load error: {exc}", text_color="#ff5555"
                )
            return False

        profile = data.get("profile", {})
        for key, widget in self._profile_widgets.items():
            value = str(profile.get(key, ""))
            if isinstance(widget, ctk.CTkComboBox):
                widget.set(value)
            else:
                widget.delete(0, "end")
                widget.insert(0, value)

        # Also sync settings into settings tab widgets if they exist
        settings = data.get("settings", {})
        self._sync_settings_from_dict(settings)

        self._profile_notice.configure(text="")
        if not silent:
            self._profile_status_lbl.configure(
                text="Profile loaded.", text_color="#55ff55"
            )
        return True

    def _save_profile(self) -> None:
        """Write current widget values to profile.yaml."""
        profile: dict[str, str] = {}
        for key, widget in self._profile_widgets.items():
            if isinstance(widget, ctk.CTkComboBox):
                profile[key] = widget.get()
            else:
                profile[key] = widget.get()

        # Merge in current settings values
        settings = self._collect_settings_dict()

        data = {"profile": profile, "settings": settings}

        try:
            with PROFILE_PATH.open("w", encoding="utf-8") as fh:
                yaml.dump(data, fh, default_flow_style=False, allow_unicode=True)
            self._profile_status_lbl.configure(text="Saved!", text_color="#55ff55")
        except Exception as exc:
            self._profile_status_lbl.configure(
                text=f"Save error: {exc}", text_color="#ff5555"
            )

    def _auto_load_profile(self) -> None:
        loaded = self._load_profile(silent=True)
        if not loaded:
            self._profile_notice.configure(
                text="No profile.yaml found. Fill in your details and click Save."
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Tab 2 — Run
    # ─────────────────────────────────────────────────────────────────────────

    def _build_run_tab(self, parent: ctk.CTkFrame) -> None:
        # Stats bar
        stats_frame = ctk.CTkFrame(parent)
        stats_frame.pack(fill="x", padx=10, pady=(10, 4))

        self._stat_labels: dict[str, ctk.CTkLabel] = {}
        for stat_key, display in [
            ("found",   "Found"),
            ("entered", "Entered"),
            ("skipped", "Skipped"),
            ("captcha", "CAPTCHA"),
        ]:
            f = ctk.CTkFrame(stats_frame, fg_color="transparent")
            f.pack(side="left", padx=16, pady=6)
            ctk.CTkLabel(f, text=display + ":", text_color="#aaaaaa").pack()
            lbl = ctk.CTkLabel(f, text="0", font=ctk.CTkFont(size=20, weight="bold"))
            lbl.pack()
            self._stat_labels[stat_key] = lbl

        # Buttons
        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=4)

        self._btn_discover = ctk.CTkButton(
            btn_frame, text="Discover Only", command=self._start_discover
        )
        self._btn_discover.pack(side="left", padx=6)

        self._btn_run = ctk.CTkButton(
            btn_frame, text="Run Full", command=self._start_run_full
        )
        self._btn_run.pack(side="left", padx=6)

        self._btn_stop = ctk.CTkButton(
            btn_frame,
            text="Stop",
            fg_color="#883333",
            hover_color="#aa4444",
            command=self._stop_run,
        )
        self._btn_stop.pack(side="left", padx=6)
        self._btn_stop.configure(state="disabled")

        # Status label
        self._run_status_lbl = ctk.CTkLabel(
            parent, text="Ready", font=ctk.CTkFont(size=13)
        )
        self._run_status_lbl.pack(anchor="w", padx=12, pady=2)

        # Progress bar
        self._progress = ctk.CTkProgressBar(parent, mode="indeterminate")
        self._progress.pack(fill="x", padx=10, pady=4)
        self._progress.set(0)

        # Log output
        log_label = ctk.CTkLabel(parent, text="Log Output:", anchor="w")
        log_label.pack(anchor="w", padx=12, pady=(6, 0))

        self._log_text = ctk.CTkTextbox(parent, state="disabled", wrap="word")
        self._log_text.pack(fill="both", expand=True, padx=10, pady=(2, 10))

    def _append_log(self, msg: str) -> None:
        self._log_text.configure(state="normal")
        self._log_text.insert("end", msg + "\n")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _set_run_status(self, text: str) -> None:
        self._run_status_lbl.configure(text=text)

    def _set_buttons_running(self, running: bool) -> None:
        state_inactive = "disabled" if running else "normal"
        state_stop     = "normal"   if running else "disabled"
        self._btn_discover.configure(state=state_inactive)
        self._btn_run.configure(state=state_inactive)
        self._btn_stop.configure(state=state_stop)
        if running:
            self._progress.configure(mode="indeterminate")
            self._progress.start()
        else:
            self._progress.stop()
            self._progress.configure(mode="determinate")
            self._progress.set(0)

    def _start_discover(self) -> None:
        self._stop_flag.clear()
        self._set_buttons_running(True)
        self._set_run_status("Discovering…")
        self._append_log(f"[{_ts()}] Starting discovery…")

        def task() -> None:
            try:
                from sweepstake_genie.database import Database
                from sweepstake_genie.scraper import discover_all, list_sources

                db = Database(self._db_path)
                sources = list_sources()
                self._log_queue.put(
                    f"[{_ts()}] Scraping {len(sources)} sources: "
                    + ", ".join(sources)
                )
                sweepstakes = discover_all()
                new_count = 0
                for sw in sweepstakes:
                    if self._stop_flag.is_set():
                        self._log_queue.put(f"[{_ts()}] Stopped by user.")
                        break
                    added = db.add_sweepstake(sw["url"], sw["title"], sw["source"])
                    if added:
                        new_count += 1
                self._log_queue.put(
                    f"[{_ts()}] Found {len(sweepstakes)} sweepstakes, "
                    f"{new_count} new added to database."
                )
                self._log_queue.put(("stats_refresh", None))
            except Exception as exc:
                self._log_queue.put(f"[{_ts()}] ERROR: {exc}")
            finally:
                self._log_queue.put(("done", "Discovering" if not self._stop_flag.is_set() else "Stopped"))

        self._running_thread = _run_in_thread(task)

    def _start_run_full(self) -> None:
        self._stop_flag.clear()
        self._set_buttons_running(True)
        self._set_run_status("Starting…")
        self._append_log(f"[{_ts()}] Starting full run…")

        def task() -> None:
            try:
                from sweepstake_genie.config import Config
                from sweepstake_genie.database import Database
                from sweepstake_genie.scraper import discover_all, list_sources
                from sweepstake_genie.browser import BrowserManager
                from sweepstake_genie.form_filler import enter_sweepstake

                # Load config
                try:
                    config = Config(PROFILE_PATH)
                except FileNotFoundError:
                    self._log_queue.put(
                        f"[{_ts()}] ERROR: profile.yaml not found. "
                        "Please fill in your profile and click Save first."
                    )
                    self._log_queue.put(("done", "Error"))
                    return

                db = Database(self._db_path)

                # ── Discover phase ────────────────────────────────────────
                self._log_queue.put(("status", "Discovering…"))
                sources = list_sources()
                self._log_queue.put(
                    f"[{_ts()}] Scraping {len(sources)} sources: "
                    + ", ".join(sources)
                )
                sweepstakes = discover_all()
                new_count = 0
                for sw in sweepstakes:
                    if self._stop_flag.is_set():
                        break
                    added = db.add_sweepstake(sw["url"], sw["title"], sw["source"])
                    if added:
                        new_count += 1
                self._log_queue.put(
                    f"[{_ts()}] Found {len(sweepstakes)} sweepstakes, "
                    f"{new_count} new."
                )
                self._log_queue.put(("stats_refresh", None))

                if self._stop_flag.is_set():
                    self._log_queue.put(("done", "Stopped"))
                    return

                # ── Entry phase ───────────────────────────────────────────
                pending = db.get_pending()
                if not pending:
                    self._log_queue.put(f"[{_ts()}] No pending sweepstakes to enter.")
                    self._log_queue.put(("done", "Done"))
                    return

                limit = config.max_entries_per_run
                if len(pending) > limit:
                    self._log_queue.put(
                        f"[{_ts()}] {len(pending)} pending; capping at {limit}."
                    )
                    pending = pending[:limit]

                self._log_queue.put(("status", f"Entering {len(pending)} sweepstakes…"))
                self._log_queue.put(
                    f"[{_ts()}] Entering {len(pending)} sweepstakes…"
                )

                counts: dict[str, int] = {
                    "entered": 0, "captcha": 0, "no_form": 0, "error": 0
                }
                total = len(pending)

                async def _run_entries() -> None:
                    async with BrowserManager(headless=config.headless) as bm:
                        page = await bm.new_page()
                        for idx, sw in enumerate(pending, start=1):
                            if self._stop_flag.is_set():
                                self._log_queue.put(
                                    f"[{_ts()}] Stopped by user after {idx - 1} entries."
                                )
                                break

                            url   = sw["url"]
                            title = (sw.get("title") or url)[:70]

                            result = await enter_sweepstake(page, url, config.profile)
                            status = result["status"]

                            if status == "entered":
                                db.mark_entered(url)
                                counts["entered"] += 1
                                icon = "✓"
                            elif status == "captcha":
                                db.mark_captcha(url)
                                counts["captcha"] += 1
                                icon = "⚠"
                            elif status == "no_form":
                                db.mark_skipped(url, "no entry form detected")
                                counts["no_form"] += 1
                                icon = "–"
                            else:
                                msg = result.get("message", "unknown error")
                                db.mark_error(url, msg)
                                counts["error"] += 1
                                icon = "✗"

                            self._log_queue.put(
                                f"[{_ts()}] {icon} [{idx}/{total}] {title}"
                            )
                            self._log_queue.put(("stats_refresh", None))

                            pct = idx / total
                            self._log_queue.put(("progress", pct))

                            if config.delay_between_entries > 0:
                                await asyncio.sleep(config.delay_between_entries)

                asyncio.run(_run_entries())

                self._log_queue.put(
                    f"[{_ts()}] Done — Entered: {counts['entered']}, "
                    f"CAPTCHA: {counts['captcha']}, "
                    f"No form: {counts['no_form']}, "
                    f"Errors: {counts['error']}"
                )
                self._log_queue.put(("done", "Done"))

            except Exception as exc:
                self._log_queue.put(f"[{_ts()}] ERROR: {exc}")
                self._log_queue.put(("done", "Error"))

        self._running_thread = _run_in_thread(task)

    def _stop_run(self) -> None:
        self._stop_flag.set()
        self._set_run_status("Stopping…")
        self._append_log(f"[{_ts()}] Stop requested…")

    # ─────────────────────────────────────────────────────────────────────────
    # Tab 3 — History
    # ─────────────────────────────────────────────────────────────────────────

    def _build_history_tab(self, parent: ctk.CTkFrame) -> None:
        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=(10, 4))

        ctk.CTkButton(btn_frame, text="Refresh", command=self._refresh_history).pack(
            side="left", padx=6
        )

        self._history_count_lbl = ctk.CTkLabel(btn_frame, text="")
        self._history_count_lbl.pack(side="left", padx=10)

        # Treeview inside a CTk frame
        tree_frame = ctk.CTkFrame(parent)
        tree_frame.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background="#2b2b2b",
            foreground="#ffffff",
            rowheight=26,
            fieldbackground="#2b2b2b",
            bordercolor="#3a3a3a",
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background="#1f1f1f",
            foreground="#aaaaaa",
            relief="flat",
        )
        style.map("Treeview", background=[("selected", "#1f6aa5")])

        columns = ("title", "url", "status", "entered_at")
        self._history_tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self._history_tree.heading("title",      text="Title")
        self._history_tree.heading("url",        text="URL")
        self._history_tree.heading("status",     text="Status")
        self._history_tree.heading("entered_at", text="Entered At")

        self._history_tree.column("title",      width=250, minwidth=100)
        self._history_tree.column("url",        width=280, minwidth=100)
        self._history_tree.column("status",     width=90,  minwidth=60, anchor="center")
        self._history_tree.column("entered_at", width=160, minwidth=100)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                             command=self._history_tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal",
                             command=self._history_tree.xview)
        self._history_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._history_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

    def _refresh_history(self) -> None:
        rows = _load_history(self._db_path)
        for item in self._history_tree.get_children():
            self._history_tree.delete(item)
        for row in rows:
            self._history_tree.insert(
                "",
                "end",
                values=(
                    row.get("title") or "",
                    row.get("url") or "",
                    row.get("status") or "",
                    row.get("entered_at") or "",
                ),
            )
        self._history_count_lbl.configure(text=f"{len(rows)} entries shown")

    # ─────────────────────────────────────────────────────────────────────────
    # Tab 4 — Settings
    # ─────────────────────────────────────────────────────────────────────────

    def _build_settings_tab(self, parent: ctk.CTkFrame) -> None:
        parent.columnconfigure(1, weight=1)

        row = 0

        # Headless mode
        ctk.CTkLabel(parent, text="Headless mode:", anchor="e", width=200).grid(
            row=row, column=0, sticky="e", padx=(10, 4), pady=8
        )
        self._headless_var = ctk.BooleanVar(value=True)
        self._headless_cb = ctk.CTkCheckBox(
            parent, text="Run browser without a visible window",
            variable=self._headless_var
        )
        self._headless_cb.grid(row=row, column=1, sticky="w", padx=(0, 10), pady=8)
        row += 1

        # Delay between entries
        ctk.CTkLabel(parent, text="Delay between entries (s):", anchor="e", width=200).grid(
            row=row, column=0, sticky="e", padx=(10, 4), pady=8
        )
        delay_frame = ctk.CTkFrame(parent, fg_color="transparent")
        delay_frame.grid(row=row, column=1, sticky="w", padx=(0, 10), pady=8)
        self._delay_var = ctk.IntVar(value=3)
        self._delay_slider = ctk.CTkSlider(
            delay_frame, from_=1, to=10, number_of_steps=9,
            variable=self._delay_var, width=200,
            command=lambda v: self._delay_lbl.configure(text=str(int(v)))
        )
        self._delay_slider.pack(side="left")
        self._delay_lbl = ctk.CTkLabel(delay_frame, text="3", width=30)
        self._delay_lbl.pack(side="left", padx=6)
        row += 1

        # Max entries per run
        ctk.CTkLabel(parent, text="Max entries per run:", anchor="e", width=200).grid(
            row=row, column=0, sticky="e", padx=(10, 4), pady=8
        )
        self._max_entries_var = ctk.StringVar(value="50")
        self._max_entries_entry = ctk.CTkEntry(
            parent, textvariable=self._max_entries_var, width=100
        )
        self._max_entries_entry.grid(row=row, column=1, sticky="w", padx=(0, 10), pady=8)
        row += 1

        # Database path
        ctk.CTkLabel(parent, text="Database path:", anchor="e", width=200).grid(
            row=row, column=0, sticky="e", padx=(10, 4), pady=8
        )
        self._db_path_var = ctk.StringVar(value=DEFAULT_DB)
        self._db_path_entry = ctk.CTkEntry(
            parent, textvariable=self._db_path_var, width=340
        )
        self._db_path_entry.grid(row=row, column=1, sticky="w", padx=(0, 10), pady=8)
        row += 1

        # Log file path
        ctk.CTkLabel(parent, text="Log file path:", anchor="e", width=200).grid(
            row=row, column=0, sticky="e", padx=(10, 4), pady=8
        )
        self._log_file_var = ctk.StringVar(value="entries.log")
        self._log_file_entry = ctk.CTkEntry(
            parent, textvariable=self._log_file_var, width=340
        )
        self._log_file_entry.grid(row=row, column=1, sticky="w", padx=(0, 10), pady=8)
        row += 1

        # Save button
        save_frame = ctk.CTkFrame(parent, fg_color="transparent")
        save_frame.grid(row=row, column=0, columnspan=2, pady=14)
        ctk.CTkButton(save_frame, text="Save Settings", command=self._save_settings).pack(
            side="left", padx=6
        )
        self._settings_status_lbl = ctk.CTkLabel(save_frame, text="")
        self._settings_status_lbl.pack(side="left", padx=10)

    def _collect_settings_dict(self) -> dict[str, Any]:
        try:
            delay = int(self._delay_var.get())
        except Exception:
            delay = 3
        try:
            max_entries = int(self._max_entries_var.get())
        except Exception:
            max_entries = 50
        return {
            "headless": bool(self._headless_var.get()),
            "delay_between_entries": delay,
            "max_entries_per_run": max_entries,
            "skip_captcha": True,
            "log_file": self._log_file_var.get() or "entries.log",
            "database": self._db_path_var.get() or DEFAULT_DB,
        }

    def _sync_settings_from_dict(self, settings: dict[str, Any]) -> None:
        """Push settings dict values into the Settings tab widgets (if they exist)."""
        try:
            if "headless" in settings:
                self._headless_var.set(bool(settings["headless"]))
            if "delay_between_entries" in settings:
                v = int(settings["delay_between_entries"])
                self._delay_var.set(v)
                self._delay_lbl.configure(text=str(v))
            if "max_entries_per_run" in settings:
                self._max_entries_var.set(str(settings["max_entries_per_run"]))
            if "database" in settings:
                self._db_path_var.set(str(settings["database"]))
                self._db_path = str(settings["database"])
            if "log_file" in settings:
                self._log_file_var.set(str(settings["log_file"]))
        except Exception:
            pass  # widgets may not exist yet on first call

    def _save_settings(self) -> None:
        settings = self._collect_settings_dict()
        self._db_path = settings["database"]

        # Merge into profile.yaml
        if PROFILE_PATH.exists():
            try:
                with PROFILE_PATH.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
            except Exception:
                data = {}
        else:
            data = {}

        data["settings"] = settings
        try:
            with PROFILE_PATH.open("w", encoding="utf-8") as fh:
                yaml.dump(data, fh, default_flow_style=False, allow_unicode=True)
            self._settings_status_lbl.configure(text="Settings saved!", text_color="#55ff55")
        except Exception as exc:
            self._settings_status_lbl.configure(
                text=f"Error: {exc}", text_color="#ff5555"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Queue polling — runs on the main UI thread every 100 ms
    # ─────────────────────────────────────────────────────────────────────────

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self._log_queue.get_nowait()

                if isinstance(item, tuple):
                    kind, value = item
                    if kind == "done":
                        final_status = value or "Done"
                        self._set_run_status(final_status)
                        self._set_buttons_running(False)
                        self._refresh_stats()
                    elif kind == "status":
                        self._set_run_status(value)
                    elif kind == "progress":
                        self._progress.stop()
                        self._progress.configure(mode="determinate")
                        self._progress.set(float(value))
                    elif kind == "stats_refresh":
                        self._refresh_stats()
                else:
                    # Plain string log message
                    self._append_log(str(item))

        except queue.Empty:
            pass
        finally:
            self.after(100, self._poll_queue)

    # ─────────────────────────────────────────────────────────────────────────
    # Stats refresh
    # ─────────────────────────────────────────────────────────────────────────

    def _refresh_stats(self) -> None:
        stats = _load_stats(self._db_path)
        self._stat_labels["found"].configure(text=str(stats.get("total", 0)))
        self._stat_labels["entered"].configure(text=str(stats.get("entered", 0)))
        # "Skipped" = no_form + skipped statuses
        skipped = stats.get("skipped", 0) + stats.get("no_form", 0)
        self._stat_labels["skipped"].configure(text=str(skipped))
        self._stat_labels["captcha"].configure(text=str(stats.get("captcha", 0)))

    # ─────────────────────────────────────────────────────────────────────────
    # Playwright browser check on startup
    # ─────────────────────────────────────────────────────────────────────────

    def _startup_browser_check(self) -> None:
        def check() -> None:
            if not _check_playwright_browsers():
                # Ask on main thread
                self.after(0, self._prompt_install_browsers)

        _run_in_thread(check)

    def _prompt_install_browsers(self) -> None:
        answer = messagebox.askyesno(
            "Playwright Browsers Not Found",
            "Playwright Chromium is not installed.\n\n"
            "Click Yes to install it now (one-time setup, ~150 MB).\n"
            "Click No to skip — automation will not work without it.",
            parent=self,
        )
        if answer:
            self._tabview.set("Run")
            self._append_log(f"[{_ts()}] Starting Playwright Chromium install…")
            _run_in_thread(
                lambda: _install_playwright_browsers(self._log_queue)
            )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = SweepstakeGenieApp()
    app.mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
