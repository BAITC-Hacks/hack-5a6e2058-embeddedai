"""Clearly synthetic data for public demos, generated without organizer records."""

from pathlib import Path

import networkx as nx
import pandas as pd


def create_demo(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    base = 9007199254741000
    nodes = [
        {
            "gid": base + i,
            "depth": 0 if i < 8 else (1 if i < 18 else (2 if i < 38 else (3 if i < 55 else 4))),
            "is_seed": i < 8,
        }
        for i in range(80)
    ]
    tx = []

    def transfer(src: int, dst: int, amount: float, day: int = 1) -> None:
        tx.append(
            {"src": base + src, "dst": base + dst, "sum_kzt": amount, "date": f"2026-07-{day:02d}"}
        )

    for seed in range(7):
        transfer(seed, 8, 100_000 + seed * 5000)
        transfer(seed, 9, 70_000)
        transfer(seed, 10, 30_000)
    for dst in range(18, 38):
        transfer(8, dst, 10_000)
    transfer(9, 38, 490_000, 2)
    transfer(10, 39, 5000, 2)
    for i in range(18, 38):
        transfer(i, 40 + i % 15, 10_000, 3)
    for i in range(40, 55):
        transfer(i, 55 + i % 25, 5000, 4)
    # Every non-seed has a documented path; keep seed #7 as a true isolate.
    reached = {r["src"] for r in tx} | {r["dst"] for r in tx}
    for index in range(8, 80):
        if base + index not in reached:
            transfer(40, index, 5000, 5)
    graph: nx.DiGraph = nx.DiGraph()
    graph.add_edges_from((r["src"], r["dst"]) for r in tx)
    distances: dict[int, int] = {}
    for seed in range(7):
        for gid, distance in nx.single_source_shortest_path_length(graph, base + seed).items():
            distances[gid] = min(distances.get(gid, distance), distance)
    for row in nodes:
        row["depth"] = 0 if row["is_seed"] else distances[row["gid"]]
    transactions = pd.DataFrame(tx)
    edges = transactions.groupby(["src", "dst"], as_index=False).agg(
        sum_kzt=("sum_kzt", "sum"), n_tx=("sum_kzt", "size")
    )
    depth = {r["gid"]: r["depth"] for r in nodes}
    edges["depth"] = edges.src.map(lambda gid: min(depth[gid] + 1, 4)).astype("int8")
    pd.DataFrame(nodes).to_parquet(directory / "nodes.parquet", index=False)
    edges.to_parquet(directory / "edges.parquet", index=False)
    transactions.to_parquet(directory / "transactions.parquet", index=False)
