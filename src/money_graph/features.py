"""Directed flow metrics; an undirected projection is used only for communities."""

import math
from typing import Any

import networkx as nx
import pandas as pd


def percentile(values: pd.Series, eligible: pd.Series | None = None) -> pd.Series:
    """Mid-distribution rank among positive, observable values.

    P(x) = (count(values < x) + 0.5 * count(values == x)) / count(values).
    Equal evidence always gets 0.5, including a singleton. Repeating a
    population does not inflate its ranks; zeros are not positive evidence.
    """
    mask = values > 0
    if eligible is not None:
        mask &= eligible
    result = pd.Series(0.0, index=values.index)
    positive = values.loc[mask]
    if len(positive):
        result.loc[mask] = (positive.rank(method="average") - 0.5) / len(positive)
    return result


def build_graph(nodes: pd.DataFrame, edges: pd.DataFrame) -> nx.DiGraph:
    graph: nx.DiGraph = nx.DiGraph()
    graph.add_nodes_from(int(gid) for gid in nodes.gid)
    for src, dst, amount, count in edges[["src", "dst", "sum_kzt", "n_tx"]].itertuples(
        index=False, name=None
    ):
        graph.add_edge(int(src), int(dst), sum_kzt=float(amount), n_tx=int(count))
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
    self_flows = {gid: attrs for gid, _, attrs in nx.selfloop_edges(graph, data=True)}
    external = graph
    if self_flows:
        external = graph.copy()
        external.remove_edges_from((gid, gid) for gid in self_flows)
    frame["self_transfer_kzt"] = pd.Series(
        {gid: attrs["sum_kzt"] for gid, attrs in self_flows.items()}, dtype="float64"
    ).reindex(frame.index, fill_value=0.0)
    frame["self_transfer_tx"] = pd.Series(
        {gid: attrs["n_tx"] for gid, attrs in self_flows.items()}, dtype="int64"
    ).reindex(frame.index, fill_value=0)
    active = [gid for gid, degree in external.degree() if degree]
    exact = len(active) <= rules["betweenness_exact_max_nodes"]
    # Degree views are cheap; materialize one metric at a time instead of
    # retaining all metric dictionaries alongside the graph and dataframe.
    for column, degree in (
        ("in_deg", external.in_degree()),
        ("out_deg", external.out_degree()),
        ("in_tx", external.in_degree(weight="n_tx")),
        ("out_tx", external.out_degree(weight="n_tx")),
    ):
        frame[column] = pd.Series(dict(degree))
    # Weighted degree uses naive float summation in NetworkX. Preserve small
    # transfers alongside large ones before computing balances or role thresholds.
    for direction, edge_view in (("in", external.in_edges), ("out", external.out_edges)):
        frame[f"{direction}_kzt"] = pd.Series(
            {
                gid: math.fsum(attrs["sum_kzt"] for _, _, attrs in edge_view(gid, data=True))
                for gid in external
            }
        )
    # NetworkX stops at N * tol: keep the same global numerical accuracy as
    # dataset size grows instead of allowing more error on larger graphs.
    frame["pagerank"] = pd.Series(
        nx.pagerank(external, weight="sum_kzt", max_iter=500, tol=1e-8 / max(len(external), 1))
    )
    if exact:
        centrality = nx.betweenness_centrality(external, weight=None)
    else:
        # An isolated seed cannot lie on a path. Sampling it wastes a pivot
        # and can erase observed intermediaries when empty records are added.
        connected = external.subgraph(active)
        centrality = nx.betweenness_centrality(
            connected,
            k=min(rules["betweenness_samples"], len(active)),
            seed=rules["random_seed"],
            weight=None,
        )
        # Keep raw centrality comparable to NetworkX on the full input graph.
        factor = (
            (len(active) - 1) * (len(active) - 2) / ((len(external) - 1) * (len(external) - 2))
            if len(external) > 2
            else 1.0
        )
        centrality = {gid: value * factor for gid, value in centrality.items()}
    frame["betweenness"] = pd.Series(centrality).reindex(frame.index, fill_value=0.0)
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
    isolates: list[int] = []
    active: list[int] = []
    for gid, degree in undirected.degree():
        (active if degree else isolates).append(gid)
    connected = undirected.subgraph(active) if isolates else undirected
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
