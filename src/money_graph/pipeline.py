"""The single computation path used by CLI and HTTP."""

import hashlib
import json
import math
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, cast

import networkx as nx
import pandas as pd

from . import clusters as cluster_analysis
from . import features, temporal
from .investigation import sensitivity
from .loader import FILES, DataError, load
from .roles import LABELS, classify

ROOT = Path(__file__).resolve().parents[2]
EXPORTS = ("nodes_roles.csv", "clusters.csv", "top_nodes.csv")


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
        cfg = json.loads(path.read_text())
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
    ranked = sorted(records, key=lambda row: (-row["priority_score"], row["gid"]))
    for rank, row in enumerate(ranked, 1):
        row["rank"] = rank
    stability = sensitivity(records, rules["priority_weights"])
    clusters, cluster_edges = cluster_analysis.summarize(
        ranked, cast(list[dict[str, Any]], edges.to_dict("records"))
    )
    report = {
        "n_nodes": len(nodes),
        "n_edges": len(edges),
        "n_transactions": len(tx),
        "n_seed": int(nodes.is_seed.sum()),
        "n_clusters": len(clusters),
        "n_isolates": int(frame.isolated.sum()),
        "n_boundary": int(frame.boundary_censored.sum()),
        "n_self_transfer_nodes": int((frame.self_transfer_tx > 0).sum()),
        "self_transfer_kzt": round(float(frame.self_transfer_kzt.sum()), 2),
        "betweenness_method": "exact"
        if len(nodes) <= rules["betweenness_exact_max_nodes"]
        else "sampled",
        "betweenness_pivots": len(nodes)
        if len(nodes) <= rules["betweenness_exact_max_nodes"]
        else min(rules["betweenness_samples"], len(nodes)),
        "n_components": nx.number_weakly_connected_components(graph),
        "n_connected_components": sum(len(c) > 1 for c in nx.weakly_connected_components(graph)),
        "sensitivity": stability,
        "turnover_kzt": round(float(edges.sum_kzt.sum()), 2),
        "period_from": str(tx.date.min().date()) if len(tx) else None,
        "period_to": str(tx.date.max().date()) if len(tx) else None,
        "role_counts": {role: sum(row["role"] == role for row in records) for role in LABELS},
        "rules_version": rules["version"],
        "rules": rules,
        "input_sha256": {
            name: hashlib.sha256((data_dir / f"{name}.parquet").read_bytes()).hexdigest()
            for name in FILES
        },
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
    nodes = result["nodes"]
    if len(nodes) != result["report"]["n_nodes"] or len({r["gid"] for r in nodes}) != len(nodes):
        raise DataError("Потеря или дублирование узлов при расчёте")
    for row in nodes:
        if (
            row["role"] not in LABELS
            or not row["evidence"]
            or len(row["evidence"]) > 200
            or not all(
                math.isfinite(row[k]) and 0 <= row[k] <= 1 for k in ("role_score", "priority_score")
            )
        ):
            raise DataError("Некорректный результат классификации")
    if sum(c["n_nodes"] for c in result["clusters"]) != len(nodes):
        raise DataError("Неполное покрытие кластеров")


def write_result(result: dict[str, Any], out_dir: Path) -> None:
    """Publish a complete generation; restore the previous directory on failure."""
    out_dir = out_dir.resolve()
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}-", dir=out_dir.parent))
    backup = out_dir.with_name(f".{out_dir.name}-previous")
    try:
        columns = ["gid", "role", "role_score", "cluster_id", "priority_score", "evidence"]
        pd.DataFrame(result["nodes"])[columns].to_csv(
            staging / EXPORTS[0], index=False, float_format="%.8f"
        )
        clusters = pd.DataFrame(result["clusters"])[
            ["cluster_id", "n_nodes", "n_seed", "sum_kzt_internal", "top_gids", "hypothesis"]
        ]
        clusters["top_gids"] = clusters.top_gids.map(
            lambda ids: json.dumps(ids, ensure_ascii=False)
        )
        clusters.to_csv(staging / EXPORTS[1], index=False)
        pd.DataFrame(result["top"]).to_csv(staging / EXPORTS[2], index=False)
        for name, payload in (("result.json", result), ("run_report.json", result["report"])):
            (staging / name).write_text(
                json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            )
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
