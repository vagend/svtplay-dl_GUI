#!/usr/bin/env python3
"""
svtplay-dl GUI: a desktop front end for svtplay-dl (https://github.com/spaam/svtplay-dl),
laid out after Open Video Downloader (https://github.com/jely2002/youtube-dl-gui).

The window is a web page (web/index.html) served by this script on 127.0.0.1 only, and the
downloads run here in Python. It uses the standard library only, so it needs no venv and no
pip installs, and any Python 3.8+ will do.

Run:  python3 svtplay_dl_gui.py          (or double-click run_gui.command on a Mac)
"""

from __future__ import annotations

import dataclasses
import html
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import svtdl_core as core  # noqa: E402
import svtdl_follow as follow  # noqa: E402

# Inside the packaged app the page files are unpacked next to the code (PyInstaller's _MEIPASS).
RES_DIR = Path(getattr(sys, "_MEIPASS", HERE))
WEB_DIR = (RES_DIR / "web").resolve()
def _res_text(name: str) -> str:
    try:
        return (RES_DIR / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


APP_VERSION = _res_text("VERSION")
APP_BUILD = int(_res_text("APP_BUILD") or 0)        # GitHub's build number, written by the build workflow
APP_REPO = _res_text("APP_REPO")                    # e.g. vagend/svtplay-dl_GUI, written by the build workflow
FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    # The packaged app has its own copy of Python, which may not see the system's certificates:
    # use the certificate list bundled with it for https.
    try:
        import ssl
        import certifi
        ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())  # noqa: E731
    except ImportError:
        pass
WINDOW = None   # the app's window when it runs as an app (pywebview), else None
ACTIVE = {"starting", "downloading", "merging", "moving"}
STARTABLE = {"new", "error", "stopped", "fetching"}   # (no need to wait for the list of qualities)
STATUS_TEXT = {
    "new": "Ready",
    "fetching": "Checking available qualities…",
    "waiting": "Waiting in queue",
    "starting": "Starting…",
    "downloading": "Downloading",
    "merging": "Merging audio, video and subtitles…",
    "moving": "Copying to the network folder…",
    "done": "Done",
    "skipped": "Already downloaded",
    "error": "Failed",
    "stopped": "Stopped",
    "expanding": "Finding episodes…",
    "picking": "Choose which episodes to add",
}


# ==========================================================================
# Page metadata (title + thumbnail) for nicer cards
# ==========================================================================

_META_RE = re.compile(r"<meta\s+[^>]*>", re.I)
_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')')


