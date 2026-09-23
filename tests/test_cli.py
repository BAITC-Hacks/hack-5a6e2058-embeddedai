import json
import sys
from pathlib import Path

import pytest

from money_graph.cli import main
from money_graph.demo import create_demo


def invoke(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["money-graph", *(str(arg) for arg in args)])
    main()


@pytest.fixture()
def source(tmp_path):
    directory = tmp_path / "input"
    create_demo(directory)
    return directory


@pytest.mark.parametrize("target", ["source", "parent", "cwd", "unrelated", "symlink", "file"])
def test_analyze_refuses_destructive_output_paths(tmp_path, monkeypatch, source, target):
    work = tmp_path / "repository"
    work.mkdir()
    marker = work / "README.md"
    marker.write_text("valuable source code")
    monkeypatch.chdir(work)
    if target == "source":
        output = source
    elif target == "parent":
        output = source.parent
    elif target == "cwd":
        output = Path(".")
    elif target == "unrelated":
        output = tmp_path / "other"
        output.mkdir()
        (output / "notes.txt").write_text("preserve")
    elif target == "symlink":
        output = tmp_path / "linked-output"
        output.symlink_to(source, target_is_directory=True)
    else:
        output = tmp_path / "existing-file"
        output.write_text("preserve")
    original = (source / "nodes.parquet").read_bytes()
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "analyze", "--data", source, "--out", output)
    assert error.value.code == 2
    assert marker.read_text() == "valuable source code"
    assert (source / "nodes.parquet").read_bytes() == original


def test_demo_cannot_overwrite_real_input_files(monkeypatch, source):
    original = (source / "nodes.parquet").read_bytes()
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "demo", "--out", source)
    assert error.value.code == 2
    assert (source / "nodes.parquet").read_bytes() == original


def test_previous_exports_can_be_replaced_and_bad_port_env_does_not_break_analyze(
    tmp_path, monkeypatch, source
):
    output = tmp_path / "output"
    monkeypatch.setenv("PORT", "invalid-for-serve")
    invoke(monkeypatch, "analyze", "--data", source, "--out", output)
    original = (output / "nodes_roles.csv").read_bytes()
    invoke(monkeypatch, "analyze", "--data", source, "--out", output)
    assert (output / "nodes_roles.csv").read_bytes() == original
    assert json.loads((output / "run_report.json").read_text())["n_nodes"] == 80


@pytest.mark.parametrize("port", ["broken", "0", "-1", "65536"])
def test_bad_port_is_an_actionable_argument_error(monkeypatch, capsys, port):
    monkeypatch.setenv("PORT", port)
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "serve")
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "PORT/--port" in stderr and "Traceback" not in stderr


def test_custom_rules_are_not_removed_with_output_directory(tmp_path, monkeypatch, source):
    output = tmp_path / "output"
    output.mkdir()
    rules = output / "rules.json"
    rules.write_text("{}")
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "analyze", "--data", source, "--out", output, "--rules", rules)
    assert error.value.code == 2
    assert rules.read_text() == "{}"
