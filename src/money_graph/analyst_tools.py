"""Bounded, read-only graph tools for a conversational analyst.

Tool arguments never become code or queries. Every reported metric is read from
the validated run or computed from its observed edges; no tool accesses a network.
"""

import json
import math
from copy import deepcopy
from datetime import date
from typing import Any

import networkx as nx

from .graph_query import LIMITATIONS, execute_query
from .loader import DataError
from .roles import LABELS

MAX_OUTPUT_CHARS = 25_000
METRICS = (
    "priority_score",
    "rank",
    "volume",
    "in_kzt",
    "out_kzt",
    "in_deg",
    "out_deg",
    "role_score",
    "seed_reach",
    "pagerank",
    "betweenness",
    "anomaly_count",
)
SUMMARY_KEYS = (
    "gid",
    "role",
    "role_score",
    "priority_score",
    "rank",
    "cluster_id",
    "depth",
    "is_seed",
    "in_deg",
    "out_deg",
    "in_kzt",
    "out_kzt",
    "volume",
    "evidence",
)


def _enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def _nullable(kind: str, **kwargs: Any) -> dict[str, Any]:
    return {"type": [kind, "null"], **kwargs}


def _tool(name: str, description: str, **properties: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


GIDS = {"type": "array", "items": {"type": "string"}, "maxItems": 20}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 20}
TOOL_SCHEMAS = [
    _tool(
        "inspect_graph",
        "Overview, data quality or exact scoring methodology of this run.",
        topic=_enum("overview", "methodology", "quality"),
    ),
    _tool(
        "find_nodes",
        "Find/rank nodes with AND filters. Null means no filter; gids=[] searches the entire graph. All money is KZT. Anomaly is a descriptive cohort outlier, not guilt.",
        gids=GIDS,
        role=_nullable("string", enum=[None, *LABELS]),
        depth=_nullable("integer", minimum=0, maximum=4),
        cluster_id=_nullable("integer", minimum=0),
        seeds_only={"type": "boolean"},
        anomaly_only={"type": "boolean"},
        min_in_kzt=_nullable("number", minimum=0),
        min_out_kzt=_nullable("number", minimum=0),
        min_volume=_nullable("number", minimum=0),
        sort_by=_enum(*METRICS),
        order=_enum("asc", "desc"),
        limit=LIMIT,
    ),
    _tool(
        "inspect_nodes",
        "Explain or compare given nodes. Temporal uses saved daily aggregates; at most 3 nodes and 31 displayed days per node, exact totals for requested date range. Null dates mean full period.",
        gids=GIDS,
        detail=_enum("overview", "role", "temporal", "anomaly", "next_checks", "compare"),
        date_from=_nullable("string"),
        date_to=_nullable("string"),
        limit=LIMIT,
    ),
    _tool(
        "trace_flows",
        "Directed observed links: common recipients/senders, shortest path between exactly two gids, or bounded cycle examples. min_sources=0 means all selected nodes; max_hops=1 means direct transfers. Paths do not trace the same money or temporal order.",
        operation=_enum("common_recipients", "common_senders", "path", "cycles"),
        gids=GIDS,
        max_hops={"type": "integer", "minimum": 1, "maximum": 4},
        min_sources={"type": "integer", "minimum": 0, "maximum": 20},
        limit=LIMIT,
    ),
    _tool(
        "inspect_community",
        "Summarize a detected community and its top members. Specify exactly one of cluster_id or gid; the other must be null.",
        cluster_id=_nullable("integer", minimum=0),
        gid=_nullable("string"),
        limit=LIMIT,
    ),
    _tool(
        "assess_removal",
        "What-if: remove the specified nodes from observed graph, measure weak connectivity and incident turnover. This is not a forecast of blocked funds; up to 25000 nodes/100000 edges.",
        gids=GIDS,
    ),
]


