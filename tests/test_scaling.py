"""Semantic invariants for allocation/aggregation optimizations, not timing gates."""

import networkx as nx
import pandas as pd
import pytest

from money_graph.clusters import summarize as summarize_clusters
from money_graph.features import build_graph, communities, compute
from money_graph.pipeline import read_rules
from money_graph.temporal import summarize as summarize_temporal


def test_graph_streaming_preserves_column_names_signed_ids_and_isolates():
    minimum, maximum = -(2**63), 2**63 - 1
    nodes = pd.DataFrame({"gid": [minimum, 0, maximum]})
    # Explicit column selection must work regardless of input column order.
    edges = pd.DataFrame(
        {
            "n_tx": [2],
            "sum_kzt": [5000.25],
            "dst": [maximum],
            "extra": ["ignored"],
            "src": [minimum],
        }
    )
    graph = build_graph(nodes, edges)
    assert set(graph) == {minimum, 0, maximum}
    assert list(graph.edges) == [(minimum, maximum)]
    assert graph[minimum][maximum] == {"sum_kzt": 5000.25, "n_tx": 2}
    assert list(nx.isolates(graph)) == [0]


@pytest.mark.parametrize("self_transfer", [False, True])
def test_compute_does_not_mutate_source_graph_or_change_external_metrics(self_transfer):
    nodes = pd.DataFrame({"gid": [1, 2, 3], "depth": [0, 1, 0], "is_seed": [True, False, True]})
    edges = pd.DataFrame({"src": [1], "dst": [2], "sum_kzt": [7500.0], "n_tx": [1]})
    graph = build_graph(nodes, edges)
    base = compute(graph, nodes, read_rules())
    if self_transfer:
        graph.add_edge(2, 2, sum_kzt=100_000.0, n_tx=20)
    before = list(graph.edges(data=True))
    actual = compute(graph, nodes, read_rules())
    assert list(graph.edges(data=True)) == before
    for metric in (
        "in_deg",
        "out_deg",
        "in_kzt",
        "out_kzt",
        "pagerank",
        "betweenness",
        "priority_score",
    ):
        pd.testing.assert_series_equal(actual[metric], base[metric])
    assert actual.loc[1, "self_transfer_tx"] == (20 if self_transfer else 0)
    # The source graph still retains a self-loop if one was provided.
    assert graph.has_edge(2, 2) is self_transfer


def test_temporal_accumulation_keeps_unique_counterparties_and_calendar_days():
    tx = pd.DataFrame(
        [(1, 3, "2026-07-01 01:00", 5000.25)] * 300
        + [(1, 3, "2026-07-01 23:59", 5000.25)] * 200
        + [(2, 3, "2026-07-01 12:00", 6000.0)]
        + [(3, 4, "2026-07-02 09:00", 10000.0), (3, 3, "2026-07-02 10:00", 999999.0)],
        columns=["src", "dst", "date", "sum_kzt"],
    )
    tx["date"] = pd.to_datetime(tx.date)
    result = summarize_temporal(tx, [1, 2, 3, 4, 5])
    account = result[3]
    assert len(account["daily"]) == 2
    assert account["daily"][0]["in_tx"] == 501
    assert account["daily"][0]["senders"] == 2
    assert account["in_kzt"] == 500 * 5000.25 + 6000
    assert account["out_kzt"] == 10000
    assert account["matched_1_2d_kzt"] == 10000
    assert account["same_day_overlap_kzt"] == 0
    assert result[5]["daily"] == []


def test_many_communities_aggregate_each_directed_flow_without_double_counting():
    size = 1000
    records = [
        {
            "gid": str(gid),
            "cluster_id": gid,
            "role": "peripheral",
            "is_seed": gid == 0,
            "boundary_censored": False,
            "isolated": False,
        }
        for gid in range(size)
    ]
    edges = [
        {"src": str(gid), "dst": str((gid + offset) % size), "sum_kzt": amount}
        for gid in range(size)
        for offset, amount in ((0, 10.0), (1, 20.0), (2, 30.0))
    ]
    clusters, links = summarize_clusters(records, edges)
    assert len(clusters) == size and len(links) == 2 * size
    assert all(row["incoming_kzt"] == row["outgoing_kzt"] == 50 for row in clusters)
    assert all(row["sum_kzt_internal"] == 10 for row in clusters)
    assert (
        sum(row["sum_kzt_internal"] for row in clusters) + sum(edge["sum_kzt"] for edge in links)
        == 60 * size
    )
    assert all(edge["n_edges"] == 1 for edge in links)


def test_community_fast_path_retains_deterministic_partition_with_isolates():
    graph = nx.DiGraph()
    graph.add_weighted_edges_from(
        [(1, 2, 10), (2, 1, 10), (3, 4, 10), (4, 3, 10)], weight="sum_kzt"
    )
    active = communities(graph, read_rules())
    graph.add_node(9)
    with_isolate = communities(graph, read_rules())
    assert active == {gid: cid for gid, cid in with_isolate.items() if gid != 9}
    assert with_isolate[9] not in active.values()
