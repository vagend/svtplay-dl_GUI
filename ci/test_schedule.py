"""
Build check for automatic downloads, run on GitHub's Mac and Windows machines against the built app:
  python ci/test_schedule.py "<path to the app's program>"

1. A run with --run-scheduled (no window) downloads only the episode that is new.
2. --set-schedule creates the real launchd job / Task Scheduler task, pointing at this app.
3. Starting that job the way the computer would (launchctl kickstart / schtasks /Run) downloads
   the next new episode.
4. --set-schedule off removes it again.
A fake svtplay-dl stands in for the real one, so nothing is fetched from SVT.
"""
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import svtdl_core as core      # noqa: E402
import svtdl_follow as follow  # noqa: E402

app = str(Path(sys.argv[1]).resolve())
work = Path(tempfile.mkdtemp(prefix="schedtest-"))
out = work / "Downloads"
out.mkdir()
eps = work / "episodes.txt"
SHOW = "https://www.svtplay.se/test-show"
EP = [f"https://www.svtplay.se/video/id{n}/test-show/avsnitt-{n}" for n in (1, 2, 3)]

fake = work / "fake_svtplay.py"
fake.write_text(f'''import os, sys
a = sys.argv[1:]
if "--version" in a: print("svtplay-dl 4.999 (fake)"); sys.exit(0)
if "-A" in a:
    for l in open({str(eps)!r}): print("INFO: Url: " + l.strip())
    sys.exit(0)
o = a[a.index("-o") + 1]
p = os.path.join(o, a[-1].rstrip("/").split("/")[-1] + ".mp4")
print("INFO: Outfile: " + p, flush=True)
open(p, "w").write("video")
''')
if core.IS_WINDOWS:
    wrapper = work / "svtplay-dl.cmd"
    wrapper.write_text(f'@"{sys.executable}" "{fake}" %*\r\n')
else:
    wrapper = work / "svtplay-dl"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    wrapper.chmod(0o755)

data = follow.data_dir()
data.mkdir(parents=True, exist_ok=True)
(data / "settings.json").write_text(json.dumps({"svtplay_path": str(wrapper), "output_dir": str(out)}))
for f in ("follows.json", "scheduled.log", "scheduled-run.json"):
    (data / f).unlink(missing_ok=True)
follow.follow(SHOW, "Test show", "", [EP[0]])        # episode 1 existed when the show was followed


def check(ok, what):
    print(("OK   " if ok else "FAIL ") + what, flush=True)
    if not ok:
        print("---- scheduled.log ----")
        try:
            print(follow.log_path().read_text(encoding="utf-8"))
        except OSError:
            print("(none)")
        sys.exit(1)


def wait_for(path: Path, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if path.exists() and not follow.running():
            return True
        time.sleep(1)
    return path.exists()


# 1. a run without a window
eps.write_text("\n".join(EP[:2]) + "\n")
subprocess.run([app, "--run-scheduled"], timeout=180)
check((out / "avsnitt-2.mp4").exists(), "run without a window downloaded the new episode")
check(not (out / "avsnitt-1.mp4").exists(), "the episode that was already there when followed was left alone")
check(EP[1] in follow.load()["shows"][0]["seen"], "the new episode is remembered as done")

# 2. set the schedule with the app itself
r = subprocess.run([app, "--set-schedule", "03:17"], timeout=60)
res = json.loads((data / "set-schedule-result.json").read_text())
check(r.returncode == 0 and res.get("ok"), f"--set-schedule 03:17 -> {res}")
prog = follow.installed()
check(bool(prog) and Path(prog[0]).resolve() == Path(app), f"the schedule starts this app: {prog}")
if core.IS_MAC:
    pl = plistlib.loads(follow.mac_plist_path().read_bytes())
    check(pl["StartCalendarInterval"] == {"Hour": 3, "Minute": 17}, f"launchd time: {pl['StartCalendarInterval']}")
    pr = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{follow.MAC_LABEL}"], capture_output=True, text=True)
    check(pr.returncode == 0, "launchd has loaded the job")
else:
    q = subprocess.run(["schtasks", "/Query", "/TN", follow.WIN_TASK, "/XML"], capture_output=True, text=True)
    check("T03:17:00" in q.stdout and "<StartWhenAvailable>true" in q.stdout, "Task Scheduler has the task at 03:17")

# 3. let the computer's scheduler start it now
eps.write_text("\n".join(EP) + "\n")
if core.IS_MAC:
    k = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{follow.MAC_LABEL}"], capture_output=True, text=True)
else:
    k = subprocess.run(["schtasks", "/Run", "/TN", follow.WIN_TASK], capture_output=True, text=True)
print(k.stdout, k.stderr)
check(wait_for(out / "avsnitt-3.mp4", 180), "the scheduler started the app and it downloaded the next new episode")
log = follow.log_path().read_text(encoding="utf-8")
check("(schedule)" in log, "the log shows a scheduled run")
runs = follow.load()["runs"]
check(runs and runs[-1]["downloaded"] and runs[-1]["downloaded"][0]["file"] == "avsnitt-3.mp4", f"last run: {runs[-1:]}")

# 4. turn it off
r = subprocess.run([app, "--set-schedule", "off"], timeout=60)
check(r.returncode == 0 and follow.installed() is None, "--set-schedule off removed it")
print("\nAll scheduled-download checks passed.")
print(log)
