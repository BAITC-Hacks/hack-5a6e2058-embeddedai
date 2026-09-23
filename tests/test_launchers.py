import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from money_graph.demo import create_demo

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("entry", ["script", "module"])
def test_files_to_csv_entrypoints_work_without_node_or_uv_on_path(tmp_path, entry):
    source = tmp_path / "input"
    create_demo(source)
    command = [sys.executable]
    command += [str(ROOT / "analyze.py")] if entry == "script" else ["-m", "money_graph", "analyze"]
    command += ["--data", str(source), "--out", str(tmp_path / "output")]
    process = subprocess.run(
        command,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=os.environ | {"PATH": "", "PYTHONUTF8": "1"},
    )
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["n_nodes"] == 80
    assert all(
        (tmp_path / "output" / filename).is_file()
        for filename in ("nodes_roles.csv", "clusters.csv", "top_nodes.csv")
    )


def test_script_without_installed_packages_has_an_actionable_error(tmp_path):
    process = subprocess.run(
        [sys.executable, "-S", str(ROOT / "analyze.py"), "--data", "missing"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=os.environ | {"PYTHONUTF8": "1"},
    )
    assert process.returncode == 2
    assert "uv run --frozen --no-dev" in process.stderr
    assert "Traceback" not in process.stderr


def test_json_and_csv_are_utf8_under_legacy_process_encodings(tmp_path):
    source = tmp_path / "input"
    create_demo(source)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "analyze.py"),
            "--data",
            str(source),
            "--out",
            str(tmp_path / "output"),
        ],
        cwd=tmp_path,
        capture_output=True,
        env=os.environ
        | {
            "LC_ALL": "C",
            "PYTHONCOERCECLOCALE": "0",
            "PYTHONUTF8": "0",
            "PYTHONIOENCODING": "cp1252",
        },
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert json.loads(result.stdout.decode("utf-8"))["n_nodes"] == 80
    report = json.loads((tmp_path / "output/run_report.json").read_text(encoding="utf-8"))
    assert "Роли" in " ".join(report["warnings"])
    assert "≥" in (tmp_path / "output/nodes_roles.csv").read_text(encoding="utf-8")


def test_pip_requirements_are_the_exact_runtime_export_of_uv_lock():
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("Lockfile export consistency is checked by the uv verification command")
    result = subprocess.run(
        [
            uv,
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--no-header",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    while lines and lines[0].startswith("#"):
        lines.pop(0)
    assert "\n".join(lines).strip() == result.stdout.strip()


@pytest.fixture()
def runners(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("_runner"), importlib.import_module("verify")


def test_backend_gate_never_looks_for_node(runners, monkeypatch):
    _, verify = runners
    commands = []
    monkeypatch.setattr(verify, "run", commands.append)

    def no_node():
        raise AssertionError("Node is not required for --backend-only")

    monkeypatch.setattr(verify, "require_node", no_node)
    assert verify.main(["--backend-only"]) == 0
    assert all(command[0] == "uv" for command in commands)
    assert any("pytest" in command for command in commands)


def test_missing_uv_is_explained(runners, monkeypatch):
    runner, _ = runners
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    with pytest.raises(runner.RunnerError, match="Установите uv") as error:
        runner.run(["uv", "sync", "--frozen"])
    assert error.value.code == 2


def test_quality_gate_stops_and_preserves_failed_child_exit_code(runners, monkeypatch, capsys):
    runner, verify = runners
    commands = []

    def fail(command):
        commands.append(command)
        raise runner.RunnerError("check failed", code=7)

    monkeypatch.setattr(verify, "run", fail)
    assert verify.main(["--backend-only"]) == 7
    assert len(commands) == 1
    assert "check failed" in capsys.readouterr().err


def test_runner_propagates_real_subprocess_failure(runners, monkeypatch):
    runner, _ = runners
    monkeypatch.setattr(runner, "executable", lambda name: sys.executable)
    with pytest.raises(runner.RunnerError) as error:
        runner.run(["python", "-c", "raise SystemExit(7)"])
    assert error.value.code == 7


@pytest.mark.parametrize(
    "version,valid",
    [("v20.19.0", False), ("v22.11.0", False), ("v22.12.0", True), ("v24.0.0", True)],
)
def test_web_runner_checks_node_requirement(runners, monkeypatch, version, valid):
    runner, _ = runners
    monkeypatch.setattr(runner, "executable", lambda name: name)
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, version),
    )
    if valid:
        runner.require_node()
    else:
        with pytest.raises(runner.RunnerError, match="22.12"):
            runner.require_node()
