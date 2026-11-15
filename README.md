# svtplay-dl GUI

A desktop app for [svtplay-dl](https://github.com/spaam/svtplay-dl), laid out after
[Open Video Downloader](https://github.com/jely2002/youtube-dl-gui). One app for Mac, one for Windows.
It's a normal app: double-click to open, close the window to quit. Nothing runs in the background,
and it doesn't need Python.

## Getting the app

Every time the code is pushed to GitHub, GitHub builds both apps and puts them on this repo's
**Releases** page under **Latest build** (the build takes about 5 minutes; you can watch it on the
**Actions** tab).

### Mac

1. Download **svtplay-dl-GUI-mac.zip** from Releases and open it.
2. Drag **svtplay-dl GUI** into **Applications**.
3. The first time only: double-click it. macOS says it can't check the app for malware, because it
   isn't signed with a paid Apple developer certificate. Click **Done**, then open
   **System Settings > Privacy & Security**, scroll down, and click **Open Anyway** next to
   "svtplay-dl GUI". Confirm with your password. From then on it opens normally.

### Windows

1. Download **svtplay-dl-GUI-windows.zip** from Releases and open it.
2. Drag **svtplay-dl GUI.exe** somewhere to keep it, e.g. Documents. Right-click it and choose
   **Pin to Start** or **Pin to taskbar** if you like.
3. The first time only: Windows SmartScreen may say it protected your PC. Click **More info**, then
   **Run anyway**.

### svtplay-dl and ffmpeg

The app runs svtplay-dl and ffmpeg, which are installed separately. If either is missing, the app
shows an **Install** button at the top that installs them for you, with Homebrew on a Mac or
winget on Windows. (If the Mac doesn't have Homebrew yet, install it from https://brew.sh first.)

## Updating

- **The app updates itself.** When a newer build is on the Releases page, a green
  "Update app to …" button appears at the top. Click it: the app downloads the new version,
  closes and reopens by itself. Settings shows which version you have and can check right away.
  (Versions before 1.1.0 can't do this yet: download 1.1.0 by hand once.)
- **svtplay-dl:** a green "Update svtplay-dl to …" button appears in the app when a newer version
  is out. It updates through Homebrew, winget or pip, whichever installed it.

## Saving to a network folder

Saving straight to a network folder (for example a Plex server's share) works. The app sees that
the folder is on another computer, downloads to this computer first, and copies the finished
video over at the end. It copies to a temporary `.part` name and renames it when complete, so
Plex never picks up half a file. If the share is busy or unreachable, it keeps trying for about
30 seconds. If it still can't copy, the download is kept on this computer and the card says where.

## Closing

Close the window to quit. If downloads are running, the app asks first: keep downloading, or stop
them and quit. The power button at the top right does the same.

## Features

- Paste one or many links (Paste button, ⌘V or Ctrl+V anywhere, Enter), or drag a link onto the window.
- **Whole series**: finds every episode of a show and lets you choose which to add:
  - tick episodes one by one, or shift-click to tick a run of them
  - type ranges like `1-5, 8, 12-`, a whole season like `S2`, or `S1E3-E8`
  - "Latest N", season buttons, All and None
- Checks each video's streams so you can pick an exact one, or use a preset (Best, 1080p … 360p, audio only).
- Subtitles: download .srt, merge into the video, all languages, or subtitles only. Output as mp4 or mkv.
- Download queue with a limit on how many run at once. Stop, retry, Show in Finder or Explorer, and a log for each video.
- Location chip: shows which country your downloads appear to come from, e.g. through your VPN.
  Hover for details, click to check again. A browser-extension VPN doesn't cover downloads, so use
  the VPN's own app, or a proxy in Settings.
- Settings: svtplay-dl location, file name pattern, subfolder per show, overwrite, thumbnail,
  .nfo files for Plex, proxy, extra arguments.

## How it's built

- The window is [pywebview](https://pywebview.flowrl.com): the system's own web view (WebKit on a
  Mac, Edge WebView2 on Windows) showing the page in `web/index.html`.
- `svtplay_dl_gui.py` runs svtplay-dl, keeps the download queue, and serves the page to the window
  from a private address on this computer (127.0.0.1, a new port each time). It only accepts
  requests from the app's own page.
- `svtdl_core.py` finds svtplay-dl and ffmpeg, builds the command-line arguments, and reads
  svtplay-dl's progress output.
- [PyInstaller](https://pyinstaller.org) packs it all, Python included, into the Mac app and the
  Windows .exe. The recipe is `svtplay-dl-gui.spec`; the GitHub workflow is
  `.github/workflows/build.yml`. Every push makes a new build (the build number goes up by
  itself); change `VERSION` for a new version number.
- Settings and the log file (`app.log`) are in `~/Library/Application Support/svtplay-dl-gui/`
  on a Mac and `%APPDATA%\svtplay-dl-gui\` on Windows.
- `tools/make_icons.py` redraws the icon (a computer with a download arrow, in the Swedish flag's
  blue and yellow) and writes `assets/icon.icns` and `assets/icon.ico`.
- To run it from the code while working on it: `pip install pywebview`, then
  `python3 svtplay_dl_gui.py`. Without pywebview it opens in your browser instead.

`check_mac_setup.command` is a read-only check of Python, svtplay-dl and ffmpeg on a Mac.
