"""
WonderBane Patcher
==================

Downloads the latest client files from the WonderBane server, compares
against local copies via SHA256, and replaces only files that are
different. Files not in the manifest are NEVER touched, so user
preferences, hotkeys, graphics settings, macros, and login info stay
intact.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import urllib.error
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageTk, ImageEnhance
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

# --------------------------------------------------------------------- config
SERVER_BASE = "http://87.99.132.84/"
MANIFEST_URL = SERVER_BASE + "manifest.json"
DEFAULT_INSTALL_DIR = r"C:\WonderBane"
SETTINGS_FILE = Path(os.environ.get("APPDATA", os.path.expanduser("~"))) / "WonderBanePatcher" / "settings.json"
GAME_EXE_REL = "sb.exe"

PROTECTED_PATTERNS = (
    "userOptions/", "UserOptions/",
    "Macros/", "macros/",
    "Logs/", "logs/",
    "Saves/", "saves/",
    # User-modifiable settings files the game writes to.
    "Config/ArcanePref.cfg",
    "Config/ArcaneLanguage.cfg",
    "settingsV5.cfg",
    "DoubleFusion/User.var",
    "DoubleFusion/cache/",
    # Patcher self-protect (cant overwrite a running .exe on Windows).
    "WonderBanePatcher.exe",
)

CHUNK = 1 << 20


# --------------------------------------------------------------------- utils
def resource_path(name: str) -> Path:
    """Locate a bundled data file, both when running as script and when frozen."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def is_protected(rel_path: str) -> bool:
    p = rel_path.replace("\\", "/")
    return any(p.startswith(pat) for pat in PROTECTED_PATTERNS)


def load_settings() -> dict:
    if SETTINGS_FILE.is_file():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_settings(d: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(d, indent=2))


