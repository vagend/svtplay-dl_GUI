# dmgbuild settings for the Mac installer window: the app on the left, Applications on the right,
# and a background telling you to drag one onto the other.
# Used by .github/workflows/build.yml:  dmgbuild -s packaging/dmg_settings.py -D app=... -D background=... "svtplay-dl GUI" out.dmg
import os.path

application = defines["app"]  # noqa: F821
appname = os.path.basename(application)

format = "UDZO"
files = [application]
symlinks = {"Applications": "/Applications"}
icon = os.path.join(application, "Contents", "Resources", "icon.icns")

background = defines["background"]  # noqa: F821
window_rect = ((200, 160), (640, 428))   # 640 x 400 of content plus the title bar
default_view = "icon-view"
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
icon_size = 112
text_size = 13
icon_locations = {appname: (160, 196), "Applications": (480, 196)}
hide_extensions = [appname]
