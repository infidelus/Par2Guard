#!/usr/bin/env python3
# Par2Guard - Python 3 GTK3 (PyGObject) GUI wrapper around par2cmdline
#
# Features:
# - Create PAR2 sets (redundancy %, archive base name, file/folder selection)
# - Verify & Repair (single files or recursive scan; runs one job per PAR2 set)
# - Project-local config.ini (default_path, redundancy_percent, verbose_logging 0/1)
# - Session-only "last used" folder while app is open (does not overwrite config default_path)
# - Queued jobs + clear end-of-run summary (needs repair / repaired / failed)
# - Clean logging by default (optional verbose)
#
# Inspired by the original PyPAR2 project: https://pypar2.fingelrest.net/

import os
import shlex
import signal
import subprocess
import threading
import time
import configparser
from pathlib import Path
from typing import Callable, Optional, List, Tuple, Dict, Any

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk

APP_TITLE = "Par2Guard"
PAR2_BIN = "par2"

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.ini"

DEFAULTS = {
    "default_path": str(Path.home()),
    "redundancy_percent": "10",
    "verbose_logging": "0",
}

def which(cmd: str) -> Optional[str]:
    from shutil import which as _which
    return _which(cmd)

def _coerce_int(s: str, default: int) -> int:
    try:
        return int(str(s).strip())
    except Exception:
        return default

def load_config() -> configparser.ConfigParser:
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

def create_file_dialog(parent: Gtk.Window, title: str, action: Gtk.FileChooserAction, start_path: Optional[str]) -> Gtk.FileChooserDialog:
    dialog = Gtk.FileChooserDialog(title=title, transient_for=parent, modal=True, action=action)
    dialog.add_buttons(
        Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
        Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
    )
    if start_path and os.path.isdir(start_path):
        try:
            dialog.set_current_folder(start_path)
        except Exception:
            pass
    return dialog

def par2_set_key_from_filename(name: str) -> str:
    # Groups "Album.par2" with "Album.volXXX+YYY.par2"
    if name.lower().endswith(".par2"):
        stem = name[:-5]
    else:
        stem = name
    if ".vol" in stem:
        stem = stem.split(".vol", 1)[0]
    return stem

class LogPane(Gtk.Box):
    def __init__(self):
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
        end_iter = self.buffer.get_end_iter()
        self.buffer.insert(end_iter, text)
        mark = self.buffer.create_mark(None, self.buffer.get_end_iter(), False)
        self.textview.scroll_mark_onscreen(mark)

    def _on_copy(self, *_):
        start, end = self.buffer.get_bounds()
        txt = self.buffer.get_text(start, end, True)
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(txt, -1)

