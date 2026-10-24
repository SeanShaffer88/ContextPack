import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import contextpack  # noqa: E402

MIT_TEXT = """MIT License

Copyright (c) 2026 Sean Shaffer

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction.
"""

GPL_TEXT = """GNU GENERAL PUBLIC LICENSE
Version 3, 29 June 2007

Copyright (C) 2007 Free Software Foundation, Inc.
Everyone is permitted to copy and distribute verbatim copies
of this license document, but changing it is not allowed.
"""


def write(root: Path, name: str, content: str) -> None:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


class TestRustFixture(unittest.TestCase):
    def test_rust_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write(root, "Cargo.toml",
                  '[package]\nname = "demo-kit"\nversion = "1.2.3"\nedition = "2021"\n\n'
                  '[dependencies]\nserde = "1"\nanyhow = "1"\n')
            write(root, "LICENSE", MIT_TEXT)
            write(root, "README.md",
                  "# Demo\n\n## TL;DR\n\nDemo does one useful thing well.\n\n## Usage\n\nMore.\n")
            write(root, "src/lib.rs", "pub fn x() {}\n")
            data = contextpack.collect(root)
            self.assertEqual(data["license"]["identifier"], "MIT")
            self.assertEqual(data["license"]["first_line"], "MIT License")
            rust = data["stacks"]["rust"]
            self.assertEqual(rust["package"], "demo-kit")
            self.assertEqual(rust["version"], "1.2.3")
            self.assertEqual(rust["edition"], "2021")
            self.assertEqual(rust["dependencies_count"], 2)
            self.assertIn("Demo does one useful thing well.", data["docs"]["tldr"])
            self.assertIn("cargo test", data["build"]["inferred_commands"])
            md = contextpack.render_markdown(data)
            self.assertIn("verify before acting", md)
            self.assertIn("demo-kit", md)


class TestPythonGplFixture(unittest.TestCase):
    def test_python_and_gpl_detected_not_assumed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write(root, "pyproject.toml",
                  '[project]\nname = "pything"\nversion = "0.3.0"\n'
                  'requires-python = ">=3.10"\ndependencies = ["requests"]\n')
            write(root, "LICENSE", GPL_TEXT)
            write(root, "pything.py", "x = 1\n")
            write(root, "tests/test_pything.py", "import unittest\n")
            data = contextpack.collect(root)
            # The lesson: license comes from the file, never a sibling project.
            self.assertTrue(data["license"]["identifier"].startswith("GPL-3.0"),
                            data["license"]["identifier"])
            self.assertNotEqual(data["license"]["identifier"], "MIT")
            py = data["stacks"]["python"]
            self.assertEqual(py["name"], "pything")
            self.assertEqual(py["version"], "0.3.0")
            self.assertEqual(py["requires_python"], ">=3.10")
            self.assertIn("python -m unittest discover", data["build"]["inferred_commands"])


class TestEmptyDir(unittest.TestCase):
    def test_graceful(self):
        with tempfile.TemporaryDirectory() as td:
            data = contextpack.collect(Path(td))
            self.assertEqual(data["stacks"], {})
            self.assertIsNone(data["license"]["file"])
            self.assertEqual(data["tree"]["file_count"], 0)
            md = contextpack.render_markdown(data)
            self.assertIn("No known stack detected", md)
            self.assertIn("Unknown", md)


class TestJsonAndCli(unittest.TestCase):
    def test_json_matches_md_facts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            root.mkdir()
            write(root, "Cargo.toml",
                  '[package]\nname = "jsondemo"\nversion = "2.0.0"\nedition = "2021"\n')
            write(root, "LICENSE", MIT_TEXT)
            out_md = Path(td) / "CONTEXT.md"
            out_json = Path(td) / "context.json"
            rc = contextpack.main(["pack", str(root), "--out", str(out_md),
                                   "--json", str(out_json)])
            self.assertEqual(rc, 0)
            parsed = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(parsed["stacks"]["rust"]["package"], "jsondemo")
            self.assertEqual(parsed["license"]["identifier"], "MIT")
            md = out_md.read_text(encoding="utf-8")
            self.assertIn("jsondemo", md)
            self.assertIn("MIT", md)
            self.assertLess(len(md.splitlines()), 250)

    def test_cli_subprocess_stdout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write(root, "README.md", "# Hi\n\n## TL;DR\n\nHello brief.\n")
            script = Path(__file__).resolve().parent.parent / "contextpack.py"
            r = subprocess.run([sys.executable, str(script), "pack", str(root)],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Hello brief.", r.stdout)

    def test_other_stacks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write(root, "package.json",
                  '{"name": "webthing", "version": "1.0.0", "scripts": {"test": "node t.js", "build": "tsc"}}')
            write(root, "App.csproj",
                  '<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>')
            write(root, "App.sln", "")
            write(root, "pubspec.yaml", 'name: flutterthing\nenvironment:\n  sdk: ">=3.0.0 <4.0.0"\n')
            write(root, "run.ps1", "Write-Host hi\n")
            write(root, "Makefile", "build:\n\tcargo build\ntest:\n\tcargo test\n")
            write(root, ".github/workflows/ci.yml", "name: CI\non: [push]\n")
            write(root, "gate.sh", "#!/bin/sh\ntrue\n")
            data = contextpack.collect(root)
            self.assertEqual(data["stacks"]["node"]["name"], "webthing")
            self.assertEqual(data["stacks"]["dotnet"]["projects"][0]["target_framework"], "net8.0")
            self.assertEqual(data["stacks"]["flutter"]["name"], "flutterthing")
            self.assertEqual(data["stacks"]["powershell"]["script_count"], 1)
            self.assertEqual(data["ci"][0]["name"], "CI")
            self.assertIn("gate.sh", data["build"]["gate_files"])
            targets = data["build"]["makefiles"][0]["targets"]
            self.assertIn("build", targets)
            self.assertIn("npm run test", data["build"]["inferred_commands"])

    def test_simple_toml_fallback(self):
        data = contextpack._simple_toml(
            '[package]\nname = "fb"\nversion = "0.1.0"\nedition = "2021"\n')
        self.assertEqual(data["package"]["name"], "fb")
        self.assertEqual(data["package"]["edition"], "2021")


if __name__ == "__main__":
    unittest.main()
