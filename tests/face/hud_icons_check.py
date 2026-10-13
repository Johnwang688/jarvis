"""Offline PNG size, nonblank centre and maskable safe-zone checks; no new dependency."""
import base64
import json
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

HUD = Path(__file__).resolve().parents[2] / "hud"


class Icons(unittest.TestCase):
    def test_manifest_and_pngs(self):
        manifest = json.loads((HUD / "public/manifest.webmanifest").read_text())
        for key, value in dict(id="/", start_url="/", scope="/", display="standalone",
                               name="J.A.R.V.I.S.", short_name="Jarvis",
                               background_color="#141413", theme_color="#141413").items():
            self.assertEqual(manifest[key], value)
        expected = {f"{size}x{size}": "any" for size in (16, 32, 48, 192, 512)}
        self.assertEqual({i["sizes"]: i["purpose"] for i in manifest["icons"] if i["purpose"] == "any"}, expected)
        self.assertEqual(len(manifest["icons"]), 6)
        html = (HUD / "index.html").read_text()
        self.assertIn('<title>J.A.R.V.I.S.</title>', html)
        self.assertIn('rel="manifest" href="/manifest.webmanifest"', html)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            for icon in manifest["icons"]:
                with self.subTest(icon=icon["src"]):
                    path = HUD / "public" / icon["src"].lstrip("/")
                    self.assertTrue(path.is_file())
                    built = HUD / "dist" / icon["src"].lstrip("/")
                    if built.exists():  # a built tree carries the same bytes; an unbuilt one is fine
                        self.assertEqual(built.read_bytes(), path.read_bytes())
                    data = base64.b64encode(path.read_bytes()).decode()
                    result = page.evaluate("""async data => {
                      const image = new Image(); image.src = 'data:image/png;base64,' + data;
                      await image.decode();
                      const c = document.createElement('canvas'); c.width = image.width; c.height = image.height;
                      const ctx = c.getContext('2d'); ctx.drawImage(image, 0, 0);
                      const pixels = ctx.getImageData(0, 0, c.width, c.height).data;
                      const at = (Math.floor(c.height / 2) * c.width + Math.floor(c.width / 2)) * 4;
                      let outside = 0;
                      for (let y = 0; y < c.height; y++) for (let x = 0; x < c.width; x++) {
                        const p = (y * c.width + x) * 4;
                        if (Math.hypot(x + 0.5 - c.width / 2, y + 0.5 - c.height / 2) > c.width * 0.4
                            && (pixels[p] !== 20 || pixels[p+1] !== 20 || pixels[p+2] !== 19)) outside++;
                      }
                      return {width: c.width, height: c.height, centre: Array.from(pixels.slice(at, at+4)), outside};
                    }""", data)
                    self.assertEqual(f"{result['width']}x{result['height']}", icon["sizes"])
                    self.assertNotEqual(result["centre"][:3], [20, 20, 19])
                    self.assertEqual(result["centre"][3], 255)
                    if icon["purpose"] == "maskable":
                        self.assertEqual(icon["sizes"], "512x512")
                        self.assertEqual(result["outside"], 0, "orb stays inside the inner 80% circle")
                    else:
                        self.assertIn(f'href="{icon["src"]}"', html)
            browser.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
