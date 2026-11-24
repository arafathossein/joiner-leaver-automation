"""Render docs/preview.png from the demo report (needs: pip install playwright && playwright install chromium)."""

import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
out = Path(tempfile.mkdtemp())
subprocess.run([sys.executable, "-m", "jml.cli", "demo", "--out", str(out)], check=True, cwd=root)

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1040, "height": 1500}, device_scale_factor=2, color_scheme="light")
    page.goto((out / "applied.html").as_uri())
    # Summary table plus the mover and the leaver, which show the most.
    page.evaluate("""() => document.querySelectorAll('section.card').forEach((c, i) => { if (i !== 2 && i !== 3) c.remove(); })""")
    h = min(page.evaluate("document.body.scrollHeight"), 1500)
    page.screenshot(path=str(root / "docs" / "preview.png"), clip={"x": 0, "y": 0, "width": 1040, "height": h})
    browser.close()
print("wrote docs/preview.png")
