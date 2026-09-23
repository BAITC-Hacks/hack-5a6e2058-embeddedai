"""Small counterexamples for analytical claims; no labeled fraud data are assumed."""

from copy import deepcopy
from pathlib import Path

import networkx as nx
import pandas as pd
import pytest

from money_graph.demo import create_demo
from money_graph.features import build_graph, compute
from money_graph.investigation import sensitivity
from money_graph.loader import DataError
from money_graph.pipeline import analyze, read_rules, validate_result, write_result
from money_graph.roles import classify


def test_exact_betweenness_does_not_miss_unsampled_intermediaries():
    graph = nx.path_graph(7, create_using=nx.DiGraph)
    nx.set_edge_attributes(graph, 5_000.0, "sum_kzt")
    nx.set_edge_attributes(graph, 1, "n_tx")
    nodes = pd.DataFrame(
        {"gid": range(7), "depth": [0, 1, 2, 3, 4, 4, 4], "is_seed": [True] + [False] * 6}
    )
    rules = read_rules() | {"betweenness_samples": 1}
    exact = compute(graph, nodes, rules).set_index("gid")
    sampled = compute(graph, nodes, rules | {"betweenness_exact_max_nodes": 1}).set_index("gid")
    expected = nx.betweenness_centrality(graph)
    assert exact.betweenness.to_dict() == pytest.approx(expected)
    assert exact.loc[3, "betweenness"] > 0
    assert sampled.loc[3, "betweenness"] == 0


def test_weight_sensitivity_uses_the_same_rounding_and_numeric_ties_as_ranking():
    # Normalizing the only nonzero weight leaves the model unchanged. Both
    # scores round to .5, so the numeric ID tie-break must stay unchanged too.
    records = [
        {"gid": "2", "rank": 1, "priority_score": 0.5, "p_volume": 0.4999998},
        {"gid": "10", "rank": 2, "priority_score": 0.5, "p_volume": 0.5000002},
    ]
    report = sensitivity(records, {"volume": 1.0})
    assert report["minimum_top_overlap"] == 1
    assert [node["rank_range"] for node in records] == [[1, 1], [2, 2]]


def test_seed_rule_trace_explains_the_reported_support():
    nodes = pd.DataFrame(
        {"gid": range(11), "depth": [0] + [1] * 10, "is_seed": [True] + [False] * 10}
    )
    edges = pd.DataFrame(
        {"src": [0] * 10, "dst": range(1, 11), "sum_kzt": [5_000.0] * 10, "n_tx": [1] * 10}
    )
    rules = read_rules()
    node = compute(build_graph(nodes, edges), nodes, rules).to_dict("records")[0]
    hypothesis = classify(node, rules)
    assert hypothesis["role"] == "distributor"
    trace = hypothesis["rule_trace"][0]
    assert trace["support"] == hypothesis["role_score"]
    assert trace["observation_multiplier"] == rules["incomplete_support_multiplier"]
    assert trace["raw_support"] * trace["observation_multiplier"] == pytest.approx(
        trace["support"], abs=1e-6
    )


def test_self_transfers_are_preserved_but_cannot_manufacture_transit(tmp_path: Path):
    # Seed 1 pays customer 3 once. Customer 3 only pays themself; customer 2
    # has no counterparties at all. Neither customer transits money onward.
    nodes = pd.DataFrame({"gid": [1, 2, 3], "depth": [0, 0, 1], "is_seed": [True, True, False]})
    tx = pd.DataFrame(
        [
            (1, 3, "2026-07-01", 5_000.0),
            (3, 3, "2026-07-02", 500_000.0),
            (3, 3, "2026-07-03", 500_000.0),
            (2, 2, "2026-07-01", 10_000.0),
        ],
        columns=["src", "dst", "date", "sum_kzt"],
    )
    edges = tx.groupby(["src", "dst"], as_index=False).agg(
        sum_kzt=("sum_kzt", "sum"), n_tx=("sum_kzt", "size")
    )
    edges["depth"] = edges.src.map({1: 1, 2: 1, 3: 2})
    for name, frame in (("nodes", nodes), ("edges", edges), ("transactions", tx)):
        frame.to_parquet(tmp_path / f"{name}.parquet", index=False)
    result = analyze(tmp_path)
    by_id = {node["gid"]: node for node in result["nodes"]}
    recipient = by_id["3"]
    assert recipient["role"] == "terminal"
    assert recipient["in_deg"] == 1 and recipient["out_deg"] == 0
    assert recipient["in_kzt"] == 5_000 and recipient["out_kzt"] == 0
    assert recipient["self_transfer_kzt"] == 1_000_000
    assert recipient["self_transfer_tx"] == 2
    assert recipient["temporal"]["matched_1_2d_kzt"] == 0
    assert recipient["temporal"]["in_kzt"] == recipient["in_kzt"]
    assert recipient["temporal"]["out_kzt"] == recipient["out_kzt"]
    assert by_id["2"]["self_only"] and not by_id["2"]["isolated"]
    assert by_id["2"]["priority_score"] == 0
    assert by_id["2"]["role"] == "peripheral"
    assert by_id["2"]["temporal"]["daily"] == []
    assert len(result["edges"]) == 3  # No input edges silently disappear.
    assert result["report"]["turnover_kzt"] == tx.sum_kzt.sum()
    assert result["report"]["n_self_transfer_nodes"] == 2
    assert result["report"]["self_transfer_kzt"] == 1_010_000
    assert result["report"]["betweenness_method"] == "exact"
    assert result["report"]["betweenness_pivots"] == len(nodes)
    assert (
        sum(c["sum_kzt_internal"] for c in result["clusters"])
        + sum(edge["sum_kzt"] for edge in result["cluster_edges"])
        == tx.sum_kzt.sum()
    )


