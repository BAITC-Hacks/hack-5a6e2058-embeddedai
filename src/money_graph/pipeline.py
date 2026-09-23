"""The single computation path used by CLI and HTTP."""

import hashlib
import json
import math
import os
import shutil
import tempfile
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, cast

import networkx as nx
import pandas as pd

from . import anomalies, features, temporal
from . import clusters as cluster_analysis
from .exports import export_manifest
from .investigation import sensitivity
from .loader import FILES, DataError, load
from .offline_report import render_report
from .roles import LABELS, classify

ROOT = Path(__file__).resolve().parents[2]
EXPORTS = ("nodes_roles.csv", "clusters.csv", "top_nodes.csv")
RESULT_VERSION = 2


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def read_rules(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        source_path = ROOT / "config/rules.json"
        path = (
            source_path if source_path.is_file() else Path(__file__).parent / "resources/rules.json"
        )
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataError("Не удалось прочитать конфигурацию правил JSON") from exc
    integers = (
        "random_seed",
        "max_depth",
        "betweenness_samples",
        "betweenness_exact_max_nodes",
        "coordinator_min_seeds",
        "coordinator_min_in",
        "coordinator_min_out",
        "distributor_min_out",
        "consolidator_min_in",
    )
    numbers = (
        "louvain_resolution",
        "coordinator_betweenness_percentile",
        "transit_ratio_min",
        "transit_ratio_max",
        "consolidator_ratio_max",
        "incomplete_support_multiplier",
    )
    if not isinstance(cfg, dict) or not isinstance(cfg.get("version"), str) or not cfg["version"]:
        raise DataError("В конфигурации нужна строковая version")
    if any(
        type(cfg.get(k)) is not int or cfg[k] < (0 if k == "random_seed" else 1) for k in integers
    ):
        raise DataError("Пороги количества и seed должны быть корректными целыми числами")
    if any(
        type(cfg.get(k)) not in (int, float) or not math.isfinite(cfg[k]) or cfg[k] < 0
        for k in numbers
    ):
        raise DataError("Пороги правил должны быть конечными неотрицательными числами")
    if (
        cfg["max_depth"] != 4
        or cfg["louvain_resolution"] <= 0
        or not 0 <= cfg["coordinator_betweenness_percentile"] <= 1
        or not 0 < cfg["transit_ratio_min"] <= cfg["transit_ratio_max"]
    ):
        raise DataError("Некорректные границы правил или глубина (поддерживается 4)")
    if not isinstance(cfg.get("priority_weights"), dict) or any(
        type(v) not in (int, float) for v in cfg["priority_weights"].values()
    ):
        raise DataError("Веса приоритета должны быть числами")
    weights = cfg["priority_weights"]
    required = {"pagerank", "betweenness", "seed_reach", "in_deg", "out_deg", "volume"}
    if set(weights) != required or any(not math.isfinite(v) or v < 0 for v in weights.values()):
        raise DataError("Неверные веса приоритета")
    if not math.isclose(sum(weights.values()), 1):
        raise DataError("Сумма весов приоритета должна быть 1")
    if (
        not 0 < cfg["consolidator_ratio_max"] < 1
        or not 0 <= cfg["incomplete_support_multiplier"] <= 1
    ):
        raise DataError("Некорректные пороги ролей")
    return cfg


def input_fingerprints(data_dir: Path) -> dict[str, str]:
    """Hash original bytes without allocating another copy of each Parquet file."""
    digests = {}
    for name in FILES:
        with (data_dir / f"{name}.parquet").open("rb") as source:
            digests[name] = hashlib.file_digest(source, "sha256").hexdigest()
    return digests


def analyze(data_dir: Path, rules_path: Path | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    rules = read_rules(rules_path)
    nodes, edges, tx = load(data_dir)
    graph = features.build_graph(nodes, edges)
    frame = features.compute(graph, nodes, rules)
    mapping = features.communities(graph, rules)
    dates = temporal.summarize(tx, list(graph))
    frame["cluster_id"] = frame.gid.map(mapping)
    records = cast(list[dict[str, Any]], frame.to_dict("records"))
    n_active = int(((frame.in_deg + frame.out_deg) > 0).sum())
    for row in records:
        row.update(classify(row, rules))
        row["temporal"] = dates[row["gid"]]
        row["priority_score"] = round(row["priority_score"], 6)
        row["priority_parts"] = {
            name: round(row[f"p_{name}"] * weight, 6)
            for name, weight in rules["priority_weights"].items()
        }
        names = {
            "pagerank": "входящая значимость",
            "betweenness": "посредничество",
            "seed_reach": "охват seed",
            "in_deg": "плательщики",
            "out_deg": "получатели",
            "volume": "денежный объём",
        }
        strongest = sorted(row["priority_parts"].items(), key=lambda item: -item[1])[:3]
        row["why"] = (
            "Вклад в приоритет: "
            + "; ".join(f"{names[key]} +{value * 100:.1f} п.п." for key, value in strongest)
            + f". Seed-предков {row['seed_reach']}; объём max(вход,выход) {row['volume']:,.0f} KZT. {row['evidence']}"
        )
        row["next_checks"] = [
            "Проверить входящие из других банков и переводы ниже порога 5 000 KZT"
        ]
        if row["boundary_censored"]:
            row["next_checks"].append("Запросить следующий уровень исходящих переводов")
        if row["is_seed"]:
            row["next_checks"].append("Дополнить историю входящих переводов seed-клиента")
        if row["role"] in ("transit", "terminal", "consolidator"):
            row["next_checks"].append("Проверить точное время операций и последующие периоды")
        if row["temporal"]["max_same_day_senders"] >= 3:
            row["next_checks"].append(
                "Проверить назначение синхронных поступлений от нескольких плательщиков"
            )
        patterns = row["temporal"]["patterns"]
        if patterns["activity"]["spike_day_count"]:
            row["next_checks"].append(
                "Сопоставить дни всплесков с обычной активностью и назначениями операций"
            )
        if patterns["repeated_amounts"]["group_count"]:
            row["next_checks"].append(
                "Проверить повторные малые суммы: регулярные платежи или гипотеза дробления; запросить операции ниже порога"
            )
    anomalies.annotate(records)
    ranked = sorted(records, key=lambda row: (-row["priority_score"], row["gid"]))
    for rank, row in enumerate(ranked, 1):
        row["rank"] = rank
    stability = sensitivity(records, rules["priority_weights"])
    clusters, cluster_edges = cluster_analysis.summarize(
        ranked, cast(list[dict[str, Any]], edges.to_dict("records"))
    )
    report = {
        "result_version": RESULT_VERSION,
        "temporal_patterns_version": "1",
        "n_activity_spike_nodes": sum(
            row["temporal"]["patterns"]["activity"]["spike_day_count"] > 0 for row in records
        ),
        "n_synchronous_nodes": sum(
            row["temporal"]["patterns"]["synchronous"]["day_count"] > 0 for row in records
        ),
        "n_repeated_amount_nodes": sum(
            row["temporal"]["patterns"]["repeated_amounts"]["group_count"] > 0 for row in records
        ),
        "n_nodes": len(nodes),
        "n_active_nodes": n_active,
        "n_anomalous_profiles": sum(bool(row["anomaly_profile"]["signals"]) for row in records),
        "n_edges": len(edges),
        "n_transactions": len(tx),
        "n_seed": int(nodes.is_seed.sum()),
        "n_clusters": len(clusters),
        "n_isolates": int(frame.isolated.sum()),
        "n_boundary": int(frame.boundary_censored.sum()),
        "n_self_transfer_nodes": int((frame.self_transfer_tx > 0).sum()),
        "self_transfer_kzt": round(math.fsum(frame.self_transfer_kzt), 2),
        "betweenness_method": "exact"
        if n_active <= rules["betweenness_exact_max_nodes"]
        else "sampled",
        "betweenness_pivots": n_active
        if n_active <= rules["betweenness_exact_max_nodes"]
        else min(rules["betweenness_samples"], n_active),
        "n_components": nx.number_weakly_connected_components(graph),
        "n_connected_components": sum(len(c) > 1 for c in nx.weakly_connected_components(graph)),
        "sensitivity": stability,
        "turnover_kzt": round(math.fsum(edges.sum_kzt), 2),
        "period_from": str(tx.date.min().date()) if len(tx) else None,
        "period_to": str(tx.date.max().date()) if len(tx) else None,
        "role_counts": {role: sum(row["role"] == role for row in records) for role in LABELS},
        "rules_version": rules["version"],
        "rules": rules,
        "input_sha256": input_fingerprints(data_dir),
        "warnings": [
            "Наблюдаются только исходящие внутрибанковские переводы на 4 уровня",
            "Отсутствие перевода не доказывает отсутствие движения денег",
            "Роли и приоритеты — гипотезы для проверки, не вывод о виновности",
        ],
        "runtime_seconds": round(time.perf_counter() - started, 3),
    }
    # JSON must retain int64 IDs exactly, including at the browser boundary.
    for row in records:
        row["gid"] = str(row["gid"])
    edge_records = [
        {
            "src": str(r["src"]),
            "dst": str(r["dst"]),
            "sum_kzt": float(r["sum_kzt"]),
            "n_tx": int(r["n_tx"]),
        }
        for r in cast(list[dict[str, Any]], edges.to_dict("records"))
    ]
    top = [
        {key: row[key] for key in ("rank", "gid", "role", "priority_score", "why")}
        for row in ranked[:50]
    ]
    result = clean(
        {
            "report": report,
            "nodes": records,
            "edges": edge_records,
            "clusters": clusters,
            "cluster_edges": cluster_edges,
            "top": top,
        }
    )
    validate_result(result)
    return result


def validate_result(result: dict[str, Any]) -> None:
    """Check stored data before the API/UI consumes it; do not rerun the analysis."""
    try:
        _validate_result(result)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError("Повреждена структура сохранённого результата") from exc


def _validate_result(result: dict[str, Any]) -> None:
    def require(condition: bool, field: str) -> None:
        if not condition:
            raise DataError(f"Некорректный сохранённый результат: {field}")

    def number(value: Any, minimum: float = 0, maximum: float = math.inf) -> bool:
        return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum

    def integer(value: Any, minimum: int = 0) -> bool:
        return type(value) is int and value >= minimum

    def identifier(value: Any) -> bool:
        return (
            isinstance(value, str)
            and value.isascii()
            and value.removeprefix("-").isdecimal()
            and len(value) <= 20
            and str(int(value)) == value
            and -(2**63) <= int(value) <= 2**63 - 1
        )

    def text(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def strings(value: Any) -> bool:
        return isinstance(value, list) and all(text(item) for item in value)

    def fields(row: Any, checks: dict[str, Any], name: str) -> None:
        require(isinstance(row, dict), name)
        for key, check in checks.items():
            require(key in row and check(row[key]), f"{name}.{key}")

    def iso_date(value: Any) -> bool:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value

    require(isinstance(result, dict), "root")
    for name in ("nodes", "edges", "clusters", "cluster_edges", "top"):
        require(isinstance(result.get(name), list), name)
    report = result["report"]
    fields(
        report,
        {
            **dict.fromkeys(
                (
                    "n_nodes",
                    "n_active_nodes",
                    "n_anomalous_profiles",
                    "n_edges",
                    "n_transactions",
                    "n_seed",
                    "n_clusters",
                    "n_isolates",
                    "n_boundary",
                    "n_components",
                    "n_connected_components",
                    "betweenness_pivots",
                ),
                integer,
            ),
            "runtime_seconds": number,
            "turnover_kzt": number,
            "rules_version": text,
            "warnings": strings,
            "rules": lambda value: isinstance(value, dict),
            "betweenness_method": lambda value: value in ("exact", "sampled"),
            "period_from": lambda value: value is None or iso_date(value),
            "period_to": lambda value: value is None or iso_date(value),
        },
        "report",
    )
    require(report["rules"].get("version") == report["rules_version"], "rules_version")
    weights = report["rules"]["priority_weights"]
    metrics = {"pagerank", "betweenness", "seed_reach", "in_deg", "out_deg", "volume"}
    require(isinstance(weights, dict) and set(weights) == metrics, "priority_weights")
    require(
        all(number(w, maximum=1) for w in weights.values())
        and math.isclose(sum(weights.values()), 1),
        "priority_weights",
    )
    digests = report["input_sha256"]
    require(isinstance(digests, dict) and set(digests) == set(FILES), "input_sha256")
    require(
        all(
            isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v)
            for v in digests.values()
        ),
        "input_sha256",
    )
    nodes = result["nodes"]
    require(bool(nodes) and len(nodes) == report["n_nodes"], "n_nodes")

    def score(value: Any) -> bool:
        return number(value, maximum=1)

    def boolean(value: Any) -> bool:
        return type(value) is bool

    node_checks = {
        "gid": identifier,
        "role": lambda value: isinstance(value, str) and value in LABELS,
        "role_score": score,
        "priority_score": score,
        "cluster_id": integer,
        "depth": lambda value: integer(value) and value <= 4,
        "rank": lambda value: integer(value, 1),
        "evidence": lambda value: text(value) and len(value) <= 200,
        "why": text,
        "warnings": strings,
        "next_checks": strings,
        **dict.fromkeys(("is_seed", "boundary_censored", "isolated", "self_only"), boolean),
        **dict.fromkeys(
            ("in_deg", "out_deg", "in_tx", "out_tx", "seed_reach", "self_transfer_tx"), integer
        ),
        **dict.fromkeys(
            ("in_kzt", "out_kzt", "volume", "pagerank", "betweenness", "self_transfer_kzt"), number
        ),
        "pass_through": lambda value: value is None or number(value),
    }
    for row in nodes:
        fields(row, node_checks, "node")
        bounds = row["rank_range"]
        require(
            isinstance(bounds, list)
            and len(bounds) == 2
            and all(integer(v, 1) for v in bounds)
            and bounds[0] <= row["rank"] <= bounds[1] <= len(nodes),
            "rank_range",
        )
        parts = row["priority_parts"]
        require(
            isinstance(parts, dict)
            and set(parts) == metrics
            and all(score(v) for v in parts.values()),
            "priority_parts",
        )
        require(
            math.isclose(sum(parts.values()), row["priority_score"], abs_tol=4e-6),
            "priority_parts sum",
        )
        require(
            strings(row["matched_rules"]) and all(role in LABELS for role in row["matched_rules"]),
            "matched_rules",
        )
        require(isinstance(row["rule_trace"], list), "rule_trace")
        for trace in row["rule_trace"]:
            fields(
                trace,
                {
                    "role": lambda v: isinstance(v, str) and v in LABELS,
                    "matched": boolean,
                    "support": score,
                    "raw_support": number,
                    "observation_multiplier": score,
                },
                "rule_trace",
            )
        temporal_data = row["temporal"]
        fields(
            temporal_data,
            {
                "daily": lambda v: isinstance(v, list),
                "active_days": integer,
                "max_same_day_senders": integer,
                "peak_in_share": score,
                "matched_1_2d_share": score,
                "matched_1_2d_kzt": number,
                "same_day_overlap_kzt": number,
                "in_kzt": number,
                "out_kzt": number,
            },
            "temporal",
        )
        for day in temporal_data["daily"]:
            fields(
                day,
                {
                    "date": iso_date,
                    "in_kzt": number,
                    "out_kzt": number,
                    "in_tx": integer,
                    "out_tx": integer,
                    "senders": integer,
                    "receivers": integer,
                },
                "daily",
            )
        dates = [day["date"] for day in temporal_data["daily"]]
        require(
            dates == sorted(set(dates)) and len(dates) == temporal_data["active_days"],
            "daily dates",
        )
        if report.get("result_version") == RESULT_VERSION or "patterns" in temporal_data:
            patterns = temporal_data["patterns"]
            fields(patterns, {"version": lambda v: v == "1", "caveat": text}, "temporal.patterns")
            activity = patterns["activity"]
            fields(
                activity,
                {
                    "status": lambda v: v in ("assessed", "insufficient_history"),
                    "active_days": integer,
                    "minimum_days": lambda v: v == 7,
                    "baseline_median_tx": lambda v: v is None or number(v),
                    "threshold_tx": lambda v: v is None or number(v),
                    "spike_day_count": integer,
                    "spike_days": lambda v: isinstance(v, list) and len(v) <= 5,
                    "rule": text,
                },
                "patterns.activity",
            )
            require(
                activity["active_days"] == len(dates)
                and activity["spike_day_count"] >= len(activity["spike_days"]),
                "activity counts",
            )
            for spike in activity["spike_days"]:
                fields(
                    spike,
                    {
                        "date": lambda v: v in dates,
                        "n_tx": integer,
                        "in_tx": integer,
                        "out_tx": integer,
                        "in_kzt": number,
                        "out_kzt": number,
                    },
                    "activity.spike",
                )
                require(
                    activity["status"] == "assessed"
                    and number(activity["threshold_tx"])
                    and spike["n_tx"] > activity["threshold_tx"],
                    "activity threshold",
                )
            sync = patterns["synchronous"]
            fields(
                sync,
                {
                    "minimum_senders": lambda v: v == 3,
                    "day_count": integer,
                    "days": lambda v: isinstance(v, list) and len(v) <= 5,
                },
                "patterns.synchronous",
            )
            require(sync["day_count"] >= len(sync["days"]), "synchronous count")
            for signal in sync["days"]:
                fields(
                    signal,
                    {
                        "date": lambda v: v in dates,
                        "senders": lambda v: integer(v, 3),
                        "in_tx": integer,
                        "in_kzt": number,
                    },
                    "synchronous day",
                )
            repeat = patterns["repeated_amounts"]
            fields(
                repeat,
                {
                    "minimum_repeats": lambda v: v == 3,
                    "minimum_transactions": lambda v: v == 8,
                    "group_count": integer,
                    "groups": lambda v: isinstance(v, list) and len(v) <= 5,
                    "rule": text,
                },
                "patterns.repeated_amounts",
            )
            require(repeat["group_count"] >= len(repeat["groups"]), "repeat count")
            for direction in ("in", "out"):
                fields(
                    repeat["directions"][direction],
                    {
                        "status": lambda v: v in ("assessed", "insufficient_transactions"),
                        "n_transactions": integer,
                        "q1_kzt": lambda v: v is None or number(v),
                    },
                    "repeat.direction",
                )
            for group in repeat["groups"]:
                fields(
                    group,
                    {
                        "date": lambda v: v in dates,
                        "direction": lambda v: v in ("in", "out"),
                        "amount_kzt": number,
                        "n_tx": lambda v: integer(v, 3),
                        "total_kzt": number,
                        "counterparties": lambda v: integer(v, 1),
                    },
                    "repeat.group",
                )
                direction = repeat["directions"][group["direction"]]
                require(
                    direction["status"] == "assessed"
                    and number(direction["q1_kzt"])
                    and group["amount_kzt"] <= direction["q1_kzt"],
                    "repeat threshold",
                )
        profile = row["anomaly_profile"]
        fields(
            profile,
            {
                "cohort_depth": integer,
                "cohort_size": integer,
                "minimum_cohort_size": lambda value: value == anomalies.MINIMUM_COHORT_SIZE,
                "signals": lambda value: isinstance(value, list),
                "caveat": text,
            },
            "anomaly_profile",
        )
        require(profile["cohort_depth"] == row["depth"], "anomaly cohort_depth")
        seen_metrics = set()
        for signal in profile["signals"]:
            fields(
                signal,
                {
                    "metric": lambda value: isinstance(value, str) and value in anomalies.METRICS,
                    **dict.fromkeys(("value", "threshold", "q1", "q3"), number),
                    "text": text,
                },
                "anomaly signal",
            )
            require(
                signal["metric"] not in seen_metrics
                and row["in_deg"] + row["out_deg"] > 0
                and profile["cohort_size"] >= anomalies.MINIMUM_COHORT_SIZE
                and signal["value"] == row[signal["metric"]]
                and signal["q3"] > signal["q1"]
                and signal["threshold"] == signal["q3"] + 3 * (signal["q3"] - signal["q1"])
                and signal["value"] > signal["threshold"],
                "anomaly signal evidence",
            )
            seen_metrics.add(signal["metric"])
    by_id = {row["gid"]: row for row in nodes}
    require(len(by_id) == len(nodes), "duplicate gid")
    n_active = sum(row["in_deg"] + row["out_deg"] > 0 for row in nodes)
    exact = n_active <= report["rules"]["betweenness_exact_max_nodes"]
    require(report["n_active_nodes"] == n_active, "n_active_nodes")
    cohort_sizes = Counter(row["depth"] for row in nodes if row["in_deg"] + row["out_deg"] > 0)
    require(
        all(row["anomaly_profile"]["cohort_size"] == cohort_sizes[row["depth"]] for row in nodes),
        "anomaly cohort_size",
    )
    require(
        report["n_anomalous_profiles"]
        == sum(bool(row["anomaly_profile"]["signals"]) for row in nodes),
        "n_anomalous_profiles",
    )
    require(report["betweenness_method"] == ("exact" if exact else "sampled"), "betweenness_method")
    require(
        report["betweenness_pivots"]
        == (n_active if exact else min(report["rules"]["betweenness_samples"], n_active)),
        "betweenness_pivots",
    )
    ranked = sorted(nodes, key=lambda row: (-row["priority_score"], int(row["gid"])))
    require([row["rank"] for row in ranked] == list(range(1, len(nodes) + 1)), "ranks")
    expected_counts = Counter(row["role"] for row in nodes)
    require(
        report["role_counts"] == {role: expected_counts[role] for role in LABELS}, "role_counts"
    )
    for count, field in (
        ("n_seed", "is_seed"),
        ("n_isolates", "isolated"),
        ("n_boundary", "boundary_censored"),
    ):
        require(report[count] == sum(row[field] for row in nodes), count)
    flow_amounts: dict[tuple[int, int], list[float]] = defaultdict(list)
    edge_counts: Counter[tuple[int, int]] = Counter()
    pairs = set()
    for edge in result["edges"]:
        fields(
            edge,
            {
                "src": identifier,
                "dst": identifier,
                "sum_kzt": lambda v: number(v) and v > 0,
                "n_tx": lambda v: integer(v, 1),
            },
            "edge",
        )
        src, dst = edge["src"], edge["dst"]
        require(src in by_id and dst in by_id and (src, dst) not in pairs, "edge endpoints")
        pairs.add((src, dst))
        cluster_pair = (by_id[src]["cluster_id"], by_id[dst]["cluster_id"])
        flow_amounts[cluster_pair].append(edge["sum_kzt"])
        edge_counts[cluster_pair] += 1
    flows = {pair: math.fsum(amounts) for pair, amounts in flow_amounts.items()}
    require(len(pairs) == report["n_edges"], "n_edges")
    require(
        sum(edge["n_tx"] for edge in result["edges"]) == report["n_transactions"], "n_transactions"
    )
    require(
        math.isclose(
            math.fsum(edge["sum_kzt"] for edge in result["edges"]),
            report["turnover_kzt"],
            rel_tol=0,
            abs_tol=0.01,
        ),
        "turnover_kzt",
    )
    members: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in ranked:
        members[row["cluster_id"]].append(row)
    cluster_ids = set()
    for cluster in result["clusters"]:
        fields(
            cluster,
            {
                "cluster_id": integer,
                "n_nodes": lambda v: integer(v, 1),
                "n_seed": integer,
                "sum_kzt_internal": number,
                "top_gids": lambda v: isinstance(v, list) and all(identifier(gid) for gid in v),
                "hypothesis": text,
                "incoming_kzt": number,
                "outgoing_kzt": number,
                "boundary_nodes": integer,
            },
            "cluster",
        )
        cid = cluster["cluster_id"]
        require(cid in members and cid not in cluster_ids, "cluster membership")
        cluster_ids.add(cid)
        group = members[cid]
        require(
            cluster["n_nodes"] == len(group)
            and cluster["n_seed"] == sum(row["is_seed"] for row in group),
            "cluster counts",
        )
        require(cluster["top_gids"] == [row["gid"] for row in group[:5]], "cluster top_gids")
        require(
            cluster["role_counts"] == dict(Counter(row["role"] for row in group)),
            "cluster role_counts",
        )
        require(
            math.isclose(
                cluster["sum_kzt_internal"], flows.get((cid, cid), 0.0), rel_tol=0, abs_tol=0.01
            ),
            "cluster internal turnover",
        )
    require(
        cluster_ids == set(members) and len(cluster_ids) == report["n_clusters"],
        "clusters coverage",
    )
    linked = set()
    for edge in result["cluster_edges"]:
        fields(
            edge,
            {"src": integer, "dst": integer, "sum_kzt": number, "n_edges": lambda v: integer(v, 1)},
            "cluster_edge",
        )
        pair = (edge["src"], edge["dst"])
        require(
            pair[0] != pair[1] and pair not in linked and pair in edge_counts,
            "cluster_edge endpoints",
        )
        require(
            edge["n_edges"] == edge_counts[pair]
            and math.isclose(edge["sum_kzt"], flows[pair], rel_tol=0, abs_tol=0.01),
            "cluster_edge totals",
        )
        linked.add(pair)
    require(
        linked == {pair for pair in edge_counts if pair[0] != pair[1]}, "cluster_edges coverage"
    )
    top_fields = ("rank", "gid", "role", "priority_score", "why")
    require(
        result["top"] == [{key: row[key] for key in top_fields} for row in ranked[:50]],
        "top_nodes schema/ranking",
    )
    stability = report["sensitivity"]
    fields(
        stability,
        {
            "top_n": lambda v: integer(v, 1) and v == min(20, len(nodes)),
            "minimum_top_overlap": score,
            "caveat": text,
            "scenarios": lambda v: isinstance(v, list) and len(v) == 2 * len(metrics),
        },
        "sensitivity",
    )
    for scenario in stability["scenarios"]:
        fields(
            scenario,
            {
                "metric": lambda v: isinstance(v, str) and v in metrics,
                "multiplier": lambda v: v in (0.8, 1.2),
                "top_overlap": score,
            },
            "sensitivity scenario",
        )


def write_result(result: dict[str, Any], out_dir: Path) -> None:
    """Publish a complete generation; restore the previous directory on failure."""
    out_dir = out_dir.resolve()
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}-", dir=out_dir.parent))
    backup = out_dir.with_name(f".{out_dir.name}-previous")
    try:
        columns = ["gid", "role", "role_score", "cluster_id", "priority_score", "evidence"]
        pd.DataFrame(result["nodes"])[columns].to_csv(
            staging / EXPORTS[0],
            index=False,
            float_format="%.8f",
            encoding="utf-8",
            lineterminator="\n",
        )
        clusters = pd.DataFrame(result["clusters"])[
            ["cluster_id", "n_nodes", "n_seed", "sum_kzt_internal", "top_gids", "hypothesis"]
        ]
        clusters["top_gids"] = clusters.top_gids.map(
            lambda ids: json.dumps(ids, ensure_ascii=False)
        )
        clusters.to_csv(staging / EXPORTS[1], index=False, encoding="utf-8", lineterminator="\n")
        pd.DataFrame(result["top"]).to_csv(
            staging / EXPORTS[2], index=False, encoding="utf-8", lineterminator="\n"
        )
        for name, payload in (("result.json", result), ("run_report.json", result["report"])):
            (staging / name).write_text(
                json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                encoding="utf-8",
            )
        (staging / "report.html").write_text(render_report(result), encoding="utf-8")
        # A serialization error must never replace a previously valid generation.
        export_manifest(result, staging)
        if backup.exists():
            raise DataError(f"Обнаружена резервная выгрузка {backup}; проверьте предыдущий запуск")
        if out_dir.exists():
            os.replace(out_dir, backup)
        try:
            os.replace(staging, out_dir)
        except Exception:
            if backup.exists():
                os.replace(backup, out_dir)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
