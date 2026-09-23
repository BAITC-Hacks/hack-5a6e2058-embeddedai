from pathlib import Path

import networkx as nx
import pandas as pd
import pytest

from money_graph.demo import create_demo
from money_graph.features import projection
from money_graph.loader import DataError, load
from money_graph.pipeline import EXPORTS, analyze, read_rules, write_result
from money_graph.roles import classify


@pytest.fixture()
def dataset(tmp_path):
    create_demo(tmp_path)
    return tmp_path


def test_complete_deterministic_exports_and_big_ids(dataset, tmp_path):
    result = analyze(dataset)
    original = pd.read_parquet(dataset / "nodes.parquet")
    assert {row["gid"] for row in result["nodes"]} == set(original.gid.map(str))
    assert all(isinstance(row["gid"], str) for row in result["nodes"])
    assert max(int(row["gid"]) for row in result["nodes"]) > 2**53
    assert len(result["top"]) == 50
    assert sum(c["n_nodes"] for c in result["clusters"]) == len(original)
    assert sum(c["n_seed"] for c in result["clusters"]) == int(original.is_seed.sum())
    a, b = tmp_path / "a", tmp_path / "b"
    write_result(result, a)
    shuffled = tmp_path / "shuffled"
    shuffled.mkdir()
    for name in ("nodes", "edges", "transactions"):
        pd.read_parquet(dataset / f"{name}.parquet").sample(frac=1, random_state=7).to_parquet(
            shuffled / f"{name}.parquet", index=False
        )
    write_result(analyze(shuffled), b)
    for name in EXPORTS:
        assert (a / name).read_bytes() == (b / name).read_bytes()
    exported = pd.read_csv(a / "nodes_roles.csv", dtype={"gid": "str"})
    assert set(exported.gid) == set(original.gid.map(str))
    for row in result["nodes"]:
        assert 0 <= row["role_score"] <= 1 and 0 <= row["priority_score"] <= 1
        assert 0 < len(row["evidence"]) <= 200
        if row["isolated"]:
            assert row["priority_score"] == 0 and row["role"] == "peripheral"
        if row["boundary_censored"]:
            assert row["role"] != "terminal"


def test_validator_catches_amount_and_count_mismatch(dataset):
    path = dataset / "edges.parquet"
    edges = pd.read_parquet(path)
    edges.loc[0, "sum_kzt"] += 1
    edges.to_parquet(path)
    with pytest.raises(DataError, match="Суммы"):
        load(dataset)
    edges.loc[0, "sum_kzt"] -= 1
    edges.loc[0, "n_tx"] += 1
    edges.to_parquet(path)
    with pytest.raises(DataError, match="Количество"):
        load(dataset)


def test_validator_never_coerces_float_identifiers(dataset):
    path = dataset / "nodes.parquet"
    nodes = pd.read_parquet(path)
    nodes["gid"] = nodes.gid.astype(float)
    nodes.to_parquet(path)
    with pytest.raises(DataError, match="целочисленный"):
        load(dataset)


def test_projection_adds_reverse_flows_without_loss():
    graph = nx.DiGraph()
    graph.add_nodes_from([1, 2, 3])
    graph.add_edge(1, 2, sum_kzt=12)
    graph.add_edge(2, 1, sum_kzt=7)
    projected = projection(graph)
    assert projected[1][2]["weight"] == 19
    assert 3 in projected and projected.degree(3) == 0


def row(**kwargs):
    base = dict(
        in_deg=1,
        out_deg=0,
        in_kzt=100,
        out_kzt=0,
        in_tx=1,
        out_tx=0,
        seed_reach=0,
        is_seed=False,
        boundary_censored=False,
        isolated=False,
        observed_out_exceeds_in=False,
        pass_through=0.0,
        p_betweenness=0.0,
        p_in_tx=0.5,
        p_out_tx=0.5,
        p_out_kzt=0.5,
    )
    return base | kwargs


@pytest.mark.parametrize(
    "features,role",
    [
        (row(), "terminal"),
        (row(in_deg=6, in_tx=8), "consolidator"),
        (row(out_deg=1, out_tx=1, out_kzt=100, pass_through=1), "transit"),
        (row(out_deg=20, out_tx=25), "distributor"),
        (row(seed_reach=3, in_deg=5, out_deg=4, out_tx=5, p_betweenness=0.95), "coordinator"),
        (row(boundary_censored=True), "peripheral"),
        (row(is_seed=True, out_deg=1, out_tx=1, pass_through=1), "peripheral"),
        (row(in_deg=0, in_kzt=0, isolated=True, pass_through=float("nan")), "peripheral"),
    ],
)
def test_role_hypotheses_and_observation_limits(features, role):
    result = classify(features, read_rules())
    assert result["role"] == role
    assert 0 <= result["role_score"] <= 1


def test_overlapping_rules_preserve_evidence():
    result = classify(
        row(seed_reach=5, in_deg=6, out_deg=20, out_tx=20, p_betweenness=1), read_rules()
    )
    assert result["role"] == "coordinator"
    assert "distributor" in result["matched_rules"]


def test_failed_validation_keeps_previous_generation(dataset, tmp_path):
    output = tmp_path / "result"
    write_result(analyze(dataset), output)
    previous = (output / EXPORTS[0]).read_bytes()
    (dataset / "transactions.parquet").write_bytes(b"not parquet")
    with pytest.raises(DataError):
        write_result(analyze(dataset), output)
    assert (output / EXPORTS[0]).read_bytes() == previous


def test_official_contract_if_present():
    source = Path("data/data")
    if not source.is_dir():
        pytest.skip(
            "Official dataset is absent in this checkout; synthetic contract tests still run"
        )
    result = analyze(source)
    report = result["report"]
    assert report["n_nodes"] == 2248
    assert report["n_isolates"] == 19
    assert report["n_boundary"] == 444
    assert report["runtime_seconds"] < 300
    assert len(result["top"]) >= 20
    assert (
        sum(row["role"] == "terminal" and row["boundary_censored"] for row in result["nodes"]) == 0
    )
