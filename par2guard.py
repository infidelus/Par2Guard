#!/usr/bin/env python3
"""
Par2Guard - GTK3 (PyGObject) GUI wrapper around par2cmdline.

Provides a user-friendly interface for:
- Creating PAR2 parity sets
- Verifying existing sets
- Repairing damaged files

Design goals:
- Clean output by default (optional verbose mode)
- Safe, sequential execution of PAR2 jobs
- Clear user feedback and summaries

Inspired by the original PyPAR2 project: https://pypar2.fingelrest.net/

"""

# -----------------------------------------------------------------------------
# Imports and GTK (PyGObject) setup
# -----------------------------------------------------------------------------

import os
import signal
import subprocess
import threading
import time
import configparser
import re
from pathlib import Path
from typing import Callable, Optional, List, Tuple, Dict, Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GLib", "2.0")

from gi.repository import Gtk, Gdk, GLib


# -----------------------------------------------------------------------------
# Application constants and defaults
# -----------------------------------------------------------------------------

APP_TITLE = "Par2Guard"
PAR2_BIN = "par2"

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.ini"

DEFAULTS = {
    "default_path": str(Path.home()),
    "redundancy_percent": "10",
    "verbose_logging": "0",
    # Advanced create defaults
    "create_mode": "redundancy",  # redundancy | recovery_blocks
    "block_size_kb": "1024",  # shown as KB in UI
    "recovery_blocks": "100",
}

DISC_RE = re.compile(
    r"^(disc|disk|cd)\s*\d+$",
    re.IGNORECASE,
)


# -----------------------------------------------------------------------------
# Utility helper functions
# -----------------------------------------------------------------------------

def which(cmd: str) -> Optional[str]:
    from shutil import which as _which
    return _which(cmd)

def _coerce_int(s: str, default: int) -> int:
    try:
        return int(str(s).strip())
    except Exception:
        return default

def derive_archive_base(folder: Path, user_base: str, multi: bool) -> str:
    """
    Determine archive base name.

    - If user_base is set and we're not multi-folder: use it.
    - If folder is a disc subfolder, prefix with parent folder name.
    - Otherwise, default to folder name.
    """
    if user_base and not multi:
        return user_base

    name = folder.name
    parent = folder.parent.name if folder.parent else ""

    if DISC_RE.match(name) and parent:
        return f"{parent} - {name}"

    return name


# -----------------------------------------------------------------------------
# Configuration file handling (config.ini)
# -----------------------------------------------------------------------------

def load_config() -> configparser.ConfigParser:
    """
    Load or initialize config.ini in the application directory.

    Ensures all required default keys exist.
    """
    cp = configparser.ConfigParser()
    if not CONFIG_FILE.exists():
        cp["defaults"] = DEFAULTS.copy()
        with CONFIG_FILE.open("w", encoding="utf-8") as f:
            cp.write(f)
    cp.read(CONFIG_FILE, encoding="utf-8")
    if "defaults" not in cp:
        cp["defaults"] = DEFAULTS.copy()
    changed = False
    for k, v in DEFAULTS.items():
        if k not in cp["defaults"]:
            cp["defaults"][k] = v
            changed = True
    if changed:
        with CONFIG_FILE.open("w", encoding="utf-8") as f:
            cp.write(f)
    return cp


CONFIG = load_config()


def cfg_get(key: str, fallback: str) -> str:
    return CONFIG.get("defaults", key, fallback=fallback)


def cfg_set(key: str, value: str) -> None:
    CONFIG["defaults"][key] = value
    with CONFIG_FILE.open("w", encoding="utf-8") as f:
        CONFIG.write(f)


# -----------------------------------------------------------------------------
# File chooser and PAR2 filename helpers
# -----------------------------------------------------------------------------

def create_file_dialog(
    parent: Gtk.Window,
    title: str,
    action: Gtk.FileChooserAction,
    start_path: Optional[str],
) -> Gtk.FileChooserDialog:
    """Create a standard file/folder chooser dialog."""
    dialog = Gtk.FileChooserDialog(
        title=title,
        transient_for=parent,
        modal=True,
        action=action,
    )
    dialog.add_buttons(
        Gtk.STOCK_CANCEL,
        Gtk.ResponseType.CANCEL,
        Gtk.STOCK_OPEN,
        Gtk.ResponseType.OK,
    )

    if start_path and os.path.isdir(start_path):
        try:
            dialog.set_current_folder(start_path)
        except Exception:
            pass

    return dialog


def par2_set_key_from_filename(name: str) -> str:
    """
    Normalize PAR2 filenames to a set key.

    Groups:
      Album.par2
      Album.vol000+01.par2
    """
    stem = name[:-5] if name.lower().endswith(".par2") else name
    return stem.split(".vol", 1)[0]


# -----------------------------------------------------------------------------
# Log pane widget (read-only scrolling log output)
# -----------------------------------------------------------------------------

