"""Local jury archives contain reproducible evidence, not the author's machine."""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from money_graph.demo import create_demo
from money_graph.pipeline import analyze, read_rules, write_result

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "jury_package", ROOT / "scripts/package_submission.py"
)
assert spec is not None and spec.loader is not None
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


def put(root, relative, content="file contents\n"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    root = tmp_path_factory.mktemp("jury-package-base")
    create_demo(root / "data/data")
    write_result(analyze(root / "data/data"), root / "artifacts")
    for relative in (
        "README.md",
        "analyze.py",
        "uv.lock",
        "requirements.txt",
        ".python-version",
        "src/money_graph/pipeline.py",
        "web/dist/index.html",
        "web/dist/assets/app.js",
        "web/src/main.ts",
        "web/package.json",
        "deploy/money-graph.service",
        ".github/workflows/verify.yml",
        "docs/SOLUTION.svg",
        "output/submission/SOLUTION.pdf",
        "output/submission/START_HERE.md",
        "output/submission/node_cards/one.md",
        "output/submission/node_cards/two.md",
    ):
        put(root, relative)
    put(root, ".env.example", "OPENAI_API_KEY=\nOPENAI_MODEL=example\n")
    put(root, "pyproject.toml", '[project]\nname="example"\nversion="0.8.0"\n')
    put(root, "config/rules.json", json.dumps(read_rules()))
    put(root, ".gitignore", "output/\n__pycache__/\n")
    (root / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/check_submission.py", root / "scripts/check_submission.py")
    shutil.copy(ROOT / "scripts/package_submission.py", root / "scripts/package_submission.py")
    return root


@pytest.fixture
def project(tmp_path, baseline):
    root = tmp_path / "project"
    shutil.copytree(baseline, root)
    return root


def test_archive_is_reproducible_and_manifest_matches_every_extracted_file(project, tmp_path):
    first = packager.package_submission(project)
    destination = Path(first["path"])
    original = destination.read_bytes()
    second = packager.package_submission(project)
    assert destination.read_bytes() == original
    assert first["sha256"] == second["sha256"] == hashlib.sha256(original).hexdigest()
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert all(name.startswith("money-graph/") for name in names)
        archive.extractall(tmp_path / "extracted")
        manifest = json.loads(archive.read("money-graph/manifest.json"))
        assert manifest["app_version"] == "0.8.0" and manifest["rules_version"] == "2.2"
        assert manifest["source_tree"] == "current_working_tree"
        assert manifest["source_git_commit"] is None and manifest["git_dirty"] is None
        assert {entry["path"] for entry in manifest["entries"]} == {
            name.removeprefix("money-graph/")
            for name in names
            if name != "money-graph/manifest.json"
        }
        for entry in manifest["entries"]:
            data = (tmp_path / "extracted/money-graph" / entry["path"]).read_bytes()
            assert len(data) == entry["size"]
            assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert "money-graph/submission/START_HERE.md" in names
        assert "money-graph/web/dist/assets/app.js" in names
        assert "money-graph/data/data/nodes.parquet" in names
        assert "money-graph/deploy/money-graph.service" in names
        assert "money-graph/.github/workflows/verify.yml" in names
    # The independent check still runs with site-packages disabled after extraction.
    checked = subprocess.run(
        [
            sys.executable,
            "-S",
            str(tmp_path / "extracted/money-graph/scripts/check_submission.py"),
            "--out",
            str(tmp_path / "extracted/money-graph/artifacts"),
        ],
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["status"] == "success"


def test_private_caches_env_and_symlink_targets_never_enter_archive(project, tmp_path):
    for relative in (
        "private/openai_api_key.txt",
        ".env",
        ".git/config",
        ".venv/site.py",
        "web/node_modules/vendor/index.js",
        "scripts/__pycache__/secret.pyc",
        "docs/private/password.txt",
        "web/test-results/report.json",
        "data/unapproved.md",
        "AGENTS.md",
        "output/other-proof.json",
    ):
        put(project, relative, "must-not-be-packaged")
    outside = put(tmp_path, "outside.txt", "external sensitive content")
    (project / "docs/symlink.txt").symlink_to(outside)
    result = packager.package_submission(project)
    with zipfile.ZipFile(result["path"]) as archive:
        names = archive.namelist()
        assert "money-graph/.env.example" in names
        assert not any(
            any(
                part in {"private", ".git", ".venv", "node_modules", "__pycache__", "test-results"}
                for part in Path(name).parts
            )
            for name in names
        )
        assert "money-graph/.env" not in names
        assert "money-graph/AGENTS.md" not in names
        assert "money-graph/docs/symlink.txt" not in names
        assert "money-graph/data/unapproved.md" not in names
        assert not any(b"must-not-be-packaged" in archive.read(name) for name in names)


@pytest.mark.parametrize(
    "relative",
    ["docs/api_key.txt", "web/credentials.json", "scripts/.env.local", "docs/identity.pem"],
)
def test_sensitive_names_inside_allowlisted_tree_fail_closed(project, relative):
    put(project, relative)
    with pytest.raises(packager.PackageError, match="Чувствительное"):
        packager.package_submission(project)


@pytest.mark.parametrize(
    "relative",
    [
        "artifacts/report.html",
        "artifacts/top_nodes.csv",
        "artifacts/result.json",
        "web/dist/index.html",
        "output/submission/SOLUTION.pdf",
        "output/submission/START_HERE.md",
    ],
)
def test_missing_required_artifact_cannot_replace_previous_archive(project, relative):
    destination = project / "output/previous.zip"
    destination.write_bytes(b"old-archive")
    (project / relative).unlink()
    with pytest.raises(packager.PackageError, match="Отсутствует"):
        packager.package_submission(project, destination)
    assert destination.read_bytes() == b"old-archive"
    assert not list(destination.parent.glob(".jury-package-*"))


def test_stale_raw_inputs_or_invalid_csv_are_rejected_before_publication(project):
    raw = project / "data/data/nodes.parquet"
    raw.write_bytes(raw.read_bytes() + b"changed")
    with pytest.raises(packager.PackageError, match="SHA-256"):
        packager.package_submission(project)
    raw.write_bytes(raw.read_bytes().removesuffix(b"changed"))
    (project / "artifacts/nodes_roles.csv").write_text("invalid,csv\n", encoding="utf-8")
    with pytest.raises(ValueError, match="схема"):
        packager.package_submission(project)


def test_git_provenance_records_uncommitted_worktree_bytes(project):
    if shutil.which("git") is None:
        pytest.skip("git is optional for package creation")

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(project), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init")
    git("add", ".")
    git(
        "-c",
        "user.name=Package Test",
        "-c",
        "user.email=package@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    commit = git("rev-parse", "HEAD")
    put(project, "README.md", "UNCOMMITTED CURRENT README")
    result = packager.package_submission(project)
    with zipfile.ZipFile(result["path"]) as archive:
        manifest = json.loads(archive.read("money-graph/manifest.json"))
        assert manifest["source_git_commit"] == commit and manifest["git_dirty"] is True
        assert archive.read("money-graph/README.md") == b"UNCOMMITTED CURRENT README"


def test_zip_replacement_is_atomic_on_write_failure(project, monkeypatch):
    destination = project / "output/archive.zip"
    destination.write_bytes(b"previous-success")

    def failure(*args, **kwargs):
        raise OSError("simulated full disk")

    monkeypatch.setattr(packager.os, "replace", failure)
    with pytest.raises(OSError, match="full disk"):
        packager.package_submission(project, destination)
    assert destination.read_bytes() == b"previous-success"
    assert not list(destination.parent.glob(".jury-package-*"))


def test_required_symlink_and_secret_in_example_are_rejected(project, tmp_path):
    destination = project / "output/submission/SOLUTION.pdf"
    destination.unlink()
    destination.symlink_to(put(tmp_path, "slide.pdf"))
    with pytest.raises(packager.PackageError, match="ссылкой"):
        packager.package_submission(project)
    destination.unlink()
    destination.write_text("slide")
    put(project, ".env.example", "OPENAI_API_KEY=should-not-be-distributed\n")
    with pytest.raises(packager.PackageError, match="секрета"):
        packager.package_submission(project)