def fetch_page_meta(url: str, timeout: float = 10) -> dict:
    """Read og:title / og:image from the program page. Best effort; {} on failure."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                          "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            "Accept-Language": "sv,en;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(800_000)
            charset = r.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, errors="replace")
    except Exception:
        return {}
    meta: dict = {}
    for tag in _META_RE.findall(text):
        attrs = {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4))
                 for m in _ATTR_RE.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        val = attrs.get("content")
        if not val:
            continue
        if key in ("og:title", "twitter:title") and "title" not in meta:
            meta["title"] = html.unescape(val).strip()
        elif key in ("og:image", "og:image:url", "twitter:image") and "image" not in meta:
            meta["image"] = html.unescape(val).strip()
        elif key in ("og:description", "description") and "description" not in meta:
            meta["description"] = html.unescape(val).strip()
    if "title" not in meta:
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        if m:
            meta["title"] = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    if meta.get("image"):
        meta["image"] = urljoin(url, meta["image"])
    return meta


GEO_URLS = [u for u in os.environ.get("SVTDL_GUI_GEO_URL", "").split(",") if u] or [
    "https://ipinfo.io/json",      # free without a key, 1,000 lookups/day
    "https://ipapi.co/json/",      # fallback
]


def check_public_location(proxy: str = "") -> dict:
    """Which IP address and country do downloads appear to come from?
    Uses the same proxy setting as svtplay-dl (curl understands http, https and socks proxies)."""
    proxy = proxy.strip()
    if proxy.startswith("socks5://"):
        proxy = proxy.replace("socks5://", "socks5h://", 1)   # same as svtplay-dl: DNS via the proxy
    curl = shutil.which("curl") or ("/usr/bin/curl" if Path("/usr/bin/curl").exists() else None)
    last_err = ""
    for url in GEO_URLS:
        try:
            if curl:
                cmd = [curl, "-sS", "-m", "12", "-H", "Accept: application/json", "-A", "svtplay-dl-gui"]
                if proxy:
                    cmd += ["--proxy", proxy]
                r = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=20, **core._popen_flags())
                if r.returncode != 0:
                    last_err = (r.stderr.strip().splitlines() or [f"curl exit {r.returncode}"])[-1]
                    continue
                body = r.stdout
            else:
                if proxy.startswith("socks"):
                    return {"error": "Checking through a SOCKS proxy needs curl"}
                handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
                opener = urllib.request.build_opener(*handlers)
                with opener.open(urllib.request.Request(url, headers={"User-Agent": "svtplay-dl-gui"}), timeout=12) as resp:
                    body = resp.read().decode("utf-8", "replace")
            data = json.loads(body)
        except Exception as e:  # network errors, bad JSON
            last_err = str(e)
            continue
        if not isinstance(data, dict) or data.get("error") or data.get("bogon"):
            last_err = str(data.get("reason") or data.get("error") or "no answer") if isinstance(data, dict) else "bad answer"
            continue
        code = (data.get("country") if len(str(data.get("country", ""))) == 2 else data.get("country_code")) or ""
        return {
            "ip": data.get("ip", ""),
            "country": str(code).upper(),
            "city": data.get("city", ""),
            "org": data.get("org", "") or data.get("org_name", ""),
            "via": "proxy" if proxy else "direct",
        }
    return {"error": last_err or "Couldn't reach the location service"}


# ==========================================================================
# Updating svtplay-dl
# ==========================================================================

BREW_API = os.environ.get("SVTDL_GUI_BREW_API", "https://formulae.brew.sh/api/formula/svtplay-dl.json")
PYPI_API = os.environ.get("SVTDL_GUI_PYPI_API", "https://pypi.org/pypi/svtplay-dl/json")


def _get_json(url: str) -> dict:
    curl = shutil.which("curl") or ("/usr/bin/curl" if Path("/usr/bin/curl").exists() else None)
    if curl:
        r = subprocess.run([curl, "-sSfL", "-m", "15", url], capture_output=True, text=True, timeout=25,
                           **core._popen_flags())
        if r.returncode != 0:
            raise RuntimeError((r.stderr.strip().splitlines() or ["download failed"])[-1])
        return json.loads(r.stdout)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "svtplay-dl-gui"}),
                                timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def version_tuple(v: str) -> tuple:
    m = re.search(r"\d+(?:\.\d+)*", v or "")
    return tuple(int(x) for x in m.group(0).split(".")) if m else ()


def detect_install(cmd: Optional[list[str]]) -> dict:
    """Work out how svtplay-dl was installed, and so how to update it."""
    if not cmd:
        return {"method": "none"}
    if len(cmd) >= 3 and cmd[1:3] == ["-m", "svtplay_dl"]:
        return {"method": "pip", "python": cmd[0]}
    exe = Path(cmd[-1] if cmd[0].endswith(("python", "python3")) and len(cmd) == 2 else cmd[0])
    try:
        real = exe.resolve()
    except OSError:
        real = exe
    parts = real.parts
    low = str(real).replace("\\", "/").lower()
    if "/winget/packages/spaam.svtplay-dl" in low:
        return {"method": "winget", "winget": find_winget() or "winget"}
    if core.IS_WINDOWS and real.parent.name.lower() == "scripts":
        # pip on Windows puts svtplay-dl.exe in <python>\Scripts (or <venv>\Scripts)
        for py in (real.parent.parent / "python.exe", real.parent / "python.exe"):
            if py.exists():
                return {"method": "pip", "python": str(py)}
    if "Cellar" in parts and "svtplay-dl" in parts:
        prefix = Path(*parts[:parts.index("Cellar")])
        brew = prefix / "bin" / "brew"
        if not brew.exists():
            brew = Path(shutil.which("brew") or "/opt/homebrew/bin/brew")
        return {"method": "brew", "brew": str(brew)}
    # a pip-installed console script: its first line names the Python it runs on
    try:
        first = real.open("rb").readline(300).decode("utf-8", "replace")
    except OSError:
        first = ""
    if first.startswith("#!") and "python" in first:
        py = first[2:].strip().split()[0]
        if py.endswith("/env"):
            py = shutil.which("python3") or py
        if Path(py).exists():
            try:
                r = subprocess.run([py, "-c", "import importlib.util as u, sys; "
                                    "sys.exit(0 if u.find_spec('svtplay_dl') else 1)"],
                                   capture_output=True, timeout=20)
                if r.returncode == 0:
                    return {"method": "pip", "python": py}
            except (OSError, subprocess.SubprocessError):
                pass
    return {"method": "manual", "path": str(real)}


def find_brew() -> str:
    for c in (shutil.which("brew"), "/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if c and Path(c).exists():
            return str(c)
    return ""


def find_winget() -> str:
    c = shutil.which("winget")
    if c:
        return c
    c = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "winget.exe"
    return str(c) if os.environ.get("LOCALAPPDATA") and c.exists() else ""


WINGET_ID = "spaam.svtplay-dl"
WINGET_FLAGS = ["--id", WINGET_ID, "--exact", "--accept-source-agreements", "--disable-interactivity"]


def winget_latest(winget: str) -> str:
    """Newest svtplay-dl version in the winget catalog."""
    r = subprocess.run([winget, "show", *WINGET_FLAGS], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=90, **core._popen_flags())
    m = re.search(r"^\s*Version:\s*(\S+)", r.stdout or "", re.M)
    if not m:
        raise RuntimeError((r.stdout or r.stderr or "winget gave no answer").strip().splitlines()[-1])
    return m.group(1)


_SPINNER_RE = re.compile(r"^[\s\-\\|/]*$|[\u2580-\u259f]")  # winget's spinner and progress-bar lines


_RELEASE_RE = re.compile(r"(\d+(?:\.\d+)+)\s*\(build\s*(\d+)\)", re.I)


def app_bundle() -> Optional[Path]:
    """The installed app: the .app bundle on a Mac, the .exe on Windows. None when run from the code."""
    if not FROZEN:
        return None
    exe = Path(sys.executable).resolve()
    if core.IS_MAC:
        for parent in exe.parents:
            if parent.suffix == ".app":
                return parent
        return None
    return exe


class AppUpdater:
    """Keeps the app itself up to date from the repo's GitHub Releases ("Latest build")."""

    def __init__(self, app: "App"):
        self.app = app
        self.state = {"supported": bool(app_bundle() and APP_REPO and (core.IS_MAC or core.IS_WINDOWS)),
                      "current": f"{APP_VERSION} (build {APP_BUILD})" if APP_BUILD else APP_VERSION,
                      "latest": "", "available": False, "checking": False, "running": False,
                      "percent": None, "error": "", "checkError": "", "checkedAt": 0,
                      # the Mac app runs from outside Applications (e.g. straight from Downloads or the .dmg)
                      "notInApplications": bool(core.IS_MAC and app_bundle()
                                                and not str(app_bundle()).startswith(("/Applications/",
                                                                                      str(Path.home() / "Applications") + "/")))}
        self.asset: dict = {}

    def snapshot(self) -> dict:
        return dict(self.state)

    def check(self) -> None:
        if not self.state["supported"] or self.state["checking"] or self.state["running"]:
            return
        self.state.update(checking=True, checkError="")

        def work():
            try:
                rel = _get_json(os.environ.get("SVTDL_GUI_RELEASE_API")   # (tests point this elsewhere)
                                or f"https://api.github.com/repos/{APP_REPO}/releases/latest")
                m = _RELEASE_RE.search(rel.get("name") or "")
                want = "svtplay-dl-GUI-mac.zip" if core.IS_MAC else "svtplay-dl-GUI-windows.zip"
                asset = next((a for a in rel.get("assets", []) if a.get("name") == want), None)
                if m and asset:
                    latest = (version_tuple(m.group(1)), int(m.group(2)))
                    self.asset = asset
                    self.state.update(latest=f"{m.group(1)} (build {m.group(2)})",
                                      available=latest > (version_tuple(APP_VERSION), APP_BUILD))
            except Exception as e:  # noqa: BLE001
                self.state["checkError"] = f"Couldn't check for app updates: {e}"
            finally:
                self.state.update(checking=False, checkedAt=time.time())
        threading.Thread(target=work, daemon=True).start()

    def run(self) -> dict:
        if not self.state["supported"]:
            return {"ok": False, "message": "Updating itself only works in the installed app."}
        if self.state["running"]:
            return {"ok": True}
        if not self.state["available"] or not self.asset:
            return {"ok": False, "message": "There's no newer version of the app."}
        if self.app.busy():
            return {"ok": False, "message": "Wait for the downloads to finish (or stop them), then update."}
        target = app_bundle()
        if core.IS_MAC and "/AppTranslocation/" in str(target):
            return {"ok": False, "message": "This copy of svtplay-dl GUI is running from a temporary place, because macOS "
                                           "does that for apps opened straight from Downloads. Quit the app, drag "
                                           "svtplay-dl GUI from Downloads into Applications, open it from there, and "
                                           "then update. From then on, updates work by themselves."}
        probe = target.parent / ".svtplay-dl-gui-write-test"
        try:
            probe.write_bytes(b"")
            probe.unlink()
        except OSError:
            return {"ok": False, "message": f"svtplay-dl GUI can't write to {target.parent}, so it can't replace "
                                           "itself there. Move the app to a folder of your own (for example "
                                           "Documents) and update from there."}
        self.state.update(running=True, percent=0, error="")
        threading.Thread(target=self._install, args=(target,), daemon=True).start()
        return {"ok": True}

    def _install(self, target: Path) -> None:
        work = core.staging_root() / "app-update"
        try:
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            zpath = work / self.asset["name"]
            self._download(self.asset["browser_download_url"], zpath, int(self.asset.get("size") or 0))
            new = work / "new"
            if core.IS_MAC:   # ditto keeps the app's signature and permissions intact
                r = subprocess.run(["/usr/bin/ditto", "-x", "-k", str(zpath), str(new)], capture_output=True, text=True)
                if r.returncode != 0:
                    raise RuntimeError(r.stderr.strip() or "couldn't unzip the update")
                fresh = new / target.name
                if not fresh.is_dir():
                    fresh = next(new.glob("*.app"))
                old = work / "old.app"
                shutil.move(str(target), str(old))         # a running app can be moved aside
                try:
                    shutil.move(str(fresh), str(target))
                except Exception as e:
                    try:
                        shutil.move(str(old), str(target))     # put the old one back
                    except Exception:
                        raise RuntimeError(f"{e}. Your previous copy of the app is in {work}: "
                                           f"drag old.app back into {target.parent} and rename it "
                                           f"“{target.name}”.") from e
                    raise
                relaunch = ["/usr/bin/open", "-n", "-a", str(target), "--args", "--after-update"]
            else:
                import zipfile
                with zipfile.ZipFile(zpath) as z:
                    exe_name = next(n for n in z.namelist() if n.lower().endswith(".exe"))
                    z.extract(exe_name, new)
                fresh = new / exe_name
                # The running .exe can't be renamed or replaced: it keeps its own file open (it unpacks
                # itself from it). So a little script waits for the app to close, swaps the file and
                # starts the new one.
                script = work / "finish-update.cmd"
                pids = [os.getpid()] + ([os.getppid()] if os.getppid() > 4 else [])
                waits = "\r\n".join(
                    f'tasklist /FI "PID eq {pid}" /NH 2>NUL | find "{pid}" >NUL && (ping -n 2 127.0.0.1 >NUL & goto wait)'
                    for pid in pids)
                log = core.settings_path().parent / "update.log"
                script.write_text("\r\n".join([
                    "@echo off",
                    f'set LOG="{log}"',
                    'echo %date% %time% waiting for the old app to close > %LOG%',
                    "set tries=0",
                    ":wait",
                    waits,
                    ":move",
                    f'move /Y "{fresh}" "{target}" >> %LOG% 2>&1 && goto done',
                    "set /a tries+=1",
                    "if %tries% GEQ 30 goto failed",
                    "ping -n 2 127.0.0.1 >NUL",
                    "goto move",
                    ":failed",
                    'echo %date% %time% could not replace the app, starting the old one >> %LOG%',
                    f'start "" "{target}"',
                    "goto end",
                    ":done",
                    'echo %date% %time% replaced, starting the new one >> %LOG%',
                    f'start "" /D "{target.parent}" "{target}" --after-update',
                    'echo %date% %time% start returned %errorlevel% >> %LOG%',
                    ":end",
                    '(goto) 2>NUL & del "%~f0"',
                    ""]), encoding="mbcs" if core.IS_WINDOWS else "utf-8")
                relaunch = ["cmd.exe", "/c", str(script)]
            self.state.update(percent=100)
            try:
                flags = ({"creationflags": 0x08000000 | 0x00000200} if core.IS_WINDOWS   # no window, own group
                         else core._popen_flags(detached=True))
                subprocess.Popen(relaunch, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, env=core.clean_env(), **flags)
            except OSError as e:   # updated, but it couldn't reopen itself: the user opens it again
                print("Couldn't reopen the app after updating:", e)
            threading.Thread(target=shutdown, daemon=True).start()
        except Exception as e:  # noqa: BLE001
            self.state.update(running=False, percent=None, error=f"The app update didn't work: {e}")

    def _download(self, url: str, dest: Path, size: int) -> None:
        curl = shutil.which("curl") or ("/usr/bin/curl" if Path("/usr/bin/curl").exists() else None)
        if curl:
            p = subprocess.Popen([curl, "-sSfL", "-o", str(dest), url], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, **core._popen_flags())
            while p.poll() is None:
                if size and dest.exists():
                    self.state["percent"] = min(99, dest.stat().st_size * 100 // size)
                time.sleep(0.3)
            if p.returncode != 0:
                raise RuntimeError((p.stderr.read().decode("utf-8", "replace").strip() or "download failed"))
            return
        req = urllib.request.Request(url, headers={"User-Agent": "svtplay-dl-gui"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            got = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                got += len(chunk)
                if size:
                    self.state["percent"] = min(99, got * 100 // size)


class Updater:
    def __init__(self, app: "App"):
        self.app = app
        self.state = {"checking": False, "running": False, "method": "", "installed": "",
                      "latest": "", "available": False, "error": "", "log": [], "result": ""}

    def snapshot(self) -> dict:
        st = dict(self.state)
        st["log"] = list(st["log"][-400:])
        return st

    def check(self) -> None:
        if self.state["checking"] or self.state["running"]:
            return
        self.state.update(checking=True, error="")

        def work():
            try:
                # wait for the version lookup that check_tools() started
                for _ in range(100):
                    with self.app.lock:
                        if not self.app.tools.get("checking"):
                            break
                    time.sleep(0.2)
                with self.app.lock:
                    cmd = self.app.svtplay_cmd
                    installed = self.app.tools.get("version", "")
                info = detect_install(cmd)
                latest, err = "", ""
                try:
                    if info["method"] == "brew":
                        latest = _get_json(BREW_API)["versions"]["stable"]
                    elif info["method"] == "winget":
                        latest = winget_latest(info["winget"])
                    elif info["method"] == "pip":
                        latest = _get_json(PYPI_API)["info"]["version"]
                except Exception as e:
                    err = f"Couldn't look up the latest version: {e}"
                inst_v = version_tuple(installed)
                avail = bool(latest and inst_v and version_tuple(latest) > inst_v)
                self.state.update(method=info["method"], info=info,
                                  installed=".".join(map(str, inst_v)) if inst_v else "",
                                  latest=latest, available=avail, error=err, checkedAt=time.time())
            finally:
                self.state["checking"] = False
        threading.Thread(target=work, daemon=True).start()

    def run(self) -> dict:
        if self.state["running"]:
            return {"ok": False, "message": "An update is already running."}
        with self.app.lock:
            if any(it.status in ACTIVE for it in self.app.items):
                return {"ok": False, "message": "Wait for the downloads to finish (or stop them) before updating."}
            cmd_now = self.app.svtplay_cmd
        info = self.state.get("info") or detect_install(cmd_now)
        if info["method"] == "brew":
            cmd = [info["brew"], "upgrade", "svtplay-dl"]
        elif info["method"] == "winget":
            cmd = [info["winget"], "upgrade", *WINGET_FLAGS, "--silent", "--accept-package-agreements"]
        elif info["method"] == "pip":
            cmd = [info["python"], "-m", "pip", "install", "--upgrade", "svtplay-dl"]
        elif info["method"] == "none":
            return {"ok": False, "message": "svtplay-dl isn't installed, or wasn't found."}
        else:
            # installed by hand (e.g. an old zip): install a current copy with winget / Homebrew and use that one,
            # so later updates work from here too
            return self._switch_to_managed()
        return self._run_cmds([cmd], "update", info["method"])

    def _switch_to_managed(self) -> dict:
        if core.IS_WINDOWS:
            winget = find_winget()
            if not winget:
                return {"ok": False, "message": "This copy of svtplay-dl was installed by hand. winget wasn't found to "
                                               "install a current one: install “App Installer” from the Microsoft "
                                               "Store, then try again."}
            cmd = [winget, "install", "--id", WINGET_ID, "--exact", "--accept-source-agreements",
                   "--accept-package-agreements", "--disable-interactivity", "--silent"]

            def after():
                base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
                hits = sorted(base.glob("spaam.svtplay-dl_*/**/svtplay-dl.exe"), key=lambda f: f.stat().st_mtime)
                return str(hits[-1]) if hits else ""
            method = "winget"
        elif core.IS_MAC:
            brew = find_brew()
            if not brew:
                return {"ok": False, "message": "This copy of svtplay-dl was installed by hand. Homebrew wasn't found "
                                               "to install a current one: install it from brew.sh, then try again."}
            cmd = [brew, "install", "svtplay-dl"]

            def after():
                exe = Path(brew).parent / "svtplay-dl"
                return str(exe) if exe.exists() else ""
            method = "brew"
        else:
            return {"ok": False, "message": "This copy of svtplay-dl was installed by hand, so it can't be updated "
                                           "from here."}
        with self.app.lock:
            old = (self.app.svtplay_cmd or [""])[0]
        self.state["log_note"] = f"Replacing the hand-installed svtplay-dl ({old}) with one from {method}."
        return self._run_cmds([cmd], "switch", method, after=after)

    def install(self) -> dict:
        """Install whatever is missing (svtplay-dl and/or ffmpeg) with Homebrew or winget."""
        if self.state["running"]:
            return {"ok": False, "message": "An update is already running."}
        with self.app.lock:
            if self.app.tools.get("checking"):
                return {"ok": False, "message": "Still checking what's installed. Try again in a moment."}
            need_svt = not self.app.svtplay_cmd
            need_ff = not self.app.tools.get("ffmpeg")
        if not (need_svt or need_ff):
            return {"ok": False, "message": "svtplay-dl and ffmpeg are already installed."}
        if core.IS_WINDOWS:
            winget = find_winget()
            if not winget:
                return {"ok": False, "message": "winget wasn't found. Install “App Installer” from the Microsoft "
                                               "Store, then try again."}
            ids = ([WINGET_ID] if need_svt else []) + (["Gyan.FFmpeg"] if need_ff else [])
            cmds = [[winget, "install", "--id", i, "--exact", "--accept-source-agreements",
                     "--accept-package-agreements", "--disable-interactivity", "--silent"] for i in ids]
            method = "winget"
        elif core.IS_MAC:
            brew = find_brew()
            if not brew:
                return {"ok": False, "message": "Homebrew wasn't found. Install it from brew.sh, then try again."}
            cmds = [[brew, "install"] + (["svtplay-dl"] if need_svt else []) + (["ffmpeg"] if need_ff else [])]
            method = "brew"
        else:
            return {"ok": False, "message": "Install svtplay-dl and ffmpeg with your package manager."}
        return self._run_cmds(cmds, "install", method)

    def _run_cmds(self, cmds: list, mode: str, method: str, after=None) -> dict:
        env = core.child_env()
        env.update(HOMEBREW_NO_ENV_HINTS="1", NONINTERACTIVE="1", HOMEBREW_NO_INSTALL_CLEANUP="1",
                   PIP_DISABLE_PIP_VERSION_CHECK="1")
        note = self.state.pop("log_note", "")
        self.state.update(running=True, mode=mode, result="", error="", log=[note, ""] if note else [])

        def work():
            ok = True
            for cmd in cmds:
                self.state["log"] += ["$ " + " ".join(cmd), ""]
                try:
                    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                         errors="replace", env=env, **core._popen_flags())
                    core.keep_with_app(p)
                    for line in p.stdout:
                        line = line.rstrip()
                        if line and not _SPINNER_RE.search(line):
                            self.state["log"].append(line)
                    rc = p.wait()
                    if rc != 0:
                        ok = False
                        self.state["log"].append(f"(exited with code {rc})")
                except OSError as e:
                    ok = False
                    self.state["log"].append(f"Couldn't start it: {e}")
                self.state["log"].append("")
            before = self.state.get("installed", "")
            if after is not None:   # (winget says "already installed" with an error code; what counts is the file)
                try:
                    new_exe = after()
                except Exception as e:  # noqa: BLE001
                    new_exe = ""
                    self.state["log"].append(f"Couldn't find the new svtplay-dl: {e}")
                if new_exe:
                    ok = True
                    with self.app.lock:   # use the new copy from now on, whatever else is on PATH
                        self.app.settings.svtplay_path = new_exe
                        self.app.settings.save()
                    self.state["log"].append(f"svtplay-dl GUI now uses {new_exe}")
                else:
                    ok = False
            self.app.check_tools()
            for _ in range(150):
                time.sleep(0.2)
                with self.app.lock:
                    if not self.app.tools.get("checking"):
                        break
            with self.app.lock:
                now = ".".join(map(str, version_tuple(self.app.tools.get("version", ""))))
                have_svt, have_ff = bool(self.app.svtplay_cmd), bool(self.app.tools.get("ffmpeg"))
            if mode == "install":
                if have_svt and have_ff:
                    self.state["result"] = "svtplay-dl and ffmpeg are installed. You're ready to download."
                    ok = True
                else:
                    missing = " and ".join(n for n, h in (("svtplay-dl", have_svt), ("ffmpeg", have_ff)) if not h)
                    self.state["result"] = (f"{missing} still wasn't found. See the output above"
                                            + (" (on Windows, quitting and reopening the app can help)."
                                               if core.IS_WINDOWS else "."))
                    ok = False
            elif mode == "switch":
                source = {"brew": "Homebrew", "winget": "winget"}.get(method, method)
                self.state["result"] = (f"svtplay-dl GUI now uses svtplay-dl {now} from {source}. Updates work from "
                                        f"here from now on." if ok else
                                        f"Installing svtplay-dl with {source} didn't work. See the output above.")
            elif ok and now and now != before:
                self.state["result"] = f"Updated svtplay-dl from {before} to {now}."
            elif ok:
                source = {"brew": "Homebrew", "winget": "winget", "pip": "pip"}.get(method, method)
                self.state["result"] = f"svtplay-dl {now} is the newest version available from {source}."
            else:
                self.state["result"] = "The update didn't finish. See the output above."
            self.state.update(running=False, ok=ok)
            self.state["checking"] = False
            self.check()
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}


_SEASON_RE = re.compile(r"(?:season|sasong|säsong|sesong|saeson|sæson|kausi)[\s/_.-]*(\d{1,4})(?!\d)", re.I)
# "Avsnitt 3", "avsnitt-3", "Episode 3", "Del 3 av 8" (how SVT and others number episodes)
_EPISODE_RE = re.compile(r"(?:episode|avsnitt|episod|jakso|afsnit|folge)[\s/_.-]*(\d{1,4})(?!\d)|\bdel\s+(\d{1,4})\s+av\s+\d",
                         re.I)
# an episode name that starts "3. " ("Bron – 3. Name" in a page title)
_NUMBERED_NAME_RE = re.compile(r"(?:^|\s[–—-]\s)(\d{1,3})\.\s+\S")


def is_show_page(url: str) -> bool:
    """An SVT Play link to a show rather than to one video. svtplay-dl only downloads single
    videos from /video, /klipp and /kanaler links ("This mode is not supported anymore"),
    but it can list a show page's episodes, so such a link goes to the episode picker."""
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = u.netloc.lower().split(":")[0]
    if host not in ("www.svtplay.se", "svtplay.se"):
        return False
    path = u.path.rstrip("/")
    return bool(path) and not path.startswith(("/video", "/klipp", "/kanaler"))


def order_episodes(episodes: list) -> None:
    """Put the episodes in season/episode order and number them #1, #2 … in that order.
    Only when every episode's number is known; otherwise keep svtplay-dl's order."""
    if episodes and all(e.get("episode") is not None for e in episodes):
        pos = {id(e): i for i, e in enumerate(episodes)}
        episodes.sort(key=lambda e: (e.get("season") or 0, e["episode"], pos[id(e)]))
    for n, e in enumerate(episodes, 1):
        e["n"] = n


def clean_site_title(t: str) -> str:
    return re.sub(r"\s*[|–-]\s*(SVT Play|TV4 Play|TV4|NRK TV|DRTV|DR TV|UR Play|Viafree|Dplay|discovery\+|Pluto TV)\s*$",
                  "", t, flags=re.I).strip()


def season_episode(url: str, title: str = "", description: str = "") -> tuple:
    """Best guess at (season, episode) from an episode's link, title or description; None where unknown.
    Follows the places svtplay-dl itself looks: "Avsnitt 3" or "3. Name" in the title,
    "Del 3 av 8" in the description, "säsong 2" in the link."""
    text = unquote(urlparse(url).path) + " " + (title or "") + " " + (description or "")
    m = re.search(r"\bS(\d{1,4})\s*E(\d{1,4})\b", text, re.I)
    if m:
        return int(m.group(1)), int(m.group(2))
    s = _SEASON_RE.search(text)
    e = _EPISODE_RE.search(text)
    ep = int(e.group(1) or e.group(2)) if e else None
    if ep is None and title:
        n = _NUMBERED_NAME_RE.search(title)
        ep = int(n.group(1)) if n else None
    if ep is None:
        # SVT turns an episode called "6. Therese Alshammar" into the link …/agent-ish/6-therese-alshammar
        last = unquote(urlparse(url).path).rstrip("/").rsplit("/", 1)[-1]
        n = re.match(r"(\d{1,3})-[^\d]", last)
        ep = int(n.group(1)) if n else None
    return (int(s.group(1)) if s else None), ep


def pretty_name(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"-[A-Za-z0-9]{6,}-\w+$", "", stem)
    stem = re.sub(r"\.s(\d+)e(\d+)\.?", r" S\1E\2 ", stem)
    stem = re.sub(r"[._-]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem[:1].upper() + stem[1:] if stem else filename


# ==========================================================================
# One download
# ==========================================================================

class Item:
    _next = 1

    def __init__(self, app: "App", url: str):
        self.id = Item._next
        Item._next += 1
        self.app = app
        self.url = url
        self.site = core.site_from_url(url)
        self.title = core.title_from_url(url)
        self.page_title = ""
        self.thumb = ""
        self.status = "new"
        self.note = ""
        self.note_kind = ""          # "", "error", "warn", "ok"
        self.percent: Optional[float] = None
        self.speed = ""
        self.eta = ""
        self.rows: list[dict] = []
        self.quality = "default"     # "default", a preset name, or "row:<n>"
        self.episodes: list[dict] = []   # series card only: episodes found, waiting to be chosen
        self.runner: Optional[core.Runner] = None
        self.log: list[str] = []
        self.stage_dir: Optional[Path] = None
        self.output_name = ""
        self.output_dir = ""
        self.had_error = False
        self.skipped = False
        self.last_error = ""

    # ----- view model -----------------------------------------------------
    def to_json(self) -> dict:
        s = self.app.settings
        options = [{"value": "default", "label": f"Default ({s.quality})"}]
        options += [{"value": k, "label": k} for k in core.QUALITY_PRESETS]
        options += [{"value": f"row:{i}", "label": core.quality_label(r), "exact": True}
                    for i, r in enumerate(self.rows)]
        return {
            "id": self.id, "url": self.url, "site": self.site,
            "title": self.page_title or self.title, "thumb": self.thumb,
            "status": self.status, "statusText": STATUS_TEXT.get(self.status, self.status),
            "note": self.note, "noteKind": self.note_kind,
            "percent": self.percent, "speed": self.speed, "eta": self.eta,
            "quality": self.quality, "qualityOptions": options,
            "file": self.output_name,
            "episodes": self.episodes if self.status == "picking" else None,
        }

    def quality_args(self) -> Optional[list[str]]:
        q = self.quality
        if q == "default":
            return None
        if q.startswith("row:"):
            try:
                return core.quality_row_args(self.rows[int(q[4:])])
            except (ValueError, IndexError):
                return None
        return core.QUALITY_PRESETS.get(q)

    def set(self, status: str, note: str = "", kind: str = "") -> None:
        self.status = status
        self.note = note
        self.note_kind = kind or {"error": "error", "done": "ok", "skipped": "warn"}.get(status, "")
        if status in ("new", "waiting", "starting", "fetching", "expanding", "stopped", "error"):
            self.percent = None if status != "starting" else 0
            self.speed = self.eta = ""
        if status == "done":
            self.percent = 100
            self.speed = self.eta = ""
        if status == "merging":
            self.speed = self.eta = ""

    # ----- running ----------------------------------------------------------
    def start(self, verbose: bool = False) -> None:
        s = self.app.settings
        cmd = self.app.svtplay_cmd
        if not cmd:
            self.set("error", "svtplay-dl was not found. Set its location in Settings.")
            return
        if getattr(self.app, "scheduled", False) and not Path(s.output_dir).expanduser().is_dir():
            # a network folder that isn't connected right now: don't make an empty local folder instead
            self.set("error", f"The download folder isn't available: {s.output_dir}")
            return
        try:
            Path(s.output_dir).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.set("error", f"Can't create the download folder: {e}")
            return
        self.output_dir = str(Path(s.output_dir).expanduser())
        self.output_name = ""
        # A network folder: svtplay-dl and ffmpeg delete and rename files at the end, which shares
        # often refuse while a media server or another computer has the file open. So download on
        # this computer first and copy the finished file over afterwards.
        self.drop_stage()
        work_dir = self.output_dir
        # Network folders download here first and are copied over at the end. Scheduled runs always do:
        # if the computer restarts mid-download, the half file stays in the temporary folder (cleared
        # next run) instead of the real one, where svtplay-dl would later take it for a finished episode.
        if core.is_network_path(self.output_dir) or getattr(self.app, "scheduled", False):
            self.stage_dir = getattr(self.app, "stage_root", None) or core.staging_root()
            self.stage_dir = self.stage_dir / f"item-{self.id}-{int(time.time())}"
            try:
                self.stage_dir.mkdir(parents=True, exist_ok=True)
                work_dir = str(self.stage_dir)
            except OSError:
                self.stage_dir = None
        self.had_error = self.skipped = False
        self.last_error = ""
        settings = core.Settings(**{**s.__dict__, "output_dir": work_dir})
        full = cmd + core.build_args(settings, self.quality_args()) + (["--verbose"] if verbose else []) + [self.url]
        earlier = self.log if verbose else []
        self.verbose_run = verbose
        self.log = earlier + (["", "--- svtplay-dl crashed, so it was run again with --verbose for details ---"]
                              if verbose else []) + ["$ " + " ".join(full), ""]
        self.set("starting", "svtplay-dl crashed. Trying once more to get details…" if verbose else "")
        self.runner = core.Runner(full, self.on_event, self.on_exit)
        self.runner.start()

    def on_event(self, ev: core.Event) -> None:
        with self.app.lock:
            if ev.kind == "progress":
                if self.status != "downloading":
                    self.set("downloading")
                self.percent = (ev.pos * 100.0 / ev.total) if ev.total else None
                self.speed = ev.speed
                self.eta = ev.eta if ev.eta not in ("(unknown)", "complete") else ""
                return
            prefix = {"error": "ERROR: ", "warning": "WARNING: ", "outfile": "Outfile: ", "url": "Url: "}.get(ev.kind, "")
            self.log.append(prefix + ev.text)
            if ev.kind == "outfile":
                self.output_name = ev.text
                if not self.page_title:
                    self.title = pretty_name(ev.text)
            elif ev.kind == "merging":
                m = re.search(r" into (.+)$", ev.text)
                if m:
                    self.output_name = m.group(1).strip()
                self.set("merging")
            elif ev.kind == "error":
                self.had_error = True
                self.last_error = ev.text
            elif ev.kind == "warning":
                if "already exists" in ev.text:
                    self.skipped = True
                elif "ffmpeg" in ev.text.lower():
                    self.last_error = ev.text

    def on_exit(self, rc: int, cancelled: bool) -> None:
        with self.app.lock:
            if self.runner and self.runner.raw_log:
                last = self.runner.raw_log[-1]
                if not self.log or self.log[-1] != last:
                    self.log.append(last)
            self.runner = None
            staged = getattr(self, "stage_dir", None)
            ok = not cancelled and not self.skipped and rc == 0 and not self.had_error
            if ok and staged:
                self.set("moving")
                self.percent = 0
                threading.Thread(target=self._move_to_share, daemon=True).start()
                return
            if not ok:
                self.drop_stage()
            if cancelled:
                self.set("stopped")
            elif self.skipped:
                self.set("skipped", "Turn on “Overwrite” in Settings to download it again")
            elif ((rc != 0 or self.had_error) and not getattr(self, "verbose_run", False)
                  and any("svtplay-dl crashed" in l for l in self.log)):
                # svtplay-dl hides the reason unless it runs with --verbose, so run it once more that way
                self.start(verbose=True)
                return
            elif rc != 0 or self.had_error:
                msg = self.last_error
                if "svtplay-dl crashed" in msg or "Include the URL used" in msg or "report it at" in msg:
                    msg = ""
                if not msg:
                    # svtplay-dl crashed: show the Python exception line from its traceback
                    crash = [l for l in self.log if re.match(r"^\w+(Error|Exception)\b.*:", l)]
                    if crash:
                        msg = "svtplay-dl crashed: " + crash[-1] + " (click the log icon for details)"
                        if "--resolution" in self.log[0] and "IndexError" in crash[-1]:
                            msg += " (this stream has no resolution info, so pick Quality: Best)"
                self.set("error", msg or f"svtplay-dl exited with code {rc}")
            else:
                self.set("done", self.output_name and Path(self.output_name).name, "ok")
        self.app.pump()

    def drop_stage(self) -> None:
        staged = getattr(self, "stage_dir", None)
        if staged:
            shutil.rmtree(staged, ignore_errors=True)
        self.stage_dir = None

    def _move_to_share(self) -> None:
        staged, dest = self.stage_dir, Path(self.output_dir)
        with self.app.lock:
            self.log.append(f"Copying the finished files to {dest} …")
        t0 = [time.time(), 0]

        def progress(done: int, total: int) -> None:
            with self.app.lock:
                self.percent = done * 100.0 / total
                dt = time.time() - t0[0]
                if dt >= 1:
                    self.speed = f"{(done - t0[1]) / dt / 1e6:.1f} MB/s"
                    t0[0], t0[1] = time.time(), done
        res = core.move_finished(staged, dest, self.app.settings.overwrite, progress)
        with self.app.lock:
            for r in res["moved"]:
                self.log.append(f"Copied: {r}")
            for r in res["skipped"]:
                self.log.append(f"Already there, kept the existing file: {r}")
            if res["error"]:
                self.log.append("ERROR: " + res["error"])
                self.set("error", res["error"] + f" The download is kept in {staged}.")
            else:
                self.stage_dir = None
                main = sorted(res["moved"] or res["skipped"],
                              key=lambda r: (dest / r).stat().st_size if (dest / r).exists() else 0)
                if main:
                    self.output_name = main[-1]
                if res["moved"]:
                    self.set("done", Path(self.output_name).name if self.output_name else "", "ok")
                else:
                    self.set("skipped", "Turn on “Overwrite” in Settings to download it again")
        self.app.pump()

    def stop(self) -> None:
        if self.runner:
            self.runner.cancel()

    def output_path(self) -> Path:
        base = Path(self.output_dir or self.app.settings.output_dir).expanduser()
        if self.output_name:
            p = base / self.output_name
            if p.exists():
                return p
            for ext in (".mp4", ".mkv", ".srt", ".m4a"):
                if p.with_suffix(ext).exists():
                    return p.with_suffix(ext)
        return base


# ==========================================================================
# Application state
# ==========================================================================

class App:
    def __init__(self):
        self.lock = threading.RLock()
        self.settings = core.Settings.load()
        self.items: list[Item] = []
        self.waiting: list[Item] = []
        self.svtplay_cmd: Optional[list[str]] = None
        self.tools = {"svtplay": "", "version": "", "ffmpeg": "", "checking": True}
        self.fetch_slots = threading.Semaphore(3)
        self.meta_slots = threading.Semaphore(4)
        self.notice = ""
        self.net: dict = {"checking": True}
        self._net_lock = threading.Lock()
        self.last_seen = time.time()   # last time the window asked for state
        self.quit_ask = False          # the window was closed while downloads were running
        self.quitting = False
        self.updater = Updater(self)
        self.app_updater = AppUpdater(self)
        self.app_updater.check()
        # leftovers from downloads for network folders that never finished (app closed or crashed)
        threading.Thread(target=shutil.rmtree, args=(core.staging_root(),), kwargs={"ignore_errors": True}, daemon=True).start()
        self.check_tools()
        self.check_network()
        self.updater.check()
        threading.Thread(target=self._net_timer, daemon=True).start()
        threading.Thread(target=follow.repair, daemon=True).start()   # the app may have been moved

    # ----- where do downloads appear to come from? ------------------------
    def check_network(self) -> None:
        if not self._net_lock.acquire(blocking=False):
            return  # a check is already running
        with self.lock:
            self.net = {**self.net, "checking": True}
            proxy = self.settings.proxy

        def work():
            try:
                res = check_public_location(proxy)
                res["checkedAt"] = time.time()
                with self.lock:
                    self.net = {**res, "checking": False}
            finally:
                self._net_lock.release()
        threading.Thread(target=work, daemon=True).start()

    def _net_timer(self) -> None:
        while True:  # a VPN can be switched on or off at any time, so look again now and then,
            time.sleep(300)  # but only while the window is being looked at
            if time.time() - self.last_seen < 120:
                self.check_network()

    def window_seen(self) -> None:
        """Called on every poll from the page. After a long quiet spell, refresh what may be stale."""
        now = time.time()
        if now - self.last_seen > 600:
            self.check_network()
            if now - self.updater.state.get("checkedAt", 0) > 6 * 3600:
                self.updater.check()
            if now - self.app_updater.state.get("checkedAt", 0) > 6 * 3600:
                self.app_updater.check()
        self.last_seen = now

    # ----- tools ------------------------------------------------------------
    def check_tools(self) -> None:
        with self.lock:
            self.svtplay_cmd = core.find_svtplay(self.settings.svtplay_path)
            self.tools = {"svtplay": " ".join(self.svtplay_cmd or []), "version": "",
                          "ffmpeg": "", "checking": True}
        cmd = self.svtplay_cmd

        def work():
            ver = core.svtplay_version(cmd) if cmd else ""
            ff = core.find_ffmpeg(cmd) or ""
            with self.lock:
                self.tools.update(version=ver, ffmpeg=ff, checking=False)
        threading.Thread(target=work, daemon=True).start()

    # ----- state for the page ---------------------------------------------
    def state(self) -> dict:
        with self.lock:
            s = self.settings
            return {
                "items": [it.to_json() for it in self.items],
                "settings": {k: v for k, v in s.__dict__.items() if k != "window_geometry"},
                "presets": {"quality": list(core.QUALITY_PRESETS), "subtitles": list(core.SUBTITLE_PRESETS)},
                "tools": dict(self.tools),
                "net": dict(self.net),
                "update": self.updater.snapshot(),
                "active": sum(1 for it in self.items if it.status in ACTIVE),
                "platform": "mac" if core.IS_MAC else ("windows" if core.IS_WINDOWS else "linux"),
                "quitAsk": self.quit_ask,
                "appWindow": WINDOW is not None,
                "appVersion": self.app_updater.state["current"],
                "appUpdate": self.app_updater.snapshot(),
                "follow": self._follow_cache(),
            }

    def _follow_cache(self) -> dict:
        # the page asks for state twice a second: read the file again only when it has changed
        try:
            stamp = (follow.follows_path().stat().st_mtime, follow.run_lock_path().exists())
        except OSError:
            stamp = (0, follow.run_lock_path().exists())
        cached = getattr(self, "_fcache", None)
        if not cached or cached[0] != stamp or time.time() - cached[2] > 5:
            self._fcache = (stamp, self.follow_state(), time.time())
        return self._fcache[1]

    def busy(self) -> bool:
        """Is anything running that closing the window shouldn't interrupt?"""
        with self.lock:
            return (any(it.status in ACTIVE for it in self.items) or bool(self.waiting)
                    or bool(self.updater.state.get("running")))

    def updating_app(self) -> bool:
        return bool(self.app_updater.state.get("running"))

    def find(self, item_id: int) -> Optional[Item]:
        for it in self.items:
            if it.id == item_id:
                return it
        return None

    # ----- adding -----------------------------------------------------------
    URL_RE = re.compile(r"https?://\S+|\b[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?::\d+)?/\S*", re.I)

    def add(self, text: str, series: bool, last: int) -> dict:
        urls = []
        for u in self.URL_RE.findall(text or ""):
            u = u.rstrip(").,;\"'>")
            if not u.lower().startswith("http"):
                u = "https://" + u
            urls.append(u)
        if not urls:
            return {"ok": False, "message": "That doesn't look like a link."}
        with self.lock:
            self.settings.expand_series = bool(series)
            self.settings.series_last = max(0, int(last or 0))
            self.settings.save()
            existing = {it.url for it in self.items}
            added = 0
            for u in urls:
                if u in existing:
                    continue
                existing.add(u)
                added += 1
                if series or is_show_page(u):
                    self._expand(u)
                else:
                    self._add_item(u)
        if not added:
            return {"ok": False, "message": "That link is already in the list."}
        return {"ok": True, "added": added}

    def _add_item(self, url: str, fetch: bool = True) -> Item:
        it = Item(self, url)
        self.items.append(it)
        self._fetch_meta(it)
        if fetch and self.settings.fetch_qualities_on_add and self.svtplay_cmd:
            self._fetch_qualities(it)
        return it

    def _fetch_meta(self, it: Item) -> None:
        def work():
            with self.meta_slots:
                meta = fetch_page_meta(it.url)
            with self.lock:
                if meta.get("title"):
                    it.page_title = clean_site_title(meta["title"])
                if meta.get("image"):
                    it.thumb = meta["image"]
        threading.Thread(target=work, daemon=True).start()

    def _fetch_qualities(self, it: Item) -> None:
        it.set("fetching")
        cmd = self.svtplay_cmd + ["--list-quality"] + core.split_extra(self.settings.extra_args) + [it.url]

        def work():
            with self.fetch_slots:
                rc, events, log = core.run_collect(cmd, timeout=120)
            rows = core.parse_quality_table([e.text for e in events if e.kind == "info"])
            errs = [e.text for e in events if e.kind == "error"]
            crash = ""
            if not rows and any("svtplay-dl crashed" in l for l in log):
                # svtplay-dl hides the reason unless it runs with --verbose, so ask once more that way
                with self.fetch_slots:
                    _rc, _ev, vlog = core.run_collect(cmd[:-1] + ["--verbose", cmd[-1]], timeout=120)
                log = log + ["", "--- svtplay-dl crashed, so it was run again with --verbose for details ---",
                             "$ " + " ".join(cmd[:-1] + ["--verbose", cmd[-1]]), ""] + vlog
                lines = [l for l in vlog if re.match(r"^\w+(Error|Exception)\b.*:", l)]
                crash = ("svtplay-dl crashed while checking this video: " + lines[-1]
                         + " (click the log icon for details)") if lines else ""
            with self.lock:
                it.rows = rows
                if it.status == "fetching":
                    it.log = ["(quality check)"] + log
                    if crash:
                        it.set("new", crash, "error")
                    elif errs and not rows:
                        it.set("new", errs[-1], "error")
                    else:
                        it.set("new", (f"{len(rows)} streams available" if len(rows) != 1 else "1 stream available") if rows else "")
        threading.Thread(target=work, daemon=True).start()

    def _expand(self, url: str) -> None:
        holder = self._add_item(url, fetch=False)
        holder.show_url = url
        if not self.svtplay_cmd:
            return
        holder.set("expanding")
        cmd = self.svtplay_cmd + ["-A", "--get-url", "--get-only-episode-url"]
        cmd += core.split_extra(self.settings.extra_args) + [url]

        def work():
            rc, events, log = core.run_collect(cmd, timeout=300)
            found = []
            for e in events:
                if e.kind == "url" and e.text not in found:
                    found.append(e.text)
            errs = [e.text for e in events if e.kind == "error"]
            with self.lock:
                if holder not in self.items:
                    return
                if len(found) <= 1:
                    holder.log = ["(episode search)"] + log
                    if found:
                        holder.url = found[0]
                    holder.set("new", errs[-1] if errs and not found else "Only one episode found",
                               "error" if errs and not found else "")
                    if self.settings.fetch_qualities_on_add:
                        self._fetch_qualities(holder)
                    return
                existing = {x.url for x in self.items if x is not holder}
                holder.episodes = []
                for n, u in enumerate(found, 1):
                    se, ep = season_episode(u)
                    holder.episodes.append({"n": n, "url": u, "title": "", "thumb": "", "season": se,
                                            "episode": ep, "inList": u in existing})
                holder.log = ["(episode search)"] + log
                order_episodes(holder.episodes)
                holder.set("picking", f"{len(found)} episodes found")
                self._fetch_episode_meta(holder)
        threading.Thread(target=work, daemon=True).start()

    def _fetch_episode_meta(self, holder: Item) -> None:
        """Fill in episode titles and thumbnails for the picker, a few at a time."""
        left = [len(holder.episodes[:400])]

        def one(ep: dict) -> None:
            meta = {}
            with self.meta_slots:
                if holder in self.items and holder.status == "picking":
                    meta = fetch_page_meta(ep["url"])
            with self.lock:
                if meta.get("title"):
                    ep["title"] = clean_site_title(meta["title"])
                if meta.get("title") or meta.get("description"):
                    if ep["season"] is None or ep["episode"] is None:
                        se, epn = season_episode(ep["url"], ep.get("title", ""), meta.get("description", ""))
                        ep["season"] = ep["season"] if ep["season"] is not None else se
                        ep["episode"] = ep["episode"] if ep["episode"] is not None else epn
                if meta.get("image"):
                    ep["thumb"] = meta["image"]
                    if not holder.thumb:
                        holder.thumb = meta["image"]
                left[0] -= 1
                if left[0] == 0 and holder.status == "picking":
                    order_episodes(holder.episodes)
        for ep in holder.episodes[:400]:
            threading.Thread(target=one, args=(ep,), daemon=True).start()

    def choose_episodes(self, holder_id: int, urls: list, follow_show: bool = False) -> dict:
        with self.lock:
            holder = self.find(holder_id)
            if not holder or holder.status != "picking":
                return {"ok": False, "message": "That series is no longer waiting for a choice."}
            wanted = [ep for ep in holder.episodes if ep["url"] in set(urls)]
            if not wanted and not follow_show:
                return {"ok": False, "message": "No episodes were selected."}
            if follow_show:
                # Every episode there is now counts as seen: from here on only new ones are downloaded.
                try:
                    follow.follow(getattr(holder, "show_url", "") or holder.url, holder.page_title or holder.title,
                                  holder.thumb, [ep["url"] for ep in holder.episodes], self.settings.output_dir)
                except OSError as e:
                    return {"ok": False, "message": f"Couldn't save the followed show: {e}"}
            idx = self.items.index(holder)
            self.items.remove(holder)
            existing = {x.url for x in self.items}
            new_items = []
            for ep in wanted:
                if ep["url"] in existing:
                    continue
                it = Item(self, ep["url"])
                if ep.get("title"):
                    it.page_title = ep["title"]
                if ep.get("thumb"):
                    it.thumb = ep["thumb"]
                else:
                    self._fetch_meta(it)
                if self.settings.fetch_qualities_on_add and len(wanted) <= 30:
                    self._fetch_qualities(it)
                new_items.append(it)
            self.items[idx:idx] = new_items
            return {"ok": True, "added": len(new_items), "followed": bool(follow_show)}

    # ----- followed shows and the schedule ----------------------------------------------
    def follow_state(self) -> dict:
        d = follow.load()
        run = follow.running()
        sched = d.get("schedule") or {}
        return {
            "shows": [{"url": x.get("url"), "title": x.get("title") or x.get("url"), "thumb": x.get("thumb", ""),
                       "seen": len(x.get("seen") or []), "lastChecked": x.get("lastChecked", 0),
                       "lastNew": x.get("lastNew", 0), "lastError": x.get("lastError", ""),
                       "folder": x.get("folder") or self.settings.output_dir, "catchUp": bool(x.get("catchUp"))}
                      for x in d["shows"]],
            "schedule": sched,
            "runs": (d.get("runs") or [])[-8:][::-1],
            "running": run,
            "supported": follow.supported(),
            "problem": follow.location_problem(),
        }

    def follow_run_now(self) -> dict:
        if follow.running():
            return {"ok": False, "message": "A check is already running."}
        if not follow.load()["shows"]:
            return {"ok": False, "message": "You aren't following any shows yet."}
        cmd = follow.program_args() + ["--manual"]
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=core.clean_env(), close_fds=True,
                             **({"creationflags": 0x08000000 | 0x00000200} if core.IS_WINDOWS else {"start_new_session": True}))
        except OSError as e:
            return {"ok": False, "message": f"Couldn't start the check: {e}"}
        return {"ok": True}

    # ----- queue -------------------------------------------------------------
    def enqueue(self, it: Item) -> None:
        if it.status in ACTIVE or it in self.waiting:
            return
        it.set("waiting")
        self.waiting.append(it)

    def pump(self) -> None:
        with self.lock:
            active = sum(1 for it in self.items if it.status in ACTIVE)
            while self.waiting and active < max(1, int(self.settings.max_parallel)):
                it = self.waiting.pop(0)
                if it.status != "waiting" or it not in self.items:
                    continue
                it.start()
                if it.status in ACTIVE:
                    active += 1

    # ----- actions from the page -------------------------------------------
    def action(self, name: str, data: dict) -> dict:
        # Native dialogs can stay open a long time, so don't hold the lock while they do.
        if name == "choose_folder":
            folder = choose_folder(self.settings.output_dir)
            if folder:
                with self.lock:
                    self.settings.output_dir = folder
                    self.settings.save()
            return {"ok": True, "folder": folder}
        if name == "choose_program":
            return {"ok": True, "path": choose_file()}
        if name == "update_check":
            self.updater.check()
            return {"ok": True}
        if name == "update_run":
            return self.updater.run()
        if name == "app_update":
            return self.app_updater.run()
        if name == "app_update_check":
            self.app_updater.check()
            return {"ok": True}
        if name == "tools_install":
            return self.updater.install()
        if name == "follow_schedule":
            return follow.set_schedule(bool(data.get("enabled")), str(data.get("time", "07:00")),
                                       list(data.get("days") or []))
        if name == "follow_folder":   # (a native dialog: not under the lock)
            url = str(data.get("url", ""))
            show = follow.find_show(follow.load(), url)
            if show is None:
                return {"ok": False, "message": "That show isn't followed any more."}
            folder = choose_folder(show.get("folder") or self.settings.output_dir)
            if folder:
                follow.set_folder(url, folder)
            return {"ok": True, "folder": folder}
        if name == "follow_remove":
            follow.unfollow(str(data.get("url", "")))
            return {"ok": True}
        if name == "follow_run":
            return self.follow_run_now()
        if name == "follow_latest":
            if follow.running():
                return {"ok": False, "message": "A check is running. Try again when it's finished."}
            url = str(data.get("url", ""))
            on = not (follow.find_show(follow.load(), url) or {}).get("catchUp")   # the button toggles
            follow.update(lambda d: (follow.find_show(d, url) or {}).__setitem__("catchUp", 1 if on else 0))
            return {"ok": True, "on": on}
        if name == "follow_log":
            try:
                lines = follow.log_path().read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
            except OSError:
                lines = []
            return {"ok": True, "log": lines or ["No scheduled check has run yet."]}
        if name == "quit_cancel":
            self.quit_ask = False
            return {"ok": True}
        with self.lock:
            it = self.find(int(data.get("id", 0))) if "id" in data else None
            if name == "download" and it:
                if it.status in ("done", "skipped") or it.status in STARTABLE:
                    self.enqueue(it)
            elif name == "stop" and it:
                if it.status == "waiting":
                    if it in self.waiting:
                        self.waiting.remove(it)
                    it.set("stopped")
                else:
                    it.stop()
            elif name == "remove" and it:
                it.stop()
                if it in self.waiting:
                    self.waiting.remove(it)
                self.items.remove(it)
            elif name == "quality" and it:
                it.quality = str(data.get("value", "default"))
            elif name == "reveal" and it:
                p = it.output_path()
                open_in_finder(p, reveal=p.is_file())
            elif name == "log" and it:
                lines = list(it.log)
                if it.runner:
                    lines += ["", "(still running…)"]
                return {"ok": True, "log": lines or ["Nothing has run yet for this item."]}
            elif name == "download_all":
                for x in self.items:
                    if x.status in STARTABLE:
                        self.enqueue(x)
            elif name == "stop_all":
                for x in list(self.waiting):
                    x.set("stopped")
                self.waiting.clear()
                for x in self.items:
                    x.stop()
            elif name == "clear_finished":
                self.items = [x for x in self.items if x.status not in ("done", "skipped")]
            elif name == "settings":
                return self._save_settings(data.get("settings", {}))
            elif name == "choose_episodes":
                result = self.choose_episodes(int(data.get("id", 0)), list(data.get("urls") or []),
                                              bool(data.get("follow")))
                self.pump()
                return result
            elif name == "check_network":
                self.check_network()
            elif name == "open_folder":
                open_in_finder(Path(self.settings.output_dir).expanduser())
            else:
                return {"ok": False, "message": f"Unknown action {name}"}
        self.pump()
        return {"ok": True}

    def _save_settings(self, data: dict) -> dict:
        s = self.settings
        path = str(data.get("svtplay_path", s.svtplay_path)).strip()
        if path and not core.find_svtplay(path):
            return {"ok": False, "message": f"Can't find a program at {path}"}
        old_proxy = s.proxy
        types = {k: type(v) for k, v in core.Settings().__dict__.items()}
        for k, v in data.items():
            if k not in types or k == "window_geometry":
                continue
            try:
                if types[k] is bool:
                    v = bool(v)
                elif types[k] is int:
                    v = int(v)
                else:
                    v = str(v).strip()
            except (TypeError, ValueError):
                continue
            setattr(s, k, v)
        s.max_parallel = max(1, min(6, s.max_parallel))
        if s.quality not in core.QUALITY_PRESETS:
            s.quality = "Best"
        if s.subtitles not in core.SUBTITLE_PRESETS:
            s.subtitles = "None"
        if s.output_format not in ("mp4", "mkv"):
            s.output_format = "mp4"
        s.save()
        if "svtplay_path" in data:
            self.check_tools()
            self.updater.check()
        if s.proxy != old_proxy:
            self.check_network()
        return {"ok": True}


# ==========================================================================
# Native dialogs and Finder
# ==========================================================================

def _osascript(script: str) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _powershell(script: str) -> str:
    ps = shutil.which("powershell") or "powershell"
    full = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Add-Type -AssemblyName System.Windows.Forms; " \
           "$owner = New-Object System.Windows.Forms.Form -Property @{TopMost=$true; ShowInTaskbar=$false}; " + script
    try:
        r = subprocess.run([ps, "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", full],
                           capture_output=True, timeout=900, **core._popen_flags())
        return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _window_dialog(kind: str, start: str) -> Optional[str]:
    """The app window's own dialog. None means "not available, use the fallback"."""
    if WINDOW is None:
        return None
    try:
        import webview
        dlg = getattr(webview, "FileDialog", None)
        typ = (dlg.FOLDER if kind == "folder" else dlg.OPEN) if dlg else (20 if kind == "folder" else 10)
        res = WINDOW.create_file_dialog(typ, directory=str(Path(start).expanduser()))
    except Exception as e:  # noqa: BLE001 - fall back to the old way
        print("App dialog failed, using the system one:", e)
        return None
    if not res:
        return ""
    return str(res[0] if isinstance(res, (list, tuple)) else res).rstrip("/\\") or ""


def choose_folder(start: str) -> str:
    got = _window_dialog("folder", start)
    if got is not None:
        return got
    if core.IS_WINDOWS:
        start = str(Path(start).expanduser())
        return _powershell(
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description = 'Save downloads to'; $d.ShowNewFolderButton = $true; "
            f"$d.SelectedPath = {_ps_quote(start)}; "
            "if ($d.ShowDialog($owner) -eq 'OK') { [Console]::Out.Write($d.SelectedPath) }"
        ) or ""
    if core.IS_MAC:
        start = str(Path(start).expanduser()).replace('"', '\\"')
        return _osascript(
            'tell application "System Events" to activate\n'
            f'POSIX path of (choose folder with prompt "Save downloads to:" default location (POSIX file "{start}"))'
        ).rstrip("/") or ""
    return _tk_dialog("folder", start)


def choose_file() -> str:
    got = _window_dialog("file", str(Path.home()))
    if got is not None:
        return got
    if core.IS_WINDOWS:
        return _powershell(
            "$d = New-Object System.Windows.Forms.OpenFileDialog; $d.Title = 'Locate svtplay-dl'; "
            "$d.Filter = 'svtplay-dl (svtplay-dl.exe)|svtplay-dl.exe|Programs (*.exe)|*.exe|All files (*.*)|*.*'; "
            "if ($d.ShowDialog($owner) -eq 'OK') { [Console]::Out.Write($d.FileName) }"
        ) or ""
    if core.IS_MAC:
        return _osascript('tell application "System Events" to activate\n'
                          'POSIX path of (choose file with prompt "Locate svtplay-dl:")')
    return _tk_dialog("file", str(Path.home()))


def _tk_dialog(kind: str, start: str) -> str:
    """Windows/Linux fallback: a Tk dialog in a separate process (if Tk is available)."""
    code = ("import tkinter as tk,sys;from tkinter import filedialog as f;r=tk.Tk();r.withdraw();"
            "r.attributes('-topmost',True);"
            + ("print(f.askdirectory(initialdir=sys.argv[1]) or '')" if kind == "folder"
               else "print(f.askopenfilename(initialdir=sys.argv[1]) or '')"))
    try:
        r = subprocess.run([sys.executable, "-c", code, start], capture_output=True, text=True, timeout=600)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def open_in_finder(path: Path, reveal: bool = False) -> None:
    try:
        if core.IS_MAC:
            subprocess.Popen(["open", "-R", str(path)] if reveal else ["open", str(path)])
        elif core.IS_WINDOWS:
            if reveal:
                subprocess.Popen(f'explorer /select,"{path}"')
            else:
                os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
    except OSError:
        pass


# ==========================================================================
# HTTP server
# ==========================================================================

APP: Optional[App] = None
SERVER: Optional[ThreadingHTTPServer] = None

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png", ".ico": "image/x-icon", ".webmanifest": "application/manifest+json",
                 ".json": "application/json"}


class Handler(BaseHTTPRequestHandler):
    server_version = "svtplay-dl-gui"

    def log_message(self, fmt, *args):  # keep the Terminal quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _authorized(self) -> bool:
        """Only this app's own page may use the API.
        - Host must be this computer, which blocks DNS-rebinding tricks from websites.
        - The page sends a custom header. A web page on another site can't add one without the
          browser first asking this server for permission (CORS), and this server never gives it."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            return False
        return self.headers.get("X-SVTDL") == "1"

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/ping":   # lets a second start find this running copy
            return self._json({"app": "svtplay-dl-gui", "pid": os.getpid()})
        if path == "/api/focus":  # a second start asks this copy to show its window
            if WINDOW is not None:
                for name in ("restore", "show"):
                    try:
                        getattr(WINDOW, name)()
                    except Exception:  # noqa: BLE001
                        pass
            return self._json({"ok": True})
        if path == "/api/state":
            if not self._authorized():
                return self._json({"error": "forbidden"}, 403)
            APP.window_seen()
            return self._json(APP.state())
        if path in ("/", "/index.html"):
            path = "/index.html"
        f = (WEB_DIR / path.lstrip("/")).resolve()
        if WEB_DIR in f.parents and f.is_file():
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(f.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            if f.name == "sw.js":
                self.send_header("Service-Worker-Allowed", "/")
            self.end_headers()
            self.wfile.write(body)
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authorized():
            return self._json({"error": "forbidden"}, 403)
        path = u.path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            data = {}
        if path == "/api/add":
            return self._json(APP.add(str(data.get("text", "")), bool(data.get("series")),
                                      int(data.get("last") or 0)))
        if path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=shutdown, daemon=True).start()
            return
        if path.startswith("/api/"):
            return self._json(APP.action(path[5:], data))
        self._send(404, b"not found", "text/plain")


def stop_all_downloads() -> bool:
    if not APP:
        return False
    with APP.lock:
        running = [it for it in APP.items if it.runner]
        APP.waiting.clear()
        for it in running:
            it.stop()
    return bool(running)


def shutdown() -> None:
    time.sleep(0.3)
    if APP:
        APP.quitting = True
    stop_all_downloads()
    if WINDOW is not None:
        try:
            WINDOW.destroy()   # webview.start() then returns and main() finishes up
            return
        except Exception:  # noqa: BLE001
            pass
    if SERVER:
        SERVER.shutdown()


def open_window(url: str) -> None:
    """Open the page in an app-style window (no tabs or address bar) when Chrome/Edge/Brave
    is installed, otherwise in the default browser."""
    if core.IS_MAC:
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            if Path(f"/Applications/{app}.app").exists() or (Path.home() / f"Applications/{app}.app").exists():
                try:
                    subprocess.Popen(["open", "-na", app, "--args", f"--app={url}",
                                      "--window-size=1100,760"])
                    return
                except OSError:
                    break
    if core.IS_WINDOWS:
        roots = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe",
                    r"BraveSoftware\Brave-Browser\Application\brave.exe"):
            for root in roots:
                exe = Path(root) / rel if root else None
                if exe and exe.is_file():
                    try:
                        subprocess.Popen([str(exe), f"--app={url}", "--window-size=1100,760"])
                        return
                    except OSError:
                        pass
    webbrowser.open(url)


def instance_file() -> Path:
    return core.settings_path().parent / "instance.json"


def running_instance() -> Optional[str]:
    """URL of a copy of the app that is already running, if any."""
    try:
        info = json.loads(instance_file().read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{int(info['port'])}/"
        with urllib.request.urlopen(url + "api/ping", timeout=2) as r:
            if json.loads(r.read()).get("app") == "svtplay-dl-gui":
                return url
    except Exception:
        pass
    return None


def on_closing() -> bool:
    """The window's close button. Returning False keeps the window open."""
    if APP is None or APP.quitting or not APP.busy():
        return True
    APP.quit_ask = True   # the page asks "stop the downloads and quit?"
    return False


def log_to_file() -> None:
    """The packaged app has no console, so keep its messages in a file for troubleshooting."""
    log = core.settings_path().parent / "app.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(log, "w", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        pass


# ==========================================================================
# Scheduled run: started by launchd / Task Scheduler with --run-scheduled. No window.
# ==========================================================================

class ScheduledApp:
    """Just enough of App for Item to download one episode at a time without a window."""
    scheduled = True

    def __init__(self, settings: core.Settings, cmd: list[str]):
        self.lock = threading.RLock()
        self.settings = settings
        self.svtplay_cmd = cmd
        self.items: list = []
        # its own temporary folder, which the window doesn't clear when it opens
        self.stage_root = core.staging_root().parent / "scheduled"

    def pump(self) -> None:
        pass


def find_episodes(cmd: list[str], settings: core.Settings, url: str) -> tuple[list[str], str, list[str]]:
    """The show's episode links as svtplay-dl lists them, oldest first when the numbers are known."""
    full = cmd + ["-A", "--get-url", "--get-only-episode-url"] + core.split_extra(settings.extra_args) + [url]
    rc, events, log = core.run_collect(full, timeout=300)
    found: list[str] = []
    for e in events:
        if e.kind == "url" and e.text not in found:
            found.append(e.text)
    errs = [e.text for e in events if e.kind == "error"]
    if not found:
        crash = [l for l in log if re.match(r"^\w+(Error|Exception)\b.*:", l)]
        return [], (errs[-1] if errs else (crash[-1] if crash else f"svtplay-dl exited with code {rc}")), log
    eps = [{"url": u, "season": None, "episode": None} for u in found]
    for ep in eps:
        ep["season"], ep["episode"] = season_episode(ep["url"])
    order_episodes(eps)
    if not all(e["episode"] is not None for e in eps):
        eps.reverse()   # svtplay-dl lists the newest first
    return [e["url"] for e in eps], "", log


def run_scheduled(trigger: str) -> int:
    """Download new episodes of every followed show, then quit."""
    log_file = follow.log_path()
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if log_file.exists() and log_file.stat().st_size > 2_000_000:   # keep it small
            log_file.replace(log_file.with_suffix(".old.log"))
        out = open(log_file, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        out = None

    def say(text: str) -> None:
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + text
        if out:
            print(line, file=out)

    if not follow.take_run_lock(trigger):
        say("Another check is already running, so this one stops.")
        return 0
    started = time.time()
    run = {"start": started, "end": 0, "trigger": trigger, "downloaded": [], "errors": [], "checked": 0}
    say(f"--- Checking for new episodes ({trigger}), svtplay-dl GUI {APP_VERSION} ---")
    try:
        settings = core.Settings.load()
        cmd = core.find_svtplay(settings.svtplay_path)
        if not cmd:
            raise RuntimeError("svtplay-dl was not found. Open the app and check Settings.")
        app = ScheduledApp(settings, cmd)
        shutil.rmtree(app.stage_root, ignore_errors=True)   # left over from a run that was cut off
        for show in follow.load()["shows"]:
            url, title = show["url"], show.get("title") or show["url"]
            folder = show.get("folder") or settings.output_dir
            app.settings = dataclasses.replace(settings, output_dir=folder)   # this show's own folder
            follow.set_run_status(f"Checking {title}")
            say(f"{title}: looking for episodes ({url})")
            found, err, log = find_episodes(cmd, settings, url)
            run["checked"] += 1
            seen = set(show.get("seen") or [])
            latest = found[-int(show.get("catchUp") or 0):] if show.get("catchUp") else []   # "Get latest now"
            new = [u for u in found if u not in seen or u in latest]

            def note(fn, url=url):
                def change(d):
                    s = follow.find_show(d, url)
                    if s is not None:   # (it may have been unfollowed in the window meanwhile)
                        fn(s)
                follow.update(change)

            if err:
                say(f"{title}: couldn't list the episodes: {err}")
                for l in log[-15:]:
                    say("    " + l)
                run["errors"].append(f"{title}: {err}")
                note(lambda s: s.update(lastChecked=time.time(), lastError=err))
                continue
            say(f"{title}: {len(found)} episodes, {len(new)} new")
            if new and not Path(folder).expanduser().is_dir():
                # e.g. a network folder that isn't connected right now: try again at the next check
                err = f"The download folder isn't available: {folder}"
                say(f"{title}: {err}")
                run["errors"].append(f"{title}: {err}")
                note(lambda s: s.update(lastChecked=time.time(), lastError=err))
                continue
            note(lambda s: s.update(lastChecked=time.time(), lastError="", catchUp=0))
            for ep_url in new[:follow.MAX_PER_SHOW]:
                follow.set_run_status(f"Downloading {title}")
                it = Item(app, ep_url)
                meta = fetch_page_meta(ep_url)
                if meta.get("title"):
                    it.page_title = clean_site_title(meta["title"])
                say(f"{title}: downloading {it.page_title or ep_url}")
                it.start()
                while it.status in ACTIVE or it.status == "starting" or it.runner is not None:
                    time.sleep(0.5)
                for l in it.log:
                    say("    " + l)
                name = Path(it.output_name).name if it.output_name else (it.page_title or ep_url)
                if it.status == "done":
                    say(f"{title}: done: {name}")
                    run["downloaded"].append({"show": title, "file": name})
                    note(lambda s, u=ep_url: s.update(seen=sorted(set(s.get("seen", [])) | {u}), lastNew=time.time()))
                elif it.status == "skipped":
                    say(f"{title}: already there, skipped: {name}")
                    note(lambda s, u=ep_url: s.update(seen=sorted(set(s.get("seen", [])) | {u})))
                else:
                    msg = it.note or f"didn't finish ({it.status})"
                    if "folder isn't available" in msg:   # not this episode's fault: try it next time
                        say(f"{title}: {msg}")
                        run["errors"].append(f"{title}: {msg}")
                        break
                    say(f"{title}: failed: {msg}")
                    run["errors"].append(f"{title}: {msg}")

                    def failed(s, u=ep_url):
                        f = s.setdefault("failures", {})
                        f[u] = f.get(u, 0) + 1
                        if f[u] >= follow.GIVE_UP_AFTER:   # stop trying this one
                            s["seen"] = sorted(set(s.get("seen", [])) | {u})
                            f.pop(u, None)
                    note(failed)
    except Exception as e:  # noqa: BLE001
        say(f"Stopped: {e}")
        run["errors"].append(str(e))
    finally:
        run["end"] = time.time()
        follow.update(lambda d: d.__setitem__("runs", (d.get("runs") or [])[-19:] + [run]))
        follow.drop_run_lock()
        n = len(run["downloaded"])
        say(f"--- Finished: {n} downloaded, {len(run['errors'])} problems ---")
        if n:
            shows = sorted({x["show"] for x in run["downloaded"]})
            follow.notify("svtplay-dl GUI", f"Downloaded {n} new episode{'s' if n != 1 else ''}: {', '.join(shows)}")
        elif run["errors"]:
            follow.notify("svtplay-dl GUI", "Checking for new episodes had a problem. Open the app for details.")
        if out:
            out.close()
    return 0



def main() -> None:
    global APP, SERVER, WINDOW
    import argparse
    ap = argparse.ArgumentParser(description="svtplay-dl GUI")
    ap.add_argument("--port", type=int, default=0, help="port for the page (default: any free one)")
    ap.add_argument("--browser", action="store_true", help="use a browser window instead of the app window")
    ap.add_argument("--no-browser", action="store_true", help="don't open any window (for testing)")
    ap.add_argument("--after-update", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--run-scheduled", action="store_true", help="download new episodes of followed shows, then quit")
    ap.add_argument("--manual", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--set-schedule", metavar="HH:MM|off", help=argparse.SUPPRESS)   # used by the build's tests
    if (getattr(sys, "frozen", False) or (sys.stdout is None)) and not ({"--run-scheduled", "--set-schedule"} & set(sys.argv)):
        log_to_file()   # (a scheduled run keeps its own log, and mustn't wipe the open window's)
        print("Started", APP_VERSION, sys.argv[1:])
    args, _unknown = ap.parse_known_args()   # macOS may add its own arguments when starting an app
    if args.run_scheduled:
        sys.exit(run_scheduled("manual" if args.manual else "schedule"))
    if args.set_schedule:
        off = args.set_schedule.lower() == "off"
        res = follow.set_schedule(not off, "07:00" if off else args.set_schedule, [])
        follow.data_dir().mkdir(parents=True, exist_ok=True)
        (follow.data_dir() / "set-schedule-result.json").write_text(json.dumps(res), encoding="utf-8")
        sys.exit(0 if res.get("ok") else 1)

    webview = None
    if not (args.browser or args.no_browser):
        try:
            import webview  # pywebview: the native app window
        except ImportError:
            print("pywebview isn't installed, so the GUI opens in your browser instead.")
    if args.after_update:   # started by the old copy just before it quit: give it time to finish
        for _ in range(60):
            if not running_instance():
                break
            time.sleep(0.25)
    if FROZEN and core.IS_WINDOWS:   # the copy replaced by the last app update
        try:
            Path(sys.executable).with_name(Path(sys.executable).stem + ".old.exe").unlink()
        except OSError:
            pass

    other = running_instance()
    if other:   # already open: bring that window forward instead of starting a second copy
        try:
            req = urllib.request.Request(other + "api/focus")
            urllib.request.urlopen(req, timeout=2).read()
        except Exception:  # noqa: BLE001
            pass
        if webview is None and not args.no_browser:
            open_window(other)
        print(f"svtplay-dl GUI is already running at {other}")
        return

    APP = App()
    SERVER = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    SERVER.daemon_threads = True
    port = SERVER.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    try:
        instance_file().parent.mkdir(parents=True, exist_ok=True)
        instance_file().write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")
    except OSError:
        pass
    print(f"svtplay-dl GUI is running at {url}")

    def on_signal(signum, frame):
        raise KeyboardInterrupt
    for name in ("SIGHUP", "SIGTERM"):   # SIGHUP = the Terminal window was closed
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), on_signal)
            except (ValueError, OSError):
                pass
    try:
        if webview is not None:
            threading.Thread(target=SERVER.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True).start()
            WINDOW = webview.create_window("svtplay-dl GUI", url, width=1120, height=800, min_size=(720, 520),
                                           background_color="#f3f4f6", text_select=True)
            WINDOW.events.closing += on_closing
            webview.start()   # returns when the window has closed
        else:
            if not args.no_browser:
                print("Close this window (or press Ctrl+C, or use Quit in the app) to stop it.")
                threading.Timer(0.4, open_window, args=(url,)).start()
            SERVER.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        if APP:
            APP.quitting = True
        if stop_all_downloads():
            time.sleep(0.5)
        try:
            SERVER.shutdown() if webview is not None else None
        except Exception:  # noqa: BLE001
            pass
        try:
            if json.loads(instance_file().read_text(encoding="utf-8")).get("pid") == os.getpid():
                instance_file().unlink()
        except (OSError, ValueError):
            pass
        print("Stopped.")


if __name__ == "__main__":
    main()
