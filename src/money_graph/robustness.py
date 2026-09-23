"""Bounded, on-demand sensitivity checks, independent of the submitted CSVs."""

import math
import time
from collections import Counter
from collections.abc import Hashable, Sequence
from typing import Any

import networkx as nx

from .features import projection
from .loader import DataError
from .roles import classify

MAX_NODES = 10_000
MAX_EDGES = 50_000
COMMUNITY_SECONDS = 20.0
CAVEAT = (
    "Это чувствительность к выбранным настройкам на неизменных данных, не accuracy, "
    "не вероятность правильности роли и не доказательство виновности. "
    "Не проверяет ошибки исходников и пропущенные переводы. "
    "Смена resolution меняет масштаб сообществ; её эффект отличается от случайности seed. "
    "Узлы без внешних контрагентов исключены из долей согласия."
)


def adjusted_rand(left: Sequence[Hashable], right: Sequence[Hashable]) -> float:
    """Chance-adjusted partition agreement in O(N), independent of label names."""
    if len(left) != len(right):
        raise ValueError("Partitions must cover the same nodes")
    if len(left) < 2:
        return 1.0

    def pairs(counts: Any) -> int:
        return sum(n * (n - 1) // 2 for n in counts)

    joint = pairs(Counter(zip(left, right, strict=True)).values())
    left_pairs = pairs(Counter(left).values())
    right_pairs = pairs(Counter(right).values())
    total = len(left) * (len(left) - 1) // 2
    # Integer arithmetic avoids cancellation in nearly equal large partitions.
    denominator = total * (left_pairs + right_pairs) - 2 * left_pairs * right_pairs
    if denominator == 0:
        return 1.0
    return 2 * (total * joint - left_pairs * right_pairs) / denominator


def role_scenarios(rules: dict[str, Any]) -> list[dict[str, Any]]:
    """At most 16 one-factor variants; actual values remain visible to reviewers."""
    scenarios: list[dict[str, Any]] = []

    def add(identifier: str, label: str, changes: dict[str, Any]) -> None:
        if any(rules[key] != value for key, value in changes.items()):
            scenarios.append({"id": identifier, "label": label, "changes": changes})

    for key, label in (
        ("coordinator_min_seeds", "Связующий: минимум seed-предков"),
        ("coordinator_min_in", "Связующий: минимум плательщиков"),
        ("coordinator_min_out", "Связующий: минимум получателей"),
        ("distributor_min_out", "Распределитель: минимум получателей"),
        ("consolidator_min_in", "Консолидатор: минимум плательщиков"),
    ):
        value = rules[key]
        step = max(1, round(value * 0.2))
        for direction, candidate in (("lower", max(1, value - step)), ("upper", value + step)):
            add(f"{key}_{direction}", f"{label}: {value} → {candidate}", {key: candidate})
    key = "coordinator_betweenness_percentile"
    for direction, offset in (("lower", -0.05), ("upper", 0.05)):
        value = round(min(1.0, max(0.0, rules[key] + offset)), 8)
        add(f"{key}_{direction}", f"Связующий: порог посредничества → {value:.0%}", {key: value})
    low, high = rules["transit_ratio_min"], rules["transit_ratio_max"]
    center = (low + high) / 2
    half_width = (high - low) / 2
    for name, multiplier in (("narrow", 0.75), ("wide", 1.25)):
        # A zero-width custom band cannot be narrowed; a wide alternative is
        # still meaningful. Preserve the selected band's centre, even if != 1.
        width = half_width * multiplier
        if half_width == 0 and name == "wide":
            width = min(0.05, center / 2)
        lower = round(max(min(low, 1e-8), center - width), 8)
        upper = round(center + width, 8)
        add(
            f"transit_band_{name}",
            f"Транзит: интервал выход/вход → [{lower:.0%}; {upper:.0%}]",
            {"transit_ratio_min": lower, "transit_ratio_max": upper},
        )
    key = "consolidator_ratio_max"
    for direction, offset in (("lower", -0.05), ("upper", 0.05)):
        value = round(min(0.99999999, max(0.00000001, rules[key] + offset)), 8)
        add(f"{key}_{direction}", f"Консолидатор: максимум выход/вход → {value:.0%}", {key: value})
    return scenarios


def _roles(nodes: list[dict[str, Any]], rules: dict[str, Any]) -> dict[str, Any]:
    # Saved JSON uses null for an unobserved denominator, classify uses NaN.
    rows = [dict(row, pass_through=row["pass_through"] or math.nan) for row in nodes]
    for original, row in zip(nodes, rows, strict=True):
        if original["pass_through"] == 0:
            row["pass_through"] = 0.0
        if classify(row, rules)["role"] != row["role"]:
            raise DataError("Сохранённые роли не соответствуют правилам; пересчитайте исходный набор")
    active = [row["in_deg"] + row["out_deg"] > 0 for row in rows]
    n_active = sum(active)
    alternatives: list[Counter[str]] = [Counter() for _ in rows]
    changed: list[list[str]] = [[] for _ in rows]
    scenarios = []
    for scenario in role_scenarios(rules):
        cfg = rules | scenario["changes"]
        transitions: Counter[tuple[str, str]] = Counter()
        n_changed = 0
        for index, row in enumerate(rows):
            role = classify(row, cfg)["role"]
            alternatives[index][role] += 1
            if role != row["role"]:
                changed[index].append(scenario["id"])
                if active[index]:
                    transitions[(row["role"], role)] += 1
                    n_changed += 1
        scenarios.append(
            scenario
            | {
                "unchanged_fraction": (n_active - n_changed) / n_active if n_active else None,
                "changed_nodes": n_changed,
                "transitions": [
                    {"from_role": source, "to_role": target, "count": count}
                    for (source, target), count in sorted(transitions.items())
                ],
            }
        )
    details = [
        {
            "gid": row["gid"],
            "baseline_role": row["role"],
            "assessed": assessed,
            "unchanged_fraction": counts[row["role"]] / len(scenarios) if assessed else None,
            "alternatives": [
                {"role": role, "scenario_count": count} for role, count in sorted(counts.items())
            ],
            "changed_scenarios": identifiers,
        }
        for row, assessed, counts, identifiers in zip(rows, active, alternatives, changed, strict=True)
    ]
    by_gid = {row["gid"]: row for row in details}
    return {
        "n_nodes": len(nodes),
        "n_active_nodes": n_active,
        "scenario_count": len(scenarios),
        "minimum_unchanged_fraction": min(
            (row["unchanged_fraction"] for row in scenarios if row["unchanged_fraction"] is not None),
            default=None,
        ),
        "scenarios": scenarios,
        "nodes": details,
        "top20": [by_gid[row["gid"]] for row in sorted(nodes, key=lambda row: row["rank"])[:20]],
    }


def _communities(result: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    nodes = result["nodes"]
    directed: nx.DiGraph = nx.DiGraph()
    directed.add_nodes_from(int(row["gid"]) for row in nodes)
    directed.add_edges_from(
        (int(row["src"]), int(row["dst"]), {"sum_kzt": row["sum_kzt"]})
        for row in result["edges"]
    )
    graph = projection(directed)
    active = [gid for gid, degree in graph.degree() if degree]
    connected = graph.subgraph(active) if len(active) != len(graph) else graph
    baseline = {int(row["gid"]): row["cluster_id"] for row in nodes}
    baseline_labels = [baseline[gid] for gid in active]
    baseline_sizes = Counter(baseline_labels)
    top = sorted(nodes, key=lambda row: row["rank"])[:20]
    overlaps: dict[int, list[float]] = {int(row["gid"]): [] for row in top}
    scenarios: list[dict[str, Any]] = []
    warnings: list[str] = []
    status = "complete" if active else "not_applicable"
    seed = rules["random_seed"]
    base_resolution = rules["louvain_resolution"]
    configs = [
        (candidate, base_resolution * factor)
        for factor in (1.0, 0.8, 1.2)
        for candidate in (seed, seed + 1, seed + 2)
        if not (factor == 1 and candidate == seed)
    ]
    for candidate, resolution in configs if active else []:
        if time.monotonic() - started >= COMMUNITY_SECONDS:
            status = "limited"
            warnings.append(
                "Достигнут бюджет 20 секунд между сценариями Louvain: показаны только завершённые. "
                "Один начатый сценарий выполняется полностью; это не жёсткий тайм-аут."
            )
            break
        groups = nx.community.louvain_communities(
            connected, weight="weight", resolution=resolution, seed=candidate
        )
        mapping = {gid: index for index, group in enumerate(groups) for gid in group}
        labels = [mapping[gid] for gid in active]
        sizes = Counter(labels)
        intersections = Counter(zip(baseline_labels, labels, strict=True))
        for gid in overlaps:
            if gid not in mapping:
                continue
            # Excluding the focal node avoids artificially high agreement for
            # tiny communities. Compare peers, not arbitrary cluster numbers.
            overlap = intersections[(baseline[gid], mapping[gid])] - 1
            union = baseline_sizes[baseline[gid]] + sizes[mapping[gid]] - 2 - overlap
            overlaps[gid].append(overlap / union if union else 1.0)
        scenarios.append(
            {
                "id": f"louvain_{candidate}_{resolution:g}",
                "seed": candidate,
                "resolution": resolution,
                "kind": "seed" if resolution == base_resolution else "resolution_and_seed",
                "n_communities": len(groups),
                "adjusted_rand": adjusted_rand(baseline_labels, labels),
            }
        )
    return {
        "status": status,
        "baseline_seed": seed,
        "baseline_resolution": base_resolution,
        "n_active_nodes": len(active),
        "excluded_isolates": len(nodes) - len(active),
        "scenario_count": len(scenarios),
        "planned_scenario_count": len(configs) if active else 0,
        "minimum_adjusted_rand": min((row["adjusted_rand"] for row in scenarios), default=None),
        "scenarios": scenarios,
        "top20": [
            {
                "gid": row["gid"],
                "assessed": bool(overlaps[int(row["gid"])]),
                "baseline_cluster_id": row["cluster_id"],
                "mean_membership_jaccard": math.fsum(overlaps[int(row["gid"])])
                / len(overlaps[int(row["gid"])])
                if overlaps[int(row["gid"])]
                else None,
                "minimum_membership_jaccard": min(overlaps[int(row["gid"])], default=None),
            }
            for row in top
        ],
        "warnings": warnings,
    }


def analyze_robustness(result: dict[str, Any]) -> dict[str, Any]:
    """Diagnose a validated result without changing roles, ranks or CSV exports."""
    if len(result["nodes"]) > MAX_NODES or len(result["edges"]) > MAX_EDGES:
        raise DataError("Проверка устойчивости поддерживает до 10 000 узлов и 50 000 рёбер")
    rules = result["report"]["rules"]
    try:
        return {
            "version": "1",
            "caveat": CAVEAT,
            "roles": _roles(result["nodes"], rules),
            "communities": _communities(result, rules),
        }
    except nx.NetworkXException as exc:
        raise DataError("Не удалось проверить сообщества; исходный результат не изменён") from exc
