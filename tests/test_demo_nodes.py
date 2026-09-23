import json
import runpy
from pathlib import Path

import pytest

from money_graph.demo import create_demo
from money_graph.pipeline import analyze


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    directory = tmp_path_factory.mktemp("demo-cards")
    create_demo(directory / "input")
    result = analyze(directory / "input")
    path = directory / "result.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    return path, result


@pytest.fixture()
def command():
    return runpy.run_path(str(Path(__file__).parents[1] / "scripts/demo_nodes.py"))["main"]


def test_arbitrary_three_gid_cards_preserve_id_rules_and_provenance(command, prepared, tmp_path):
    path, result = prepared
    nodes = sorted(result["nodes"], key=lambda node: node["rank"])[::25][:3]
    before = path.read_bytes()
    args = ["--result", str(path), "--out", str(tmp_path)]
    for node in nodes:
        args += ["--gid", node["gid"]]
    assert command(args) == 0
    assert path.read_bytes() == before
    for node in nodes:
        card = (tmp_path / f"{node['gid']}.md").read_text(encoding="utf-8")
        assert f"# Узел {node['gid']}" in card
        assert str(node["role_score"]) in card
        assert "P(betweenness) ≥ 0.9" in card
        assert "не является вероятностью виновности" in card
        assert result["report"]["input_sha256"]["nodes"] in card
        assert "не доказательство последовательности" in card


def test_unknown_gid_produces_no_partial_cards(command, prepared, tmp_path, capsys):
    path, result = prepared
    destination = tmp_path / "cards"
    assert (
        command(
            [
                "--result",
                str(path),
                "--out",
                str(destination),
                "--gid",
                result["nodes"][0]["gid"],
                "--gid",
                "123456789",
            ]
        )
        == 2
    )
    assert not destination.exists()
    assert "Неизвестные gid" in capsys.readouterr().err


def test_stdout_card_works_without_destination_and_rejects_corrupt_input(
    command, prepared, tmp_path, capsys
):
    path, result = prepared
    gid = result["nodes"][0]["gid"]
    assert command(["--result", str(path), "--gid", gid]) == 0
    assert f"# Узел {gid}" in capsys.readouterr().out
    corrupt = tmp_path / "invalid.json"
    corrupt.write_text("{}", encoding="utf-8")
    assert command(["--result", str(corrupt), "--gid", gid]) == 2
    assert "Не удалось подготовить карточки" in capsys.readouterr().err
