"""Versioned heuristic evidence, not a probability of criminal involvement."""
import math
from typing import Any

LABELS = {
    "coordinator": "Связующий узел", "distributor": "Распределитель",
    "transit": "Транзит", "consolidator": "Консолидатор",
    "terminal": "Конечный в выборке", "peripheral": "Периферия / мало данных",
}


def support(x: float, threshold: float) -> float:
    return min(max(x / threshold, 0), 1)


def classify(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    ratio = row["pass_through"]
    observed = not row["is_seed"] and not row["boundary_censored"] and math.isfinite(ratio)
    candidates: dict[str, float] = {}
    if (row["seed_reach"] >= cfg["coordinator_min_seeds"]
        and row["in_deg"] >= cfg["coordinator_min_in"]
        and row["out_deg"] >= cfg["coordinator_min_out"]
        and row["p_betweenness"] >= cfg["coordinator_betweenness_percentile"]):
        candidates["coordinator"] = (.35 * support(row["seed_reach"], 4)
            + .30 * row["p_betweenness"] + .20 * support(row["in_deg"], 6)
            + .15 * support(row["out_deg"], 5))
    if row["out_deg"] >= cfg["distributor_min_out"] and row["out_tx"] >= cfg["distributor_min_out"]:
        candidates["distributor"] = (.60 * support(row["out_deg"], 20)
            + .25 * row["p_out_tx"] + .15 * row["p_out_kzt"])
    if observed and row["out_deg"] > 0 and cfg["transit_ratio_min"] <= ratio <= cfg["transit_ratio_max"]:
        candidates["transit"] = (1 - abs(ratio - 1) / .4) * (.5 + .5 * support(min(row["in_tx"], row["out_tx"]), 3))
    if observed and row["in_deg"] >= cfg["consolidator_min_in"] and ratio <= cfg["consolidator_ratio_max"]:
        candidates["consolidator"] = (.50 * support(row["in_deg"], 6)
            + .30 * (1 - min(ratio / cfg["consolidator_ratio_max"], 1)) + .20 * row["p_in_tx"])
    if observed and row["in_kzt"] > 0 and row["out_deg"] == 0:
        candidates["terminal"] = .50 + .30 * row["p_in_tx"] + .20 * support(row["in_deg"], 3)
    role = next(iter(candidates), "peripheral")
    score = candidates.get(role, 0.0)
    if row["is_seed"] or row["boundary_censored"]:
        score *= cfg["incomplete_support_multiplier"]
    warnings = []
    if row["boundary_censored"]:
        warnings.append("Граница 4-го уровня: дальнейшие переводы не наблюдаются")
    if row["is_seed"]:
        warnings.append("Seed: входящие извне выборки не видны; отношение потоков не доказывает роль")
    if row["isolated"]:
        warnings.append("Узел включён в исходный список, но не имеет наблюдаемых рёбер")
    if row["observed_out_exceeds_in"]:
        warnings.append("Выход превышает наблюдаемый вход: это не доказательство аномалии")
    if role == "terminal":
        warnings.append("Конечный только в наблюдаемом периоде, банке и пороге суммы")
    percent = f"{ratio:.0%}" if math.isfinite(ratio) else "н/д"
    evidence = f"Плательщиков {row['in_deg']}; получателей {row['out_deg']}; вход {row['in_kzt']:,.0f} KZT; выход/вход {percent}; seed-предков {row['seed_reach']}."
    if row["boundary_censored"]:
        evidence += " Граница depth=4: удержание неизвестно."
    elif row["isolated"]:
        evidence += " Нет рёбер: недостаточно наблюдений."
    elif row["is_seed"]:
        evidence += " Seed: вход неполный."
    return {"role": role, "role_score": round(max(0, min(score, 1)), 6),
            "matched_rules": list(candidates), "evidence": evidence[:200], "warnings": warnings}
