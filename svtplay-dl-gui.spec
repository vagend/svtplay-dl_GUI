# PyInstaller recipe for svtplay-dl GUI.
# GitHub builds the app with this (see .github/workflows/build.yml). To build by hand on a Mac or PC:
#   pip install -r requirements-build.txt
#   pyinstaller --noconfirm --clean svtplay-dl-gui.spec
# Mac:     dist/svtplay-dl GUI.app
# Windows: dist/svtplay-dl GUI.exe   (one file)
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = Path(SPECPATH)
VERSION = (ROOT / "VERSION").read_text().strip()
NAME = "svtplay-dl GUI"
IS_MAC = sys.platform == "darwin"

datas = [(str(ROOT / "web"), "web"), (str(ROOT / "VERSION"), ".")]
datas += [(str(ROOT / n), ".") for n in ("BUILD", "REPO") if (ROOT / n).exists()]   # written by the GitHub build
datas += collect_data_files("certifi")          # certificates for https inside the app
datas += collect_data_files("webview")          # pywebview's own scripts
binaries = collect_dynamic_libs("webview")      # and, on Windows, its WebView2 helper DLLs
hiddenimports = ["clr"] if sys.platform == "win32" else []   # pythonnet, used by pywebview on Windows

a = Analysis(
    [str(ROOT / "svtplay_dl_gui.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "cefpython3"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if IS_MAC:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, console=False,
              icon=str(ROOT / "assets" / "icon.icns"))
    coll = COLLECT(exe, a.binaries, a.datas, name=NAME)
    app = BUNDLE(
        coll,
        name=f"{NAME}.app",
        icon=str(ROOT / "assets" / "icon.icns"),
        bundle_identifier="io.github.svtplay-dl-gui",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": NAME,
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.video",
            "NSHumanReadableCopyright": "A GUI for svtplay-dl",
        },
    )
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=NAME, console=False, upx=False,
              icon=str(ROOT / "assets" / "icon.ico"))
