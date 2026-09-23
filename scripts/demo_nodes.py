"""Export factual Markdown cards for arbitrary exact gids, without a server or AI."""

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

from money_graph.investigation import node_evidence
from money_graph.loader import DataError
from money_graph.pipeline import validate_result
from money_graph.roles import LABELS


def text(value: Any) -> str:
    return html.escape(str(value)).replace("|", "&#124;").replace("\n", " ")


def render_card(result: dict[str, Any], node: dict[str, Any]) -> str:
    report, cfg = result["report"], result["report"]["rules"]
    ratio = node["pass_through"]
    metrics = {
        "Роль": f"{node['role']} — {LABELS[node['role']]}",
        "role_score / priority_score": f"{node['role_score']} / {node['priority_score']}",
        "Ранг / сообщество / depth": f"{node['rank']} / {node['cluster_id']} / {node['depth']}",
        "Seed / граница / изолят": f"{node['is_seed']} / {node['boundary_censored']} / {node['isolated']}",
        "Плательщики / получатели": f"{node['in_deg']} / {node['out_deg']}",
        "Вход / выход, KZT": f"{node['in_kzt']:.2f} / {node['out_kzt']:.2f}",
        "Входящих / исходящих операций": f"{node['in_tx']} / {node['out_tx']}",
        "Выход / вход": "не определён" if ratio is None else f"{ratio:.9f}",
        "Seed-предков": node["seed_reach"],
        "P(betweenness)": f"{node['p_betweenness']:.9f}",
    }
    lines = [
        f"# Узел {node['gid']}",
        "",
        f"Правила {text(report['rules_version'])}; период {text(report['period_from'])} — {text(report['period_to'])}.",
        "Роль — гипотеза для проверки; role_score не является вероятностью виновности.",
        "",
        "| Показатель | Значение |",
        "|---|---|",
        *[f"| {name} | {text(value)} |" for name, value in metrics.items()],
        "",
        "## Обоснование и порядок правил",
        "",
        text(node["evidence"]),
        "",
        "Приоритет правил: coordinator → distributor → transit → consolidator → terminal → peripheral.",
        f"Совпавшие правила: {', '.join(node['matched_rules']) or 'нет; peripheral означает недостаточность признаков'}.",
        "",
        "| Правило | Формальное условие |",
        "|---|---|",
        f"| coordinator | seed_reach ≥ {cfg['coordinator_min_seeds']}; in_deg ≥ {cfg['coordinator_min_in']}; out_deg ≥ {cfg['coordinator_min_out']}; P(betweenness) ≥ {cfg['coordinator_betweenness_percentile']} |",
        f"| distributor | out_deg ≥ {cfg['distributor_min_out']}; out_tx ≥ {cfg['distributor_min_out']} |",
        f"| transit | Не seed/граница; вход и выход положительны; {cfg['transit_ratio_min']} ≤ выход/вход ≤ {cfg['transit_ratio_max']} |",
        f"| consolidator | Не seed/граница; in_deg ≥ {cfg['consolidator_min_in']}; выход/вход ≤ {cfg['consolidator_ratio_max']} |",
        "| terminal | Не seed/граница; вход положителен; out_deg = 0 |",
        "| peripheral | Ни одно более конкретное правило не выполнено |",
        "",
        "Поддержка совпавших правил (до поправки × наблюдаемость = итог):",
        "",
        *[
            f"- {item['role']}: {item['raw_support']} × {item['observation_multiplier']} → {item['support']}"
            for item in node["rule_trace"]
        ],
        *(["- Нет конкретной роли; role_score = 0."] if not node["rule_trace"] else []),
        "",
        "## Ограничения и следующие проверки",
        "",
        *[f"- {text(item)}" for item in [*report["warnings"], *node["warnings"]]],
        "",
        *[f"- {text(item)}" for item in node["next_checks"]],
        "",
        "## Наблюдаемые пути",
        "",
    ]
    evidence = node_evidence(result, node["gid"])
    for name, label in [("seed_paths", "От seed"), ("cycles", "Короткие циклы")]:
        lines.extend([f"### {label}", ""])
        for path in evidence[name]:
            lines.append("- " + " → ".join(path["gids"]))
            for edge in path["edges"]:
                lines.append(
                    f"  - {edge['src']} → {edge['dst']}: {edge['sum_kzt']:.2f} KZT, {edge['n_tx']} операций."
                )
        if not evidence[name]:
            lines.append(
                "Ограниченный поиск не нашёл примеров; это не доказательство отсутствия длинных маршрутов."
            )
        lines.append("")
    lines.extend([text(evidence["caveat"]), "", "## Происхождение", ""])
    lines.extend(
        f"- {name}.parquet SHA-256: `{digest}`" for name, digest in report["input_sha256"].items()
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Карточки произвольных gid без сервера и AI")
    parser.add_argument("--result", type=Path, default=Path("artifacts/result.json"))
    parser.add_argument(
        "--gid", action="append", required=True, help="Точный gid; можно повторить до 20 раз"
    )
    parser.add_argument("--out", type=Path, help="Каталог карточек .md; иначе печать в stdout")
    args = parser.parse_args(argv)
    try:
        if len(args.gid) > 20 or len(set(args.gid)) != len(args.gid):
            raise DataError("Нужны от 1 до 20 различных gid")
        result = json.loads(args.result.read_text(encoding="utf-8"))
        validate_result(result)
        nodes = {node["gid"]: node for node in result["nodes"]}
        missing = [gid for gid in args.gid if gid not in nodes]
        if missing:
            raise DataError(f"Неизвестные gid: {', '.join(missing)}")
        cards = [(gid, render_card(result, nodes[gid])) for gid in args.gid]
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            for gid, card in cards:
                destination = args.out / f"{gid}.md"
                destination.write_text(card, encoding="utf-8", newline="\n")
                print(destination)
        else:
            print("\n---\n\n".join(card for _, card in cards))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"Не удалось подготовить карточки: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
