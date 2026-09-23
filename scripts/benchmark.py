"""Reproducible Linux benchmark; each measurement runs in a fresh process.

Run: uv run python scripts/benchmark.py --nodes 1000 10000 --repeat 3
Add --data data/data for the organizer's locally available dataset. This does
not relax upload limits or claim that the full pipeline supports a million nodes.
"""

import argparse
import hashlib
import json
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

from money_graph.pipeline import EXPORTS, analyze, write_result


def create_layered_dataset(directory: Path, size: int) -> None:
    """Seeded, four-hop DAG with exact signed-int64 IDs and two operations/edge."""
    if not 100 <= size <= 10000:
        raise ValueError("Synthetic full-pipeline sizes must be between 100 and 10000")
    directory.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)
    seed_count = min(32, size // 100)
    gids = np.arange(size, dtype=np.int64) + 9007199254741000
    layers = [np.arange(seed_count), *np.array_split(np.arange(seed_count, size), 4)]
    depths = np.empty(size, dtype=np.int8)
    pairs: set[tuple[int, int]] = set()
    for depth, layer in enumerate(layers):
        depths[layer] = depth
        if depth:
            parents = layers[depth - 1]
            for offset, dst in enumerate(layer):
                pairs.add((int(parents[offset % len(parents)]), int(dst)))
    # Random pairs stay within adjacent layers; the first pass guarantees
    # reachability, and no extra edge changes the minimum depth.
    while len(pairs) < 3 * size:
        depth = int(rng.integers(0, 4))
        pairs.add((int(rng.choice(layers[depth])), int(rng.choice(layers[depth + 1]))))
    ordered = sorted(pairs)
    src = np.array([pair[0] for pair in ordered])
    dst = np.array([pair[1] for pair in ordered])
    amounts = (5000 + np.arange(len(ordered)) % 20_000).astype(float)
    edges = pd.DataFrame(
        {
            "src": gids[src],
            "dst": gids[dst],
            "sum_kzt": amounts * 2,
            "n_tx": np.full(len(ordered), 2, dtype=np.int64),
            "depth": depths[src] + 1,
        }
    )
    transactions = pd.DataFrame(
        {
            "src": np.repeat(gids[src], 2),
            "dst": np.repeat(gids[dst], 2),
            "sum_kzt": np.repeat(amounts, 2),
            "date": pd.Timestamp("2026-07-01")
            + pd.to_timedelta(
                np.repeat(np.arange(len(ordered)) % 28, 2) + np.tile([0, 1], len(ordered)), unit="D"
            ),
        }
    )
    nodes = pd.DataFrame({"gid": gids, "depth": depths, "is_seed": depths == 0})
    for name, frame in (("nodes", nodes), ("edges", edges), ("transactions", transactions)):
        frame.to_parquet(directory / f"{name}.parquet", index=False)


def measure(source: Path, output: Path) -> dict:
    start = time.perf_counter()
    result = analyze(source)
    analyzed = time.perf_counter()
    write_result(result, output)
    finished = time.perf_counter()
    return {
        "nodes": result["report"]["n_nodes"],
        "edges": result["report"]["n_edges"],
        "transactions": result["report"]["n_transactions"],
        "betweenness_method": result["report"]["betweenness_method"],
        "analysis_seconds": round(analyzed - start, 4),
        "total_seconds": round(finished - start, 4),
        # Linux ru_maxrss is KiB; includes imports and generation in this worker.
        "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
        "csv_sha256": {
            name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in EXPORTS
        },
    }


def probe_csr(size: int = 1_000_000) -> dict:
    """A storage/kernel experiment only, not the application or an AML benchmark."""
    from scipy.sparse import csr_array

    start = time.perf_counter()
    offsets = np.array([1, 17, 257, 4099, 65537], dtype=np.int32)
    indices = ((np.arange(size, dtype=np.int32)[:, None] + offsets) % size).reshape(-1)
    pointers = np.arange(size + 1, dtype=np.int32) * len(offsets)
    graph = csr_array((np.ones(len(indices)), indices, pointers), shape=(size, size))
    graph.sort_indices()
    built = time.perf_counter()
    reverse = graph.T.tocsr()
    transposed = time.perf_counter()
    outgoing = np.asarray(graph.sum(axis=1)).reshape(-1)
    incoming = np.asarray(reverse.sum(axis=1)).reshape(-1)
    strengths = time.perf_counter()
    vector = np.full(size, 1 / size)
    updated = 0.85 * (reverse @ (vector / outgoing)) + 0.15 / size
    finished = time.perf_counter()
    assert graph.nnz == 5 * size and (outgoing == 5).all() and (incoming == 5).all()
    assert np.allclose(updated, vector, rtol=1e-12, atol=0)
    return {
        "scope": "CSR storage, strengths and one matrix-vector step only; no full pipeline",
        "nodes": size,
        "edges": graph.nnz,
        "build_seconds": round(built - start, 4),
        "reverse_seconds": round(transposed - built, 4),
        "strengths_seconds": round(strengths - transposed, 4),
        "one_matvec_seconds": round(finished - strengths, 4),
        "two_csr_bytes": sum(
            array.nbytes
            for matrix in (graph, reverse)
            for array in (matrix.data, matrix.indices, matrix.indptr)
        ),
        "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
        "mass_sum": float(updated.sum()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, nargs="+", default=[1000, 10000])
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--csr-probe",
        action="store_true",
        help="Separate 1M-node/5M-edge CSR experiment; not the full pipeline",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 10:
        parser.error("--repeat must be between 1 and 10")
    if any(not 100 <= n <= 10000 for n in args.nodes):
        parser.error("--nodes must stay within 100..10000; this is a full-pipeline benchmark")
    if args.csr_probe:
        if args.worker:
            print(json.dumps(probe_csr()))
            return
        completed = subprocess.run(
            [sys.executable, __file__, "--worker", "--csr-probe"],
            capture_output=True,
            text=True,
            check=True,
        )
        rendered = json.dumps(json.loads(completed.stdout), indent=2)
        if args.output:
            args.output.write_text(rendered + "\n")
        print(rendered)
        return
    if args.worker:
        with tempfile.TemporaryDirectory(prefix="money-graph-benchmark-") as tmp:
            work = Path(tmp)
            source = args.data or work / "input"
            if args.data is None:
                create_layered_dataset(source, args.nodes[0])
            print(json.dumps(measure(source, work / "output")))
        return
    cases = [(f"synthetic-{size}", ["--nodes", str(size)]) for size in args.nodes]
    if args.data:
        cases.insert(0, ("provided-data", ["--data", str(args.data.resolve())]))
    results = []
    for name, options in cases:
        trials = []
        for _ in range(args.repeat):
            completed = subprocess.run(
                [sys.executable, __file__, "--worker", *options],
                capture_output=True,
                text=True,
                check=True,
            )
            trials.append(json.loads(completed.stdout))
        if any(trial["csv_sha256"] != trials[0]["csv_sha256"] for trial in trials):
            raise RuntimeError(f"Non-reproducible CSV exports for {name}")
        results.append(
            {
                "case": name,
                "trials": trials,
                "median_seconds": statistics.median(t["total_seconds"] for t in trials),
                "maximum_rss_mib": max(t["peak_rss_mib"] for t in trials),
            }
        )
    rendered = json.dumps(
        {
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "seed": 42,
            "results": results,
        },
        indent=2,
    )
    if args.output:
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