def _validate(value: Any, schema: dict[str, Any], label: str) -> None:
    kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    actual = (
        "null"
        if value is None
        else "boolean"
        if type(value) is bool
        else "integer"
        if type(value) is int
        else "number"
        if type(value) is float
        else "string"
        if isinstance(value, str)
        else "array"
        if isinstance(value, list)
        else "object"
        if isinstance(value, dict)
        else "invalid"
    )
    if actual not in kinds and not (actual == "integer" and "number" in kinds):
        raise DataError(f"Некорректный тип аргумента {label}")
    if "enum" in schema and value not in schema["enum"]:
        raise DataError(f"Недопустимое значение {label}")
    if value is None:
        return
    if actual in ("integer", "number"):
        if (
            abs(value) > 1e308
            or not math.isfinite(value)
            or value < schema.get("minimum", -math.inf)
            or value > schema.get("maximum", math.inf)
        ):
            raise DataError(f"Аргумент {label} вне допустимых границ")
    if actual == "string" and len(value) > 100:
        raise DataError(f"Слишком длинный аргумент {label}")
    if actual == "array":
        if len(value) > schema.get("maxItems", 20):
            raise DataError(f"Слишком много значений {label}")
        for item in value:
            _validate(item, schema["items"], label)
    if actual == "object":
        if set(value) != set(schema["properties"]):
            raise DataError(f"Нужны ровно объявленные аргументы инструмента {label}")
        for key, item in value.items():
            _validate(item, schema["properties"][key], key)


def _summary(node: dict[str, Any]) -> dict[str, Any]:
    return {key: node[key] for key in SUMMARY_KEYS if key in node} | {
        "anomaly_count": len(node.get("anomaly_profile", {}).get("signals", []))
    }


def _reply(
    answer: str,
    facts: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    limitations: list[str] | None = None,
) -> dict[str, Any]:
    unique = {node["gid"]: _summary(node) for node in nodes}
    extra = list(limitations or [])
    if len(unique) > 20:
        extra.append(
            f"Показано 20 кратких профилей из {len(unique)} упомянутых узлов; остальные gid сохранены в фактах."
        )
    output: dict[str, Any] = {
        "answer": answer,
        "facts": deepcopy(facts),
        "nodes": list(unique.values())[:20],
        "limitations": list(LIMITATIONS) + extra,
    }
    omitted = 0
    while len(json.dumps(output, ensure_ascii=False)) > MAX_OUTPUT_CHARS - 250 and output["facts"]:
        output["facts"].pop()
        omitted += 1
    if omitted:
        output["limitations"].append(
            f"Лимит объёма ответа: не показано {omitted} записей фактов. Запросите меньше узлов или более узкий вопрос."
        )
    return output


def _inspect_graph(result: dict[str, Any], topic: str) -> dict[str, Any]:
    report = result["report"]
    if topic == "methodology":
        facts = [
            {
                "rules": report["rules"],
                "role_labels": LABELS,
                "role_precedence": list(LABELS),
                "role_score_meaning": "Степень выполнения эвристических критериев; не вероятность правонарушения.",
                "priority_formula": "Сумма percentile(metric) * weight; положительные значения используют средний CDF (average_rank - 0.5)/positive_count, нули имеют percentile=0.",
                "volume_definition": "max(in_kzt, out_kzt) между контрагентами, без самопереводов; это не сумма входа и выхода.",
                "communities": "Louvain на неориентированном взвешенном графе, вес суммы переводов в обоих направлениях; изоляты сохранены.",
                "anomalies": "Для активных узлов того же depth: значение > Q3 + 3*IQR при размере группы >=20 и IQR>0; не изменяет роль и приоритет.",
                "reproducibility": {
                    k: report[k]
                    for k in (
                        "rules_version",
                        "betweenness_method",
                        "betweenness_pivots",
                        "input_sha256",
                    )
                },
            }
        ]
    else:
        keys = (
            "n_nodes",
            "n_active_nodes",
            "n_edges",
            "n_transactions",
            "n_seed",
            "n_clusters",
            "n_isolates",
            "n_boundary",
            "n_anomalous_profiles",
            "n_components",
            "n_connected_components",
            "n_self_transfer_nodes",
            "self_transfer_kzt",
            "turnover_kzt",
            "period_from",
            "period_to",
            "role_counts",
            "rules_version",
            "betweenness_method",
            "betweenness_pivots",
            "warnings",
        )
        facts = [{key: report[key] for key in keys if key in report}]
        if topic == "quality":
            facts[0]["input_sha256"] = report["input_sha256"]
            facts[0]["priority_weight_sensitivity"] = report["sensitivity"]
    return _reply(f"Сведения о текущем расчёте: {topic}.", facts, [], report.get("warnings", []))


