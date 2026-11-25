"""Check the built window and phone entries against their static URL spaces."""

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unittest
from urllib.parse import urljoin, urlsplit


DIST = Path(__file__).resolve().parents[2] / "hud3" / "dist"
CSS_REFERENCE = re.compile(r"""url\(\s*['"]?([^'"\s)]+)|@import\s+['"]([^'"]+)""", re.I)


class References(HTMLParser):
    def __init__(self, path):
        super().__init__()
        self.paths = []
        self.inline = []
        self.feed(path.read_text(encoding="utf-8"))

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.paths.extend(value for key, value in attrs if key in ("src", "href"))
        if (tag == "script" and not attributes.get("src") or tag == "style"
            or any(key == "style" or key.startswith("on") for key in attributes)):
            self.inline.append(tag)


@unittest.skipUnless(DIST.is_dir(), "hud3/dist is absent. Build it with npm run build in hud3.")
class PhoneBuild(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((DIST / "build-manifest.json").read_text(encoding="utf-8"))

    def manifest_files(self, entry):
        pending = [entry]
        seen = set()
        files = set()
        self.assertTrue(self.manifest[entry]["isEntry"])
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            item = self.manifest[key]
            files.update([item["file"], *item.get("css", []), *item.get("assets", [])])
            pending.extend([*item.get("imports", []), *item.get("dynamicImports", [])])
        for file in files:
            self.assertRegex(file, r"^assets/[^/]+$")
            self.assertTrue((DIST / file).is_file(), file)
        return files

    def check_references(self, entry, entry_path, prefix):
        closure = self.manifest_files(entry)
        html = References(DIST / entry)
        self.assertEqual(html.inline, [])
        self.assertTrue(html.paths)
        from_html = set()

        def resolve(base, reference):
            parsed = urlsplit(reference)
            self.assertFalse(parsed.scheme or parsed.netloc, reference)
            if prefix != "/":
                self.assertFalse(reference.startswith("/"), reference)
            url = urlsplit(urljoin(base, reference)).path
            self.assertTrue(url.startswith(prefix), url)
            relative = url[len(prefix):]
            self.assertTrue((DIST / relative).is_file(), url)
            return relative

        for reference in html.paths:
            relative = resolve(entry_path, reference)
            from_html.add(relative)
            if relative.startswith("assets/"):
                self.assertIn(relative, closure)
            else:
                self.assertRegex(relative, r"^(manifest\.webmanifest|icons/[^/]+)$")
        self.assertIn(self.manifest[entry]["file"], from_html)
        for css in sorted(file for file in closure if file.endswith(".css")):
            content = (DIST / css).read_text(encoding="utf-8")
            for url, imported in CSS_REFERENCE.findall(content):
                self.assertIn(resolve(prefix + css, url or imported), closure)
        return closure

    def test_phone_entry_reaches_only_relative_build_files_from_both_routes(self):
        for path in ("/m/", "/m/pair"):
            with self.subTest(path=path):
                phone_files = self.check_references("phone.html", path, "/m/")
                self.assertNotIn(self.manifest["index.html"]["file"], phone_files)

    def test_window_entry_resolves_from_root(self):
        window_files = self.check_references("index.html", "/", "/")
        self.assertNotIn(self.manifest["phone.html"]["file"], window_files)


if __name__ == "__main__":
    unittest.main(verbosity=2)
