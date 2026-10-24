#!/usr/bin/env python3
"""ContextPack — generate a compact, AI-ready brief for any code repo.

Stdlib only, Python 3.10+. Reads what files actually say — it never
assumes a license, version, or stack from a sibling project.

Usage:
    python contextpack.py pack <repo-dir> [--out CONTEXT.md]
                                [--json context.json] [--max-files 400]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

__version__ = "0.1.0"

SKIP_DIRS = {".git", "__pycache__", "node_modules", "target", ".venv",
             "venv", ".tox", ".mypy_cache", ".pytest_cache", "dist",
             "build", ".idea", ".vs"}
LICENSE_NAMES = {"license", "licence", "license.md", "license.txt",
                 "licence.md", "licence.txt", "copying", "copying.md"}


# ---------------------------------------------------------------- helpers
def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{int(f)} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024.0
    return f"{n} B"


def read_text(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def walk_files(root: Path, max_files: int) -> tuple[list[Path], bool]:
    files: list[Path] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if len(files) >= max_files:
                truncated = True
                return files, truncated
            files.append(Path(dirpath) / name)
    return files, truncated


def rel(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------- git
def git_info(root: Path) -> dict:
    info = {"is_repo": False, "remote_origin": None, "branch": None,
            "head": None, "dirty_count": None}
    try:
        def run(*args):
            r = subprocess.run(["git", "-C", str(root), *args],
                               capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else None
        head = run("rev-parse", "--short", "HEAD")
        if head is None:
            return info
        info["is_repo"] = True
        info["head"] = head
        info["branch"] = run("branch", "--show-current") or "(detached)"
        info["remote_origin"] = run("remote", "get-url", "origin")
        status = run("status", "--porcelain")
        info["dirty_count"] = 0 if status == "" else len(status.splitlines()) if status is not None else None
    except (OSError, subprocess.SubprocessError):
        pass
    return info


# ---------------------------------------------------------------- license
def detect_license(root: Path, files: list[Path]) -> dict:
    result = {"file": None, "first_line": None, "identifier": None}
    candidates = [p for p in files if p.parent == root and p.name.lower() in LICENSE_NAMES]
    if not candidates:
        return result
    path = sorted(candidates)[0]
    text = read_text(path)
    result["file"] = path.name
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    result["first_line"] = lines[0] if lines else ""
    upper = text.upper()
    ident = "Unknown (see license file)"
    if "GNU AFFERO GENERAL PUBLIC LICENSE" in upper:
        ident = "AGPL-3.0 (from license text)" if "VERSION 3" in upper else "AGPL (from license text)"
    elif "GNU LESSER GENERAL PUBLIC LICENSE" in upper:
        ident = "LGPL (from license text)"
    elif "GNU GENERAL PUBLIC LICENSE" in upper:
        if "VERSION 3" in upper:
            ident = "GPL-3.0 (from license text)"
        elif "VERSION 2" in upper:
            ident = "GPL-2.0 (from license text)"
        else:
            ident = "GPL (from license text)"
    elif "APACHE LICENSE" in upper:
        ident = "Apache-2.0 (from license text)" if "VERSION 2.0" in upper else "Apache (from license text)"
    elif "MIT LICENSE" in upper or "PERMISSION IS HEREBY GRANTED, FREE OF CHARGE" in upper:
        ident = "MIT"
    elif "MOZILLA PUBLIC LICENSE" in upper:
        ident = "MPL-2.0 (from license text)"
    elif "REDISTRIBUTION AND USE IN SOURCE AND BINARY FORMS" in upper:
        ident = "BSD (from license text)"
    elif "ISC LICENSE" in upper or ("PERMISSION TO USE, COPY, MODIFY" in upper and "ISC" in upper):
        ident = "ISC (from license text)"
    elif "THE UNLICENSE" in upper or "UNLICENSE" == (result["first_line"] or "").upper():
        ident = "Unlicense (from license text)"
    result["identifier"] = ident
    return result


# ---------------------------------------------------------------- TOML
def load_toml(path: Path) -> dict:
    if tomllib is not None:
        try:
            with path.open("rb") as fh:
                return tomllib.load(fh)
        except Exception:
            return {}
    return _simple_toml(read_text(path))


def _simple_toml(text: str) -> dict:
    """Very small TOML subset parser: sections, strings, ints, arrays."""
    data: dict = {}
    current = data
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[([^\]]+)\]$", line)
        if m:
            current = data
            for part in m.group(1).split("."):
                current = current.setdefault(part.strip().strip('"'), {})
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)\s*=\s*(.+)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val.startswith('"') and val.endswith('"'):
                current[key] = val[1:-1]
            elif val.startswith("[") and val.endswith("]"):
                items = re.findall(r'"([^"]*)"', val)
                current[key] = items
            elif re.fullmatch(r"-?\d+", val):
                current[key] = int(val)
            elif val in ("true", "false"):
                current[key] = val == "true"
            else:
                current[key] = val.strip('"')
    return data


# ---------------------------------------------------------------- stacks
def detect_rust(root: Path, files: list[Path]) -> dict | None:
    cargos = [p for p in files if p.name == "Cargo.toml"]
    if not cargos:
        return None
    cargos.sort(key=lambda p: (len(p.parts), str(p)))
    data = load_toml(cargos[0])
    pkg = data.get("package", {}) if isinstance(data, dict) else {}
    deps = data.get("dependencies", {}) if isinstance(data, dict) else {}
    workspace = data.get("workspace", {}) if isinstance(data, dict) else {}
    return {
        "manifest": rel(root, cargos[0]),
        "manifest_count": len(cargos),
        "package": pkg.get("name"),
        "version": str(pkg.get("version")) if pkg.get("version") is not None else None,
        "edition": str(pkg.get("edition")) if pkg.get("edition") is not None else None,
        "dependencies_count": len(deps) if isinstance(deps, dict) else 0,
        "workspace_members": workspace.get("members") if isinstance(workspace, dict) else None,
    }


def detect_python(root: Path, files: list[Path]) -> dict | None:
    pyproject = root / "pyproject.toml"
    has_pyproject = pyproject.is_file()
    setup_py = (root / "setup.py").is_file()
    req = root / "requirements.txt"
    has_req = req.is_file()
    py_files = [p for p in files if p.suffix == ".py"]
    if not (has_pyproject or setup_py or has_req or py_files):
        return None
    out: dict = {"pyproject": has_pyproject, "setup_py": setup_py,
                 "requirements_txt": has_req, "python_files": len(py_files),
                 "name": None, "version": None, "requires_python": None,
                 "dependencies_count": None, "requirements_count": None}
    if has_pyproject:
        data = load_toml(pyproject)
        proj = data.get("project", {}) if isinstance(data, dict) else {}
        if isinstance(proj, dict) and proj:
            out["name"] = proj.get("name")
            out["version"] = str(proj["version"]) if proj.get("version") is not None else None
            out["requires_python"] = proj.get("requires-python")
            deps = proj.get("dependencies")
            out["dependencies_count"] = len(deps) if isinstance(deps, list) else None
    if has_req:
        out["requirements_count"] = len([ln for ln in read_text(req).splitlines()
                                         if ln.strip() and not ln.strip().startswith("#")])
    return out


def detect_dotnet(root: Path, files: list[Path]) -> dict | None:
    csprojs = [p for p in files if p.suffix == ".csproj"]
    slns = [p for p in files if p.suffix == ".sln"]
    if not csprojs and not slns:
        return None
    projects = []
    for p in sorted(csprojs)[:10]:
        text = read_text(p, 50_000)
        tf = None
        m = re.search(r"<TargetFrameworks?>([^<]+)</TargetFrameworks?>", text)
        if m:
            tf = m.group(1).strip()
        projects.append({"file": rel(root, p), "target_framework": tf})
    return {"projects": projects, "solutions": [rel(root, p) for p in sorted(slns)]}


def detect_node(root: Path, files: list[Path]) -> dict | None:
    pkg_path = root / "package.json"
    if not pkg_path.is_file():
        return None
    try:
        data = json.loads(read_text(pkg_path))
    except (json.JSONDecodeError, ValueError):
        return {"file": "package.json", "name": None, "version": None, "scripts": {}}
    if not isinstance(data, dict):
        return {"file": "package.json", "name": None, "version": None, "scripts": {}}
    return {"file": "package.json", "name": data.get("name"),
            "version": data.get("version"),
            "scripts": data.get("scripts", {}) if isinstance(data.get("scripts"), dict) else {}}


def detect_flutter(root: Path, files: list[Path]) -> dict | None:
    pub = root / "pubspec.yaml"
    if not pub.is_file():
        return None
    text = read_text(pub)
    name = None
    m = re.search(r"^name:\s*(.+)$", text, re.M)
    if m:
        name = m.group(1).strip()
    sdk = None
    m = re.search(r"^\s*sdk:\s*(.+)$", text, re.M)
    if m:
        sdk = m.group(1).strip().strip('"\'')
    dart_files = len([p for p in files if p.suffix == ".dart"])
    return {"pubspec": "pubspec.yaml", "name": name, "sdk_constraint": sdk,
            "dart_files": dart_files}


def detect_powershell(root: Path, files: list[Path]) -> dict | None:
    ps1 = [p for p in files if p.suffix.lower() == ".ps1"]
    if not ps1:
        return None
    return {"script_count": len(ps1),
            "scripts": [rel(root, p) for p in sorted(ps1)[:10]]}


# ---------------------------------------------------------------- docs / CI
def extract_tldr(root: Path) -> dict:
    readmes = sorted(p for p in root.iterdir()
                     if p.is_file() and p.name.lower().startswith("readme")) if root.is_dir() else []
    if not readmes:
        return {"readme": None, "tldr": None}
    text = read_text(readmes[0])
    tldr = None
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^##\s+TL;DR\s*$", ln.strip(), re.I):
            start = i + 1
            break
    if start is not None:
        buf = []
        for ln in lines[start:]:
            if re.match(r"^#{1,2}\s+", ln):
                break
            buf.append(ln)
        tldr = "\n".join(buf).strip() or None
    return {"readme": readmes[0].name, "tldr": tldr}


def detect_ci(root: Path) -> list[dict]:
    wf_dir = root / ".github" / "workflows"
    out = []
    if wf_dir.is_dir():
        for p in sorted(wf_dir.iterdir()):
            if p.suffix in (".yml", ".yaml") and p.is_file():
                name = None
                for ln in read_text(p, 20_000).splitlines():
                    m = re.match(r"^name:\s*(.+)$", ln.strip())
                    if m:
                        name = m.group(1).strip().strip('"\'')
                        break
                out.append({"file": p.name, "name": name})
    return out


def detect_docs(root: Path) -> dict:
    docs_dir = root / "docs"
    listing = []
    if docs_dir.is_dir():
        listing = sorted(p.name + ("/" if p.is_dir() else "")
                         for p in docs_dir.iterdir())[:20]
    return {"agents_md": (root / "AGENTS.md").is_file(),
            "docs_dir": listing}


def detect_build(root: Path, files: list[Path], stacks: dict) -> dict:
    inferred: list[str] = []
    if stacks.get("rust"):
        inferred += ["cargo build", "cargo test"]
    if stacks.get("dotnet"):
        inferred += ["dotnet build", "dotnet test"]
    if stacks.get("python"):
        has_tests = any(p.name.startswith("test_") or p.name.endswith("_test.py")
                        or "tests" in p.parts for p in files)
        if has_tests:
            inferred += ["python -m unittest discover", "pytest (if installed / configured)"]
        else:
            inferred += ["python -m unittest discover (no tests detected)"]
    if stacks.get("node"):
        for script in sorted(stacks["node"].get("scripts", {})):
            inferred.append(f"npm run {script}")
    if stacks.get("flutter"):
        inferred += ["flutter pub get", "flutter analyze", "flutter test (if test/ exists)"]
    extra = {"gate_files": [], "makefiles": []}
    for p in files:
        if p.name in ("gate.sh", "gate.ps1"):
            extra["gate_files"].append(rel(root, p))
        if p.name in ("Makefile", "makefile", "GNUmakefile"):
            targets = []
            for ln in read_text(p, 50_000).splitlines():
                m = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?![=])", ln)
                if m and not ln.startswith("\t") and m.group(1) not in ("PHONY",):
                    if m.group(1) not in targets:
                        targets.append(m.group(1))
            extra["makefiles"].append({"file": rel(root, p), "targets": targets})
    extra["gate_files"].sort()
    return {"inferred_commands": inferred, **extra}


def tree_summary(root: Path, files: list[Path], truncated: bool) -> dict:
    by_ext: dict[str, int] = {}
    total = 0
    sized = []
    for p in files:
        ext = p.suffix.lower() or "(no ext)"
        by_ext[ext] = by_ext.get(ext, 0) + 1
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        total += size
        sized.append((size, rel(root, p)))
    top_ext = sorted(by_ext.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
    largest = [{"path": path, "size_bytes": size, "size": _human(size)}
               for size, path in sorted(sized, reverse=True)[:5]]
    return {"file_count": len(files), "truncated": truncated,
            "total_size_bytes": total, "total_size": _human(total),
            "by_extension": [{"ext": e, "count": c} for e, c in top_ext],
            "largest_files": largest}


def conventions(root: Path) -> dict:
    return {name: (root / name).is_file() for name in
            [".editorconfig", "rustfmt.toml", ".gitattributes"]}


# ---------------------------------------------------------------- collect
def collect(root: Path, max_files: int = 400) -> dict:
    root = root.resolve()
    files, truncated = walk_files(root, max_files) if root.is_dir() else ([], False)
    git = git_info(root) if root.is_dir() else {"is_repo": False, "remote_origin": None,
                                               "branch": None, "head": None, "dirty_count": None}
    stacks: dict = {}
    for key, fn in (("rust", detect_rust), ("python", detect_python),
                    ("dotnet", detect_dotnet), ("node", detect_node),
                    ("flutter", detect_flutter), ("powershell", detect_powershell)):
        val = fn(root, files) if root.is_dir() else None
        if val is not None:
            stacks[key] = val
    docs_info = extract_tldr(root) if root.is_dir() else {"readme": None, "tldr": None}
    docs_extra = detect_docs(root) if root.is_dir() else {"agents_md": False, "docs_dir": []}
    return {
        "tool": "ContextPack", "version": __version__,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "identity": {"name": root.name, "path": str(root), **git},
        "license": detect_license(root, files) if root.is_dir() else
                   {"file": None, "first_line": None, "identifier": None},
        "stacks": stacks,
        "build": detect_build(root, files, stacks) if root.is_dir() else
                 {"inferred_commands": [], "gate_files": [], "makefiles": []},
        "ci": detect_ci(root) if root.is_dir() else [],
        "docs": {**docs_info, **docs_extra},
        "tree": tree_summary(root, files, truncated),
        "conventions": conventions(root) if root.is_dir() else
                       {".editorconfig": False, "rustfmt.toml": False, ".gitattributes": False},
    }


# ---------------------------------------------------------------- render
def _v(val, fallback="Unknown"):
    return fallback if val is None or val == "" else val


def render_markdown(d: dict) -> str:
    ident = d["identity"]
    head = ident.get("head") or "unknown (not a git repo / no HEAD)"
    lines: list[str] = []
    lines.append(f"# ContextPack Brief: {ident['name']}")
    lines.append("")
    lines.append(f"> Generated — verify before acting; license and versions "
                 f"read from files at {head}.")
    lines.append(f"> Generated by ContextPack {d['version']} at {d['generated_at']} (UTC).")
    lines.append("")
    lines.append("## Identity")
    lines.append(f"- Directory: `{ident['name']}`")
    lines.append(f"- Git remote (origin): {_v(ident.get('remote_origin'))}")
    lines.append(f"- Branch: {_v(ident.get('branch'))}")
    lines.append(f"- HEAD: {head}")
    lines.append(f"- Dirty files: {_v(ident.get('dirty_count'))}")
    lines.append("")
    lines.append("## License")
    lic = d["license"]
    if lic.get("file"):
        lines.append(f"- File: `{lic['file']}`")
        lines.append(f"- First line: {lic.get('first_line') or ''}")
        lines.append(f"- Identifier (from file text): **{_v(lic.get('identifier'))}**")
    else:
        lines.append("- Unknown — no LICENSE/LICENCE file found at repo root.")
    lines.append("")
    lines.append("## Stacks")
    stacks = d["stacks"]
    if not stacks:
        lines.append("- No known stack detected.")
    if "rust" in stacks:
        r = stacks["rust"]
        lines.append(f"- **Rust** — `{r['manifest']}`"
                     + (f" ({r['manifest_count']} Cargo.toml files)" if r["manifest_count"] > 1 else "")
                     + f": package `{_v(r['package'])}`, version {_v(r['version'])}, edition {_v(r['edition'])}, "
                       f"{r['dependencies_count']} direct dependencies in root manifest")
        if r.get("workspace_members"):
            lines.append(f"  - Workspace members: {', '.join(map(str, r['workspace_members']))}")
    if "python" in stacks:
        p = stacks["python"]
        bits = [f"{p['python_files']} .py files"]
        if p.get("name"):
            bits.insert(0, f"project `{p['name']}` v{_v(p['version'])}")
        if p.get("requires_python"):
            bits.append(f"requires-python {p['requires_python']}")
        if p.get("dependencies_count") is not None:
            bits.append(f"{p['dependencies_count']} dependencies in pyproject")
        lines.append("- **Python** — " + ", ".join(bits)
                     + f"; pyproject.toml: {'yes' if p['pyproject'] else 'no'}, "
                       f"setup.py: {'yes' if p['setup_py'] else 'no'}, "
                       f"requirements.txt: {'yes' if p['requirements_txt'] else 'no'}"
                     + (f" ({p['requirements_count']} requirement lines)" if p.get("requirements_count") is not None else ""))
    if "dotnet" in stacks:
        lines.append("- **.NET**")
        for pr in stacks["dotnet"]["projects"]:
            lines.append(f"  - `{pr['file']}` — TargetFramework: {_v(pr['target_framework'])}")
        for s in stacks["dotnet"]["solutions"]:
            lines.append(f"  - Solution: `{s}`")
    if "node" in stacks:
        n = stacks["node"]
        lines.append(f"- **Node** — `package.json`: name `{_v(n['name'])}`, version {_v(n['version'])}")
        if n.get("scripts"):
            lines.append(f"  - Scripts: {', '.join(sorted(n['scripts']))}")
    if "flutter" in stacks:
        f = stacks["flutter"]
        lines.append(f"- **Flutter/Dart** — pubspec name `{_v(f['name'])}`, "
                     f"SDK constraint {_v(f['sdk_constraint'])}, {f['dart_files']} .dart files")
    if "powershell" in stacks:
        ps = stacks["powershell"]
        lines.append(f"- **PowerShell** — {ps['script_count']} .ps1 file(s): "
                     + ", ".join(f"`{s}`" for s in ps["scripts"]))
    lines.append("")
    lines.append("## Build / Test / Gate (inferred — verify against repo docs)")
    build = d["build"]
    if build["inferred_commands"]:
        for cmd in build["inferred_commands"]:
            lines.append(f"- `{cmd}`")
    else:
        lines.append("- No build/test commands inferred.")
    for g in build["gate_files"]:
        lines.append(f"- Gate file found: `{g}`")
    for mk in build["makefiles"]:
        targets = ", ".join(mk["targets"]) if mk["targets"] else "none parsed"
        lines.append(f"- Makefile: `{mk['file']}` — targets: {targets}")
    lines.append("")
    lines.append("## CI")
    if d["ci"]:
        for wf in d["ci"]:
            lines.append(f"- `{wf['file']}`" + (f" — name: {wf['name']}" if wf.get("name") else ""))
    else:
        lines.append("- No workflows found under `.github/workflows/`.")
    lines.append("")
    lines.append("## Docs")
    docs = d["docs"]
    lines.append(f"- README: {_v(docs.get('readme'))}")
    lines.append(f"- AGENTS.md: {'present' if docs.get('agents_md') else 'absent'}")
    if docs.get("docs_dir"):
        lines.append("- `docs/` (top level): " + ", ".join(docs["docs_dir"]))
    else:
        lines.append("- `docs/`: absent or empty")
    if docs.get("tldr"):
        lines.append("")
        lines.append("### README TL;DR")
        lines.append("")
        lines.append(docs["tldr"])
    else:
        lines.append("- README TL;DR: not found (no `## TL;DR` section)")
    lines.append("")
    lines.append("## Tree Summary")
    tree = d["tree"]
    lines.append(f"- Files scanned: {tree['file_count']}"
                 + (" (truncated at --max-files)" if tree["truncated"] else "")
                 + f"; total size: {tree['total_size']}")
    if tree["by_extension"]:
        lines.append("- By extension: " + ", ".join(
            f"{e['ext']} ×{e['count']}" for e in tree["by_extension"]))
    if tree["largest_files"]:
        lines.append("- Largest files:")
        for lf in tree["largest_files"]:
            lines.append(f"  - `{lf['path']}` — {lf['size']}")
    lines.append("")
    lines.append("## Conventions")
    conv = d["conventions"]
    present = [k for k, v in conv.items() if v]
    lines.append("- Present: " + (", ".join(f"`{p}`" for p in present) if present else "none of the checked files"))
    lines.append("- Checked: " + ", ".join(f"`{k}`" for k in conv))
    lines.append("")
    lines.append("## Notes")
    lines.append("- Inference is heuristic. Commands above are suggestions — "
                 "confirm them against the repo's own README/AGENTS.md before running.")
    lines.append("- License and versions are read from files in this checkout only; "
                 "they are never assumed from another project.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="contextpack", description=__doc__)
    parser.add_argument("--version", action="version", version=f"ContextPack {__version__}")
    sub = parser.add_subparsers(dest="command")
    pack = sub.add_parser("pack", help="Generate a brief for a repo directory")
    pack.add_argument("repo_dir")
    pack.add_argument("--out", default=None, help="Write Markdown brief here (default: stdout)")
    pack.add_argument("--json", dest="json_out", default=None, help="Also write JSON brief here")
    pack.add_argument("--max-files", type=int, default=400)
    args = parser.parse_args(argv)
    if args.command != "pack":
        parser.print_help()
        return 2
    root = Path(args.repo_dir)
    if not root.is_dir():
        print(f"warning: {root} is not a directory — producing an empty brief", file=sys.stderr)
    data = collect(root, max_files=max(1, args.max_files))
    md = render_markdown(data)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(md, end="")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
