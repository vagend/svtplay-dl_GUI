"""
Core logic for svtplay-dl GUI (no GUI code in here, so it can be tested on its own).

- finding the svtplay-dl and ffmpeg programs
- turning GUI settings into svtplay-dl command-line arguments
- running svtplay-dl in the background and parsing its progress output
"""

from __future__ import annotations

import codecs
import json
import os
import platform
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse, unquote

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

# Apps launched from Finder/Dock on macOS don't get the shell's PATH,
# so look in the usual Homebrew / MacPorts locations too.
if IS_WINDOWS:
    _local = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    EXTRA_BIN_DIRS = [
        str(Path(_local) / "Microsoft" / "WinGet" / "Links"),   # winget "portable" apps (svtplay-dl, ffmpeg)
        str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ffmpeg" / "bin"),
        r"C:\ffmpeg\bin",
    ]
else:
    EXTRA_BIN_DIRS = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/opt/local/bin",
        str(Path.home() / ".local" / "bin"),
    ]


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def settings_path() -> Path:
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "svtplay-dl-gui" / "settings.json"


def default_download_dir() -> str:
    d = Path.home() / "Downloads"
    return str(d if d.exists() else Path.home())


@dataclass
class Settings:
    svtplay_path: str = ""            # empty = auto-detect
    output_dir: str = field(default_factory=default_download_dir)
    quality: str = "Best"             # key of QUALITY_PRESETS
    subtitles: str = "None"           # key of SUBTITLE_PRESETS
    output_format: str = "mp4"        # mp4 / mkv
    max_parallel: int = 2
    subfolder: bool = False           # --subfolder
    overwrite: bool = False           # --force
    thumbnail: bool = False           # -t
    nfo: bool = False                 # --nfo
    filename_template: str = ""       # --filename, empty = svtplay-dl default
    proxy: str = ""                   # --proxy
    extra_args: str = ""              # free text, split like a shell would
    fetch_qualities_on_add: bool = True
    expand_series: bool = False
    series_last: int = 0              # 0 = all episodes, else --all-last N
    window_geometry: str = ""

    @classmethod
    def load(cls) -> "Settings":
        p = settings_path()
        s = cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except (OSError, ValueError):
            pass
        return s

    def save(self) -> None:
        p = settings_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except OSError:
            pass


# Quality presets shown in the dropdowns. --resolution takes the video
# height and accepts <=, >=, <, > prefixes (see svtplay_dl/utils/stream.py).
QUALITY_PRESETS: dict[str, list[str]] = {
    "Best": [],
    "1080p or lower": ["--resolution", "<=1080"],
    "720p or lower": ["--resolution", "<=720"],
    "540p or lower": ["--resolution", "<=540"],
    "480p or lower": ["--resolution", "<=480"],
    "360p or lower": ["--resolution", "<=360"],
    "Audio only": ["--only-audio"],
}

SUBTITLE_PRESETS: dict[str, list[str]] = {
    "None": [],
    "Download (.srt)": ["-S"],
    "Merge into video": ["-S", "-M"],
    "All languages, merged": ["-S", "-M", "--all-subtitles"],
    "Subtitles only": ["-S", "--force-subtitle"],
}


# --------------------------------------------------------------------------
# Finding programs
# --------------------------------------------------------------------------

