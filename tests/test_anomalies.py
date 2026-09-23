from copy import deepcopy

from money_graph.anomalies import annotate


def cohort(depth=1):
    return [
        {"gid": str(i), "depth": depth, "in_deg": i + 1, "out_deg": 1, "volume": (i + 1) * 1000}
        for i in range(20)
    ]


def test_depth_cohorts_exclude_isolates_and_preserve_roles_and_priorities():
    rows = cohort()
    rows[-1].update(in_deg=200, volume=2_000_000, role="peripheral", priority_score=0.2)
    rows.extend(cohort(depth=2))
    rows.extend(
        {"gid": str(1000 + i), "depth": 1, "in_deg": 0, "out_deg": 0, "volume": 0}
        for i in range(100)
    )
    before = deepcopy(rows)
    annotate(rows)
    flagged = rows[19]["anomaly_profile"]
    assert flagged["cohort_size"] == 20
    assert {s["metric"] for s in flagged["signals"]} == {"in_deg", "volume"}
    assert flagged["signals"][0]["q1"] == 5.75
    assert flagged["signals"][0]["q3"] == 15.25
    assert flagged["signals"][0]["threshold"] == 43.75
    assert "Колено 1, 20 активных узлов" in flagged["signals"][0]["text"]
    assert not any(row["anomaly_profile"]["signals"] for row in rows[20:])
    assert "исключён из сравнения" in rows[-1]["anomaly_profile"]["caveat"]
    assert [{k: v for k, v in row.items() if k != "anomaly_profile"} for row in rows] == before


def test_small_or_zero_iqr_cohorts_abstain_even_with_one_huge_outlier():
    small = cohort()[:19]
    small[-1]["volume"] = 10**12
    annotate(small)
    assert not small[-1]["anomaly_profile"]["signals"]
    assert "только 19" in small[-1]["anomaly_profile"]["caveat"]
    assert "Оценка не выполнялась" in small[-1]["anomaly_profile"]["caveat"]
    flat = [dict(row, in_deg=1, out_deg=1, volume=1000) for row in cohort()]
    flat[-1]["volume"] = 10**12
    annotate(flat)
    assert not flat[-1]["anomaly_profile"]["signals"]
    assert "IQR=0" in flat[-1]["anomaly_profile"]["caveat"]


def test_threshold_is_strict_and_balance_self_transfers_do_not_flag():
    rows = cohort()
    rows[-1].update(volume=43_750, pass_through=1e12, self_transfer_kzt=1e12)
    annotate(rows)
    assert not rows[-1]["anomaly_profile"]["signals"]
    rows[-1]["volume"] += 0.01
    annotate(rows)
    signal = rows[-1]["anomaly_profile"]["signals"][0]
    assert signal["metric"] == "volume" and signal["threshold"] == 43_750
    assert "не доказательство" in rows[-1]["anomaly_profile"]["caveat"]


def test_profiles_are_stable_under_record_order_and_uniform_currency_scaling():
    rows = cohort()
    rows[-1]["volume"] = 100_000
    scaled = [dict(row, volume=row["volume"] * 10) for row in reversed(rows)]
    annotate(rows)
    annotate(scaled)
    expected = {row["gid"]: row["anomaly_profile"] for row in rows}
    for row in scaled:
        profile = row["anomaly_profile"]
        signals = expected[row["gid"]]["signals"]
        assert [s["metric"] for s in profile["signals"]] == [s["metric"] for s in signals]
        for actual, original in zip(profile["signals"], signals, strict=True):
            factor = 10 if actual["metric"] == "volume" else 1
            for field in ("value", "threshold", "q1", "q3"):
                assert actual[field] == original[field] * factor
