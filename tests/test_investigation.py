import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from money_graph.api import create_app
from money_graph.demo import create_demo
from money_graph.investigation import node_evidence, resilience
from money_graph.loader import DataError, load
from money_graph.pipeline import EXPORTS, analyze, read_rules, write_result
from money_graph.temporal import summarize


def temporal_rows(rows):
    tx = pd.DataFrame(rows, columns=["src", "dst", "date", "sum_kzt"])
    tx["date"] = pd.to_datetime(tx.date)
    return summarize(tx, [1, 2, 3, 4])


def test_temporal_cannot_reuse_funds_or_infer_same_day_order():
    result = temporal_rows(
        [
            (1, 2, "2026-07-01", 100),
            (2, 3, "2026-07-01", 100),
            (2, 3, "2026-07-02", 70),
            (2, 4, "2026-07-03", 80),
        ]
    )[2]
    assert result["matched_1_2d_kzt"] == 100
    assert result["matched_1_2d_share"] == 1
    assert result["same_day_overlap_kzt"] == 100
    same_day = temporal_rows([(1, 2, "2026-07-01", 100), (2, 3, "2026-07-01", 100)])[2]
    assert same_day["matched_1_2d_kzt"] == 0


def test_temporal_reversed_or_expired_receipts_are_not_transit():
    result = temporal_rows(
        [(2, 3, "2026-07-01", 100), (1, 2, "2026-07-02", 100), (2, 3, "2026-07-05", 100)]
    )[2]
    assert result["matched_1_2d_kzt"] == 0
    assert result["active_days"] == 3


def test_temporal_senders_are_unique_and_shuffling_has_no_effect():
    rows = [(1, 2, "2026-07-01", 50), (1, 2, "2026-07-01", 50), (3, 2, "2026-07-01", 100)]
    result = temporal_rows(rows)
    assert result == temporal_rows(rows[::-1])
    assert result[2]["max_same_day_senders"] == 2
    assert result[2]["daily"][0]["in_tx"] == 3
    assert result[4]["daily"] == []


def chain_result():
    # Two seeds converge on a bridge, with a reciprocal pair and an isolate.
    gids = [str(9007199254741000 + x) for x in range(6)]
    edges = [(0, 2), (1, 2), (2, 3), (3, 4), (4, 3)]
    return {
        "nodes": [
            {"gid": gid, "is_seed": i < 2, "rank": (1 if i == 2 else i + 2)}
            for i, gid in enumerate(gids)
        ],
        "edges": [{"src": gids[a], "dst": gids[b], "sum_kzt": 100.0, "n_tx": 2} for a, b in edges],
    }


def test_seed_paths_and_cycles_contain_only_observed_directed_edges():
    result = chain_result()
    gids = [n["gid"] for n in result["nodes"]]
    evidence = node_evidence(result, gids[3])
    assert evidence["seed_path_count"] == 2
    assert evidence["reciprocal_count"] == 1
    assert evidence["cycles"][0]["gids"] == [gids[3], gids[4], gids[3]]
    observed = {(e["src"], e["dst"]) for e in result["edges"]}
    for path in evidence["seed_paths"] + evidence["cycles"] + evidence["repeated_routes"]:
        assert all((e["src"], e["dst"]) in observed for e in path["edges"])
        assert all(isinstance(g, str) for g in path["gids"])
    assert node_evidence(result, gids[-1])["seed_paths"] == []
    assert node_evidence(result, gids[2])["repeated_route_count"] == 2


def test_removal_measures_fragmentation_among_surviving_nodes_not_removed_nodes():
    result = chain_result()
    simulation = resilience(result, 1)
    assert simulation == resilience(result, 1)
    impact = simulation["priority"]
    # Remaining active nodes: 2 isolated upstream + a connected pair = 1/6 pairs retained.
    assert impact["fragmented_pairs_share"] == pytest.approx(5 / 6, abs=1e-6)
    assert impact["largest_component"] == 2
    assert impact["removed_turnover_share"] == 0.6
    assert impact["n_nodes"] == 5  # Includes the original isolate.
    assert simulation["before"]["fragmented_pairs_share"] == 0
    assert resilience(result, 20)["count"] == 5


def test_fixed_csv_contract_and_new_metrics(tmp_path):
    source = tmp_path / "data"
    create_demo(source)
    result = analyze(source)
    output = tmp_path / "out"
    write_result(result, output)
    expected = [
        ["gid", "role", "role_score", "cluster_id", "priority_score", "evidence"],
        ["cluster_id", "n_nodes", "n_seed", "sum_kzt_internal", "top_gids", "hypothesis"],
        ["rank", "gid", "role", "priority_score", "why"],
    ]
    for filename, columns in zip(EXPORTS, expected, strict=True):
        assert list(pd.read_csv(output / filename).columns) == columns
    assert len(result["report"]["sensitivity"]["scenarios"]) == 12
    for node in result["nodes"]:
        assert node["rank_range"][0] <= node["rank"] <= node["rank_range"][1]
        assert sum(node["priority_parts"].values()) == pytest.approx(
            node["priority_score"], abs=4e-6
        )
        assert node["temporal"]["in_kzt"] == pytest.approx(node["in_kzt"])
        assert node["temporal"]["out_kzt"] == pytest.approx(node["out_kzt"])
    internal = sum(c["sum_kzt_internal"] for c in result["clusters"])
    external = sum(e["sum_kzt"] for e in result["cluster_edges"])
    assert internal + external == pytest.approx(result["report"]["turnover_kzt"])


def test_new_endpoints_and_validation(tmp_path):
    client = TestClient(create_app(storage=tmp_path))
    run = client.get("/api/bootstrap").json()["run_id"]
    gid = client.get(f"/api/runs/{run}/top").json()[0]["gid"]
    assert client.get(f"/api/runs/{run}/nodes/{gid}/investigation").status_code == 200
    assert client.get(f"/api/runs/{run}/nodes/missing/investigation").status_code == 404
    assert client.get(f"/api/runs/{run}/resilience?count=21").status_code == 422
    assert client.get(f"/api/runs/{run}/resilience?count=0").status_code == 422
    impact = client.get(f"/api/runs/{run}/resilience?count=3").json()
    assert impact["count"] == 3
    communities = client.get(f"/api/runs/{run}/community-graph").json()
    assert sum(c["n_nodes"] for c in communities["nodes"]) == 80


def test_missing_and_invalid_rules_fail_actionably(tmp_path):
    for cfg in (
        {},
        read_rules() | {"transit_ratio_min": 2, "transit_ratio_max": 1},
        read_rules() | {"betweenness_samples": 0},
    ):
        path = tmp_path / "rules.json"
        path.write_text(json.dumps(cfg))
        with pytest.raises(DataError):
            read_rules(path)


def test_nat_dates_rejected(tmp_path: Path):
    create_demo(tmp_path)
    path = tmp_path / "transactions.parquet"
    tx = pd.read_parquet(path)
    tx["date"] = tx.date.astype(str)
    tx.loc[0, "date"] = "NaT"
    tx.to_parquet(path)
    with pytest.raises(DataError, match="дата"):
        load(tmp_path)