class Runner:
    """Runs jobs sequentially, streams output, and produces end-of-run summaries."""
    def __init__(
        self,
        on_text: Callable[[str], None],
        on_state: Callable[[Any], None],    # bool or status string
        on_done_msg: Callable[[str], None],
        is_verbose: Callable[[], bool],
    ):
        self.on_text = on_text
        self.on_state = on_state
        self.on_done_msg = on_done_msg
        self.is_verbose = is_verbose

        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._queue: List[Tuple[List[str], Optional[str], str, str]] = []  # argv, cwd, mode, label
        self._cancel_requested = False

        self._total_jobs = 0
        self._completed_jobs = 0
        self._batch_mode = ""
        self._results: Dict[str, List[str]] = {}

    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def cancel(self) -> None:
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

    def run_many(self, jobs: List[Tuple[List[str], Optional[str], str, str]]) -> None:
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
        self._kick()

    def run_one(self, argv: List[str], cwd: Optional[str], mode: str, label: str = "") -> None:
        self.run_many([(argv, cwd, mode, label or mode)])

    def _kick(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            if self._queue:
                argv, cwd, mode, label = self._queue.pop(0)
                self._cancel_requested = False
                total = self._total_jobs
                done = self._completed_jobs + 1
                action = "Running"
                if mode == "verify":
                    action = "Verifying"
                elif mode == "repair":
                    action = "Repairing"
                elif mode == "create":
                    action = "Creating"
                GLib.idle_add(self.on_state, f"{action} {done} / {total} items…  ({label})")
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
        def worker():
            GLib.idle_add(self.on_text, f"$ {' '.join(shlex.quote(a) for a in argv)}\n")
            if cwd:
                GLib.idle_add(self.on_text, f"[cwd: {cwd}]\n")

            tail: List[str] = []
            buf: List[str] = []
            last_flush = time.time()

            def keep_tail(line: str) -> None:
                tail.append(line)
                if len(tail) > 300:
                    del tail[:60]

            def should_show(line: str) -> bool:
                if self.is_verbose():
                    return True
                return not (line.startswith("Loading:") or line.startswith("Scanning:"))

            def flush(force: bool = False) -> None:
                nonlocal last_flush
                if not buf:
                    return
                now = time.time()
                if force or (len(buf) >= 60) or (now - last_flush >= 0.12):
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
                        bufsize=1,
                        universal_newlines=True,
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

    def _summarize_one(self, mode: str, rc: int, tail: List[str], err: Optional[str]) -> Tuple[str, str]:
        if err is not None:
            if "not found" in err.lower():
                cls = "verify_failed" if mode == "verify" else ("repair_failed" if mode == "repair" else "")
                return ("Error: 'par2' not found. Install it (Mint/Ubuntu): sudo apt install par2", cls)
            cls = "verify_failed" if mode == "verify" else ("repair_failed" if mode == "repair" else "")
            return (f"Error: {err}", cls)

        with self._lock:
            if self._cancel_requested:
                return ("Cancelled.", "")

        joined = "".join(tail).lower()
        if rc != 0:
            if mode == "verify" and rc == 1:
                # Verification completed but repair is required (not an error)
                if "repair is required" in joined or "missing" in joined:
                    return ("Verification complete. Repair is required.", "verify_need_repair")
                return ("Verification complete.", "verify_ok")

            cls = "verify_failed" if mode == "verify" else ("repair_failed" if mode == "repair" else "")
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
        if mode == "verify":
            ok = results.get("verify_ok", [])
            need = results.get("verify_need_repair", [])
            failed = results.get("verify_failed", [])
            lines = []
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

class FileList(Gtk.Box):
    def __init__(self, title: str, col_title: str = "Path"):
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
        return [row[0] for row in self.store]

    def add_paths(self, paths: List[str]) -> None:
        existing = set(self.paths())
        for p in paths:
            if p and p not in existing:
                self.store.append([p])
                existing.add(p)

    def _on_remove(self, *_):
        sel = self.view.get_selection()
        model, it = sel.get_selected()
        if it is not None:
            model.remove(it)

class CreateTab(Gtk.Box):
    def __init__(self, win: "MainWindow", runner: Runner, log: LogPane):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.win = win
        self.runner = runner
        self.log = log

        grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        self.redundancy = Gtk.SpinButton.new_with_range(1, 100, 1)
        self.redundancy.set_value(_coerce_int(cfg_get("redundancy_percent", "10"), 10))

        self.archive_name = Gtk.Entry()
        self.archive_name.set_placeholder_text('e.g. "Now 117 D2" (base name, without .par2)')

        self.chk_recursive = Gtk.CheckButton.new_with_label("Include subfolders when adding folders (recursive)")
        self.chk_recursive.set_active(False)

        lbl_r = Gtk.Label(label="Redundancy (%)"); lbl_r.set_xalign(0)
        lbl_a = Gtk.Label(label="Archive base name (-a)"); lbl_a.set_xalign(0)

        grid.attach(lbl_r, 0, 0, 1, 1); grid.attach(self.redundancy, 1, 0, 1, 1)
        grid.attach(lbl_a, 0, 1, 1, 1); grid.attach(self.archive_name, 1, 1, 1, 1)
        grid.attach(self.chk_recursive, 0, 2, 2, 1)

        self.filelist = FileList("Files to protect")
        self.filelist.btn_add.connect("clicked", self._add_files)
        self.filelist.btn_add_folder.connect("clicked", self._add_folder)

        self.filelist.btn_add.set_tooltip_text("Add one or more files to protect with PAR2.")
        self.filelist.btn_add_folder.set_tooltip_text("Add all files from a folder (recursive optional).")

        btnbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.btn_create = Gtk.Button.new_with_label("Create PAR2")
        self.btn_create.set_tooltip_text("Create PAR2 parity files for the selected files/folders.")
        btnbox.pack_start(self.btn_create, False, False, 0)
        self.btn_create.connect("clicked", self._on_create)

        self.pack_start(grid, False, False, 0)
        self.pack_start(self.filelist, True, True, 0)
        self.pack_start(btnbox, False, False, 0)

    def _add_files(self, *_):
        dialog = create_file_dialog(self.win, "Select files", Gtk.FileChooserAction.OPEN, self.win.session_path)
        dialog.set_select_multiple(True)
        resp = dialog.run()
        if resp == Gtk.ResponseType.OK:
            files = dialog.get_filenames()
            if files:
                self.win.session_path = os.path.dirname(files[0])
            self.filelist.add_paths(files)
        dialog.destroy()

    def _add_folder(self, *_):
        dialog = create_file_dialog(self.win, "Select folder", Gtk.FileChooserAction.SELECT_FOLDER, self.win.session_path)
        resp = dialog.run()
        if resp == Gtk.ResponseType.OK:
            folder = dialog.get_filename()
            if folder:
                self.win.session_path = folder
                base = Path(folder)

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

    def _on_create(self, *_):
        files = [p for p in self.filelist.paths() if not p.lower().endswith(".par2")]
        if not files:
            self.log.append("Please add at least one file.\n")
            return

        r = int(self.redundancy.get_value())
        cfg_set("redundancy_percent", str(r))
        user_base = self.archive_name.get_text().strip()

        groups: Dict[str, List[str]] = {}
        for f in files:
            d = os.path.dirname(f)
            groups.setdefault(d, []).append(f)

        jobs: List[Tuple[List[str], Optional[str], str, str]] = []
        multi = len(groups) > 1

        if multi and user_base:
            self.log.append("Multiple folders detected — using folder names for archive base name.\n")

        for d, fs in sorted(groups.items()):
            cwd = d
            rel_files = [os.path.relpath(f, cwd) for f in fs]
            label = Path(cwd).name or "create"
            base_name = user_base if (not multi and user_base) else label
            argv = [PAR2_BIN, "c", f"-r{r}", "-a", base_name] + rel_files
            jobs.append((argv, cwd, "create", label))

        self.runner.run_many(jobs)

class VerifyRepairTab(Gtk.Box):
    def __init__(self, win: "MainWindow", runner: Runner, log: LogPane, mode: str):
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

    def _add_par2(self, *_):
        dialog = create_file_dialog(self.win, "Select .par2 file(s)", Gtk.FileChooserAction.OPEN, self.win.session_path)
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

    def _add_folder_par2(self, *_):
        dialog = create_file_dialog(self.win, "Select folder", Gtk.FileChooserAction.SELECT_FOLDER, self.win.session_path)
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

    def _on_run(self, *_):
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

class MainWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title=APP_TITLE)
        self.set_default_size(1160, 700)

        # Session path: starts from config default, then updates while app is open.
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
        self.status = Gtk.Label(label="Ready")
        self.status.set_xalign(0)

        self.chk_verbose = Gtk.CheckButton.new_with_label("Verbose")
        self.chk_verbose.set_active(cfg_get("verbose_logging", "0") == "1")
        self.chk_verbose.set_tooltip_text(
            "Show detailed PAR2 output (progress lines, etc.).\n"
            "Disable for cleaner logs."
        )
        self.chk_verbose.connect("toggled", self._on_toggle_verbose)

        self.btn_cancel = Gtk.Button.new_with_label("Cancel")
        self.btn_cancel.set_sensitive(False)
        self.btn_cancel.set_tooltip_text("Cancel the current operation and clear remaining queued items.")
        self.btn_cancel.connect("clicked", lambda *_: self.runner.cancel())

        self.btn_about = Gtk.Button.new_with_label("About")
        self.btn_about.set_tooltip_text("About Par2Guard.")
        self.btn_about.connect("clicked", self._on_about)

        footer.pack_start(self.status, True, True, 0)
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
        if isinstance(state, str):
            self.status.set_text(state)
            self.btn_cancel.set_sensitive(True)
            return False

        running = bool(state)
        self.btn_cancel.set_sensitive(running)
        if not running:
            self.status.set_text("Ready")
        return False

    def _on_done_msg(self, msg: str) -> bool:
        self.log.append(f"\n{msg}\n")
        if "\n" in msg.strip():
            self.status.set_text("Ready")
        else:
            self.status.set_text(msg)
        return False

    def _on_toggle_verbose(self, *_):
        cfg_set("verbose_logging", "1" if self.chk_verbose.get_active() else "0")

    def _on_about(self, *_):
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

    def _on_close(self, *_):
        if self.runner.is_running():
            self.runner.cancel()
        Gtk.main_quit()
        return False

def main():
    win = MainWindow()
    win.show_all()
    Gtk.main()

if __name__ == "__main__":
    main()
