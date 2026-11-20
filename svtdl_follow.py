"""
Followed shows and the scheduled check for new episodes.

- follows.json (next to settings.json) holds the followed shows, the episodes already seen,
  the schedule and the last few runs.
- The schedule is the computer's own: a launchd job on a Mac, a Task Scheduler task on Windows.
  At the chosen time it starts this app with --run-scheduled. That run has no window: it looks for
  new episodes, downloads them, writes down what it did and quits. Nothing stays running.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Callable, Optional
from xml.sax.saxutils import escape

import svtdl_core as core

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MAC_LABEL = "se.vagend.svtplay-dl-gui.new-episodes"
WIN_TASK = "svtplay-dl GUI new episodes"
MAX_PER_SHOW = 10        # new episodes downloaded per show in one run (the rest come next time)
GIVE_UP_AFTER = 5        # an episode that fails this many runs in a row is skipped from then on


# --------------------------------------------------------------------------
# The file
# --------------------------------------------------------------------------

def data_dir() -> Path:
    return core.settings_path().parent


def follows_path() -> Path:
    return data_dir() / "follows.json"


def log_path() -> Path:
    return data_dir() / "scheduled.log"


def _empty() -> dict:
    return {"shows": [], "schedule": {"enabled": False, "time": "07:00", "days": []}, "runs": []}


def load() -> dict:
    d = _empty()
    try:
        raw = json.loads(follows_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            d.update({k: raw[k] for k in d if k in raw})
    except (OSError, ValueError):
        pass
    return d


class _FileLock:
    """A lock between this app's window and a scheduled run, which are separate programs."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(100):
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 20:   # left behind by a crash
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                time.sleep(0.05)
        return self   # waited 5 s: go ahead anyway rather than hang

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


def update(fn: Callable[[dict], None]) -> dict:
    """Read the file, let fn change it, write it back (atomically, under a lock)."""
    with _FileLock(data_dir() / "follows.lock"):
        d = load()
        fn(d)
        p = follows_path()
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        return d


def find_show(d: dict, url: str) -> Optional[dict]:
    for s in d["shows"]:
        if s.get("url") == url:
            return s
    return None


def follow(url: str, title: str, thumb: str, episode_urls: list) -> None:
    def fn(d):
        s = find_show(d, url)
        if s is None:
            s = {"url": url, "title": title, "thumb": thumb, "seen": [], "added": time.time(),
                 "lastChecked": 0, "lastNew": 0, "failures": {}}
            d["shows"].append(s)
        s["title"] = title or s.get("title") or url
        s["thumb"] = thumb or s.get("thumb", "")
        s["seen"] = sorted(set(s.get("seen", [])) | set(episode_urls))
    update(fn)


def unfollow(url: str) -> None:
    update(lambda d: d.__setitem__("shows", [s for s in d["shows"] if s.get("url") != url]))


# --------------------------------------------------------------------------
# One run at a time
# --------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if core.IS_WINDOWS:
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259                   # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def run_lock_path() -> Path:
    return data_dir() / "scheduled-run.json"


def running() -> Optional[dict]:
    """The run in progress, if any: {"pid", "start", "trigger", "now"}."""
    try:
        info = json.loads(run_lock_path().read_text(encoding="utf-8"))
        if _pid_alive(int(info.get("pid", 0))) and time.time() - float(info.get("start", 0)) < 12 * 3600:
            return info
    except (OSError, ValueError, TypeError):
        pass
    return None


def take_run_lock(trigger: str) -> bool:
    with _FileLock(data_dir() / "follows.lock"):
        if running():
            return False
        run_lock_path().write_text(json.dumps({"pid": os.getpid(), "start": time.time(),
                                               "trigger": trigger, "now": ""}), encoding="utf-8")
        return True


def set_run_status(text: str) -> None:
    try:
        info = json.loads(run_lock_path().read_text(encoding="utf-8"))
        if info.get("pid") == os.getpid():
            info["now"] = text
            run_lock_path().write_text(json.dumps(info), encoding="utf-8")
    except (OSError, ValueError):
        pass