@pytest.fixture(scope="module")
def valid_saved_result(tmp_path_factory):
    source = tmp_path_factory.mktemp("saved-result")
    create_demo(source)
    return analyze(source)


@pytest.mark.parametrize(
    "path,value",
    [
        (("nodes",), [None]),
        (("nodes", 0, "gid"), "9223372036854775808"),
        (("nodes", 0, "gid"), "-9223372036854775809"),
        (("nodes", 0, "gid"), "-0"),
        (("nodes", 0, "gid"), "01"),
        (("nodes", 0, "gid"), "-01"),
        (("nodes", 0, "gid"), 9007199254741000),
        (("nodes", 0, "priority_score"), float("nan")),
        (("nodes", 0, "rank"), True),
        (("nodes", 0, "cluster_id"), 999_999),
        (("nodes", 0, "evidence"), ""),
        (("nodes", 0, "temporal", "daily"), [None]),
        (("nodes", 0, "warnings"), "not an array"),
        (("nodes", 0, "rule_trace"), [None]),
        (("edges", 0, "dst"), "999999"),
        (("edges", 0, "n_tx"), 1.5),
        (("edges", 0, "sum_kzt"), float("inf")),
        (("clusters", 0, "n_nodes"), 999_999),
        (("clusters", 0, "top_gids"), ["999999"]),
        (("top", 0, "rank"), 999_999),
        (("top", 0, "unexpected_csv_column"), "broken schema"),
        (("report", "n_transactions"), 1),
        (("report", "input_sha256", "nodes"), "not-a-hash"),
        (("report", "sensitivity", "scenarios"), [None] * 12),
    ],
)
def test_saved_result_corruption_always_fails_as_data_error(valid_saved_result, path, value):
    result = deepcopy(valid_saved_result)
    container = result
    for key in path[:-1]:
        container = container[key]
    container[path[-1]] = value
    with pytest.raises(DataError):
        validate_result(result)


def test_saved_result_checks_duplicate_ids_and_rank_order(valid_saved_result):
    duplicate = deepcopy(valid_saved_result)
    duplicate["nodes"][1]["gid"] = duplicate["nodes"][0]["gid"]
    with pytest.raises(DataError, match="duplicate gid"):
        validate_result(duplicate)
    reordered_top = deepcopy(valid_saved_result)
    reordered_top["top"][:2] = reversed(reordered_top["top"][:2])
    with pytest.raises(DataError, match="top_nodes"):
        validate_result(reordered_top)


def test_saved_result_allows_explicit_provenance_without_changing_csv(valid_saved_result):
    result = deepcopy(valid_saved_result)
    result["report"]["synthetic"] = True
    validate_result(result)


@pytest.mark.parametrize("with_edge", [True, False], ids=["signed-edge", "all-isolates"])
def test_signed_int64_endpoints_survive_analysis_and_csv(tmp_path, with_edge):
    source = tmp_path / "input"
    source.mkdir()
    minimum, maximum = -(2**63), 2**63 - 1
    nodes = pd.DataFrame(
        {
            "gid": [minimum, 0, maximum],
            "depth": [0, 0, int(with_edge)],
            "is_seed": [True, True, not with_edge],
        }
    )
    edges = pd.DataFrame(
        {
            "src": pd.Series([minimum] if with_edge else [], dtype="int64"),
            "dst": pd.Series([maximum] if with_edge else [], dtype="int64"),
            "sum_kzt": pd.Series([5000] if with_edge else [], dtype="float64"),
            "n_tx": pd.Series([1] if with_edge else [], dtype="int64"),
            "depth": pd.Series([1] if with_edge else [], dtype="int8"),
        }
    )
    transactions = edges[["src", "dst", "sum_kzt"]].copy()
    transactions["date"] = pd.Series(pd.to_datetime(["2026-07-01"] if with_edge else []))
    for name, frame in (("nodes", nodes), ("edges", edges), ("transactions", transactions)):
        frame.to_parquet(source / f"{name}.parquet", index=False)
    result = analyze(source)
    validate_result(result)
    expected = {str(minimum), "0", str(maximum)}
    assert {node["gid"] for node in result["nodes"]} == expected
    output = tmp_path / "output"
    write_result(result, output)
    exported = pd.read_csv(output / "nodes_roles.csv", dtype={"gid": str})
    assert set(exported.gid) == expected
    if with_edge:
        assert result["edges"][0]["src"] == str(minimum)
        assert result["edges"][0]["dst"] == str(maximum)
    else:
        assert result["report"]["n_isolates"] == 3
        assert result["report"]["n_edges"] == 0
        assert result["report"]["period_from"] is None
        assert all(node["priority_score"] == 0 for node in result["nodes"])


def test_demo_depths_are_minimum_bfs_distances_and_all_nonseeds_are_reachable(tmp_path):
    create_demo(tmp_path)
    nodes = pd.read_parquet(tmp_path / "nodes.parquet")
    edges = pd.read_parquet(tmp_path / "edges.parquet")
    graph = build_graph(nodes, edges)
    seeds = set(nodes.loc[nodes.is_seed, "gid"])
    assert len(seeds) == 8
    assert len(seeds & set(nx.isolates(graph))) == 1
    assert all(graph.out_degree(seed) > 0 for seed in seeds if graph.degree(seed) > 0)
    minimum_depth = {}
    for seed in seeds:
        for gid, hops in nx.single_source_shortest_path_length(graph, seed, cutoff=4).items():
            minimum_depth[gid] = min(minimum_depth.get(gid, hops), hops)
    assert set(minimum_depth) == set(nodes.gid)
    assert all(row.depth == minimum_depth[row.gid] for row in nodes.itertuples())
    assert all(row.depth == minimum_depth[row.src] + 1 <= 4 for row in edges.itertuples())
