"""Community-level flow summaries with falsifiable structural hypotheses."""

from collections import Counter, defaultdict
from typing import Any


def summarize(
    records: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    members: dict[int, list[dict[str, Any]]] = defaultdict(list)
    mapping = {str(row["gid"]): row["cluster_id"] for row in records}
    for row in records:
        members[row["cluster_id"]].append(row)
    flows: dict[tuple[int, int], float] = defaultdict(float)
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for edge in edges:
        pair = (mapping[str(edge["src"])], mapping[str(edge["dst"])])
        flows[pair] += edge["sum_kzt"]
        counts[pair] += 1
    incoming_by_cluster: dict[int, float] = defaultdict(float)
    outgoing_by_cluster: dict[int, float] = defaultdict(float)
    # Preserve the order of grouped floating-point additions, but visit each
    # inter-community pair once instead of scanning all pairs for every cluster.
    for (src, dst), amount in flows.items():
        if src != dst:
            incoming_by_cluster[dst] += amount
            outgoing_by_cluster[src] += amount
    summaries = []
    for cid, group in sorted(members.items()):
        roles = Counter(row["role"] for row in group)
        seeds = sum(row["is_seed"] for row in group)
        boundary = sum(row["boundary_censored"] for row in group)
        internal = flows[(cid, cid)]
        incoming = incoming_by_cluster[cid]
        outgoing = outgoing_by_cluster[cid]
        total = internal + incoming + outgoing
        if len(group) == 1 and group[0]["isolated"]:
            hypothesis = "Изолированный узел: связи и назначение не установлены в данной выгрузке."
        else:
            pattern = []
            for role, text in (
                ("coordinator", "связующих"),
                ("consolidator", "консолидаторов"),
                ("distributor", "распределителей"),
                ("transit", "транзитных"),
            ):
                if roles[role]:
                    pattern.append(f"{roles[role]} {text}")
            detail = ", ".join(pattern) or "выраженных промежуточных ролей нет"
            hypothesis = f"Контур: {detail}; {seeds} seed. Внутри {internal / total:.0%} наблюдаемого оборота; граница {boundary}/{len(group)}. Структурная гипотеза."
        summaries.append(
            {
                "cluster_id": cid,
                "n_nodes": len(group),
                "n_seed": seeds,
                "sum_kzt_internal": round(internal, 2),
                "top_gids": [str(row["gid"]) for row in group[:5]],
                "hypothesis": hypothesis,
                "incoming_kzt": round(incoming, 2),
                "outgoing_kzt": round(outgoing, 2),
                "boundary_nodes": boundary,
                "role_counts": dict(roles),
            }
        )
    links = [
        {"src": src, "dst": dst, "sum_kzt": round(amount, 2), "n_edges": counts[(src, dst)]}
        for (src, dst), amount in sorted(flows.items())
        if src != dst and amount > 0
    ]
    return summaries, links
