"""Conservative, depth-relative profile signals; never a verdict or a role input."""

from collections import defaultdict
from typing import Any

import numpy as np

MINIMUM_COHORT_SIZE = 20
METRICS = {
    "in_deg": "Число плательщиков",
    "out_deg": "Число получателей",
    "volume": "Наблюдаемый объём max(вход, выход), KZT",
}
CAVEAT = (
    "Сравнение только с активными узлами того же колена. "
    "Необычный профиль — повод проверить контекст, не доказательство нарушения или дробления. "
    "Баланс и отношение потоков не используются; самопереводы исключены. "
    "Сигналы не меняют роль и приоритет."
)


def annotate(records: list[dict[str, Any]]) -> None:
    """Flag values strictly above Q3 + 3*IQR; abstain for small/flat cohorts."""
    cohorts: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if row["in_deg"] + row["out_deg"] > 0:
            cohorts[row["depth"]].append(row)
    fences: dict[int, dict[str, tuple[float, float, float]]] = {}
    for depth, cohort in cohorts.items():
        if len(cohort) < MINIMUM_COHORT_SIZE:
            continue
        fences[depth] = {}
        for metric in METRICS:
            q1, q3 = np.quantile([row[metric] for row in cohort], [0.25, 0.75], method="linear")
            fences[depth][metric] = (float(q1), float(q3), float(q3 + 3 * (q3 - q1)))
    for row in records:
        size = len(cohorts[row["depth"]])
        signals = []
        notes = []
        active = row["in_deg"] + row["out_deg"] > 0
        if not active:
            notes.append("Нет связей с другими клиентами: узел исключён из сравнения профилей.")
        elif size < MINIMUM_COHORT_SIZE:
            notes.append(
                f"В колене {row['depth']} только {size} активных узлов; "
                f"для сравнения нужно не менее {MINIMUM_COHORT_SIZE}. Оценка не выполнялась."
            )
        else:
            flat = []
            for metric, (q1, q3, threshold) in fences[row["depth"]].items():
                if q3 == q1:
                    flat.append(METRICS[metric])
                    continue
                value = row[metric]
                if value > threshold:
                    signals.append(
                        {
                            "metric": metric,
                            "value": value,
                            "threshold": threshold,
                            "q1": q1,
                            "q3": q3,
                            "text": (
                                f"{METRICS[metric]}: {value:g} > {threshold:g} "
                                f"(Q3 + 3×IQR; Q1={q1:g}, Q3={q3:g}). "
                                f"Колено {row['depth']}, {size} активных узлов."
                            ),
                        }
                    )
            if not signals:
                notes.append(
                    "Превышений проверяемых порогов не найдено; это не оценка безопасности."
                )
            if flat:
                notes.append(
                    "IQR=0: сравнение не выполнялось для признаков: " + "; ".join(flat) + "."
                )
        row["anomaly_profile"] = {
            "cohort_depth": row["depth"],
            "cohort_size": size,
            "minimum_cohort_size": MINIMUM_COHORT_SIZE,
            "signals": signals,
            "caveat": " ".join([*notes, CAVEAT]),
        }