def fetch_url(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "WonderBanePatcher/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def download_to(url: str, dest: Path, on_progress=None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "WonderBanePatcher/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        read = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                read += len(chunk)
                if on_progress:
                    on_progress(read, total)
    if dest.exists():
        dest.unlink()
    tmp.replace(dest)


# --------------------------------------------------------------------- gui
class PatcherApp(tk.Tk):
    WIN_W, WIN_H = 720, 460

    def __init__(self):
        super().__init__()
        self.title("WonderBane Patcher")
        self.geometry(f"{self.WIN_W}x{self.WIN_H}")
        self.resizable(False, False)
        self.configure(bg="#0d0d10")

        settings = load_settings()
        self.install_dir = tk.StringVar(value=settings.get("install_dir", DEFAULT_INSTALL_DIR))
        self.installed_version = settings.get("installed_version")
        self.latest_version = None  # populated after manifest fetch
        self.status = tk.StringVar(value="Ready.")
        self.detail = tk.StringVar(value="")
        self.busy = False

        # ------------ background image as the entire window backdrop
        self.bg_canvas = tk.Canvas(self, width=self.WIN_W, height=self.WIN_H,
                                   highlightthickness=0, bd=0, bg="#0d0d10")
        self.bg_canvas.place(x=0, y=0)
        self._bg_image_ref = None
        self._load_bg()

        # ttk styling — Entry/Button/Progressbar are themed; flat dark blends
        # against the darkened image. Labels are NOT used; we render text
        # directly on the canvas so there are no opaque widget rectangles.
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TButton", font=("Segoe UI", 10), padding=8,
                        background="#222226", foreground="#e8e8e8", borderwidth=0)
        style.map("TButton", background=[("active", "#33333a")])
        style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"), padding=10,
                        background="#ffa033", foreground="#000000", borderwidth=0)
        style.map("Accent.TButton", background=[("active", "#ffb866")])
        style.configure("TEntry", fieldbackground="#1c1c20", foreground="#e0e0e0",
                        insertcolor="#e0e0e0", borderwidth=0)
        style.configure("Horizontal.TProgressbar", troughcolor="#1c1c20",
                        background="#ffa033", thickness=14, borderwidth=0)

        # ------------ canvas-drawn text (no widget rectangles)
        self.bg_canvas.create_text(
            self.WIN_W // 2, 36, text="WonderBane Patcher",
            font=("Segoe UI", 22, "bold"), fill="#ffa033",
        )
        self.bg_canvas.create_text(
            self.WIN_W // 2, 70,
            text="Patches the client. Your settings, hotkeys, and macros are preserved.",
            font=("Segoe UI", 9), fill="#cfcfcf",
        )

        # Version line. Updated after manifest fetch and again after a successful patch.
        self._version_id = self.bg_canvas.create_text(
            self.WIN_W // 2, 92,
            text=self._format_version_line(),
            font=("Segoe UI", 9, "bold"), fill="#ffa033",
        )

        self.bg_canvas.create_text(
            32, 120, text="Game folder", anchor="w",
            font=("Segoe UI", 10, "bold"), fill="#e8e8e8",
        )

        # entry + browse
        entry = ttk.Entry(self.bg_canvas, textvariable=self.install_dir)
        self.bg_canvas.create_window(32, 144, window=entry, anchor="nw", width=560, height=36)
        browse = ttk.Button(self.bg_canvas, text="Browse…", command=self.browse)
        self.bg_canvas.create_window(600, 144, window=browse, anchor="nw", width=88, height=36)

        # progress bar
        self.progress = ttk.Progressbar(
            self.bg_canvas, orient="horizontal", mode="determinate",
            style="Horizontal.TProgressbar",
        )
        self.bg_canvas.create_window(32, 210, window=self.progress, anchor="nw", width=656, height=16)

        # status text (canvas-drawn, updatable via itemconfig)
        self._status_id = self.bg_canvas.create_text(
            32, 244, text="Ready.", anchor="w",
            font=("Segoe UI", 10), fill="#ffffff",
        )
        self._detail_id = self.bg_canvas.create_text(
            32, 268, text="", anchor="w",
            font=("Segoe UI", 9), fill="#cfcfcf",
        )

        # buttons
        self.patch_btn = ttk.Button(
            self.bg_canvas, text="Check & Patch", style="Accent.TButton",
            command=self.start_patch,
        )
        self.bg_canvas.create_window(32, 380, window=self.patch_btn, anchor="nw", width=160, height=44)
        self.launch_btn = ttk.Button(self.bg_canvas, text="Launch", command=self.launch_game)
        self.bg_canvas.create_window(600, 380, window=self.launch_btn, anchor="nw", width=88, height=44)

    # ------------ version helpers
    def _format_version_line(self) -> str:
        installed = self.installed_version or "unknown"
        latest = self.latest_version or "checking…"
        return f"Installed: {installed}    Latest: {latest}"

    def _refresh_version_line(self) -> None:
        try:
            self.bg_canvas.itemconfig(self._version_id, text=self._format_version_line())
        except Exception:
            pass

    # ------------ background loader
    def _load_bg(self):
        if not HAVE_PIL:
            return
        try:
            path = resource_path("wb_bg.png")
            if not path.is_file():
                return
            img = Image.open(path).convert("RGB")
            # Cover-fit the window
            iw, ih = img.size
            scale = max(self.WIN_W / iw, self.WIN_H / ih)
            nw, nh = int(iw * scale), int(ih * scale)
            img = img.resize((nw, nh), Image.LANCZOS)
            # Center-crop to window size
            left = (nw - self.WIN_W) // 2
            top = (nh - self.WIN_H) // 2
            img = img.crop((left, top, left + self.WIN_W, top + self.WIN_H))
            # Darken so widget text stays legible
            img = ImageEnhance.Brightness(img).enhance(0.35)
            self._bg_image_ref = ImageTk.PhotoImage(img)
            self.bg_canvas.create_image(0, 0, image=self._bg_image_ref, anchor="nw")
        except Exception as e:
            sys.stderr.write(f"bg load failed: {e}\n")

    # ------------ actions
    def browse(self):
        d = filedialog.askdirectory(initialdir=self.install_dir.get(), title="Choose your WonderBane folder")
        if d:
            self.install_dir.set(d)

    def set_status(self, text: str, detail: str = ""):
        self.status.set(text)
        self.detail.set(detail)
        # Mirror to canvas-drawn items.
        try:
            self.bg_canvas.itemconfig(self._status_id, text=text)
            self.bg_canvas.itemconfig(self._detail_id, text=detail)
        except Exception:
            pass
        self.update_idletasks()

    def start_patch(self):
        if self.busy:
            return
        target = Path(self.install_dir.get()).expanduser()
        # Validate that the chosen folder actually has the game in it.
        if not (target / "sb.exe").is_file():
            messagebox.showerror(
                "sb.exe not found",
                f"sb.exe was not found in:\n\n{target}\n\n"
                "Check the path and make sure you've selected your WonderBane "
                "folder (the one that contains sb.exe).\n\n"
                "If you don't have WonderBane installed yet, download a fresh "
                "client from the website first, extract it, then point the "
                "patcher at that folder.",
            )
            self.set_status("sb.exe not found in selected folder.")
            return
        save_settings({"install_dir": str(target)})
        self.busy = True
        self.patch_btn.config(state="disabled")
        threading.Thread(target=self._patch_thread, args=(target,), daemon=True).start()

    def _cleanup_old_files(self, target: Path) -> None:
        """One-shot removals for renames / leftover artifacts."""
        # Old Lakebane patcher.
        for p in (target / "LakebanePatcher.exe", target / "Lakebane Patcher.exe"):
            try:
                if p.is_file():
                    p.unlink()
                    self.set_status(f"Removed legacy file: {p.name}")
            except Exception as e:
                sys.stderr.write(f"cleanup failed for {p}: {e}\n")

        # An earlier patcher build wrote into a nested Wonderbane/ folder by
        # mistake. Detect and remove it so the user has one clean install tree.
        nested = target / "Wonderbane"
        if nested.is_dir() and (target / "sb.exe").is_file():
            try:
                import shutil
                shutil.rmtree(nested)
                self.set_status("Removed stray nested Wonderbane/ folder.")
            except Exception as e:
                sys.stderr.write(f"could not remove nested Wonderbane/: {e}\n")

    def _patch_thread(self, target: Path):
        try:
            self._cleanup_old_files(target)
            self.set_status("Fetching manifest…")
            data = fetch_url(MANIFEST_URL)
            manifest = json.loads(data.decode("utf-8"))
            files = manifest.get("files", [])
            base_url = SERVER_BASE + manifest.get("baseUrl", "client/")
            self.latest_version = manifest.get("gameVersion")
            self._refresh_version_line()
            self.set_status(f"Manifest loaded ({len(files)} files). Comparing…")

            to_download = []
            ok = 0
            for entry in files:
                rel = entry["path"]
                if is_protected(rel):
                    continue
                local = target / rel
                if not local.is_file() or local.stat().st_size != entry["size"]:
                    to_download.append(entry)
                    continue
                if sha256_file(local) != entry["sha256"]:
                    to_download.append(entry)
                else:
                    ok += 1

            total_bytes = sum(e["size"] for e in to_download)
            self.set_status(
                f"{ok} files up to date, {len(to_download)} need patching ({total_bytes / 1024 / 1024:.1f} MB)."
            )

            if not to_download:
                self.progress["value"] = self.progress["maximum"] = 1
                self.progress["value"] = 1
                self.set_status("Already up to date.", "Nothing to download.")
                # Stamp the installed version even when nothing changed, so a
                # fresh-install user who runs the patcher and finds no diffs
                # still gets the version label updated from "unknown".
                if self.latest_version and self.installed_version != self.latest_version:
                    self.installed_version = self.latest_version
                    s = load_settings()
                    s["installed_version"] = self.latest_version
                    save_settings(s)
                    self._refresh_version_line()
                self.launch_btn.configure(style="Accent.TButton")
                return

            self.progress["value"] = 0
            self.progress["maximum"] = total_bytes if total_bytes > 0 else 1
            done_bytes = 0
            t0 = time.time()
            for i, entry in enumerate(to_download, 1):
                rel = entry["path"]
                local = target / rel
                url = base_url + rel
                self.set_status(
                    f"[{i}/{len(to_download)}] {rel}",
                    f"{done_bytes / 1024 / 1024:.1f} / {total_bytes / 1024 / 1024:.1f} MB",
                )
                start = done_bytes

                def on_progress(read, total, _start=start):
                    self.progress["value"] = _start + read
                    self.update_idletasks()

                try:
                    download_to(url, local, on_progress=on_progress)
                except Exception as e:
                    self.set_status("Error.", f"{rel}: {e}")
                    raise
                done_bytes = start + entry["size"]
                self.progress["value"] = done_bytes

            elapsed = time.time() - t0
            previous_version = self.installed_version
            if self.latest_version:
                self.installed_version = self.latest_version
                s = load_settings()
                s["installed_version"] = self.latest_version
                save_settings(s)
                self._refresh_version_line()
            ver_summary = ""
            if previous_version and self.latest_version and previous_version != self.latest_version:
                ver_summary = f"  Version {previous_version} → {self.latest_version}."
            elif self.latest_version:
                ver_summary = f"  Version {self.latest_version}."
            self.set_status(
                "Patch complete.",
                f"{len(to_download)} files updated, {total_bytes / 1024 / 1024:.1f} MB in {elapsed:.0f}s.{ver_summary}",
            )
            self.launch_btn.configure(style="Accent.TButton")
        except urllib.error.URLError as e:
            messagebox.showerror("Network error", f"Could not reach the patch server.\n\n{e}")
            self.set_status("Network error.")
        except Exception as e:
            messagebox.showerror("Patch failed", str(e))
            self.set_status("Patch failed.", str(e))
        finally:
            self.busy = False
            self.patch_btn.config(state="normal")

    def launch_game(self):
        target = Path(self.install_dir.get()).expanduser()
        exe = target / GAME_EXE_REL
        if not exe.is_file():
            messagebox.showerror("Game not found", f"Could not find {GAME_EXE_REL} in {target}.\n\nPatch first?")
            return

        # Refresh the latest version from the manifest so the warning is
        # accurate even if the user opened the patcher and went straight to
        # Launch without clicking Check & Patch first. A network failure here
        # is non-fatal -- we just skip the version warning and let them play.
        if not self.latest_version:
            try:
                data = fetch_url(MANIFEST_URL, timeout=10)
                self.latest_version = json.loads(data.decode("utf-8")).get("gameVersion")
                self._refresh_version_line()
            except Exception:
                pass

        if self.latest_version and self.installed_version != self.latest_version:
            if self.installed_version:
                msg = (
                    f"Your installed version is {self.installed_version}.\n"
                    f"The latest version is {self.latest_version}.\n\n"
                    "You may experience a less than ideal experience until you patch.\n\n"
                    "Launch anyway?"
                )
            else:
                msg = (
                    f"The latest version is {self.latest_version}.\n"
                    "We don't have a record of your installed version yet.\n\n"
                    "Patch first to be safe, or launch anyway?"
                )
            if not messagebox.askokcancel("Out of date", msg):
                return

        try:
            subprocess.Popen([str(exe)], cwd=str(exe.parent))
        except Exception as e:
            messagebox.showerror("Launch failed", str(e))


if __name__ == "__main__":
    app = PatcherApp()
    app.mainloop()
