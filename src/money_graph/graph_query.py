"""Read-only execution of a validated natural-language query plan.

The model selects an operation. Every number, path and node reference in the
answer is computed here from the saved graph, never supplied by the model.
"""

import math
from collections import deque
from typing import Any

from .investigation import node_evidence
from .loader import DataError
from .roles import LABELS

LIMITATIONS = [
    "Наблюдается только предоставленная выгрузка: внутрибанковские переводы на четыре уровня; отсутствие ребра не доказывает отсутствие перевода.",
    "Роль и приоритет — проверяемые структурные гипотезы, не вывод о виновности.",
    "Путь подтверждает связи за весь период, но не последовательность операций и не движение одних и тех же денег.",
]
DEFAULTS = {
    "operation": "unsupported",
    "gids": [],
    "role": "all",
    "limit": 10,
    "max_hops": 1,
    "min_sources": 0,
    "sort_by": "priority_score",
    "clarification": "",
}


def _money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ") + " KZT"


def _paths(adjacency: dict[str, list[str]], source: str, cutoff: int) -> dict[str, list[str]]:
    paths = {source: [source]}
    queue = deque([source])
    while queue:
        current = queue.popleft()
        if len(paths[current]) > cutoff:
            continue
        for neighbor in adjacency[current]:
            if neighbor not in paths:
                paths[neighbor] = paths[current] + [neighbor]
                queue.append(neighbor)
    return paths


def _finish(
    answer: str,
    claims: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    extra_limitations: list[str] | None = None,
) -> dict[str, Any]:
    referenced = dict.fromkeys(gid for claim in claims for gid in claim["gids"])
    # Intermediate nodes also need clickable references in detailed evidence.
    for fact in facts:
        for path in fact.get("paths", []):
            for gid in path["gids"]:
                referenced[gid] = None
    return {
        "answer": answer,
        "claims": claims,
        "nodes": [
            {"gid": gid, "role": by_id[gid]["role"], "evidence": by_id[gid]["evidence"]}
            for gid in referenced
        ],
        "facts": facts,
        "limitations": LIMITATIONS + (extra_limitations or []),
    }


