#!/usr/bin/env python3
"""Draws the svtplay-dl GUI app icon: the page icons in web/icons, and the app icons
assets/icon.icns (Mac) and assets/icon.ico (Windows) used when building the app.

The icon is a computer with a download arrow, in the blue and yellow of the Swedish flag: a nod
to svtplay-dl's Swedish developer and the Swedish programs it's mostly used for.
Colors: the widely used digital stand-ins for the flag, blue #006AA7 (deepening toward
Pantone 301, #004B87) and yellow #FECC02.

Renders the PNGs with headless Chrome via Playwright, then packs them with Pillow:
  python3 tools/make_icons.py [path-to-chrome]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLUE_TOP, BLUE_BOTTOM, YELLOW = "#1a7fc0", "#004b87", "#fecc02"

DEFS = f'''<defs>
 <linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{BLUE_TOP}"/><stop offset="1" stop-color="{BLUE_BOTTOM}"/></linearGradient>
 <linearGradient id="gloss" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".16"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/></linearGradient>
 <filter id="sh" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="14" stdDeviation="16" flood-color="#000" flood-opacity=".28"/></filter>
 <filter id="s2" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="6" stdDeviation="8" flood-color="#00213d" flood-opacity=".45"/></filter>
</defs>'''

# computer (screen + stand) with a download arrow, on the 1024 x 1024 grid
ARTWORK = f'''<g filter="url(#s2)" fill="none" stroke="{YELLOW}" stroke-linecap="round" stroke-linejoin="round">
  <rect x="248" y="268" width="528" height="372" rx="56" stroke-width="44"/>
  <path d="M412 742h200M512 640v102" stroke-width="44"/>
  <path d="M512 352v178" stroke-width="52"/>
  <path d="M430 452l82 82 82-82" stroke-width="52"/>
</g>'''


def tile_svg(viewbox: str = "0 0 1024 1024") -> str:
    """Rounded tile with a shadow (Mac-style 824 px tile in a 1024 canvas)."""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}">{DEFS}
<g filter="url(#sh)"><rect x="100" y="100" width="824" height="824" rx="185" fill="url(#g)"/>
<rect x="100" y="100" width="824" height="824" rx="185" fill="url(#gloss)"/></g>
{ARTWORK}
</svg>'''


def main() -> None:
    out = ROOT / "web" / "icons"
    assets = ROOT / "assets"
    out.mkdir(parents=True, exist_ok=True)
    assets.mkdir(exist_ok=True)
    for old in ("maskable-192.png", "maskable-512.png", "apple-touch-icon.png", "icon-192.png", "icon-512.png"):
        (out / old).unlink(missing_ok=True)
    (assets / "icon-maskable.svg").unlink(missing_ok=True)
    mac = tile_svg()                         # Apple's grid: 824 px tile in a 1024 canvas
    tight = tile_svg("84 84 856 856")        # less empty space: better on Windows and in the page
    (assets / "icon.svg").write_text(mac)
    (out / "icon.svg").write_text(tight)
    tmp = assets / "_render"
    tmp.mkdir(exist_ok=True)
    jobs = [(tight, 32, out / "icon-32.png"),
            (mac, 1024, tmp / "mac-1024.png")] + [(tight, n, tmp / f"win-{n}.png") for n in (16, 24, 32, 48, 64, 256)]
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=sys.argv[1] if len(sys.argv) > 1 else None)
        for svg, size, path in jobs:
            pg = b.new_page(viewport={"width": size, "height": size})
            pg.set_content('<html><body style="margin:0;background:transparent">'
                           + svg.replace("<svg ", f'<svg width="{size}" height="{size}" ', 1) + "</body></html>")
            pg.screenshot(path=str(path), omit_background=True)
            pg.close()
        b.close()
    from PIL import Image
    Image.open(tmp / "mac-1024.png").save(assets / "icon.icns")
    big = Image.open(tmp / "win-256.png")
    small = [Image.open(tmp / f"win-{n}.png") for n in (16, 24, 32, 48, 64)]
    big.save(assets / "icon.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 256)], append_images=small)
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    print("Icons written to", out, "and", assets)


if __name__ == "__main__":
    main()