def drop_run_lock() -> None:
    try:
        if json.loads(run_lock_path().read_text(encoding="utf-8")).get("pid") == os.getpid():
            run_lock_path().unlink()
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------
# The computer's scheduler
# --------------------------------------------------------------------------

def program_args() -> list[str]:
    """How the scheduler starts this app for a run."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), "--run-scheduled"]
    main = Path(__file__).resolve().with_name("svtplay_dl_gui.py")
    return [str(Path(sys.executable).resolve()), str(main), "--run-scheduled"]


def location_problem() -> str:
    """A reason the app can't be scheduled from where it is, or ''."""
    exe = program_args()[0]
    if core.IS_MAC and "/AppTranslocation/" in exe:
        return ("macOS is running this copy from a temporary place. Move svtplay-dl GUI to your "
                "Applications folder, open it from there, and set the schedule again.")
    if core.IS_MAC and ("/Volumes/" in exe and "/Applications/" not in exe):
        return "Open svtplay-dl GUI from your Applications folder (not from the disk image) first."
    return ""


def _parse_time(t: str) -> tuple[int, int]:
    try:
        h, m = (int(x) for x in str(t).split(":")[:2])
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except ValueError:
        pass
    return 7, 0


def _run(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=core.clean_env(), **core._popen_flags())
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


# ----- Mac: a launchd job in ~/Library/LaunchAgents --------------------------------------

def mac_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"


def mac_plist(sched: dict) -> bytes:
    h, m = _parse_time(sched.get("time", "07:00"))
    days = [d for d in sched.get("days") or [] if d in DAYS]
    if days and len(days) < 7:   # launchd: 0 = Sunday … 6 = Saturday
        when = [{"Weekday": (DAYS.index(d) + 1) % 7, "Hour": h, "Minute": m} for d in days]
    else:
        when = {"Hour": h, "Minute": m}
    return plistlib.dumps({
        "Label": MAC_LABEL,
        "ProgramArguments": program_args(),
        "StartCalendarInterval": when,     # missed while asleep: runs when the Mac wakes up
        "RunAtLoad": False,
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    })


def _mac_domain() -> str:
    return f"gui/{os.getuid()}"


def _mac_install(sched: dict) -> str:
    p = mac_plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _run(["launchctl", "bootout", f"{_mac_domain()}/{MAC_LABEL}"])
    p.write_bytes(mac_plist(sched))
    rc, out = _run(["launchctl", "bootstrap", _mac_domain(), str(p)])
    if rc != 0:
        # an older macOS, or a job with the same name still being unloaded: try the old way once
        rc2, out2 = _run(["launchctl", "load", "-w", str(p)])
        if rc2 != 0:
            return f"macOS didn't accept the schedule: {out or out2}"
    return ""


def _mac_remove() -> str:
    _run(["launchctl", "bootout", f"{_mac_domain()}/{MAC_LABEL}"])
    try:
        mac_plist_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        return f"Couldn't remove {mac_plist_path()}: {e}"
    return ""


def _mac_installed() -> Optional[list]:
    """The program the installed job starts, or None when there is no job."""
    try:
        d = plistlib.loads(mac_plist_path().read_bytes())
        return list(d.get("ProgramArguments") or [])
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


# ----- Windows: a Task Scheduler task for the current user --------------------------------

