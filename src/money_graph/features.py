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
    for column, values in {
        "in_deg": dict(graph.in_degree()),
        "out_deg": dict(graph.out_degree()),
        "in_kzt": dict(graph.in_degree(weight="sum_kzt")),
        "out_kzt": dict(graph.out_degree(weight="sum_kzt")),
        "in_tx": dict(graph.in_degree(weight="n_tx")),
        "out_tx": dict(graph.out_degree(weight="n_tx")),
        "pagerank": nx.pagerank(graph, weight="sum_kzt", max_iter=500),
        "betweenness": nx.betweenness_centrality(
            graph,
            k=min(rules["betweenness_samples"], len(graph)),
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
    frame["isolated"] = (frame.in_deg + frame.out_deg) == 0
    frame["boundary_censored"] = (frame.depth == rules["max_depth"]) & (frame.out_deg == 0)
    frame["inflow_unobserved"] = frame.in_kzt == 0
    frame["observed_out_exceeds_in"] = frame.out_kzt > frame.in_kzt
    frame["pass_through"] = frame.out_kzt / frame.in_kzt.where(frame.in_kzt > 0)
    for metric in (
        "pagerank",
        "betweenness",
        "seed_reach",
        "in_deg",
        "out_deg",
        "in_tx",
        "out_tx",
        "out_kzt",
    ):
        frame[f"p_{metric}"] = percentile(frame[metric], ~frame.isolated)
    frame["priority_score"] = sum(
        frame[f"p_{name}"] * weight for name, weight in rules["priority_weights"].items()
    )
    frame.loc[frame.isolated, "priority_score"] = 0.0
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
