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

from . import features
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
    cfg = json.loads((path or ROOT / "config/rules.json").read_text())
    weights = cfg["priority_weights"]
    required = {"pagerank", "betweenness", "seed_reach", "in_deg", "out_deg"}
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
    frame["cluster_id"] = frame.gid.map(mapping)
    records = cast(list[dict[str, Any]], frame.to_dict("records"))
    for row in records:
        row.update(classify(row, rules))
        row["priority_score"] = round(row["priority_score"], 6)
        row["priority_parts"] = {
            name: round(row[f"p_{name}"] * weight, 6)
            for name, weight in rules["priority_weights"].items()
        }
        row["why"] = (
            f"Seed-предков: {row['seed_reach']}; входящих связей: {row['in_deg']}; "
            f"исходящих: {row['out_deg']}; PageRank: {row['pagerank']:.5f}; "
            f"посредничество: {row['betweenness']:.5f}"
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
    ranked = sorted(records, key=lambda row: (-row["priority_score"], row["gid"]))
    for rank, row in enumerate(ranked, 1):
        row["rank"] = rank
    clusters = []
    for cluster_id in sorted(set(mapping.values())):
        members = [row for row in ranked if row["cluster_id"] == cluster_id]
        ids = {row["gid"] for row in members}
        internal = float(edges.loc[edges.src.isin(ids) & edges.dst.isin(ids), "sum_kzt"].sum())
        n_seed = sum(row["is_seed"] for row in members)
        dominant = pd.Series([row["role"] for row in members]).value_counts().index[0]
        hypothesis = (
            f"Сообщество из {len(members)} узлов, {n_seed} seed; "
            f"преобладает «{LABELS[dominant]}». Структурная гипотеза, требует проверки."
        )
        if len(members) == 1 and members[0]["isolated"]:
            hypothesis = "Изолированный узел: связи и назначение не установлены в данной выгрузке."
        clusters.append(
            {
                "cluster_id": cluster_id,
                "n_nodes": len(members),
                "n_seed": n_seed,
                "sum_kzt_internal": round(internal, 2),
                "top_gids": [str(row["gid"]) for row in members[:5]],
                "hypothesis": hypothesis,
            }
        )
    report = {
        "n_nodes": len(nodes),
        "n_edges": len(edges),
        "n_transactions": len(tx),
        "n_seed": int(nodes.is_seed.sum()),
        "n_clusters": len(clusters),
        "n_isolates": int(frame.isolated.sum()),
        "n_boundary": int(frame.boundary_censored.sum()),
        "n_components": nx.number_weakly_connected_components(graph),
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
        columns = [
            "gid",
            "role",
            "role_score",
            "cluster_id",
            "priority_score",
            "evidence",
            "in_deg",
            "out_deg",
            "in_kzt",
            "out_kzt",
            "in_tx",
            "out_tx",
            "pagerank",
            "betweenness",
            "seed_reach",
            "pass_through",
            "depth",
            "is_seed",
            "boundary_censored",
            "isolated",
        ]
        pd.DataFrame(result["nodes"])[columns].to_csv(
            staging / EXPORTS[0], index=False, float_format="%.8f"
        )
        clusters = pd.DataFrame(result["clusters"])
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
