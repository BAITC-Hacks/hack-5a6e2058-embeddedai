"""Date-resolution evidence: no inferred intraday order or provenance of funds."""

from collections import deque
from typing import Any

import pandas as pd


def summarize(transactions: pd.DataFrame, gids: list[int]) -> dict[int, dict[str, Any]]:
    days: dict[int, dict[str, dict[str, Any]]] = {gid: {} for gid in gids}
    for tx in transactions.to_dict("records"):
        if tx["src"] == tx["dst"]:
            continue
        date = tx["date"].date().isoformat()
        for gid, direction, other in ((tx["dst"], "in", tx["src"]), (tx["src"], "out", tx["dst"])):
            day = days[gid].setdefault(
                date,
                {
                    "date": date,
                    "in_kzt": 0.0,
                    "out_kzt": 0.0,
                    "in_tx": 0,
                    "out_tx": 0,
                    "senders": set(),
                    "receivers": set(),
                },
            )
            day[f"{direction}_kzt"] += float(tx["sum_kzt"])
            day[f"{direction}_tx"] += 1
            day["senders" if direction == "in" else "receivers"].add(other)
    result = {}
    for gid, activity in days.items():
        daily = [activity[key] for key in sorted(activity)]
        available: deque[list[int]] = deque()
        matched_cents = 0
        for day in daily:
            ordinal = pd.Timestamp(day["date"]).date().toordinal()
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
        total_in = sum(d["in_kzt"] for d in daily)
        total_out = sum(d["out_kzt"] for d in daily)
        for day in daily:
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
            "same_day_overlap_kzt": round(sum(min(d["in_kzt"], d["out_kzt"]) for d in daily), 2),
            "in_kzt": total_in,
            "out_kzt": total_out,
        }
    return result
