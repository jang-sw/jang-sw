"""Offline tests; run python -m unittest discover -s tests -v."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from urllib.error import HTTPError
from urllib.request import Request


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/language_atlas.py"
spec = importlib.util.spec_from_file_location("language_atlas", SCRIPT)
atlas = importlib.util.module_from_spec(spec)
spec.loader.exec_module(atlas)


def repo(name, **kwargs):
    return {"name": name, "owner": {"login": "jang-sw"}, "fork": False, "private": False, **kwargs}


def fixture():
    return {
        "repositories": [repo("big"), repo("small", archived=True), repo("empty"), repo("jang-sw"), repo("fork", fork=True), repo("secret", private=True), repo("foreign", owner={"login": "elsewhere"})],
        "languages_by_repo": {"big": {"Java": 900000, "HTML": 100000}, "small": {"Python": 10, "HTML": 10}, "empty": {}},
    }


class LanguageAtlasTests(unittest.TestCase):
    def test_scope_counts_and_normalization(self):
        data = atlas.build_model("jang-sw", fixture())
        self.assertEqual(data["repository_count"], 2)
        self.assertEqual(data["scoped_repository_count"], 3)
        self.assertEqual(data["archived_repository_count"], 1)
        languages = {item["name"]: item for item in data["languages"]}
        self.assertEqual(languages["HTML"]["repository_count"], 2)
        self.assertEqual(languages["Java"]["normalized_share_percent"], 45)
        self.assertEqual(languages["Python"]["normalized_share_percent"], 25)
        self.assertEqual(languages["HTML"]["normalized_share_percent"], 30)
        self.assertEqual(data["connections"], [{"languages": ["HTML", "Java"], "repository_count": 1}, {"languages": ["HTML", "Python"], "repository_count": 1}])

    def test_deterministic_order_and_no_timestamps(self):
        first = fixture()
        second = fixture()
        second["repositories"].reverse()
        second["languages_by_repo"]["big"] = {"HTML": 100000, "Java": 900000}
        model = atlas.build_model("jang-sw", first)
        self.assertEqual(model, atlas.build_model("jang-sw", second))
        self.assertNotIn("timestamp", json.dumps(model))
        self.assertEqual(atlas.render_svg(model), atlas.render_svg(model))

    def test_pagination_and_no_private_language_requests(self):
        page1 = [repo(f"fork-{index}", fork=True) for index in range(100)]
        responses = [page1, [repo("small")], {"Python": 10}]
        with patch.object(atlas, "request_json", side_effect=responses) as request:
            result = atlas.fetch_snapshot("jang-sw")
        self.assertEqual(request.call_count, 3)
        self.assertIn("page=2", request.call_args_list[1].args[0])
        self.assertEqual(result["languages_by_repo"], {"small": {"Python": 10}})

    def test_missing_languages_fails_instead_of_silent_partial_data(self):
        snapshot = fixture()
        del snapshot["languages_by_repo"]["small"]
        with self.assertRaises(atlas.AtlasError):
            atlas.build_model("jang-sw", snapshot)

    def test_negative_and_boolean_byte_counts_rejected(self):
        for bad in (-1, True, "3"):
            snapshot = fixture()
            snapshot["languages_by_repo"]["small"] = {"Python": bad}
            with self.assertRaises(atlas.AtlasError):
                atlas.build_model("jang-sw", snapshot)

    def test_markers_preserve_surrounding_content(self):
        original = f"before\n{atlas.START}\nold table\n{atlas.END}\nafter\n"
        result = atlas.replace_block(original, "new table")
        self.assertEqual(result, f"before\n{atlas.START}\n\nnew table\n\n{atlas.END}\nafter\n")
        for bad in ("no markers", atlas.END + atlas.START, atlas.START + atlas.START + atlas.END):
            with self.assertRaises(atlas.AtlasError):
                atlas.replace_block(bad, "table")

    def test_escaping_and_svg_validity(self):
        strange = "x|[a]<svg>"
        data = atlas.build_model("jang-sw", {"repositories": [repo(strange)], "languages_by_repo": {strange: {"A<&\"": 10}}})
        svg = atlas.render_svg(data)
        ET.fromstring(svg)
        self.assertIn("A&lt;&amp;&quot;", svg)
        table = atlas.render_table(data)
        self.assertNotIn("<svg>", table)
        self.assertIn(r"x\|\[a\]", table)
        self.assertIn("%3Csvg%3E", table)

    def test_empty_languages_graceful(self):
        data = atlas.build_model("jang-sw", {"repositories": [repo("empty")], "languages_by_repo": {"empty": {}}})
        self.assertEqual(data["language_count"], 0)
        self.assertEqual(data["repository_count"], 0)
        ET.fromstring(atlas.render_svg(data))
        self.assertIn("No public", atlas.render_table(data))

    def test_edge_gradient_renders_zero_width_and_height_paths(self):
        # objectBoundingBox gradients disappear for perfectly vertical/horizontal lines.
        svg = ET.fromstring(atlas.render_svg(atlas.build_model("jang-sw", fixture())))
        gradient = svg.find(".//{http://www.w3.org/2000/svg}linearGradient[@id='line']")
        self.assertEqual(gradient.get("gradientUnits"), "userSpaceOnUse")

    def test_writes_and_check_mode_are_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("README.md", "README.ja.md"):
                (root / name).write_text(f"Intro\n{atlas.START}\n{atlas.END}\n", encoding="utf-8")
            model = atlas.build_model("jang-sw", fixture())
            outputs = atlas.generate_outputs(root, model)
            self.assertFalse(atlas.write_outputs(outputs, check=True))
            self.assertFalse((root / "data/languages.json").exists())
            self.assertTrue(atlas.write_outputs(outputs))
            self.assertTrue(atlas.write_outputs(atlas.generate_outputs(root, model), check=True))

    def test_api_failure_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "README.md"
            path.write_text("keep this", encoding="utf-8")
            with patch.object(atlas, "fetch_snapshot", side_effect=atlas.AtlasError("API failure")):
                self.assertEqual(atlas.main(["--root", str(root)]), 2)
            self.assertEqual(path.read_text(encoding="utf-8"), "keep this")
            self.assertFalse((root / "data").exists())

    def test_image_cache_version_depends_only_on_diagram(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("README.md", "README.ja.md"):
                (root / name).write_text(f'<img src="./assets/language-atlas.svg"/>\n{atlas.START}\n{atlas.END}\n', encoding="utf-8")
            model = atlas.build_model("jang-sw", fixture())
            outputs = atlas.generate_outputs(root, model)
            self.assertRegex(outputs[root / "README.md"], r'language-atlas\.svg\?v=[a-f0-9]{12}')
            atlas.write_outputs(outputs)
            self.assertEqual(outputs, atlas.generate_outputs(root, model))

    def test_redirect_guard_keeps_tokens_on_github_api_origin(self):
        handler = atlas.SameHostRedirect()
        request = Request("https://api.github.com/start", headers={"Authorization": "Bearer test-secret"})
        for target in ("https://example.com/path", "http://api.github.com/path", "https://api.github.com:444/path", "https://token@api.github.com/path"):
            with self.assertRaises(HTTPError):
                handler.redirect_request(request, None, 302, "Found", {}, target)
        accepted = handler.redirect_request(request, None, 302, "Found", {}, "https://api.github.com:443/next")
        self.assertEqual(accepted.full_url, "https://api.github.com:443/next")

    def test_details_link_all_detected_languages_to_actual_repos(self):
        names = {f"Language{index}": 10 for index in range(8)}
        model = atlas.build_model("jang-sw", {"repositories": [repo("project")], "languages_by_repo": {"project": names}})
        table = atlas.render_table(model)
        self.assertIn("Explore all 8 languages", table)
        self.assertEqual(sum(line.startswith("| Language") and not line.startswith("| Language |") for line in table.splitlines()), 6)
        details = table.split("<details>")[1]
        for name in names:
            self.assertIn(f"**{name}** (1 repos)", details)
        self.assertEqual(details.count("https://github.com/jang-sw/project"), 8)


if __name__ == "__main__":
    unittest.main()