def _describe_path(path: list[str], edges: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    return {
        "gids": path,
        "edges": [
            dict(edges[(left, right)]) for left, right in zip(path[:-1], path[1:], strict=True)
        ],
    }


def _common(
    plan: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    adjacency: dict[str, list[str]],
    edges: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    gids = plan["gids"]
    incoming = plan["operation"] == "common_senders"
    noun = "плательщиков" if incoming else "получателей"
    if not gids:
        return _finish(f"Выберите узлы, для которых нужно найти общих {noun}.", [], [], by_id)
    minimum = plan["min_sources"] or len(gids)
    if minimum > len(gids):
        return _finish(
            "Минимум совпадений больше числа выбранных узлов; уменьшите его.", [], [], by_id
        )
    matches: dict[str, list[dict[str, Any]]] = {}
    for source in gids:
        for candidate, path in _paths(adjacency, source, plan["max_hops"]).items():
            if candidate == source:
                continue
            if plan["role"] != "all" and by_id[candidate]["role"] != plan["role"]:
                continue
            if incoming:
                path = list(reversed(path))
            # Keep only the bounded path IDs until ranking. Materializing full
            # edge dictionaries for every reachable candidate wastes memory.
            matches.setdefault(candidate, []).append({"source_gid": source, "gids": path})
    candidates = []
    for gid, paths in matches.items():
        if len(paths) < minimum:
            continue
        fact = {
            "kind": plan["operation"],
            "gid": gid,
            "matched_sources": [path["source_gid"] for path in paths],
            "matched_count": len(paths),
            "paths": paths,
        }
        if plan["max_hops"] == 1:
            fact["direct_sum_kzt"] = math.fsum(
                edges[tuple(path["gids"])]["sum_kzt"] for path in paths
            )
            fact["direct_n_tx"] = sum(edges[tuple(path["gids"])]["n_tx"] for path in paths)
        candidates.append(fact)
    candidates.sort(
        key=lambda row: (
            -row["matched_count"],
            -row.get("direct_sum_kzt", 0),
            -by_id[row["gid"]]["priority_score"],
            int(row["gid"]),
        )
    )
    claims = []
    chosen = candidates[: plan["limit"]]
    for row in chosen:
        row["paths"] = [
            {"source_gid": path["source_gid"]} | _describe_path(path["gids"], edges)
            for path in row["paths"]
        ]
        gid = row["gid"]
        if incoming:
            text = f"Узел {gid} переводит выбранным узлам: {row['matched_count']} из {len(gids)}"
        else:
            text = f"Узел {gid} получает от выбранных узлов: {row['matched_count']} из {len(gids)}"
        if plan["max_hops"] == 1:
            text += f"; напрямую {_money(row['direct_sum_kzt'])}, операций {row['direct_n_tx']}."
        else:
            direction = (
                "из него достижимы выбранные узлы" if incoming else "достижим от выбранных узлов"
            )
            text = f"Узел {gid}: {direction}, {row['matched_count']} из {len(gids)}; направленные пути длиной до {plan['max_hops']} рёбер."
        claims.append({"text": text, "gids": list(dict.fromkeys([gid] + row["matched_sources"]))})
    answer = (
        f"Найдено {len(candidates)} {noun} с охватом не менее {minimum} из {len(gids)} выбранных узлов. "
        f"Показано {len(chosen)}."
    )
    if not candidates:
        answer = f"Общих {noun} с охватом {minimum} из {len(gids)} узлов на глубине до {plan['max_hops']} не найдено в выгрузке."
    limitations = []
    if plan["max_hops"] > 1:
        limitations.append(
            "Для каждого совпадения показан один кратчайший путь. Сквозная сумма денег по цепочке не вычисляется."
        )
    return _finish(answer, claims, chosen, by_id, limitations)


def execute_query(result: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Execute supported graph operations; no model access, writes or graph mutation."""
    plan = DEFAULTS | plan
    by_id = {row["gid"]: row for row in result["nodes"]}
    if (
        not isinstance(plan["gids"], list)
        or len(plan["gids"]) > 20
        or any(not isinstance(gid, str) or gid not in by_id for gid in plan["gids"])
        or type(plan["limit"]) is not int
        or not 1 <= plan["limit"] <= 20
        or type(plan["max_hops"]) is not int
        or not 1 <= plan["max_hops"] <= 4
        or type(plan["min_sources"]) is not int
        or not 0 <= plan["min_sources"] <= 20
        or plan["role"] not in ("all", *LABELS)
        or plan["sort_by"] not in ("priority_score", "volume", "in_kzt", "out_kzt")
    ):
        raise DataError("Некорректный план вопроса или неизвестный gid")
    plan["gids"] = list(dict.fromkeys(plan["gids"]))
    operation, gids, limit = plan["operation"], plan["gids"], plan["limit"]
    claims: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    if operation == "node_summary":
        if not gids:
            return _finish("Выберите узел или укажите его gid для объяснения.", [], [], by_id)
        for gid in gids[:limit]:
            row = by_id[gid]
            text = (
                f"{gid}: {LABELS[row['role']]}; приоритет {row['priority_score']:.6f}, место {row['rank']}. "
                f"Вход {_money(row['in_kzt'])} от {row['in_deg']} плательщиков; "
                f"выход {_money(row['out_kzt'])} к {row['out_deg']} получателям. {row['evidence']}"
            )
            claims.append({"text": text, "gids": [gid]})
            facts.append(
                {"kind": operation}
                | {
                    key: row[key]
                    for key in (
                        "gid",
                        "role",
                        "priority_score",
                        "rank",
                        "in_kzt",
                        "out_kzt",
                        "in_deg",
                        "out_deg",
                        "in_tx",
                        "out_tx",
                        "is_seed",
                        "boundary_censored",
                        "evidence",
                    )
                }
            )
        return _finish(
            f"Профили выбранных узлов: показано {len(facts)} из {len(gids)}.", claims, facts, by_id
        )
    if operation == "rank_nodes":
        metric = plan["sort_by"]
        chosen = [by_id[gid] for gid in gids] if gids else list(by_id.values())
        chosen = [row for row in chosen if plan["role"] == "all" or row["role"] == plan["role"]]
        chosen.sort(key=lambda row: (-row[metric], int(row["gid"])))
        for index, row in enumerate(chosen[:limit], 1):
            gid, value = row["gid"], row[metric]
            display = f"{value:.6f}" if metric == "priority_score" else _money(value)
            claims.append(
                {
                    "text": f"{index}. {gid}: {LABELS[row['role']]}; {metric} = {display}.",
                    "gids": [gid],
                }
            )
            facts.append(
                {
                    "kind": operation,
                    "gid": gid,
                    "role": row["role"],
                    "metric": metric,
                    "value": value,
                    "position": index,
                }
            )
        return _finish(
            f"Ранжирование по {metric}: показано {len(facts)} из {len(chosen)} подходящих узлов.",
            claims,
            facts,
            by_id,
        )
    if operation == "community":
        if not gids:
            return _finish("Выберите узел, сообщество которого нужно объяснить.", [], [], by_id)
        cluster_ids = list(dict.fromkeys(by_id[gid]["cluster_id"] for gid in gids))
        for cluster_id in cluster_ids[:limit]:
            members = sorted(
                (row for row in by_id.values() if row["cluster_id"] == cluster_id),
                key=lambda row: (-row["priority_score"], int(row["gid"])),
            )
            top = [row["gid"] for row in members[:limit]]
            summary: dict[str, Any] = next(
                (row for row in result["clusters"] if row["cluster_id"] == cluster_id), {}
            )
            facts.append(
                {
                    "kind": operation,
                    "cluster_id": cluster_id,
                    "n_nodes": len(members),
                    "top_gids": top,
                    "hypothesis": summary.get("hypothesis", ""),
                }
            )
            claims.append(
                {
                    "text": f"Сообщество {cluster_id}: {len(members)} узлов. Первые по приоритету: {', '.join(top)}. {summary.get('hypothesis', '')}",
                    "gids": top,
                }
            )
        return _finish(
            f"Сообщества выбранных узлов: показано {len(facts)} из {len(cluster_ids)}.",
            claims,
            facts,
            by_id,
        )
    if operation not in ("common_recipients", "common_senders", "path", "cycles"):
        # Do not repeat free-form provider output as if it were a graph fact.
        return _finish(
            "Уточните вопрос: могу найти общих получателей или плательщиков, объяснить узлы, показать рейтинг, сообщество, направленный путь или примеры циклов. Выберите нужные узлы в очереди проверки.",
            [],
            [],
            by_id,
        )
    edges = {(row["src"], row["dst"]): row for row in result["edges"]}
    adjacency: dict[str, list[str]] = {gid: [] for gid in by_id}
    for source, target in edges:
        if operation == "common_senders":
            source, target = target, source
        adjacency[source].append(target)
    for neighbors in adjacency.values():
        neighbors.sort(key=int)
    if operation in ("common_recipients", "common_senders"):
        return _common(plan, by_id, adjacency, edges)
    if operation == "path":
        if len(gids) != 2:
            return _finish(
                "Для направленного пути выберите ровно два разных узла: отправителя, затем получателя.",
                [],
                [],
                by_id,
            )
        path = _paths(adjacency, gids[0], plan["max_hops"]).get(gids[1])
        if path is None:
            return _finish(
                f"Направленный путь от {gids[0]} к {gids[1]} длиной до {plan['max_hops']} рёбер не найден в выгрузке.",
                [],
                [],
                by_id,
            )
        fact = {"kind": operation, "hops": len(path) - 1, "paths": [_describe_path(path, edges)]}
        claim = {
            "text": f"Найден кратчайший направленный путь: {' → '.join(path)} ({len(path) - 1} рёбер).",
            "gids": path,
        }
        return _finish(
            "Направленный путь подтверждён наблюдаемыми переводами.", [claim], [fact], by_id
        )
    if not gids:
        return _finish("Выберите узлы, через которые нужно проверить циклы.", [], [], by_id)
    seen: set[tuple[str, ...]] = set()
    checked = 0
    for gid in gids:
        checked += 1
        for item in node_evidence(result, gid)["cycles"]:
            path = item["gids"]
            if len(path) - 1 > plan["max_hops"]:
                continue
            loop = path[:-1]
            key = min(tuple(loop[index:] + loop[:index]) for index in range(len(loop)))
            if key in seen:
                continue
            seen.add(key)
            facts.append({"kind": "cycle", "hops": len(path) - 1, "paths": [item]})
            claims.append(
                {
                    "text": f"Наблюдаемый цикл: {' → '.join(path)} ({len(path) - 1} рёбер).",
                    "gids": list(dict.fromkeys(path)),
                }
            )
            if len(facts) == limit:
                break
        if len(facts) == limit:
            break
    return _finish(
        f"Найдено {len(facts)} примеров циклов длиной до {plan['max_hops']} рёбер. Проверены {checked} из {len(gids)} выбранных узлов.",
        claims,
        facts,
        by_id,
        [
            "Это ограниченные примеры, не полное перечисление всех циклов: до пяти коротких циклов на проверенный узел."
        ],
    )
