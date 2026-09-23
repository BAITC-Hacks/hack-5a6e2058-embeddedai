"""Directed flow metrics; an undirected projection is used only for communities."""

from typing import Any, cast

import networkx as nx
import pandas as pd


def percentile(values: pd.Series, eligible: pd.Series | None = None) -> pd.Series:
    mask = values > 0
    if eligible is not None:
        mask &= eligible
    result = pd.Series(0.0, index=values.index)
    result.loc[mask] = values.loc[mask].rank(method="average", pct=True)
    return result


def build_graph(nodes: pd.DataFrame, edges: pd.DataFrame) -> nx.DiGraph:
    graph: nx.DiGraph = nx.DiGraph()
    graph.add_nodes_from(int(gid) for gid in nodes.gid)
    for row in cast(list[dict[str, Any]], edges.to_dict("records")):
        graph.add_edge(
            int(row["src"]), int(row["dst"]), sum_kzt=float(row["sum_kzt"]), n_tx=int(row["n_tx"])
        )
    return graph


def projection(graph: nx.DiGraph) -> nx.Graph:
    undirected: nx.Graph = nx.Graph()
    undirected.add_nodes_from(graph)
    for src, dst, attrs in graph.edges(data=True):
        if src != dst:
            previous = undirected.get_edge_data(src, dst, {}).get("weight", 0)
            undirected.add_edge(src, dst, weight=previous + attrs["sum_kzt"])
    return undirected


def compute(graph: nx.DiGraph, nodes: pd.DataFrame, rules: dict[str, Any]) -> pd.DataFrame:
    frame = nodes.copy().set_index("gid")
    # A transfer to the same client is observed turnover, but not movement
    # between counterparties and cannot support a transit or intermediary role.
    external = graph.copy()
    external.remove_edges_from(nx.selfloop_edges(graph))
    self_flows = {gid: graph.get_edge_data(gid, gid, {}) for gid in graph}
    frame["self_transfer_kzt"] = pd.Series(
        {gid: attrs.get("sum_kzt", 0.0) for gid, attrs in self_flows.items()}
    )
    frame["self_transfer_tx"] = pd.Series(
        {gid: attrs.get("n_tx", 0) for gid, attrs in self_flows.items()}
    )
    exact = len(external) <= rules["betweenness_exact_max_nodes"]
    for column, values in {
        "in_deg": dict(external.in_degree()),
        "out_deg": dict(external.out_degree()),
        "in_kzt": dict(external.in_degree(weight="sum_kzt")),
        "out_kzt": dict(external.out_degree(weight="sum_kzt")),
        "in_tx": dict(external.in_degree(weight="n_tx")),
        "out_tx": dict(external.out_degree(weight="n_tx")),
        "pagerank": nx.pagerank(external, weight="sum_kzt", max_iter=500),
        "betweenness": nx.betweenness_centrality(
            external,
            k=None if exact else min(rules["betweenness_samples"], len(external)),
            seed=rules["random_seed"],
            weight=None,
        ),
    }.items():
        frame[column] = pd.Series(values)
    reach = dict.fromkeys(graph, 0)
    for seed in nodes.loc[nodes.is_seed, "gid"]:
        for gid in nx.single_source_shortest_path_length(
            graph, int(seed), cutoff=rules["max_depth"]
        ):
            if gid != seed:
                reach[gid] += 1
    frame["seed_reach"] = pd.Series(reach)
    no_counterparties = (frame.in_deg + frame.out_deg) == 0
    frame["self_only"] = no_counterparties & (frame.self_transfer_tx > 0)
    frame["isolated"] = no_counterparties & ~frame.self_only
    frame["boundary_censored"] = (frame.depth == rules["max_depth"]) & (frame.out_deg == 0)
    frame["inflow_unobserved"] = frame.in_kzt == 0
    frame["observed_out_exceeds_in"] = frame.out_kzt > frame.in_kzt
    frame["pass_through"] = frame.out_kzt / frame.in_kzt.where(frame.in_kzt > 0)
    frame["volume"] = frame[["in_kzt", "out_kzt"]].max(axis=1)
    for metric in (
        "pagerank",
        "betweenness",
        "seed_reach",
        "in_deg",
        "out_deg",
        "in_tx",
        "out_tx",
        "out_kzt",
        "volume",
    ):
        frame[f"p_{metric}"] = percentile(frame[metric], ~no_counterparties)
    frame["priority_score"] = sum(
        frame[f"p_{name}"] * weight for name, weight in rules["priority_weights"].items()
    )
    frame.loc[no_counterparties, "priority_score"] = 0.0
    return frame.reset_index()


def communities(graph: nx.DiGraph, rules: dict[str, Any]) -> dict[int, int]:
    undirected = projection(graph)
    isolates = list(nx.isolates(undirected))
    connected = undirected.subgraph([gid for gid in undirected if undirected.degree(gid) > 0])
    groups = (
        list(
            nx.community.louvain_communities(
                connected,
                weight="weight",
                seed=rules["random_seed"],
                resolution=rules["louvain_resolution"],
            )
        )
        if len(connected)
        else []
    )
    groups.extend({gid} for gid in isolates)
    groups.sort(key=lambda members: min(members))
    return {gid: index for index, members in enumerate(groups) for gid in members}
