"""Date-resolution evidence: no inferred intraday order or provenance of funds."""

import math
from collections import Counter, deque
from datetime import date
from statistics import median, quantiles
from typing import Any

import pandas as pd


def _patterns(daily: list[dict[str, Any]]) -> dict[str, Any]:
    """Descriptive flags; they neither change roles nor establish transaction intent."""
    counts = [day["in_tx"] + day["out_tx"] for day in daily]
    baseline = float(median(counts)) if counts else None
    threshold = max(3 * baseline, baseline + 5) if baseline is not None else None
    assessed = len(daily) >= 7
    spikes = [
        {key: day[key] for key in ("date", "in_tx", "out_tx", "in_kzt", "out_kzt")}
        | {"n_tx": day["in_tx"] + day["out_tx"]}
        for day in daily
        if assessed and threshold is not None and day["in_tx"] + day["out_tx"] > threshold
    ]
    spikes.sort(key=lambda day: (-day["n_tx"], day["date"]))
    synchronous = [
        {
            "date": day["date"],
            "senders": len(day["senders"]),
            "in_tx": day["in_tx"],
            "in_kzt": day["in_kzt"],
        }
        for day in daily
        if len(day["senders"]) >= 3
    ]
    synchronous.sort(key=lambda day: (-day["senders"], -day["in_kzt"], day["date"]))
    directions = {}
    groups = []
    for direction in ("in", "out"):
        amounts = [amount for day in daily for amount, _ in day[f"_{direction}_events"]]
        enough = len(amounts) >= 8
        q1 = float(quantiles(amounts, n=4, method="inclusive")[0]) if enough else None
        directions[direction] = {
            "status": "assessed" if enough else "insufficient_transactions",
            "n_transactions": len(amounts),
            "q1_kzt": q1,
        }
        if q1 is None:
            continue
        for day in daily:
            events = day[f"_{direction}_events"]
            frequencies = Counter(amount for amount, _ in events)
            counterparties: dict[float, set[int]] = {
                amount: set()
                for amount, count in frequencies.items()
                if count >= 3 and amount <= q1
            }
            # Group once: scanning every event again for every amount is quadratic.
            for amount, other in events:
                if amount in counterparties:
                    counterparties[amount].add(other)
            for amount, others in counterparties.items():
                count = frequencies[amount]
                groups.append(
                    {
                        "date": day["date"],
                        "direction": direction,
                        "amount_kzt": amount,
                        "n_tx": count,
                        "total_kzt": math.fsum([amount] * count),
                        "counterparties": len(others),
                    }
                )
    groups.sort(
        key=lambda group: (
            -group["n_tx"],
            -group["total_kzt"],
            group["date"],
            group["direction"],
            group["amount_kzt"],
        )
    )
    return {
        "version": "1",
        "activity": {
            "status": "assessed" if assessed else "insufficient_history",
            "active_days": len(daily),
            "minimum_days": 7,
            "baseline_median_tx": baseline,
            "threshold_tx": threshold,
            "spike_day_count": len(spikes),
            "spike_days": spikes[:5],
            "rule": "Больше max(3 × медиана, медиана + 5) операций за активный день; минимум 7 активных дней. Медиана включает проверяемый день; дни без операций не входят в базу.",
        },
        "synchronous": {
            "minimum_senders": 3,
            "day_count": len(synchronous),
            "days": synchronous[:5],
        },
        "repeated_amounts": {
            "minimum_repeats": 3,
            "minimum_transactions": 8,
            "directions": directions,
            "group_count": len(groups),
            "groups": groups[:5],
            "rule": "Не менее 3 переводов с точно одинаковой суммой за день и направление; сумма ≤ Q1 переводов узла в этом направлении, минимум 8 переводов за период. Q1 — линейная интерполяция.",
        },
        "caveat": "Это описательные поводы для проверки, без влияния на роль или приоритет. Повторные малые суммы могут быть обычными платежами и лишь совместимы с гипотезой дробления; намерение не установлено. Переводы ниже порога исходной выгрузки не восстанавливаются. Общая дата не доказывает одновременность. Показано до 5 наиболее выраженных примеров каждого типа; полные количества указаны отдельно. Самопереводы исключены.",
    }


def summarize(transactions: pd.DataFrame, gids: list[int]) -> dict[int, dict[str, Any]]:
    days: dict[int, dict[str, dict[str, Any]]] = {gid: {} for gid in gids}
    dates: dict[date, str] = {}
    ordinals: dict[str, int] = {}
    for src, dst, when, amount in transactions[["src", "dst", "date", "sum_kzt"]].itertuples(
        index=False, name=None
    ):
        if src == dst:
            continue
        calendar_date = when.date()
        date_key = dates.get(calendar_date)
        if date_key is None:
            date_key = calendar_date.isoformat()
            dates[calendar_date] = date_key
            ordinals[date_key] = calendar_date.toordinal()
        for gid, direction, other in ((dst, "in", src), (src, "out", dst)):
            day = days[gid].get(date_key)
            if day is None:
                day = {
                    "date": date_key,
                    "in_kzt": 0.0,
                    "out_kzt": 0.0,
                    "in_tx": 0,
                    "out_tx": 0,
                    "senders": set(),
                    "receivers": set(),
                    "_in_events": [],
                    "_out_events": [],
                }
                days[gid][date_key] = day
            day[f"_{direction}_events"].append((float(amount), other))
            day[f"{direction}_tx"] += 1
            day["senders" if direction == "in" else "receivers"].add(other)
    result = {}
    for gid, activity in days.items():
        daily = [activity[key] for key in sorted(activity)]
        for day in daily:
            for direction in ("in", "out"):
                day[f"{direction}_kzt"] = math.fsum(
                    amount for amount, _ in day[f"_{direction}_events"]
                )
        patterns = _patterns(daily)
        available: deque[list[int]] = deque()
        matched_cents = 0
        for day in daily:
            ordinal = ordinals[day["date"]]
            while available and ordinal - available[0][0] > 2:
                available.popleft()
            outgoing = round(day["out_kzt"] * 100)
            # Consume before adding today's receipts: same-day sequence is unknown.
            while outgoing and available:
                amount = min(outgoing, available[0][1])
                matched_cents += amount
                outgoing -= amount
                available[0][1] -= amount
                if available[0][1] == 0:
                    available.popleft()
            if day["in_kzt"]:
                available.append([ordinal, round(day["in_kzt"] * 100)])
        total_in = math.fsum(d["in_kzt"] for d in daily)
        total_out = math.fsum(d["out_kzt"] for d in daily)
        for day in daily:
            del day["_in_events"], day["_out_events"]
            day["senders"] = len(day["senders"])
            day["receivers"] = len(day["receivers"])
            day["in_kzt"] = round(day["in_kzt"], 2)
            day["out_kzt"] = round(day["out_kzt"], 2)
        result[gid] = {
            "daily": daily,
            "active_days": len(daily),
            "max_same_day_senders": max((d["senders"] for d in daily), default=0),
            "peak_in_share": max((d["in_kzt"] for d in daily), default=0) / total_in
            if total_in
            else 0,
            "matched_1_2d_kzt": matched_cents / 100,
            "matched_1_2d_share": min(1.0, matched_cents / 100 / total_in) if total_in else 0,
            "same_day_overlap_kzt": round(
                math.fsum(min(d["in_kzt"], d["out_kzt"]) for d in daily), 2
            ),
            "in_kzt": total_in,
            "out_kzt": total_out,
            "patterns": patterns,
        }
    return result
