"""Descriptive patterns are measurable, conservative and independent of labels."""

import math
from copy import deepcopy

import networkx as nx
import pandas as pd
import pytest

from money_graph.features import compute
from money_graph.investigation import resilience
from money_graph.pipeline import read_rules
from money_graph.temporal import summarize


def summarize_rows(rows):
    tx = pd.DataFrame(rows, columns=["src", "dst", "date", "sum_kzt"])
    tx["date"] = pd.to_datetime(tx.date)
    before = tx.copy(deep=True)
    result = summarize(tx, [1, 2, 3, 4])
    pd.testing.assert_frame_equal(before, tx)
    return result


def burst_rows(last_day_count=7, days=7):
    return [(1, 2, f"2026-07-{day:02}", 5000) for day in range(1, days)] + [
        (1, 2, f"2026-07-{days:02}", 5000)
    ] * last_day_count


def test_spike_is_strict_above_explained_baseline_and_exposes_exact_day_counts():
    activity = summarize_rows(burst_rows())[2]["patterns"]["activity"]
    assert activity["status"] == "assessed"
    assert activity["baseline_median_tx"] == 1
    assert activity["threshold_tx"] == 6
    assert activity["spike_day_count"] == 1
    assert activity["spike_days"] == [
        {"date": "2026-07-07", "in_tx": 7, "out_tx": 0, "n_tx": 7, "in_kzt": 35_000, "out_kzt": 0}
    ]
    boundary = summarize_rows(burst_rows(last_day_count=6))[2]["patterns"]["activity"]
    assert boundary["spike_day_count"] == 0
    assert "Больше" in activity["rule"]


def test_short_history_and_empty_nodes_abstain_without_claiming_no_risk():
    result = summarize_rows(burst_rows(days=6))
    activity = result[2]["patterns"]["activity"]
    assert activity["status"] == "insufficient_history"
    assert activity["spike_day_count"] == 0
    isolate = result[4]["patterns"]
    assert isolate["activity"]["baseline_median_tx"] is None
    assert isolate["synchronous"]["day_count"] == 0
    assert isolate["repeated_amounts"]["directions"]["in"]["status"] == "insufficient_transactions"
    assert isolate["repeated_amounts"]["directions"]["in"]["q1_kzt"] is None


def test_synchronous_dates_count_distinct_senders_and_ignore_self_transfers():
    rows = [(1, 2, "2026-07-01", 5000)] * 5 + [
        (3, 2, "2026-07-01", 10_000),
        (4, 2, "2026-07-01", 15_000),
        (2, 2, "2026-07-01", 1_000_000),
    ]
    result = summarize_rows(rows)[2]
    synchronous = result["patterns"]["synchronous"]
    assert synchronous["day_count"] == 1
    assert synchronous["days"] == [
        {"date": "2026-07-01", "senders": 3, "in_tx": 7, "in_kzt": 50_000}
    ]
    assert result["in_kzt"] == 50_000
    assert "Общая дата не доказывает" in result["patterns"]["caveat"]
    assert summarize_rows(rows[:6])[2]["patterns"]["synchronous"]["day_count"] == 0


def test_repeated_small_amount_groups_use_exact_amount_q1_and_distinct_counterparties():
    rows = [(sender, 2, "2026-07-01", 5000.01) for sender in (1, 3, 4)] + [
        (1, 2, "2026-07-02", 10_000)
    ] * 5
    patterns = summarize_rows(rows)[2]["patterns"]
    repeated = patterns["repeated_amounts"]
    assert repeated["directions"]["in"] == {
        "status": "assessed",
        "n_transactions": 8,
        "q1_kzt": 5000.01,
    }
    assert repeated["group_count"] == 1
    assert repeated["groups"] == [
        {
            "date": "2026-07-01",
            "direction": "in",
            "amount_kzt": 5000.01,
            "n_tx": 3,
            "total_kzt": 15_000.03,
            "counterparties": 3,
        }
    ]
    assert "обычными платежами" in patterns["caveat"]
    assert "не восстанавливаются" in patterns["caveat"]
    assert summarize_rows(rows[:-1])[2]["patterns"]["repeated_amounts"]["group_count"] == 0


def test_amounts_that_round_to_same_cent_are_not_falsely_grouped():
    rows = [(1, 2, "2026-07-01", amount) for amount in (5000.001, 5000.002, 5000.003)] + [
        (1, 2, "2026-07-02", 10_000)
    ] * 5
    assert summarize_rows(rows)[2]["patterns"]["repeated_amounts"]["group_count"] == 0


def test_repeated_groups_do_not_merge_dates_or_directions():
    rows = [(1, 2, f"2026-07-{day:02}", 5000) for day in range(1, 5) for _ in range(2)] + [
        (2, 1, "2026-07-01", 5000)
    ] * 2
    result = summarize_rows(rows)[2]["patterns"]["repeated_amounts"]
    assert result["directions"]["in"]["status"] == "assessed"
    assert result["group_count"] == 0


def test_counts_are_complete_examples_bounded_and_shuffling_stable():
    rows = [(sender, 2, f"2026-07-{day:02}", 5000) for day in range(1, 11) for sender in (1, 3, 4)]
    before = deepcopy(rows)
    result = summarize_rows(rows)
    assert result == summarize_rows(rows[::-1])
    assert rows == before
    patterns = result[2]["patterns"]
    assert patterns["synchronous"]["day_count"] == 10
    assert len(patterns["synchronous"]["days"]) == 5
    assert patterns["repeated_amounts"]["group_count"] == 10
    assert len(patterns["repeated_amounts"]["groups"]) == 5
    assert patterns["synchronous"]["days"][0]["date"] == "2026-07-01"
    assert patterns["repeated_amounts"]["groups"][0]["date"] == "2026-07-01"
    assert patterns["activity"]["spike_day_count"] == 0


def test_large_amounts_do_not_erase_small_transfers_before_fifo():
    amounts = [1e13] + [5000.01] * 1000
    expected = math.fsum(amounts)
    rows = [(1, 2, "2026-07-01", amount) for amount in amounts] + [(2, 3, "2026-07-02", expected)]
    result = summarize_rows(rows)[2]
    assert result["daily"][0]["in_kzt"] == expected
    assert result["in_kzt"] == expected
    assert result["matched_1_2d_kzt"] == expected
    assert result["matched_1_2d_share"] == 1


def test_weighted_money_and_removal_preserve_small_amounts_next_to_large_flow():
    amounts = [1e13] + [5000.01] * 1000
    graph = nx.DiGraph()
    graph.add_nodes_from(range(len(amounts) + 1))
    graph.add_edges_from(
        (i + 1, 0, {"sum_kzt": value, "n_tx": 1}) for i, value in enumerate(amounts)
    )
    nodes = pd.DataFrame(
        {
            "gid": list(graph),
            "depth": [1] + [0] * len(amounts),
            "is_seed": [False] + [True] * len(amounts),
        }
    )
    rules = read_rules() | {"betweenness_exact_max_nodes": 1, "betweenness_samples": 1}
    features = compute(graph, nodes, rules).set_index("gid")
    assert features.loc[0, "in_kzt"] == math.fsum(amounts)
    result = {
        "nodes": [{"gid": str(gid), "rank": gid + 1} for gid in graph],
        "edges": [
            {"src": str(src), "dst": str(dst), **attrs}
            for src, dst, attrs in graph.edges(data=True)
        ],
    }
    removed = resilience(result, 1)["priority"]
    assert removed["removed_turnover_kzt"] == math.fsum(amounts)
    assert removed["removed_turnover_share"] == pytest.approx(1)
