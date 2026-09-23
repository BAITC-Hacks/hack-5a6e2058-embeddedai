"""A complete, filtered investigation queue; the global rank is never rewritten."""

from typing import Any

from .roles import LABELS

SORT_FIELDS = {"rank", "volume", "in_kzt", "out_kzt", "in_deg", "out_deg", "role_score"}
SUMMARY_FIELDS = (
    "gid", "role", "role_score", "priority_score", "rank", "cluster_id", "depth",
    "is_seed", "in_deg", "out_deg", "in_kzt", "out_kzt", "volume", "evidence",
)


def filter_nodes(
    rows: list[dict[str, Any]], *, role: str | None = None, cluster: int | None = None,
    depth: int | None = None, seeds: bool = False, search: str = "",
) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if (not role or row["role"] == role)
        and (cluster is None or row["cluster_id"] == cluster)
        and (depth is None or row["depth"] == depth)
        and (not seeds or row["is_seed"])
        and (not search or search in row["gid"])
    ]


def node_queue(
    result: dict[str, Any], *, role: str | None = None, cluster: int | None = None,
    depth: int | None = None, seeds: bool = False, search: str = "", sort: str = "rank",
    order: str = "asc", offset: int = 0, limit: int = 50,
) -> dict[str, Any]:
    if sort not in SORT_FIELDS or order not in {"asc", "desc"} or (role and role not in LABELS):
        raise ValueError("Неизвестная роль или порядок сортировки")
    rows = result["nodes"]
    matched = filter_nodes(rows, role=role, cluster=cluster, depth=depth, seeds=seeds, search=search)
    direction = 1 if order == "asc" else -1
    matched.sort(key=lambda row: (direction * row[sort], int(row["gid"])))
    selected = [
        {**{field: row[field] for field in SUMMARY_FIELDS},
         "n_anomaly_signals": len(row["anomaly_profile"]["signals"])}
        for row in matched[offset:offset + limit]
    ]
    return {"nodes": selected, "total": len(rows), "matched": len(matched), "offset": offset, "limit": limit}