def _which(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    exe = name + ".exe" if IS_WINDOWS and not name.lower().endswith(".exe") else name
    for d in EXTRA_BIN_DIRS:
        cand = Path(d) / exe
        if cand.exists() and (IS_WINDOWS or os.access(cand, os.X_OK)):
            return str(cand)
    return None


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_svtplay(user_path: str = "") -> Optional[list[str]]:
    """Return the command prefix used to start svtplay-dl, or None."""
    if user_path:
        p = Path(user_path).expanduser()
        if p.suffix == ".py":   # inside the packaged app, sys.executable is the app itself
            py = sys.executable if not getattr(sys, "frozen", False) else (_which("python3") or _which("python"))
            return [py, str(p)] if py else None
        if p.is_file():
            return [str(p)]
        return None
    exe = "svtplay-dl.exe" if IS_WINDOWS else "svtplay-dl"
    # next to this app, or the svtplay-dl folder from the svtplay-dl.se zip unpacked here
    for local in (app_dir() / exe, app_dir() / "svtplay-dl" / exe):
        if local.is_file():
            return [str(local)]
    found = _which("svtplay-dl")
    if found:
        return [found]
    try:
        import importlib.util
        if importlib.util.find_spec("svtplay_dl") is not None:
            return [sys.executable, "-m", "svtplay_dl"]
    except (ImportError, ValueError):
        pass
    return None


def find_ffmpeg(svtplay_cmd: Optional[list[str]] = None) -> Optional[str]:
    found = _which("ffmpeg")
    if found:
        return found
    name = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
    dirs = [app_dir()]
    if svtplay_cmd:
        # svtplay-dl also accepts ffmpeg sitting next to its own executable
        dirs.insert(0, Path(svtplay_cmd[0]).parent)
    for d in dirs:
        if (d / name).is_file():
            return str(d / name)
    return None


def svtplay_version(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd + ["--version"], capture_output=True, text=True,
                             timeout=20, env=child_env(), **_popen_flags())
        return (out.stdout or out.stderr).strip().splitlines()[-1]
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # make sure ffmpeg from Homebrew etc. is found when launched from Finder
    path = env.get("PATH", "")
    parts = path.split(os.pathsep) if path else []
    for d in EXTRA_BIN_DIRS + [str(app_dir()), str(app_dir() / "svtplay-dl")]:
        if d not in parts and Path(d).is_dir():
            parts.append(d)
    env["PATH"] = os.pathsep.join(parts)
    return env


# On Windows, closing the app's console window must also stop svtplay-dl and ffmpeg.
# Child processes are put in a Job Object that Windows kills when this process exits.
_job_handle = None


def _windows_job():
    global _job_handle
    if _job_handle is not None:
        return _job_handle
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    job = k32.CreateJobObjectW(None, None)
    info = EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # 9 = ExtendedLimitInformation
    _job_handle = job
    return job


def keep_with_app(proc: subprocess.Popen) -> None:
    """Make sure a child process ends when this app ends (Windows; elsewhere main() handles it)."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject(_windows_job(), int(proc._handle))  # type: ignore[attr-defined]
    except Exception:
        pass


def _popen_flags() -> dict:
    if IS_WINDOWS:
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {"start_new_session": True}        # own process group, so we can kill ffmpeg too


# --------------------------------------------------------------------------
# Building the command line
# --------------------------------------------------------------------------

def split_extra(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    try:
        return shlex.split(text, posix=not IS_WINDOWS)
    except ValueError:
        return text.split()


def build_args(settings: Settings, quality_args: Optional[list[str]] = None) -> list[str]:
    """Options shared by every download (URL not included)."""
    a: list[str] = ["-o", settings.output_dir]
    a += QUALITY_PRESETS.get(settings.quality, []) if quality_args is None else quality_args
    a += SUBTITLE_PRESETS.get(settings.subtitles, [])
    if settings.output_format == "mkv":
        a += ["--output-format", "mkv"]
    if settings.subfolder:
        a.append("--subfolder")
    if settings.overwrite:
        a.append("--force")
    if settings.thumbnail:
        a.append("--thumbnail")
    if settings.nfo:
        a.append("--nfo")
    if settings.filename_template.strip():
        a += ["--filename", settings.filename_template.strip()]
    if settings.proxy.strip():
        a += ["--proxy", settings.proxy.strip()]
    a += split_extra(settings.extra_args)
    return a


# --------------------------------------------------------------------------
# Parsing svtplay-dl output
# --------------------------------------------------------------------------

# "[07/20][=======.............] ETA: 0:00:12 | 19.1 MB/s"
PROGRESS_RE = re.compile(r"^\[(\d+)/(\d+)\]\[[=.]*\]\s*(.*)$")
ETA_RE = re.compile(r"ETA:\s*([0-9:]+|\(unknown\)|complete)")
SPEED_RE = re.compile(r"([\d.]+\s*[KMG]?B/s)")
DOWNLOADED_RE = re.compile(r"^Downloaded (\d+)kB")
LEVEL_RE = re.compile(r"^(INFO|WARNING|ERROR|DEBUG|CRITICAL):\s?(.*)$")


@dataclass
class Event:
    kind: str                 # progress | outfile | merging | info | warning | error | url | quality | episode
    text: str = ""
    pos: int = 0
    total: int = 0
    eta: str = ""
    speed: str = ""
    data: object = None


def parse_line(line: str) -> Optional[Event]:
    line = line.rstrip()
    if not line:
        return None
    m = PROGRESS_RE.match(line)
    if m:
        msg = m.group(3)
        eta = ETA_RE.search(msg)
        spd = SPEED_RE.search(msg)
        return Event("progress", line, int(m.group(1)), int(m.group(2)),
                     eta.group(1) if eta else "", spd.group(1) if spd else "")
    m = DOWNLOADED_RE.match(line)
    if m:
        return Event("progress", line, 0, 0, "", f"{int(m.group(1)) // 1024} MB")
    m = LEVEL_RE.match(line)
    if not m:
        if line.startswith(("http://", "https://")):
            return Event("url", line)
        return Event("info", line)
    level, msg = m.groups()
    if level in ("ERROR", "CRITICAL"):
        return Event("error", msg)
    if level == "WARNING":
        return Event("warning", msg)
    if msg.startswith("Outfile: "):
        return Event("outfile", msg[len("Outfile: "):])
    if msg.startswith("Merge ") or msg.startswith("Remux") or msg.startswith("Merging"):
        return Event("merging", msg)
    if msg.startswith("Url: "):
        return Event("url", msg[len("Url: "):].strip())
    m2 = re.match(r"Episode (\d+) of (\d+)", msg)
    if m2:
        return Event("episode", msg, int(m2.group(1)), int(m2.group(2)))
    return Event("info", msg)


def parse_quality_table(lines: list[str]) -> list[dict]:
    """Parse --list-quality output (lines already stripped of 'INFO: ')."""
    # Columns are left-justified, so slice each row at the header's column starts.
    names = ["bitrate", "method", "codec", "resolution", "language", "role"]
    titles = ["Quality:", "Method:", "Codec:", "Resolution:", "Language:", "Role:"]
    rows: list[dict] = []
    starts: Optional[list[int]] = None
    for line in lines:
        if line.startswith("Quality:"):
            starts = [line.find(t) for t in titles]
            if any(s < 0 for s in starts):
                starts = None
            continue
        if starts is None or not line[:1].isdigit():
            continue
        row = {}
        for i, n in enumerate(names):
            end = starts[i + 1] if i + 1 < len(starts) else None
            row[n] = line[starts[i]:end].strip()
        if row["resolution"] in ("None", "0x0"):
            row["resolution"] = ""
        if row["language"] == "None":
            row["language"] = ""
        rows.append(row)
    return rows


def quality_label(row: dict) -> str:
    bits = [f"{row['bitrate']} kbps", row["method"], row["codec"]]
    if row.get("resolution"):
        bits.insert(0, row["resolution"].split("x")[-1] + "p")
    if row.get("language"):
        bits.append(row["language"])
    return " · ".join(b for b in bits if b)


def quality_row_args(row: dict) -> list[str]:
    a = ["-q", row["bitrate"], "-P", row["method"]]
    if row.get("codec"):
        a += ["--format-preferred", row["codec"]]
    if row.get("language"):
        a += ["--audio-language", row["language"]]
    if row.get("role") and row["role"] != "main":
        a += ["--video-role", row["role"]]
    return a


def title_from_url(url: str) -> str:
    """A readable guess at a title from the URL, until svtplay-dl tells us the file name."""
    try:
        u = urlparse(url if "://" in url else "http://" + url)
    except ValueError:
        return url
    parts = [unquote(p) for p in u.path.split("/") if p]
    words = [p for p in parts if not re.fullmatch(r"[\w-]{0,3}|video|program|play|tv|serie|[a-z]?\d[\w]*", p)]
    if words:
        t = " – ".join(w.replace("-", " ").replace("_", " ") for w in words[-2:])
        return t[:1].upper() + t[1:]
    return u.netloc or url


def site_from_url(url: str) -> str:
    try:
        host = urlparse(url if "://" in url else "http://" + url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# --------------------------------------------------------------------------
# Running svtplay-dl
# --------------------------------------------------------------------------

def _decode(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252" if IS_WINDOWS else "latin-1", errors="replace")


class Runner:
    """Runs one svtplay-dl process and delivers parsed Events to a callback
    (called from a background thread)."""

    def __init__(self, cmd: list[str], on_event: Callable[[Event], None],
                 on_exit: Callable[[int, bool], None], cwd: Optional[str] = None):
        self.cmd = cmd
        self.on_event = on_event
        self.on_exit = on_exit
        self.cwd = cwd
        self.proc: Optional[subprocess.Popen] = None
        self.cancelled = False
        self.raw_log: list[str] = []
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _emit_line(self, raw: bytes) -> None:
        text = _decode(raw)
        if not text.strip():
            return
        ev = parse_line(text)
        if ev is None:
            return
        if ev.kind != "progress":
            self.raw_log.append(text.rstrip())
        else:
            self._last_progress = text.rstrip()
        self.on_event(ev)

    def _run(self) -> None:
        self._last_progress = ""
        try:
            self.proc = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, cwd=self.cwd, env=child_env(), **_popen_flags())
            keep_with_app(self.proc)
        except OSError as e:
            self.raw_log.append(f"Could not start svtplay-dl: {e}")
            self.on_event(Event("error", f"Could not start svtplay-dl: {e}"))
            self.on_exit(-1, False)
            return
        buf = b""
        fd = self.proc.stdout
        while True:
            chunk = fd.read1(4096) if hasattr(fd, "read1") else fd.read(1)
            if not chunk:
                break
            buf += chunk
            # progress lines end in \r, log lines in \n (or \r\n on Windows)
            while True:
                idx = min((i for i in (buf.find(b"\r"), buf.find(b"\n")) if i >= 0), default=-1)
                if idx < 0:
                    break
                line, buf = buf[:idx], buf[idx + 1:]
                self._emit_line(line)
        if buf:
            self._emit_line(buf)
        rc = self.proc.wait()
        if self._last_progress:
            self.raw_log.append(self._last_progress)
        self.on_exit(rc, self.cancelled)

    def cancel(self) -> None:
        self.cancelled = True
        p = self.proc
        if not p or p.poll() is not None:
            return
        try:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                               capture_output=True, **_popen_flags())
            else:
                os.killpg(p.pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            try:
                p.kill()
            except OSError:
                pass


def run_collect(cmd: list[str], timeout: float = 180) -> tuple[int, list[Event], list[str]]:
    """Run to completion (blocking) and return exit code, events and log lines."""
    events: list[Event] = []
    done = threading.Event()
    result = {"rc": -1}

    def on_exit(rc, _c):
        result["rc"] = rc
        done.set()

    r = Runner(cmd, events.append, on_exit)
    r.start()
    if not done.wait(timeout):
        r.cancel()
        done.wait(10)
    return result["rc"], events, r.raw_log