def win_task_xml(sched: dict) -> str:
    h, m = _parse_time(sched.get("time", "07:00"))
    days = [d for d in sched.get("days") or [] if d in DAYS]
    start = f"{date.today().isoformat()}T{h:02d}:{m:02d}:00"   # no time zone = this computer's local time
    names = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
             "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
    if days and len(days) < 7:
        when = ("<ScheduleByWeek><DaysOfWeek>" + "".join(f"<{names[d]} />" for d in days)
                + "</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>")
    else:
        when = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    user = os.environ.get("USERNAME", "")
    dom = os.environ.get("USERDOMAIN", "")
    user_id = f"{dom}\\{user}" if dom and user else user
    exe, *args = program_args()
    arg_text = " ".join(f'"{a}"' if " " in a else a for a in args)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>svtplay-dl GUI</Author>
    <Description>Downloads new episodes of the shows followed in svtplay-dl GUI, then quits. Turn it off in the app (Automatic downloads).</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      {when}
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user_id)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT8H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(exe)}</Command>
      <Arguments>{escape(arg_text)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _win_install(sched: dict) -> str:
    fd, name = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(name).write_text(win_task_xml(sched), encoding="utf-16")   # with the BOM schtasks expects
        rc, out = _run(["schtasks", "/Create", "/TN", WIN_TASK, "/XML", name, "/F"])
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass
    return "" if rc == 0 else f"Windows didn't accept the schedule: {out}"


def _win_remove() -> str:
    rc, out = _run(["schtasks", "/Delete", "/TN", WIN_TASK, "/F"])
    if rc != 0 and _win_installed() is not None:
        return f"Couldn't remove the scheduled task: {out}"
    return ""


def _win_installed() -> Optional[list]:
    rc, out = _run(["schtasks", "/Query", "/TN", WIN_TASK, "/XML"])
    if rc != 0:
        return None
    import re
    cmd = re.search(r"<Command>(.*?)</Command>", out, re.S)
    from xml.sax.saxutils import unescape
    return [unescape(cmd.group(1)).strip()] if cmd else []


# ----- either ---------------------------------------------------------------------------

TEST_SCHEDULER = os.environ.get("SVTDL_GUI_TEST_SCHEDULER", "")   # tests: a file stands in for the real scheduler


def supported() -> bool:
    return core.IS_MAC or core.IS_WINDOWS or bool(TEST_SCHEDULER)


def install(sched: dict) -> str:
    """Create or replace the computer's schedule. Returns an error message or ''."""
    problem = location_problem()
    if problem:
        return problem
    if TEST_SCHEDULER:
        Path(TEST_SCHEDULER).write_text(json.dumps({"program": program_args(), "schedule": sched}))
        return ""
    if core.IS_MAC:
        return _mac_install(sched)
    if core.IS_WINDOWS:
        return _win_install(sched)
    return "Scheduled downloads work on Mac and Windows."


def remove() -> str:
    if TEST_SCHEDULER:
        Path(TEST_SCHEDULER).unlink(missing_ok=True)
        return ""
    if core.IS_MAC:
        return _mac_remove()
    if core.IS_WINDOWS:
        return _win_remove()
    return ""


def installed() -> Optional[list]:
    if TEST_SCHEDULER:
        try:
            return json.loads(Path(TEST_SCHEDULER).read_text())["program"]
        except (OSError, ValueError, KeyError):
            return None
    if core.IS_MAC:
        return _mac_installed()
    if core.IS_WINDOWS:
        return _win_installed()
    return None


def repair() -> None:
    """Called when the window opens: if the schedule is on but points at an old copy of the app
    (it was moved, or this is a newer install somewhere else), point it at this copy."""
    d = load()
    sched = d.get("schedule") or {}
    if not sched.get("enabled") or location_problem():
        return
    have = installed()
    if have is None or not have or have[0] != program_args()[0]:
        install(sched)


def set_schedule(enabled: bool, at: str, days: list) -> dict:
    h, m = _parse_time(at)
    days = [x for x in DAYS if x in (days or [])]
    sched = {"enabled": bool(enabled), "time": f"{h:02d}:{m:02d}", "days": [] if len(days) == 7 else days}
    err = install(sched) if enabled else remove()
    if err:
        return {"ok": False, "message": err}
    update(lambda d: d.__setitem__("schedule", sched))
    return {"ok": True}


def notify(title: str, text: str) -> None:
    """A notification after a scheduled run (Mac only; Windows shows the result in the app)."""
    if not core.IS_MAC:
        return
    q = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    _run(["osascript", "-e", f"display notification {q(text)} with title {q(title)}"], timeout=10)