class LogPane(Gtk.Box):
    """Monospace log view with Clear/Copy controls."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        self.textview.set_monospace(True)
        self.buffer = self.textview.get_buffer()

        scroller = Gtk.ScrolledWindow()
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.add(self.textview)

        btnbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.btn_clear = Gtk.Button.new_with_label("Clear log")
        self.btn_copy = Gtk.Button.new_with_label("Copy")
        btnbox.pack_start(self.btn_clear, False, False, 0)
        btnbox.pack_start(self.btn_copy, False, False, 0)

        self.pack_start(scroller, True, True, 0)
        self.pack_start(btnbox, False, False, 0)

        self.btn_clear.connect("clicked", lambda *_: self.buffer.set_text(""))
        self.btn_copy.connect("clicked", self._on_copy)

        self.btn_clear.set_tooltip_text("Clear the log output.")
        self.btn_copy.set_tooltip_text("Copy the entire log to clipboard.")

    def append(self, text: str) -> None:
        """Append text and scroll to the end."""
        end_iter = self.buffer.get_end_iter()
        self.buffer.insert(end_iter, text)
        mark = self.buffer.create_mark(None, self.buffer.get_end_iter(), False)
        self.textview.scroll_mark_onscreen(mark)

    def _on_copy(self, *_: Any) -> None:
        """Copy entire log buffer to clipboard."""
        start, end = self.buffer.get_bounds()
        txt = self.buffer.get_text(start, end, True)
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(txt, -1)


# -----------------------------------------------------------------------------
# Runner: sequential PAR2 execution engine
# -----------------------------------------------------------------------------

class Runner:
    """
    Runs jobs sequentially in a worker thread.

    - Streams stdout to the log.
    - Provides a status line via `on_state`.
    - Emits per-job completion messages via `on_done_msg`.
    - Produces summaries for verify/repair batches.
    """

    def __init__(
        self,
        on_text: Callable[[str], None],
        on_state: Callable[[Any], None],  # bool or status string
        on_done_msg: Callable[[str], None],
        is_verbose: Callable[[], bool],
    ) -> None:
        self.on_text = on_text
        self.on_state = on_state
        self.on_done_msg = on_done_msg
        self.is_verbose = is_verbose

        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        # argv, cwd, mode, label
        self._queue: List[Tuple[List[str], Optional[str], str, str]] = []
        self._cancel_requested = False

        self._total_jobs = 0
        self._completed_jobs = 0
        self._batch_mode = ""
        self._results: Dict[str, List[str]] = {}

    def is_running(self) -> bool:
        """True if a subprocess is currently executing."""
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def cancel(self) -> None:
        """
        Cancel the current operation:
        - Clear queued jobs.
        - SIGINT the running process (if any).
        """
        with self._lock:
            self._cancel_requested = True
            p = self._proc
            self._queue.clear()
            self._total_jobs = 0
            self._completed_jobs = 0

        if p and p.poll() is None:
            try:
                p.send_signal(signal.SIGINT)
            except Exception:
                pass


    def run_many(
        self,
        jobs: List[Tuple[List[str], Optional[str], str, str]],
    ) -> None:
        """Enqueue a batch of jobs and start processing."""
        if not jobs:
            return

        with self._lock:
            self._queue.extend(jobs)
            self._total_jobs = len(self._queue)
            self._completed_jobs = 0
            self._batch_mode = jobs[0][2]
            self._results = {
                "verify_ok": [],
                "verify_need_repair": [],
                "verify_failed": [],
                "repair_repaired": [],
                "repair_not_required": [],
                "repair_failed": [],
            }

        mode = jobs[0][2]
        friendly = {
            "create": "Creating parity files",
            "verify": "Verifying files",
            "repair": "Repairing files",
        }.get(mode, "Working")

        GLib.idle_add(
            self.on_text,
            f"\n──────── {friendly} ────────\n",
        )
        self._kick()

    def run_one(
        self,
        argv: List[str],
        cwd: Optional[str],
        mode: str,
        label: str = "",
    ) -> None:
        """Convenience wrapper for running one job."""
        self.run_many([(argv, cwd, mode, label or mode)])

    def _kick(self) -> None:
        """
        Start the next job if idle, otherwise do nothing.
        When the queue empties, complete the batch.
        """
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return

            if self._queue:
                argv, cwd, mode, label = self._queue.pop(0)
                self._cancel_requested = False

                total = self._total_jobs
                done = self._completed_jobs + 1

                action = {
                    "verify": "Verifying",
                    "repair": "Repairing",
                    "create": "Creating",
                }.get(mode, "Running")

                # Log which item is being processed (requested UX improvement)
                if label:
                    GLib.idle_add(self.on_text, f"\n({label})\n")

                GLib.idle_add(
                    self.on_state,
                    f"{action} {done} / {total} items…  ({label})",
                )

                self._start(argv, cwd, mode, label)
                return

            # Queue empty: finish batch
            mode = self._batch_mode
            completed = self._completed_jobs
            results = dict(self._results)

            self._batch_mode = ""
            self._total_jobs = 0
            self._completed_jobs = 0

        if completed and mode in ("verify", "repair"):
            summary = self._format_summary(mode, completed, results)
            GLib.idle_add(self.on_done_msg, summary)

        GLib.idle_add(self.on_state, False)


    def _start(self, argv: List[str], cwd: Optional[str], mode: str, label: str) -> None:
        """Launch a subprocess in a worker thread and stream output."""

        def worker() -> None:
            tail: List[str] = []
            buf: List[str] = []
            last_flush = time.time()

            def keep_tail(line: str) -> None:
                tail.append(line)
                if len(tail) > 300:
                    del tail[:60]

            def should_show(line: str) -> bool:
                """
                In non-verbose mode, hide progress spam and show only key lines.
                """
                if self.is_verbose():
                    return True

                stripped = line.strip()
                if not stripped:
                    return False

                important_prefixes = (
                    "Block size:",
                    "Source file count:",
                    "Source block count:",
                    "Recovery block count:",
                    "Recovery file count:",
                    "There are",
                    "All files are correct",
                    "Repair is required",
                    "Repair completed",
                    "Repair not required",
                    "Verification complete",
                    "Parity files created successfully",
                )

                return stripped.startswith(important_prefixes)

            def flush(force: bool = False) -> None:
                nonlocal last_flush
                if not buf:
                    return

                now = time.time()
                if force or len(buf) >= 60 or (now - last_flush) >= 0.12:
                    text = "".join(buf)
                    buf.clear()
                    last_flush = now
                    GLib.idle_add(self.on_text, text)

            rc = 1
            err: Optional[str] = None

            try:
                if which(PAR2_BIN) is None:
                    raise FileNotFoundError(f"'{PAR2_BIN}' not found in PATH")

                with self._lock:
                    self._proc = subprocess.Popen(
                        argv,
                        cwd=cwd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                    )

                assert self._proc.stdout is not None
                for line in self._proc.stdout:
                    keep_tail(line)
                    if should_show(line):
                        buf.append(line)
                    flush(False)

                rc = self._proc.wait()
                flush(True)

            except Exception as e:
                err = str(e)

            finally:
                with self._lock:
                    self._proc = None

                msg, classification = self._summarize_one(mode, rc, tail, err)

                with self._lock:
                    self._completed_jobs += 1
                    if classification:
                        self._results[classification].append(label)

                GLib.idle_add(self.on_done_msg, msg)
                GLib.idle_add(self.on_state, False)
                self._kick()

        threading.Thread(target=worker, daemon=True).start()

    def _summarize_one(
        self,
        mode: str,
        rc: int,
        tail: List[str],
        err: Optional[str],
    ) -> Tuple[str, str]:
        """
        Turn a completed subprocess run into a short user-friendly message.

        Returns:
          (message, classification_key)
        """
        if err is not None:
            if "not found" in err.lower():
                cls = (
                    "verify_failed"
                    if mode == "verify"
                    else ("repair_failed" if mode == "repair" else "")
                )
                return (
                    "Error: 'par2' not found. Install it (Mint/Ubuntu): sudo apt install par2",
                    cls,
                )

            cls = (
                "verify_failed"
                if mode == "verify"
                else ("repair_failed" if mode == "repair" else "")
            )
            return (f"Error: {err}", cls)

        with self._lock:
            if self._cancel_requested:
                return ("Cancelled.", "")

        joined = "".join(tail).lower()

        if rc != 0:
            if mode == "verify" and rc == 1:
                # rc==1 can mean "repair required" (not necessarily a hard failure)
                if "repair is required" in joined or "missing" in joined:
                    return ("Verification complete. Repair is required.", "verify_need_repair")
                return ("Verification complete.", "verify_ok")

            cls = (
                "verify_failed"
                if mode == "verify"
                else ("repair_failed" if mode == "repair" else "")
            )
            return (f"Operation failed (exit code {rc}). See log for details.", cls)

        if mode == "create":
            return ("Parity files created successfully.", "")

        if mode == "verify":
            if "repair is required" in joined or "file not found" in joined or "missing" in joined:
                return ("Verification complete. Repair is required.", "verify_need_repair")
            if "all files are correct" in joined or "repair is not required" in joined:
                return ("All files verified successfully. Repair not required.", "verify_ok")
            return ("Verification complete.", "verify_ok")

        if mode == "repair":
            if "repair is not required" in joined:
                return ("Repair not required.", "repair_not_required")
            if "repair complete" in joined or "repair is complete" in joined:
                return ("Repair completed successfully.", "repair_repaired")
            return ("Repair complete.", "repair_repaired")

        return ("Operation completed successfully.", "")

    def _format_summary(self, mode: str, total: int, results: Dict[str, List[str]]) -> str:
        """Build a human-readable end-of-batch summary for verify/repair runs."""
        if mode == "verify":
            ok = results.get("verify_ok", [])
            need = results.get("verify_need_repair", [])
            failed = results.get("verify_failed", [])

            lines: List[str] = []
            lines.append("\n──────── Verification Summary ────────\n")
            lines.append(f"Checked: {total} items\n")
            lines.append(f"OK: {len(ok)}\n")
            lines.append(f"Require repair: {len(need)}\n")
            if failed:
                lines.append(f"Failed: {len(failed)}\n")

            if need:
                lines.append("\nItems requiring repair:\n")
                for x in need:
                    lines.append(f"- {x}\n")

            if failed:
                lines.append("\nFailed items:\n")
                for x in failed:
                    lines.append(f"- {x}\n")

            lines.append("─────────────────────────────────────\n")
            return "".join(lines)

        repaired = results.get("repair_repaired", [])
        notreq = results.get("repair_not_required", [])
        failed = results.get("repair_failed", [])

        lines = []
        lines.append("\n──────── Repair Summary ────────\n")
        lines.append(f"Processed: {total} items\n")
        lines.append(f"Repaired: {len(repaired)}\n")
        lines.append(f"No repair needed: {len(notreq)}\n")
        if failed:
            lines.append(f"Failed: {len(failed)}\n")

        if repaired:
            lines.append("\nRepaired items:\n")
            for x in repaired:
                lines.append(f"- {x}\n")

        if failed:
            lines.append("\nFailed items:\n")
            for x in failed:
                lines.append(f"- {x}\n")

        lines.append("────────────────────────────────\n")
        return "".join(lines)


# -----------------------------------------------------------------------------
# File list widget (shared by Create / Verify / Repair tabs)
# -----------------------------------------------------------------------------

class FileList(Gtk.Box):
    """Reusable list widget for file/folder path entries."""

    def __init__(self, title: str, col_title: str = "Path") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        self.store = Gtk.ListStore(str)

        header = Gtk.Label()
        header.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
        header.set_xalign(0)

        self.view = Gtk.TreeView(model=self.store)
        renderer = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn(col_title, renderer, text=0)
        col.set_resizable(True)
        col.set_expand(True)
        self.view.append_column(col)

        scroller = Gtk.ScrolledWindow()
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.add(self.view)

        btnbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.btn_add = Gtk.Button.new_with_label("Add…")
        self.btn_add_folder = Gtk.Button.new_with_label("Add folder…")
        self.btn_remove = Gtk.Button.new_with_label("Remove")
        self.btn_clear = Gtk.Button.new_with_label("Clear")

        btnbox.pack_start(self.btn_add, False, False, 0)
        btnbox.pack_start(self.btn_add_folder, False, False, 0)
        btnbox.pack_start(self.btn_remove, False, False, 0)
        btnbox.pack_start(self.btn_clear, False, False, 0)

        self.pack_start(header, False, False, 0)
        self.pack_start(scroller, True, True, 0)
        self.pack_start(btnbox, False, False, 0)

        self.btn_remove.connect("clicked", self._on_remove)
        self.btn_clear.connect("clicked", lambda *_: self.store.clear())

        self.btn_remove.set_tooltip_text("Remove the selected entry from the list.")
        self.btn_clear.set_tooltip_text("Remove all entries from the list.")

    def paths(self) -> List[str]:
        """Return all stored paths."""
        return [row[0] for row in self.store]

    def add_paths(self, paths: List[str]) -> None:
        """Add new unique paths to the list."""
        existing = set(self.paths())
        for p in paths:
            if p and p not in existing:
                self.store.append([p])
                existing.add(p)

    def _on_remove(self, *_: Any) -> None:
        """Remove the currently selected row."""
        sel = self.view.get_selection()
        model, it = sel.get_selected()
        if it is not None:
            model.remove(it)


# -----------------------------------------------------------------------------
# Create tab: PAR2 creation UI and logic
# -----------------------------------------------------------------------------

class CreateTab(Gtk.Box):
    """Create PAR2 sets for selected files (grouped by folder)."""

    def __init__(self, win: "MainWindow", runner: Runner, log: LogPane) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        self.win = win
        self.runner = runner
        self.log = log

        # If True, we skip the overwrite preflight during the current create action.
        self._overwrite_confirmed = False

        grid = Gtk.Grid(column_spacing=10, row_spacing=10)

        self.redundancy = Gtk.SpinButton.new_with_range(1, 100, 1)
        self.redundancy.set_value(_coerce_int(cfg_get("redundancy_percent", "10"), 10))

        self.archive_name = Gtk.Entry()
        self.archive_name.set_placeholder_text(
            'e.g. "My Groovy Tunes" (base name, without .par2)'
        )

        self.chk_recursive = Gtk.CheckButton.new_with_label(
            "Include subfolders when adding folders (recursive)"
        )
        self.chk_recursive.set_active(False)

        lbl_r = Gtk.Label(label="Redundancy (%)")
        lbl_r.set_xalign(0)
        lbl_a = Gtk.Label(label="Archive base name (-a)")
        lbl_a.set_xalign(0)

        grid.attach(lbl_r, 0, 0, 1, 1)
        grid.attach(self.redundancy, 1, 0, 1, 1)
        grid.attach(lbl_a, 0, 1, 1, 1)
        grid.attach(self.archive_name, 1, 1, 1, 1)
        grid.attach(self.chk_recursive, 0, 2, 2, 1)

        # --- Advanced options (collapsed by default)
        self.adv_expander = Gtk.Expander(label="Advanced options")
        self.adv_expander.set_expanded(False)

        adv = Gtk.Grid(column_spacing=10, row_spacing=10)
        adv.set_margin_top(6)
        adv.set_margin_bottom(6)

        lbl_bs = Gtk.Label(label="Block size")
        lbl_bs.set_xalign(0)

        self.blocksize_labels = [
            "Auto",
            "64 KB",
            "128 KB",
            "256 KB",
            "512 KB",
            "1 MB",
            "Custom…",
        ]

        self.blocksize_combo = Gtk.ComboBoxText()
        for t in self.blocksize_labels:
            self.blocksize_combo.append_text(t)

        self.blocksize_custom = Gtk.SpinButton.new_with_range(1, 1024 * 1024 * 1024, 1)
        self.blocksize_custom.set_value(1024 * 1024)
        self.blocksize_custom.set_sensitive(False)
        self.blocksize_custom.set_tooltip_text(
            "Block size must be divisible by 4 bytes.\n"
            "Non-conforming values will be adjusted automatically."
        )

        bs_kb = _coerce_int(cfg_get("block_size_kb", "0"), 0)
        if bs_kb == 0:
            self.blocksize_combo.set_active(0)  # Auto
        else:
            label_map = {64: "64 KB", 128: "128 KB", 256: "256 KB", 512: "512 KB", 1024: "1 MB"}
            label = label_map.get(bs_kb)
            if label and label in self.blocksize_labels:
                self.blocksize_combo.set_active(self.blocksize_labels.index(label))
            else:
                self.blocksize_combo.set_active(self.blocksize_labels.index("Custom…"))
                self.blocksize_custom.set_value(bs_kb * 1024)
                self.blocksize_custom.set_sensitive(True)

        lbl_rm = Gtk.Label(label="Redundancy mode")
        lbl_rm.set_xalign(0)

        self.rb_percent = Gtk.RadioButton.new_with_label_from_widget(None, "Percentage")
        self.rb_blocks = Gtk.RadioButton.new_with_label_from_widget(
            self.rb_percent, "Recovery blocks"
        )
        self.rb_percent.set_active(True)

        lbl_rc = Gtk.Label(label="Recovery blocks")
        lbl_rc.set_xalign(0)

        self.recovery_blocks = Gtk.SpinButton.new_with_range(1, 10_000_000, 1)
        self.recovery_blocks.set_value(100)
        self.recovery_blocks.set_sensitive(False)

        adv.attach(lbl_bs, 0, 0, 1, 1)
        adv.attach(self.blocksize_combo, 1, 0, 1, 1)
        adv.attach(Gtk.Label(label="Custom (bytes)"), 0, 1, 1, 1)
        adv.attach(self.blocksize_custom, 1, 1, 1, 1)

        adv.attach(lbl_rm, 0, 2, 1, 1)
        rm_box = Gtk.Box(spacing=10)
        rm_box.pack_start(self.rb_percent, False, False, 0)
        rm_box.pack_start(self.rb_blocks, False, False, 0)
        adv.attach(rm_box, 1, 2, 1, 1)

        adv.attach(lbl_rc, 0, 3, 1, 1)
        adv.attach(self.recovery_blocks, 1, 3, 1, 1)

        self.adv_expander.add(adv)

        self.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 6)
        self.pack_start(self.adv_expander, False, False, 0)

        # Signals
        self.blocksize_combo.connect("changed", self._on_blocksize_changed)
        self.rb_percent.connect("toggled", self._on_redundancy_mode_changed)
        self.rb_blocks.connect("toggled", self._on_redundancy_mode_changed)

        # Restore create mode from config
        create_mode = cfg_get("create_mode", "redundancy")
        self.rb_blocks.set_active(create_mode == "recovery_blocks")

        # Restore recovery block count from config
        self.recovery_blocks.set_value(_coerce_int(cfg_get("recovery_blocks", "100"), 100))

        self.filelist = FileList("Files to protect")
        self.filelist.btn_add.connect("clicked", self._add_files)
        self.filelist.btn_add_folder.connect("clicked", self._add_folder)

        self.filelist.btn_add.set_tooltip_text("Add one or more files to protect with PAR2.")
        self.filelist.btn_add_folder.set_tooltip_text(
            "Add all files from a folder (recursive optional)."
        )

        btnbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.btn_create = Gtk.Button.new_with_label("Create PAR2")
        self.btn_create.set_tooltip_text(
            "Create PAR2 parity files for the selected files/folders."
        )
        btnbox.pack_start(self.btn_create, False, False, 0)
        self.btn_create.connect("clicked", self._on_create)

        self.pack_start(grid, False, False, 0)
        self.pack_start(self.filelist, True, True, 0)
        self.pack_start(btnbox, False, False, 0)

    def _add_files(self, *_: Any) -> None:
        dialog = create_file_dialog(
            self.win, "Select files", Gtk.FileChooserAction.OPEN, self.win.session_path
        )
        dialog.set_select_multiple(True)
        resp = dialog.run()
        if resp == Gtk.ResponseType.OK:
            files = dialog.get_filenames()
            if files:
                self.win.session_path = os.path.dirname(files[0])
            self.filelist.add_paths(files)
        dialog.destroy()

    def _add_folder(self, *_: Any) -> None:
        dialog = create_file_dialog(
            self.win, "Select folder", Gtk.FileChooserAction.SELECT_FOLDER, self.win.session_path
        )
        resp = dialog.run()
        if resp == Gtk.ResponseType.OK:
            folder = dialog.get_filename()
            if folder:
                self.win.session_path = folder
                base = Path(folder)

                # Default archive base name to folder name if empty
                if not self.archive_name.get_text().strip():
                    self.archive_name.set_text(base.name)

                paths: List[str] = []
                if self.chk_recursive.get_active():
                    for p in base.rglob("*"):
                        if p.is_file() and not p.name.lower().endswith(".par2"):
                            paths.append(str(p))
                else:
                    for p in sorted(base.iterdir()):
                        if p.is_file() and not p.name.lower().endswith(".par2"):
                            paths.append(str(p))

                self.filelist.add_paths(paths)
        dialog.destroy()

    def _on_blocksize_changed(self, *_: Any) -> None:
        label = self.blocksize_combo.get_active_text() or "Auto"
        self.blocksize_custom.set_sensitive(label.startswith("Custom"))

    def _on_redundancy_mode_changed(self, *_: Any) -> None:
        use_blocks = self.rb_blocks.get_active()
        self.redundancy.set_sensitive(not use_blocks)
        self.recovery_blocks.set_sensitive(use_blocks)

    def _get_blocksize_bytes(self) -> Optional[int]:
        """Return chosen block size in bytes, or None for Auto."""
        label = self.blocksize_combo.get_active_text() or "Auto"
        if label == "Auto":
            return None
        if label == "64 KB":
            return 64 * 1024
        if label == "128 KB":
            return 128 * 1024
        if label == "256 KB":
            return 256 * 1024
        if label == "512 KB":
            return 512 * 1024
        if label == "1 MB":
            return 1024 * 1024
        return int(self.blocksize_custom.get_value())

    def _on_create(self, *_: Any) -> None:
        """
        Build one create job per folder (PAR2 sets are folder-scoped).

        If existing PAR2 files are found, prompt once to delete/recreate.
        """
        files = [p for p in self.filelist.paths() if not p.lower().endswith(".par2")]
        if not files:
            self.log.append("Please add at least one file.\n")
            return

        # Group files by folder so each folder produces a separate PAR2 set.
        groups: Dict[str, List[str]] = {}
        for f in files:
            groups.setdefault(os.path.dirname(f), []).append(f)

        user_base = self.archive_name.get_text().strip()

        # Preflight check: detect existing PAR2 files unless overwrite is confirmed.
        if not self._overwrite_confirmed:
            for folder in groups:
                base = user_base or Path(folder).name
                existing = self._existing_par2_files(folder, base)
                if existing:
                    self.prompt_overwrite_existing_par2()
                    return

        # Validate/normalize block size (par2 requires multiple of 4 bytes).
        bs = self._get_blocksize_bytes()
        if bs is not None and bs % 4 != 0:
            fixed = bs - (bs % 4)
            self.log.append(
                f"Block size adjusted from {bs} to {fixed} bytes "
                "(must be divisible by 4).\n"
            )
            self.blocksize_custom.set_value(fixed)
            bs = fixed

        # Persist UI settings to config.
        cfg_set("redundancy_percent", str(int(self.redundancy.get_value())))
        cfg_set("create_mode", "recovery_blocks" if self.rb_blocks.get_active() else "redundancy")
        cfg_set("block_size_kb", "0" if bs is None else str(bs // 1024))
        cfg_set("recovery_blocks", str(int(self.recovery_blocks.get_value())))

        jobs: List[Tuple[List[str], Optional[str], str, str]] = []
        multi = len(groups) > 1

        if multi and user_base:
            self.log.append(
                "Multiple folders detected — using folder names for archive base name.\n"
            )

        for folder, folder_files in sorted(groups.items()):
            cwd = folder
            rel_files = [os.path.relpath(f, cwd) for f in folder_files]

            # Label is folder name, used for status/log context.
            folder_path = Path(cwd)
            label = folder_path.name or "create"

            # If user selected multiple folders, we force per-folder archive base name.
            base_name = derive_archive_base(
                folder=folder_path,
                user_base=user_base,
                multi=multi,
            )

            # Use the actual archive name everywhere (log, status, summaries)
            label = base_name

            argv = [PAR2_BIN, "c"]

            bs_now = self._get_blocksize_bytes()
            if bs_now is not None:
                argv.append(f"-s{bs_now}")

            if self.rb_blocks.get_active():
                argv.append(f"-c{int(self.recovery_blocks.get_value())}")
            else:
                argv.append(f"-r{int(self.redundancy.get_value())}")

            argv += ["-a", base_name]
            argv += rel_files

            jobs.append((argv, cwd, "create", label))

        self.runner.run_many(jobs)

    def _existing_par2_files(self, folder: str, base: str) -> List[str]:
        """Return matching .par2 files in folder for a given base name."""
        if not os.path.isdir(folder):
            return []

        matches: List[str] = []
        for name in os.listdir(folder):
            if not name.lower().endswith(".par2"):
                continue
            if base and name.startswith(base):
                matches.append(name)
        return matches

    def prompt_overwrite_existing_par2(self) -> None:
        """
        Ask once whether to delete/recreate existing PAR2 files.

        If the user agrees:
        - delete all matching PAR2 files across all involved folders
        - then immediately resume creation.
        """
        dialog = Gtk.MessageDialog(
            transient_for=self.win,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text="Parity files already exist",
        )
        dialog.format_secondary_text(
            "Parity files already exist for this set.\n\n"
            "Do you want to delete the existing PAR2 files and recreate them?"
        )
        dialog.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL,
            "Delete and recreate", Gtk.ResponseType.OK,
        )

        response = dialog.run()
        dialog.destroy()

        if response != Gtk.ResponseType.OK:
            self.log.append("Create cancelled: existing PAR2 files were left untouched.\n")
            return

        self._overwrite_confirmed = True
        self._delete_existing_par2_for_selection()

        # Resume create immediately after deletion.
        self._on_create()

    def _delete_existing_par2_for_selection(self) -> None:
        """Delete existing matching PAR2 files for all selected folders."""
        files = self.filelist.paths()
        if not files:
            return

        # Build folder → files grouping, same as create.
        groups: Dict[str, List[str]] = {}
        for f in files:
            groups.setdefault(os.path.dirname(f), []).append(f)

        user_base = self.archive_name.get_text().strip()
        multi = len(groups) > 1

        total_removed = 0

        for folder in groups:
            # Determine which base name should be used in this folder.
            base = user_base if (user_base and not multi) else Path(folder).name

            for name in os.listdir(folder):
                if not name.lower().endswith(".par2"):
                    continue
                if base and not name.startswith(base):
                    continue
                try:
                    os.remove(os.path.join(folder, name))
                    total_removed += 1
                except Exception as e:
                    self.log.append(f"Failed to delete {name}: {e}\n")

        if total_removed:
            self.log.append(
                f"Deleted {total_removed} existing PAR2 file(s). Recreating parity sets…\n\n"
            )
        else:
            self.log.append("No PAR2 files were deleted.\n")


# -----------------------------------------------------------------------------
# Verify / Repair tab
# -----------------------------------------------------------------------------

class VerifyRepairTab(Gtk.Box):
    """Verify or Repair mode tab (one job per PAR2 set)."""

    def __init__(self, win: "MainWindow", runner: Runner, log: LogPane, mode: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        assert mode in ("verify", "repair")

        self.win = win
        self.runner = runner
        self.log = log
        self.mode = mode

        self.par2list = FileList("PAR2 sets (.par2)")
        self.par2list.btn_add.connect("clicked", self._add_par2)
        self.par2list.btn_add_folder.connect("clicked", self._add_folder_par2)

        self.par2list.btn_add.set_tooltip_text(
            "Add one or more specific .par2 files manually.\n"
            "Use this when you know which item you want to verify/repair."
        )
        self.par2list.btn_add_folder.set_tooltip_text(
            "Scan a folder recursively for PAR2 sets.\n"
            "Adds one entry per set (main .par2 file only)."
        )

        btnbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        label = "Verify" if mode == "verify" else "Repair"
        self.btn_run = Gtk.Button.new_with_label(label)
        self.btn_run.set_tooltip_text(f"Run par2 {label.lower()} for all entries in the list.")
        btnbox.pack_start(self.btn_run, False, False, 0)
        self.btn_run.connect("clicked", self._on_run)

        self.pack_start(self.par2list, True, True, 0)
        self.pack_start(btnbox, False, False, 0)

    def _add_par2(self, *_: Any) -> None:
        dialog = create_file_dialog(
            self.win,
            "Select .par2 file(s)",
            Gtk.FileChooserAction.OPEN,
            self.win.session_path,
        )
        dialog.set_select_multiple(True)
        flt = Gtk.FileFilter()
        flt.set_name("PAR2 files")
        flt.add_pattern("*.par2")
        dialog.add_filter(flt)

        resp = dialog.run()
        if resp == Gtk.ResponseType.OK:
            files = dialog.get_filenames()
            if files:
                self.win.session_path = os.path.dirname(files[0])
            self.par2list.add_paths(files)
        dialog.destroy()

    def _add_folder_par2(self, *_: Any) -> None:
        dialog = create_file_dialog(
            self.win,
            "Select folder",
            Gtk.FileChooserAction.SELECT_FOLDER,
            self.win.session_path,
        )
        resp = dialog.run()
        if resp == Gtk.ResponseType.OK:
            folder = dialog.get_filename()
            if folder:
                self.win.session_path = folder

                groups: Dict[str, List[Path]] = {}
                for p in Path(folder).rglob("*.par2"):
                    if not p.is_file():
                        continue
                    key = par2_set_key_from_filename(p.name)
                    groups.setdefault(key, []).append(p)

                chosen: List[str] = []
                for key, files in groups.items():
                    main = next((f for f in files if f.name == f"{key}.par2"), None)
                    chosen.append(str(main if main else files[0]))

                self.par2list.add_paths(sorted(chosen))
        dialog.destroy()

    def _on_run(self, *_: Any) -> None:
        par2s = self.par2list.paths()
        if not par2s:
            self.log.append("Please add at least one .par2 file.\n")
            return

        jobs: List[Tuple[List[str], Optional[str], str, str]] = []
        for par2 in par2s:
            cwd = os.path.dirname(par2)
            fname = os.path.basename(par2)
            label = par2_set_key_from_filename(fname) or Path(cwd).name

            if self.mode == "verify":
                argv = [PAR2_BIN, "V", fname]
                jobs.append((argv, cwd, "verify", label))
            else:
                argv = [PAR2_BIN, "R", fname]
                jobs.append((argv, cwd, "repair", label))

        self.runner.run_many(jobs)


# -----------------------------------------------------------------------------
# Main application window
# -----------------------------------------------------------------------------

class MainWindow(Gtk.Window):
    """Main application window (tabs + log pane + footer status)."""

    def __init__(self) -> None:
        super().__init__(title=APP_TITLE)
        self.set_default_size(1160, 700)

        # Session path: starts from config default, updates while app is open.
        self.session_path = cfg_get("default_path", DEFAULTS["default_path"])
        if not (self.session_path and os.path.isdir(self.session_path)):
            self.session_path = str(Path.home())

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_border_width(10)
        self.add(outer)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        outer.pack_start(paned, True, True, 0)

        self.log = LogPane()

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.spinner = Gtk.Spinner()
        self.spinner.set_no_show_all(True)
        self.spinner.set_tooltip_text("Operation in progress")
        self.spinner.set_margin_end(6)
        self.spinner.set_size_request(16, 16)

        self.status = Gtk.Label(label="Ready")
        self.status.set_xalign(0)

        footer.pack_start(self.spinner, False, False, 0)
        footer.pack_start(self.status, True, True, 0)

        self.chk_verbose = Gtk.CheckButton.new_with_label("Verbose")
        self.chk_verbose.set_active(cfg_get("verbose_logging", "0") == "1")
        self.chk_verbose.set_tooltip_text(
            "Show detailed PAR2 output (progress lines, etc.).\n"
            "Disable for cleaner logs."
        )
        self.chk_verbose.connect("toggled", self._on_toggle_verbose)

        self.btn_cancel = Gtk.Button.new_with_label("Cancel")
        self.btn_cancel.set_sensitive(False)
        self.btn_cancel.set_tooltip_text(
            "Cancel the current operation and clear remaining queued items."
        )
        self.btn_cancel.connect("clicked", self._on_cancel)

        self.btn_about = Gtk.Button.new_with_label("About")
        self.btn_about.set_tooltip_text("About Par2Guard.")
        self.btn_about.connect("clicked", self._on_about)

        footer.pack_end(self.btn_about, False, False, 0)
        footer.pack_end(self.btn_cancel, False, False, 0)
        footer.pack_end(self.chk_verbose, False, False, 0)
        outer.pack_end(footer, False, False, 0)

        def is_verbose() -> bool:
            return self.chk_verbose.get_active()

        self.runner = Runner(
            on_text=self.log.append,
            on_state=self._on_state,
            on_done_msg=self._on_done_msg,
            is_verbose=is_verbose,
        )

        nb = Gtk.Notebook()
        nb.set_scrollable(True)

        self.tab_create = CreateTab(self, self.runner, self.log)
        self.tab_verify = VerifyRepairTab(self, self.runner, self.log, "verify")
        self.tab_repair = VerifyRepairTab(self, self.runner, self.log, "repair")

        nb.append_page(self.tab_create, Gtk.Label(label="Create"))
        nb.append_page(self.tab_verify, Gtk.Label(label="Verify"))
        nb.append_page(self.tab_repair, Gtk.Label(label="Repair"))

        paned.pack1(nb, resize=True, shrink=False)
        paned.pack2(self.log, resize=True, shrink=False)
        paned.set_position(620)

        if which(PAR2_BIN) is None:
            self.log.append("WARNING: 'par2' not found in PATH. Install it: sudo apt install par2\n")

        self.connect("delete-event", self._on_close)

    def _on_state(self, state: Any) -> bool:
        """
        Update footer status/spinner.

        - If `state` is a string: show it and set busy.
        - If `state` is False: clear busy status and reset overwrite flag.
        """
        if isinstance(state, str):
            self.status.set_text(state)
            self.spinner.show()
            self.spinner.start()
            self.set_busy(True)
            return False

        running = state is not False

        if not running:
            self.spinner.stop()
            self.spinner.hide()
            self.status.set_text("Ready")
            self.set_busy(False)

            # Reset overwrite flag after any create batch finishes.
            self.tab_create._overwrite_confirmed = False

        return False

    def _on_done_msg(self, msg: str) -> bool:
        """
        Append a completion message to the log.

        Adds an extra blank line after successful create messages to separate items.
        """
        if msg.strip() == "Parity files created successfully.":
            self.log.append(f"\n{msg}\n\n")
        else:
            self.log.append(f"\n{msg}\n")

        # Keep the footer readable: show single-line messages only.
        if "\n" in msg.strip():
            self.status.set_text("Ready")
        else:
            self.status.set_text(msg)

        return False

    def _on_cancel(self, *_: Any) -> None:
        self.runner.cancel()
        self.spinner.stop()
        self.spinner.hide()
        self.status.set_text("Cancelled")

    def _on_toggle_verbose(self, *_: Any) -> None:
        cfg_set("verbose_logging", "1" if self.chk_verbose.get_active() else "0")

    def _on_about(self, *_: Any) -> None:
        d = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="Par2Guard",
        )
        d.format_secondary_text(
            "A GUI for creating, verifying and repairing PAR2 parity files.\n"
            "Uses the system 'par2' / par2cmdline tool.\n\n"
            "Inspired by the original PyPAR2 project:\n"
            "https://pypar2.fingelrest.net/\n\n"
            f"Defaults can be changed by editing '{CONFIG_FILE.name}' in the application folder."
        )
        d.run()
        d.destroy()

    def _on_close(self, *_: Any) -> bool:
        if self.runner.is_running():
            self.runner.cancel()
        Gtk.main_quit()
        return False

    def set_busy(self, busy: bool) -> None:
        """Enable/disable UI controls while work is running."""
        # Buttons
        self.tab_create.btn_create.set_sensitive(not busy)
        self.tab_verify.btn_run.set_sensitive(not busy)
        self.tab_repair.btn_run.set_sensitive(not busy)

        # Advanced options
        self.tab_create.adv_expander.set_sensitive(not busy)

        # File lists
        self.tab_create.filelist.set_sensitive(not busy)
        self.tab_verify.par2list.set_sensitive(not busy)
        self.tab_repair.par2list.set_sensitive(not busy)

        # Cancel button
        self.btn_cancel.set_sensitive(busy)


# -----------------------------------------------------------------------------
# Application entry point
# -----------------------------------------------------------------------------

def main() -> None:
    win = MainWindow()
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()

