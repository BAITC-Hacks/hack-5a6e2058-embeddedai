"""Build a reproducible LOCAL jury archive, including authorized organizer data.

No uploads or network calls. Secrets, machine state and dependency caches are
excluded by an explicit file/tree allowlist; the manifest describes this working
tree, including uncommitted changes, rather than claiming a signed release.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

ROOT_FILES = (
    "README.md",
    "analyze.py",
    "pyproject.toml",
    "uv.lock",
    "requirements.txt",
    ".python-version",
    ".env.example",
    ".gitignore",
    ".gitattributes",
    "LICENSE",
    "LICENSE.md",
    "Dockerfile",
    "Makefile",
)
TREES = ("src", "config", "scripts", "tests", "docs", "deploy", "web")
DATA_FILES = (
    "data/README.md",
    "data/ТЗ.md",
    "data/data/nodes.parquet",
    "data/data/edges.parquet",
    "data/data/transactions.parquet",
    "data/starter/README.md",
    "data/starter/starter.py",
    "data/starter/requirements.txt",
)
ARTIFACTS = (
    "nodes_roles.csv",
    "clusters.csv",
    "top_nodes.csv",
    "run_report.json",
    "result.json",
    "report.html",
)
SKIP_DIRS = {
    "private",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "cache",
    ".cache",
    "test-results",
    "playwright-report",
    "coverage",
    "htmlcov",
    ".next",
    ".idea",
    ".vscode",
}
SUFFIXES = {
    ".py",
    ".json",
    ".md",
    ".txt",
    ".toml",
    ".lock",
    ".yml",
    ".yaml",
    ".ts",
    ".tsx",
    ".js",
    ".css",
    ".html",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".ico",
    ".sh",
    ".ps1",
    ".csv",
    ".pdf",
    ".map",
}
SECRET_NAME = re.compile(
    r"(?:^|[._-])(?:secrets?|credentials?|passwords?|tokens?|api[_-]?key|apikey|id_rsa|id_ed25519|hetzner_vps)(?:[._-]|$)",
    re.I,
)
PREFIX = "money-graph/"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)


class PackageError(ValueError):
    """Packaging cannot establish the expected, non-secret local input set."""


def _regular(root: Path, relative: str) -> Path:
    path = root / relative
    if any(part.is_symlink() for part in (path, *path.parents) if part != root.parent):
        raise PackageError(f"Обязательный файл не должен быть символической ссылкой: {relative}")
    if not path.is_file():
        raise PackageError(f"Отсутствует обязательный файл: {relative}")
    return path


def _sensitive(path: Path) -> bool:
    name = path.name.lower()
    return (
        (name.startswith(".env") and name != ".env.example")
        or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".keystore"}
        or SECRET_NAME.search(name) is not None
    )


def _tree(path: Path) -> list[Path]:
    if path.is_symlink() or not path.is_dir():
        return []
    files = []
    for child in sorted(path.iterdir()):
        if child.is_symlink():
            continue
        if child.is_dir():
            if child.name not in SKIP_DIRS and not child.name.endswith(".egg-info"):
                files.extend(_tree(child))
        elif child.is_file():
            if _sensitive(child):
                raise PackageError(
                    f"Чувствительное имя файла внутри включаемого каталога: {child.name}"
                )
            if child.name in {"AGENTS.md", ".DS_Store", "Thumbs.db"}:
                continue
            if child.suffix.lower() in SUFFIXES or child.name == ".gitignore":
                files.append(child)
    return files


def _git(root: Path) -> dict[str, Any]:
    try:

        def command(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(root), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()

        if Path(command("rev-parse", "--show-toplevel")).resolve() != root:
            raise ValueError
        return {
            "source_git_commit": command("rev-parse", "HEAD"),
            "git_dirty": bool(command("status", "--porcelain", "--untracked-files=normal")),
        }
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"source_git_commit": None, "git_dirty": None}


def _check(root: Path) -> dict[str, Any]:
    # The checker itself is stdlib-only unless its optional raw-Parquet mode is used.
    path = _regular(root, "scripts/check_submission.py")
    spec = importlib.util.spec_from_file_location("jury_package_checker", path)
    if spec is None or spec.loader is None:
        raise PackageError("Не удалось открыть независимый проверяющий скрипт")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check_submission(root / "artifacts")


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _inputs(root: Path) -> tuple[dict[str, Path], dict[str, Any]]:
    required = [
        "README.md",
        "analyze.py",
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        ".python-version",
        ".env.example",
        "config/rules.json",
        "src/money_graph/pipeline.py",
        "web/dist/index.html",
        "docs/SOLUTION.svg",
        "output/submission/SOLUTION.pdf",
        "output/submission/START_HERE.md",
        "scripts/check_submission.py",
        *[f"artifacts/{name}" for name in ARTIFACTS],
        *[f"data/data/{name}.parquet" for name in ("nodes", "edges", "transactions")],
    ]
    for relative in required:
        _regular(root, relative)
    cards = [p for p in _tree(root / "output/submission/node_cards") if p.suffix == ".md"]
    if len(cards) < 2:
        raise PackageError("Для демо нужны минимум две справки .md в output/submission/node_cards")
    example = (root / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            if SECRET_NAME.search(key.strip()) and value.strip().strip("\"'"):
                raise PackageError("В .env.example значение секрета должно быть пустым")
    checked = _check(root)
    report = json.loads((root / "artifacts/run_report.json").read_text(encoding="utf-8"))
    raw_hashes = {}
    for name in ("nodes", "edges", "transactions"):
        raw_hashes[name] = _digest(root / f"data/data/{name}.parquet")
        if report.get("input_sha256", {}).get(name) != raw_hashes[name]:
            raise PackageError(
                f"Исходный {name}.parquet не соответствует SHA-256 отчёта; пересчитайте артефакты"
            )
    files = {
        name: root / name
        for name in ROOT_FILES
        if (root / name).is_file() and not (root / name).is_symlink()
    }
    for tree in TREES:
        for path in _tree(root / tree):
            files[path.relative_to(root).as_posix()] = path
    files.update({name: _regular(root, name) for name in DATA_FILES if (root / name).exists()})
    files.update({f"artifacts/{name}": root / "artifacts" / name for name in ARTIFACTS})
    for path in _tree(root / "output/submission"):
        relative = path.relative_to(root / "output/submission").as_posix()
        files[f"submission/{relative}"] = path
    return files, {"check": checked, "raw_sha256": raw_hashes}


def package_submission(root: Path, destination: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    destination = destination or root / "output/money-graph-submission.zip"
    destination = destination.absolute()
    if destination.suffix.lower() != ".zip" or destination.is_symlink():
        raise PackageError("Выход должен быть обычным файлом .zip, не символической ссылкой")
    if any(parent.is_symlink() for parent in destination.parents):
        raise PackageError("Выходной каталог не должен проходить через символическую ссылку")
    files, validation = _inputs(root)
    if any(path.resolve() == destination for path in files.values()):
        raise PackageError("Архив не должен заменять входной файл")
    provenance = _git(root)
    with (root / "pyproject.toml").open("rb") as source:
        app_version = tomllib.load(source)["project"]["version"]
    rules_version = json.loads((root / "config/rules.json").read_text(encoding="utf-8"))["version"]
    manifest = {
        "manifest_version": 1,
        "archive_root": PREFIX.rstrip("/"),
        "purpose": "Local jury submission; contains organizer data. No upload performed.",
        "source_tree": "current_working_tree",
        **provenance,
        "app_version": app_version,
        "rules_version": rules_version,
        "csv_checks": validation["check"]["checks"],
        "raw_sha256": validation["raw_sha256"],
        "entries": [],
        "provenance_note": "Hashes describe packaged bytes; this manifest is not a signature. A commit does not imply uncommitted working-tree files belong to that commit.",
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".jury-package-", suffix=".zip", dir=destination.parent, delete=False
        ) as holder:
            temporary = Path(holder.name)
        entries: list[dict[str, Any]] = []
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for relative, path in sorted(files.items()):
                before = path.stat()
                if path.is_symlink():
                    raise PackageError(f"Файл заменён ссылкой во время упаковки: {relative}")
                mode = 0o755 if path.suffix == ".sh" else 0o644
                info = zipfile.ZipInfo(PREFIX + relative, ZIP_DATE)
                info.create_system = 3
                info.external_attr = (0o100000 | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                digest, size = hashlib.sha256(), 0
                with path.open("rb") as source, archive.open(info, "w") as target:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                after = path.stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise PackageError(
                        f"Файл изменился во время упаковки: {relative}; повторите после сохранения"
                    )
                entries.append(
                    {
                        "path": relative,
                        "size": size,
                        "sha256": digest.hexdigest(),
                        "mode": oct(mode),
                    }
                )
            hashes = {entry["path"]: entry["sha256"] for entry in entries}
            for name, details in validation["check"]["exports"].items():
                if hashes[f"artifacts/{name}"] != details["sha256"]:
                    raise PackageError(
                        f"{name} изменился после проверки; запустите упаковку повторно"
                    )
            for name, checksum in validation["raw_sha256"].items():
                if hashes[f"data/data/{name}.parquet"] != checksum:
                    raise PackageError(f"{name}.parquet изменился после проверки")
            if _git(root) != provenance:
                raise PackageError("Состояние Git изменилось во время упаковки; повторите команду")
            manifest["entries"] = entries
            payload = (
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            ).encode()
            info = zipfile.ZipInfo(PREFIX + "manifest.json", ZIP_DATE)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
        os.replace(temporary, destination)
        return {
            "status": "success",
            "path": str(destination),
            "files": len(files),
            "sha256": _digest(destination),
            "manifest_sha256": hashlib.sha256(payload).hexdigest(),
            **provenance,
        }
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Локальный ZIP для жюри; включает данные организаторов, ничего не публикует"
    )
    parser.add_argument(
        "--out", type=Path, help="Выходной ZIP (по умолчанию output/money-graph-submission.zip)"
    )
    args = parser.parse_args(argv)
    try:
        result = package_submission(Path(__file__).resolve().parents[1], args.out)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        print(
            json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr
        )
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
