"""Small counterexamples for analytical claims; no labeled fraud data are assumed."""

from pathlib import Path

import networkx as nx
import pandas as pd
import pytest

from money_graph.features import build_graph, compute
from money_graph.investigation import sensitivity
from money_graph.pipeline import analyze, read_rules
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
