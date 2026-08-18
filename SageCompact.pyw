"""
NTFS Compact GUI
----------------
A simple Tkinter GUI that lets you browse for a folder, runs
`compact /c /s:"<folder>" /i /exe:<algorithm>` (or /u to decompress) on it,
streams live output into a debug log, and reports before/after folder size.

Runs the compact.exe process in a background thread with subprocess.Popen so
the GUI stays responsive (no freezing), and uses a thread-safe queue to push
log lines / progress back to the Tkinter main loop.

Requires: Windows, Python 3.8+, no third-party packages.
For best results (system/hidden files, protected folders), run this script
"as Administrator".
"""

import os
import sys
import ctypes
import queue
import threading
import subprocess
import time
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

ALGORITHMS = ["(NTFS default - none)", "XPRESS4K", "XPRESS8K", "XPRESS16K", "LZX"]


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def get_folder_size(path: str) -> int:
    """Sum of apparent file sizes (logical size, not on-disk size)."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def get_folder_size_on_disk(path: str) -> int:
    """Actual on-disk allocated size, using GetCompressedFileSizeW.
    This is the number that changes when compact.exe compresses files."""
    GetCompressedFileSizeW = ctypes.windll.kernel32.GetCompressedFileSizeW
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            fp = os.path.join(root, f)
            try:
                high = ctypes.c_ulong(0)
                low = GetCompressedFileSizeW(fp, ctypes.byref(high))
                if low == 0xFFFFFFFF:
                    err = ctypes.windll.kernel32.GetLastError()
                    if err != 0:
                        continue
                total += (high.value << 32) + low
            except Exception:
                pass
    return total


def human_size(n: int) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step:
            return f"{n:,.2f} {unit}"
        n /= step
    return f"{n:,.2f} PB"


class CompactGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NTFS Compact GUI")
        self.geometry("880x620")
        self.minsize(760, 520)

        self.log_queue = queue.Queue()
        self.worker_thread = None
        self.proc = None
        self.cancel_requested = False
        self.before_size_apparent = None
        self.before_size_disk = None

        self._build_widgets()
        self._poll_queue()

        if not is_admin():
            self._log("WARNING: Not running as Administrator. Compact may fail "
                       "on system/hidden files or protected folders. Consider "
                       "re-launching this tool elevated.")

    # ---------- UI ----------
    def _build_widgets(self):
        pad = {"padx": 8, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Folder:").pack(side="left")
        self.folder_var = tk.StringVar()
        self.folder_entry = ttk.Entry(top, textvariable=self.folder_var)
        self.folder_entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="Browse...", command=self.browse_folder).pack(side="left")

        opts = ttk.Frame(self)
        opts.pack(fill="x", **pad)

        ttk.Label(opts, text="Mode:").pack(side="left")
        self.mode_var = tk.StringVar(value="Compress (/C)")
        mode_combo = ttk.Combobox(opts, textvariable=self.mode_var, state="readonly",
                                   values=["Compress (/C)", "Uncompress (/U)"], width=18)
        mode_combo.pack(side="left", padx=(4, 16))

        ttk.Label(opts, text="Algorithm (/EXE):").pack(side="left")
        self.algo_var = tk.StringVar(value=ALGORITHMS[0])
        algo_combo = ttk.Combobox(opts, textvariable=self.algo_var, state="readonly",
                                   values=ALGORITHMS, width=20)
        algo_combo.pack(side="left", padx=(4, 16))

        self.force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="/F Force (recompress already-compressed files)",
                         variable=self.force_var).pack(side="left", padx=(0, 12))

        self.quiet_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="/Q Quiet output", variable=self.quiet_var).pack(side="left")

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        self.run_btn = ttk.Button(btns, text="Run Compact", command=self.on_run)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        ttk.Button(btns, text="Save Log...", command=self.save_log).pack(side="left", padx=6)
        ttk.Button(btns, text="Clear Log", command=self.clear_log).pack(side="left")

        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.pack(fill="x", padx=8, pady=(0, 6))

        sizes = ttk.LabelFrame(self, text="Size summary")
        sizes.pack(fill="x", padx=8, pady=6)
        self.size_before_lbl = ttk.Label(sizes, text="Before: -")
        self.size_before_lbl.grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.size_after_lbl = ttk.Label(sizes, text="After: -")
        self.size_after_lbl.grid(row=0, column=1, sticky="w", padx=8, pady=4)
        self.size_saved_lbl = ttk.Label(sizes, text="Saved: -")
        self.size_saved_lbl.grid(row=0, column=2, sticky="w", padx=8, pady=4)

        log_frame = ttk.LabelFrame(self, text="Debug log")
        log_frame.pack(fill="both", expand=True, padx=8, pady=6)
        self.log_text = tk.Text(log_frame, wrap="none", state="disabled", bg="#111", fg="#ddd",
                                 insertbackground="#ddd", font=("Consolas", 9))
        self.log_text.pack(side="left", fill="both", expand=True)
        yscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        yscroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=yscroll.set)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status_var, anchor="w").pack(fill="x", padx=8, pady=(0, 6))

    # ---------- Helpers ----------
    def browse_folder(self):
        folder = filedialog.askdirectory(title="Select folder to compress/uncompress")
        if folder:
            self.folder_var.set(folder)

    def _log(self, msg: str):
        self.log_queue.put(msg)

    def _poll_queue(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                ts = datetime.now().strftime("%H:%M:%S")
                self.log_text.insert("end", f"[{ts}] {line}\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def save_log(self):
        path = filedialog.asksaveasfilename(defaultextension=".txt",
                                             filetypes=[("Text file", "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_text.get("1.0", "end"))
            self._log(f"Log saved to {path}")

    # ---------- Run logic ----------
    def on_run(self):
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Invalid folder", "Please choose a valid, existing folder.")
            return
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Busy", "A compact operation is already running.")
            return

        mode_flag = "/C" if self.mode_var.get().startswith("Compress") else "/U"
        cmd = ["compact", mode_flag, f'/S:{folder}', "/I"]
        if self.quiet_var.get():
            cmd.append("/Q")
        if self.force_var.get():
            cmd.append("/F")
        algo = self.algo_var.get()
        if algo != ALGORITHMS[0] and mode_flag == "/C":
            cmd.append(f"/EXE:{algo}")

        self.run_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.cancel_requested = False
        self.progress.start(12)
        self.status_var.set("Scanning folder size (before)...")
        self._log(f"Command: {' '.join(cmd)}")

        self.worker_thread = threading.Thread(target=self._worker, args=(folder, cmd), daemon=True)
        self.worker_thread.start()

    def on_cancel(self):
        self.cancel_requested = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self._log("Cancel requested: terminating compact.exe process...")
            except Exception as e:
                self._log(f"Failed to terminate process: {e}")

    def _worker(self, folder: str, cmd: list):
        try:
            t0 = time.time()
            self.before_size_apparent = get_folder_size(folder)
            self.before_size_disk = get_folder_size_on_disk(folder)
            self.after(0, lambda: self.size_before_lbl.configure(
                text=f"Before: {human_size(self.before_size_disk)} on disk "
                     f"({human_size(self.before_size_apparent)} apparent)"))
            self._log(f"Before size (on disk): {human_size(self.before_size_disk)}")
            self._log(f"Before size (apparent): {human_size(self.before_size_apparent)}")
            self._log(f"Size scan took {time.time() - t0:.1f}s")

            self.after(0, lambda: self.status_var.set("Running compact.exe..."))
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, universal_newlines=True,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            for line in iter(self.proc.stdout.readline, ""):
                if line:
                    self._log(line.rstrip())
                if self.cancel_requested:
                    break
            self.proc.wait()
            rc = self.proc.returncode
            self._log(f"compact.exe exited with code {rc}")

            self.after(0, lambda: self.status_var.set("Scanning folder size (after)..."))
            after_apparent = get_folder_size(folder)
            after_disk = get_folder_size_on_disk(folder)
            self.after(0, lambda: self.size_after_lbl.configure(
                text=f"After: {human_size(after_disk)} on disk "
                     f"({human_size(after_apparent)} apparent)"))
            self._log(f"After size (on disk): {human_size(after_disk)}")
            self._log(f"After size (apparent): {human_size(after_apparent)}")

            saved = self.before_size_disk - after_disk
            pct = (saved / self.before_size_disk * 100) if self.before_size_disk else 0
            self.after(0, lambda: self.size_saved_lbl.configure(
                text=f"Saved: {human_size(saved)} ({pct:.1f}%)"))
            self._log(f"Saved: {human_size(saved)} ({pct:.1f}%)")

            if self.cancel_requested:
                self.after(0, lambda: self.status_var.set("Cancelled."))
            elif rc == 0:
                self.after(0, lambda: self.status_var.set("Done."))
            else:
                self.after(0, lambda: self.status_var.set(f"Finished with errors (exit code {rc})."))
        except FileNotFoundError:
            self._log("ERROR: compact.exe not found. This tool only works on Windows with NTFS.")
            self.after(0, lambda: self.status_var.set("Error: compact.exe not found."))
        except Exception as e:
            self._log(f"ERROR: {e}")
            self.after(0, lambda: self.status_var.set("Error - see log."))
        finally:
            self.after(0, self._finish_ui)

    def _finish_ui(self):
        self.progress.stop()
        self.run_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.proc = None


if __name__ == "__main__":
    if sys.platform != "win32":
        print("This tool only works on Windows (uses compact.exe / NTFS).")
        sys.exit(1)
    app = CompactGUI()
    app.mainloop()
