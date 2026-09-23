import hashlib
import json
import shutil
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
        try:
            output.symlink_to(source, target_is_directory=True)
        except OSError:
            pytest.skip("Creating symlinks is not permitted on this platform")
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


def test_explicit_file_paths_match_directory_mode_and_preserve_source_hashes(tmp_path, source):
    directory_out = tmp_path / "from-directory"
    main(["analyze", "--data", str(source), "--out", str(directory_out)])
    renamed = tmp_path / "Данные с пробелами"
    renamed.mkdir()
    args = ["analyze", "--out", str(tmp_path / "from-files")]
    expected = {}
    for name in ("nodes", "edges", "transactions"):
        path = renamed / f"chosen {name}.parquet"
        shutil.copyfile(source / f"{name}.parquet", path)
        args.extend([f"--{name}", str(path)])
        expected[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    main(args)
    output = tmp_path / "from-files"
    for name in ("nodes_roles.csv", "clusters.csv", "top_nodes.csv"):
        assert (output / name).read_bytes() == (directory_out / name).read_bytes()
    assert (
        json.loads((output / "run_report.json").read_text(encoding="utf-8"))["input_sha256"]
        == expected
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["--nodes", "nodes.parquet"],
        ["--nodes", "nodes.parquet", "--edges", "edges.parquet"],
        ["--edges", "edges.parquet", "--transactions", "transactions.parquet"],
        ["--data", "data", "--nodes", "nodes.parquet"],
        ["--data", "data", "--edges", "edges.parquet"],
        ["--data", "data", "--transactions", "transactions.parquet"],
    ],
)
def test_input_modes_are_exclusive_and_explicit_mode_requires_all_three_files(arguments, capsys):
    with pytest.raises(SystemExit) as error:
        main(["analyze", *arguments])
    assert error.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_explicit_output_protects_original_files_before_staging(tmp_path, source, monkeypatch):
    def forbidden_copy(*args, **kwargs):
        raise AssertionError("Input copying must not start for a destructive output path")

    monkeypatch.setattr("money_graph.cli.shutil.copyfile", forbidden_copy)
    arguments = ["analyze", "--out", str(source)]
    for name in ("nodes", "edges", "transactions"):
        arguments.extend([f"--{name}", str(source / f"{name}.parquet")])
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2
    assert (source / "nodes.parquet").exists()


def test_explicit_missing_input_is_an_actionable_error(tmp_path, source, capsys):
    arguments = ["analyze", "--out", str(tmp_path / "output")]
    for name in ("nodes", "edges", "transactions"):
        arguments.extend([f"--{name}", str(source / f"{name}.parquet")])
    (source / "transactions.parquet").unlink()
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2
    assert "--transactions" in capsys.readouterr().err
