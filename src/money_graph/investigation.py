"""On-demand graph evidence and bounded, reproducible what-if experiments."""

import random
from typing import Any

import networkx as nx


def graph_from_result(result: dict[str, Any]) -> nx.DiGraph:
    graph: nx.DiGraph = nx.DiGraph()
    graph.add_nodes_from(row["gid"] for row in result["nodes"])
    for edge in result["edges"]:
        graph.add_edge(edge["src"], edge["dst"], **edge)
    return graph


def node_evidence(result: dict[str, Any], gid: str) -> dict[str, Any]:
    graph = graph_from_result(result)
    reverse_paths = nx.single_source_shortest_path(graph.reverse(copy=False), gid, cutoff=4)
    seeds = {n["gid"] for n in result["nodes"] if n["is_seed"]}
    paths = [
        list(reversed(path))
        for seed, path in reverse_paths.items()
        if seed in seeds and seed != gid
    ]
    paths.sort(key=lambda p: (len(p), int(p[0])))

    def describe(path: list[str]) -> dict[str, Any]:
        return {
            "gids": path,
            "edges": [dict(graph[a][b]) for a, b in zip(path[:-1], path[1:], strict=True)],
        }

    reciprocal = [
        dict(graph[other][gid])
        for other in graph.successors(gid)
        if other != gid and graph.has_edge(other, gid)
    ]
    reciprocal.sort(key=lambda e: (-e["sum_kzt"], int(e["src"])))
    incoming = sorted(
        (e for e in graph.in_edges(gid, data=True) if e[2]["n_tx"] >= 2 and e[0] != gid),
        key=lambda e: (-e[2]["sum_kzt"], int(e[0])),
    )
    outgoing = sorted(
        (e for e in graph.out_edges(gid, data=True) if e[2]["n_tx"] >= 2 and e[1] != gid),
        key=lambda e: (-e[2]["sum_kzt"], int(e[1])),
    )
    repeated_count = len(incoming) * len(outgoing) - len(
        {e[0] for e in incoming} & {e[1] for e in outgoing}
    )
    # Bounded examples even for high-degree uploads; the total count stays exact.
    repeated = [[a[0], gid, b[1]] for a in incoming[:20] for b in outgoing[:20] if a[0] != b[1]]
    repeated.sort(
        key=lambda p: (
            -min(graph[p[0]][gid]["sum_kzt"], graph[gid][p[2]]["sum_kzt"]),
            int(p[0]),
            int(p[2]),
        )
    )
    cycles = []
    for successor in sorted(graph.successors(gid), key=int):
        if successor != gid and successor in reverse_paths:
            cycle = [gid] + list(reversed(reverse_paths[successor]))
            cycles.append(cycle)
    cycles.sort(key=lambda p: (len(p), [int(g) for g in p]))
    return {
        "seed_paths": [describe(path) for path in paths[:5]],
        "seed_path_count": len(paths),
        "reciprocal": reciprocal[:20],
        "reciprocal_count": len(reciprocal),
        "cycles": [describe(path) for path in cycles[:5]],
        "repeated_routes": [describe(path) for path in repeated[:10]],
        "repeated_route_count": repeated_count,
        "caveat": "Пути и циклы подтверждены рёбрами за весь период. Это не доказательство последовательности операций или движения одних и тех же денег.",
    }


def resilience(result: dict[str, Any], count: int) -> dict[str, Any]:
    graph = graph_from_result(result)
    original = list(nx.weakly_connected_components(graph))
    turnover = sum(e["sum_kzt"] for e in result["edges"])
    eligible = sorted((g for g in graph if graph.degree(g) > 0), key=int)
    count = min(count, len(eligible))

    def measure(removed: list[str]) -> dict[str, Any]:
        excluded = set(removed)
        remaining = graph.subgraph([g for g in graph if g not in excluded])
        sizes = [len(c) for c in nx.weakly_connected_components(remaining)]
        pairs = sum(s * (s - 1) // 2 for s in sizes)
        baseline_pairs = sum((s := len(c - excluded)) * (s - 1) // 2 for c in original)
        removed_flow = sum(
            e["sum_kzt"] for e in result["edges"] if e["src"] in excluded or e["dst"] in excluded
        )
        return {
            "removed_gids": removed,
            "n_nodes": len(remaining),
            "n_edges": remaining.number_of_edges(),
            "components": len(sizes),
            "largest_component": max(sizes, default=0),
            "fragmented_pairs_share": round(1 - pairs / baseline_pairs, 6) if baseline_pairs else 0,
            "removed_turnover_kzt": round(removed_flow, 2),
            "removed_turnover_share": round(removed_flow / turnover, 6) if turnover else 0,
        }

    eligible_set = set(eligible)
    ranked = sorted(
        (n for n in result["nodes"] if n["gid"] in eligible_set), key=lambda n: n["rank"]
    )
    targeted = measure([n["gid"] for n in ranked[:count]])
    degree = measure(sorted(eligible, key=lambda g: (-graph.degree(g), int(g)))[:count])
    rng = random.Random(42)
    random_trials = [measure(rng.sample(eligible, count)) for _ in range(20)]
    random_summary = {
        key: {
            "mean": sum(r[key] for r in random_trials) / len(random_trials),
            "min": min(r[key] for r in random_trials),
            "max": max(r[key] for r in random_trials),
        }
        for key in ("fragmented_pairs_share", "removed_turnover_share", "largest_component")
    }
    return {
        "count": count,
        "before": measure([]),
        "priority": targeted,
        "degree": degree,
        "random": random_summary,
        "random_trials": 20,
        "caveat": "Статическое удаление вершин в наблюдаемом графе. Связность считается без направления; исходные изоляты сохранены. Потеря связности исключает сами удалённые вершины. Это не прогноз блокировки денег или поведения сети.",
    }


def sensitivity(records: list[dict[str, Any]], weights: dict[str, float]) -> dict[str, Any]:
    """Vary each weight ±20%, renormalize; this is robustness, not validation."""
    baseline = sorted(records, key=lambda n: (-n["priority_score"], int(n["gid"])))
    count = min(20, len(baseline))
    top = {n["gid"] for n in baseline[:count]}
    intervals = {n["gid"]: [n["rank"], n["rank"]] for n in records}
    scenarios: list[dict[str, Any]] = []
    for key in weights:
        for multiplier in (0.8, 1.2):
            adjusted = weights | {key: weights[key] * multiplier}
            denominator = sum(adjusted.values())
            ranked = sorted(
                records,
                key=lambda n: (
                    -round(sum(n[f"p_{k}"] * w / denominator for k, w in adjusted.items()), 6),
                    int(n["gid"]),
                ),
            )
            for rank, node in enumerate(ranked, 1):
                bounds = intervals[node["gid"]]
                bounds[0], bounds[1] = min(bounds[0], rank), max(bounds[1], rank)
            scenarios.append(
                {
                    "metric": key,
                    "multiplier": multiplier,
                    "top_overlap": len(top & {n["gid"] for n in ranked[:count]}) / count,
                }
            )
    for node in records:
        node["rank_range"] = intervals[node["gid"]]
    return {
        "top_n": count,
        "scenarios": scenarios,
        "minimum_top_overlap": min(s["top_overlap"] for s in scenarios),
        "caveat": "Чувствительность к изменению одного веса на ±20%; не проверка истинных ролей и не доверительный интервал.",
    }
