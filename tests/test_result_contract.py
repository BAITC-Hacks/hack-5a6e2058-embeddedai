"""Published evidence must remain consistent after a result is read from disk."""

from copy import deepcopy
from math import fsum

import pandas as pd
import pytest

from money_graph.clusters import summarize
from money_graph.demo import create_demo
from money_graph.loader import MAX_TOTAL_KZT, DataError
from money_graph.pipeline import analyze, validate_result, write_result


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    source = tmp_path_factory.mktemp("result-contract")
    create_demo(source)
    # A relative tolerance would hide a material mismatch on large turnovers.
    for name in ("edges", "transactions"):
        path = source / f"{name}.parquet"
        frame = pd.read_parquet(path)
        frame["sum_kzt"] *= 1000
        frame.to_parquet(path, index=False)
    return analyze(source)


@pytest.mark.parametrize("target", ["turnover", "cluster", "cluster_edge"])
def test_saved_money_totals_use_absolute_tiyn_tolerance(result, target):
    saved = deepcopy(result)
    if target == "turnover":
        saved["report"]["turnover_kzt"] += 0.1
    elif target == "cluster":
        cluster = max(saved["clusters"], key=lambda c: c["sum_kzt_internal"])
        cluster["sum_kzt_internal"] += 0.1
    else:
        edge = max(saved["cluster_edges"], key=lambda e: e["sum_kzt"])
        edge["sum_kzt"] += 0.1
    with pytest.raises(DataError):
        validate_result(saved)


@pytest.mark.parametrize("field", ["n_active_nodes", "betweenness_pivots", "n_anomalous_profiles"])
def test_report_counts_cannot_misrepresent_evidence(result, field):
    saved = deepcopy(result)
    saved["report"][field] += 1
    with pytest.raises(DataError):
        validate_result(saved)


def test_node_cannot_claim_a_different_comparison_population(result):
    saved = deepcopy(result)
    saved["nodes"][0]["anomaly_profile"]["cohort_size"] += 1
    with pytest.raises(DataError, match="cohort_size"):
        validate_result(saved)


def test_legal_large_turnover_survives_analysis_snapshot_and_export(tmp_path):
    # Naive Python sum and pandas.sum disagree by 0.015625 KZT here, despite
    # valid two-decimal input below the supported total. This must not turn a
    # successful computation into a snapshot-validation failure.
    count = 100
    amount = 899_000_000_000.01
    source = tmp_path / "source"
    source.mkdir()
    nodes = pd.DataFrame(
        {
            "gid": range(count + 1),
            "depth": [0] + [1] * count,
            "is_seed": [True] + [False] * count,
        }
    )
    edges = pd.DataFrame(
        {
            "src": [0] * count,
            "dst": range(1, count + 1),
            "sum_kzt": [amount] * count,
            "n_tx": [1] * count,
            "depth": [1] * count,
        }
    )
    transactions = edges[["src", "dst", "sum_kzt"]].copy()
    transactions["date"] = "2026-07-01"
    for name, frame in (("nodes", nodes), ("edges", edges), ("transactions", transactions)):
        frame.to_parquet(source / f"{name}.parquet", index=False)
    assert fsum(edges.sum_kzt) <= MAX_TOTAL_KZT
    analyzed = analyze(source)
    expected = round(fsum(edges.sum_kzt), 2)
    assert analyzed["report"]["turnover_kzt"] == expected
    validate_result(analyzed)
    write_result(analyzed, tmp_path / "output")
    damaged = deepcopy(analyzed)
    damaged["report"]["turnover_kzt"] += 0.1
    with pytest.raises(DataError, match="turnover_kzt"):
        validate_result(damaged)


def test_cluster_money_summation_is_stable_with_large_and_small_transfers():
    amounts = [89_900_000_000_000.0] + [5_000.01] * 100
    records = [
        {
            "gid": str(gid),
            "cluster_id": int(gid > 0),
            "role": "peripheral",
            "is_seed": gid == 0,
            "boundary_censored": False,
            "isolated": False,
        }
        for gid in range(len(amounts) + 1)
    ]
    transfers = [
        {"src": "0", "dst": str(index + 1), "sum_kzt": amount}
        for index, amount in enumerate(amounts)
    ]
    forward, links_forward = summarize(records, transfers)
    reverse, links_reverse = summarize(records, list(reversed(transfers)))
    assert forward == reverse
    assert links_forward == links_reverse
    assert links_forward[0]["sum_kzt"] == round(fsum(amounts), 2)