def _find_nodes(result: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    selected = set(args["gids"])
    chosen = []
    for node in result["nodes"]:
        if selected and node["gid"] not in selected:
            continue
        if any(
            args[key] is not None and node[key] != args[key]
            for key in ("role", "depth", "cluster_id")
        ):
            continue
        if args["seeds_only"] and not node["is_seed"]:
            continue
        if args["anomaly_only"] and not node.get("anomaly_profile", {}).get("signals", []):
            continue
        if any(
            args[f"min_{key}"] is not None and node[key] < args[f"min_{key}"]
            for key in ("in_kzt", "out_kzt", "volume")
        ):
            continue
        chosen.append(node)
    metric, direction = args["sort_by"], 1 if args["order"] == "asc" else -1

    def value(node: dict[str, Any]) -> float:
        return (
            len(node["anomaly_profile"]["signals"]) if metric == "anomaly_count" else node[metric]
        )

    chosen.sort(key=lambda row: (direction * value(row), int(row["gid"])))
    displayed = chosen[: args["limit"]]
    return _reply(
        f"Подходят {len(chosen)} узлов; показано {len(displayed)}.",
        [{"filters": args, "matched_count": len(chosen), "shown_count": len(displayed)}]
        + [_summary(row) | {"sort_metric": metric, "sort_value": value(row)} for row in displayed],
        displayed,
    )


def _inspect_nodes(nodes: list[dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
    detail = args["detail"]
    if detail != "temporal" and (args["date_from"] is not None or args["date_to"] is not None):
        raise DataError(
            "Фильтр даты применяется только к detail=temporal; метрики роли считаются за полный период"
        )
    for key in ("date_from", "date_to"):
        if args[key] is not None:
            try:
                if date.fromisoformat(args[key]).isoformat() != args[key]:
                    raise ValueError
            except ValueError as exc:
                raise DataError("Дата должна иметь формат YYYY-MM-DD") from exc
    if args["date_from"] and args["date_to"] and args["date_from"] > args["date_to"]:
        raise DataError("Начало периода позже конца")
    limit = min(args["limit"], 3) if detail == "temporal" else args["limit"]
    facts = []
    for node in nodes[:limit]:
        fact = _summary(node)
        keys = {
            "overview": (
                "why",
                "in_tx",
                "out_tx",
                "seed_reach",
                "warnings",
                "next_checks",
                "rank_range",
            ),
            "role": (
                "matched_rules",
                "rule_trace",
                "pass_through",
                "seed_reach",
                "priority_parts",
                "why",
                "warnings",
            ),
            "anomaly": ("anomaly_profile", "warnings"),
            "next_checks": ("next_checks", "warnings", "why"),
            "compare": (
                "in_tx",
                "out_tx",
                "seed_reach",
                "pagerank",
                "betweenness",
                "pass_through",
                "priority_parts",
                "rank_range",
                "warnings",
            ),
            "temporal": (),
        }[detail]
        fact.update({key: node[key] for key in keys})
        if detail == "temporal":
            daily = [
                day
                for day in node["temporal"]["daily"]
                if (args["date_from"] is None or day["date"] >= args["date_from"])
                and (args["date_to"] is None or day["date"] <= args["date_to"])
            ]
            fact["full_period_summary"] = {
                key: val for key, val in node["temporal"].items() if key != "daily"
            }
            fact["requested_period"] = {
                "date_from": args["date_from"],
                "date_to": args["date_to"],
                "active_days": len(daily),
                "in_kzt": math.fsum(day["in_kzt"] for day in daily),
                "out_kzt": math.fsum(day["out_kzt"] for day in daily),
                "in_tx": sum(day["in_tx"] for day in daily),
                "out_tx": sum(day["out_tx"] for day in daily),
                "daily": daily[:31],
                "omitted_days": max(0, len(daily) - 31),
            }
        facts.append(fact)
    limitations = []
    if detail == "temporal":
        limitations.append(
            "Хронология агрегирована по дням без самопереводов. Итоги requested_period точны для заданных дат, full_period_summary относится ко всему периоду. Показываются первые 31 активных дней; внутридневной порядок, назначения и привязка каждого поступления к исходящему неизвестны."
        )
    return _reply(
        f"Профили ({detail}): показано {len(facts)} из {len(nodes)} узлов.",
        facts,
        nodes[:limit],
        limitations,
    )


def _community(
    result: dict[str, Any], args: dict[str, Any], by_id: dict[str, Any]
) -> dict[str, Any]:
    if (args["gid"] is None) == (args["cluster_id"] is None):
        raise DataError("Нужно ровно одно: gid узла или cluster_id сообщества")
    cluster_id = by_id[args["gid"]]["cluster_id"] if args["gid"] is not None else args["cluster_id"]
    summary = next((row for row in result["clusters"] if row["cluster_id"] == cluster_id), None)
    if summary is None:
        raise DataError("Сообщество не найдено в текущем графе")
    members = sorted(
        (node for node in result["nodes"] if node["cluster_id"] == cluster_id),
        key=lambda row: (row["rank"], int(row["gid"])),
    )
    shown = members[: args["limit"]]
    return _reply(
        f"Сообщество {cluster_id}: {len(members)} узлов; показаны первые {len(shown)} по приоритету.",
        [{"community": summary, "top_members": [_summary(row) for row in shown]}],
        shown,
        [
            "Louvain выделяет структурные сообщества; членство не доказывает общую организацию или противоправную деятельность."
        ],
    )


def _removal(result: dict[str, Any], gids: list[str]) -> dict[str, Any]:
    if len(result["nodes"]) > 25_000 or len(result["edges"]) > 100_000:
        raise DataError(
            "Для интерактивного удаления лимит 25 000 узлов и 100 000 рёбер; этот граф больше"
        )
    graph: nx.Graph = nx.Graph()
    graph.add_nodes_from(row["gid"] for row in result["nodes"])
    graph.add_edges_from((edge["src"], edge["dst"]) for edge in result["edges"])
    before = list(nx.connected_components(graph))
    removed = set(gids)
    baseline_pairs = sum((n := len(group - removed)) * (n - 1) // 2 for group in before)
    graph.remove_nodes_from(gids)
    sizes = [len(group) for group in nx.connected_components(graph)]
    remaining_pairs = sum(n * (n - 1) // 2 for n in sizes)
    affected = [
        edge for edge in result["edges"] if edge["src"] in removed or edge["dst"] in removed
    ]
    turnover = math.fsum(edge["sum_kzt"] for edge in result["edges"])
    affected_turnover = math.fsum(edge["sum_kzt"] for edge in affected)
    fact = {
        "removed_gids": gids,
        "before_nodes": len(result["nodes"]),
        "after_nodes": len(graph),
        "before_components": len(before),
        "after_components": len(sizes),
        "before_largest_component": max(map(len, before), default=0),
        "after_largest_component": max(sizes, default=0),
        "removed_edges": len(affected),
        "after_edges": len(result["edges"]) - len(affected),
        "incident_turnover_kzt": affected_turnover,
        "incident_turnover_share": affected_turnover / turnover if turnover else 0,
        "fragmented_surviving_pairs_share": 1 - remaining_pairs / baseline_pairs
        if baseline_pairs
        else 0,
    }
    return _reply(
        f"Статическое удаление {len(gids)} выбранных узлов из наблюдаемого графа.",
        [fact],
        [node for node in result["nodes"] if node["gid"] in removed],
        [
            "Связность измерена без направления. Сами удалённые узлы исключены из знаменателя пар. Оборот инцидентных рёбер посчитан один раз на ребро; это не сумма заблокированных денег и не прогноз реакции сети."
        ],
    )


def execute_tool(result: dict[str, Any], name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate every filter, execute one allowlisted tool, return bounded evidence."""
    schema = next((tool for tool in TOOL_SCHEMAS if tool["name"] == name), None)
    if schema is None:
        raise DataError("Неизвестный инструмент аналитика")
    _validate(args, schema["parameters"], name)
    by_id = {node["gid"]: node for node in result["nodes"]}
    supplied = args.get("gids", []) + ([args["gid"]] if args.get("gid") is not None else [])
    if any(gid not in by_id for gid in supplied):
        raise DataError(
            "Неизвестный gid: используйте точные строковые идентификаторы текущего графа"
        )
    if len(set(supplied)) != len(supplied):
        raise DataError("Список gid содержит повторения")
    if name not in ("inspect_graph", "find_nodes", "inspect_community") and not supplied:
        raise DataError("Для этого инструмента нужны выбранные или найденные узлы")
    if name == "inspect_graph":
        return _inspect_graph(result, args["topic"])
    if name == "find_nodes":
        return _find_nodes(result, args)
    if name == "inspect_nodes":
        return _inspect_nodes([by_id[gid] for gid in supplied], args)
    if name == "inspect_community":
        return _community(result, args, by_id)
    if name == "assess_removal":
        return _removal(result, supplied)
    if len(result["nodes"]) > 200_000 or len(result["edges"]) > 1_000_000:
        raise DataError("Поиск путей в AI ограничен графами до 200 000 узлов и 1 000 000 рёбер")
    response = execute_query(result, args)
    return _reply(
        response["answer"],
        response["facts"],
        [by_id[node["gid"]] for node in response["nodes"]],
        response["limitations"][len(LIMITATIONS) :],
    )
