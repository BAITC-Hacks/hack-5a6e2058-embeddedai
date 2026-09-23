"""Independently check the submitted files, without rerunning graph analysis.

CSV-only checks use Python's standard library. Optional --data reads and
validates raw Parquet through the project's existing loader, then checks totals.
"""

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# This is the organizer's public file contract, independent of our serializer.
SCHEMAS = {
    "nodes_roles.csv": ("gid", "role", "role_score", "cluster_id", "priority_score", "evidence"),
    "clusters.csv": (
        "cluster_id",
        "n_nodes",
        "n_seed",
        "sum_kzt_internal",
        "top_gids",
        "hypothesis",
    ),
    "top_nodes.csv": ("rank", "gid", "role", "priority_score", "why"),
}
ROLES = {"consolidator", "transit", "distributor", "terminal", "coordinator", "peripheral"}
INTEGER = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")
MONEY_TOLERANCE = Decimal("0.01")


class SubmissionError(ValueError):
    """A concrete mismatch an evaluator can fix or investigate."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SubmissionError(message)


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def identifier(value: Any, label: str) -> str:
    require(
        isinstance(value, str) and len(value) <= 20 and INTEGER.fullmatch(value) is not None,
        f"{label}: нужен канонический строковый int64 без пробелов, округления и ведущих нулей",
    )
    require(-(2**63) <= int(value) < 2**63, f"{label}: gid вне диапазона signed int64")
    return value


def integer(value: Any, label: str, minimum: int = 0) -> int:
    require(
        isinstance(value, str) and len(value) <= 20 and INTEGER.fullmatch(value) is not None,
        f"{label}: нужно целое число",
    )
    parsed = int(value)
    require(minimum <= parsed < 2**63, f"{label}: целое число вне допустимых границ")
    return parsed


def number(value: Any, label: str, maximum: Decimal | None = None) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise SubmissionError(f"{label}: нужно конечное число") from exc
    require(parsed.is_finite() and parsed >= 0, f"{label}: нужно конечное неотрицательное число")
    require(maximum is None or parsed <= maximum, f"{label}: значение должно быть в [0; 1]")
    return parsed


def read_json(path: Path) -> Any:
    def invalid(value: str) -> None:
        raise SubmissionError(f"{path.name}: недопустимое JSON-число {value}")

    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid)
    except (ValueError, UnicodeError) as exc:
        raise SubmissionError(f"{path.name}: некорректный JSON в UTF-8") from exc


def read_csv(path: Path, columns: tuple[str, ...]) -> list[dict[str, str]]:
    require(path.is_file(), f"Отсутствует обязательная выгрузка: {path}")
    try:
        with path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            require(
                reader.fieldnames == list(columns),
                f"{path.name}: нужна точная схема {','.join(columns)}",
            )
            rows = []
            for index, row in enumerate(reader, 2):
                require(
                    None not in row
                    and all(isinstance(row.get(key), str) and row[key].strip() for key in columns),
                    f"{path.name}, строка {index}: незаполненные или лишние поля",
                )
                rows.append(row)
        return rows
    except (UnicodeError, csv.Error) as exc:
        raise SubmissionError(f"{path.name}: некорректный CSV в UTF-8") from exc


def _report(
    report: Any, nodes: dict[str, dict[str, Any]], clusters: dict[int, dict[str, Any]]
) -> None:
    require(isinstance(report, dict), "run_report.json: нужен объект")
    role_counts = Counter(row["role"] for row in nodes.values())
    expected = {
        "n_nodes": len(nodes),
        "n_clusters": len(clusters),
        "n_seed": sum(row["n_seed"] for row in clusters.values()),
        "role_counts": {role: role_counts[role] for role in ROLES},
    }
    for key, value in expected.items():
        require(
            report.get(key) == value,
            f"run_report.json: {key} не соответствует CSV; возможна устаревшая выгрузка",
        )
    require(
        isinstance(report.get("rules"), dict)
        and report["rules"].get("version") == report.get("rules_version"),
        "run_report.json: версии правил не согласованы",
    )


def _snapshot(snapshot: Any, tables: dict[str, list[dict[str, Any]]], report: Any) -> None:
    require(isinstance(snapshot, dict), "result.json: нужен объект")
    if report is not None:
        require(
            snapshot.get("report") == report,
            "result.json и run_report.json относятся к разным поколениям результата",
        )
    for filename, key, identifier_key in (
        ("nodes_roles.csv", "nodes", "gid"),
        ("clusters.csv", "clusters", "cluster_id"),
        ("top_nodes.csv", "top", "gid"),
    ):
        saved = snapshot.get(key)
        require(
            isinstance(saved, list) and len(saved) == len(tables[filename]),
            f"result.json: число {key} не совпадает с {filename}",
        )
        mapping = {}
        for row in saved:
            require(
                isinstance(row, dict) and identifier_key in row,
                f"result.json: повреждена запись {key}",
            )
            identifier_value = row[identifier_key]
            require(
                type(identifier_value) is (str if identifier_key == "gid" else int),
                f"result.json: неверный тип {identifier_key}",
            )
            require(identifier_value not in mapping, f"result.json: повтор {identifier_key}")
            mapping[identifier_value] = row
        for actual in tables[filename]:
            expected = mapping.get(actual[identifier_key])
            require(
                expected is not None, f"result.json: {identifier_key} из {filename} отсутствует"
            )
            for column, value in actual.items():
                same = (
                    number(expected.get(column), f"result.json {column}") == value
                    if isinstance(value, Decimal)
                    else expected.get(column) == value
                )
                require(
                    same,
                    f"result.json: {filename}/{actual[identifier_key]}/{column} не совпадает с CSV",
                )


def _raw(
    data_dir: Path,
    nodes: dict[str, dict[str, Any]],
    clusters: dict[int, dict[str, Any]],
    report: Any,
) -> dict[str, Any]:
    # The default CSV-only command has no project/dependency imports at all.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    try:
        from money_graph.loader import load
    except ImportError as exc:
        raise SubmissionError(
            "Для --data нужны зависимости проекта: запустите uv run --frozen --no-dev python scripts/check_submission.py --out ... --data ..."
        ) from exc
    raw_nodes, edges, transactions = load(data_dir)
    source_ids = {str(gid) for gid in raw_nodes.gid}
    missing, extra = source_ids - nodes.keys(), nodes.keys() - source_ids
    require(
        not missing and not extra,
        f"nodes_roles.csv: набор gid отличается от nodes.parquet; пропущено {len(missing)}, лишних {len(extra)}",
    )
    seeds: Counter[int] = Counter()
    for gid, is_seed in raw_nodes[["gid", "is_seed"]].itertuples(index=False, name=None):
        if is_seed:
            seeds[nodes[str(gid)]["cluster_id"]] += 1
    amounts: dict[int, list[float]] = defaultdict(list)
    internal, external, self_transfers = [], [], []
    for src, dst, amount in edges[["src", "dst", "sum_kzt"]].itertuples(index=False, name=None):
        src_cluster, dst_cluster = nodes[str(src)]["cluster_id"], nodes[str(dst)]["cluster_id"]
        if src_cluster == dst_cluster:
            amounts[src_cluster].append(amount)
            internal.append(amount)
        else:
            external.append(amount)
        if src == dst:
            self_transfers.append(amount)
    for cid, cluster in clusters.items():
        require(
            cluster["n_seed"] == seeds[cid],
            f"clusters.csv: кластер {cid}, n_seed не совпадает с nodes.parquet",
        )
        total = Decimal(str(math.fsum(amounts[cid])))
        require(
            abs(cluster["sum_kzt_internal"] - total) <= MONEY_TOLERANCE,
            f"clusters.csv: кластер {cid}, внутренний оборот отличается от edges.parquet более чем на 0.01 KZT",
        )
    fingerprints = {
        name: digest(data_dir / f"{name}.parquet") for name in ("nodes", "edges", "transactions")
    }
    turnover = math.fsum(edges.sum_kzt)
    if report is not None:
        require(
            report.get("input_sha256") == fingerprints,
            "run_report.json: SHA-256 исходников отличается; отчёт устарел или использован другой набор",
        )
        for key, value in (("n_edges", len(edges)), ("n_transactions", len(transactions))):
            require(
                report.get(key) == value, f"run_report.json: {key} не совпадает с исходными данными"
            )
        require(
            abs(
                number(report.get("turnover_kzt"), "run_report.json turnover_kzt")
                - Decimal(str(turnover))
            )
            <= MONEY_TOLERANCE,
            "run_report.json: общий оборот не совпадает с исходными данными",
        )
    return {
        "input_sha256": fingerprints,
        "n_nodes": len(raw_nodes),
        "n_edges": len(edges),
        "n_transactions": len(transactions),
        "turnover_kzt": turnover,
        "internal_turnover_kzt": math.fsum(internal),
        "intercluster_turnover_kzt": math.fsum(external),
        "self_transfer_kzt_included_in_internal": math.fsum(self_transfers),
    }


def check_submission(out_dir: Path, data_dir: Path | None = None) -> dict[str, Any]:
    """Verify published CSVs and optional raw provenance; never rewrite files."""
    tables: dict[str, list[dict[str, Any]]] = {
        name: read_csv(out_dir / name, columns) for name, columns in SCHEMAS.items()
    }
    nodes: dict[str, dict[str, Any]] = {}
    for row in tables["nodes_roles.csv"]:
        gid = identifier(row["gid"], "nodes_roles.csv gid")
        require(gid not in nodes, f"nodes_roles.csv: повтор gid {gid}")
        require(row["role"] in ROLES, f"nodes_roles.csv: gid {gid}, роль вне словаря ТЗ")
        require(
            len(row["evidence"]) <= 200,
            f"nodes_roles.csv: gid {gid}, evidence длиннее 200 символов",
        )
        row["cluster_id"] = integer(row["cluster_id"], "nodes_roles.csv cluster_id")
        for key in ("role_score", "priority_score"):
            row[key] = number(row[key], f"nodes_roles.csv {gid} {key}", Decimal(1))
        nodes[gid] = row
    require(bool(nodes), "nodes_roles.csv: нет узлов")
    ranked = sorted(nodes.values(), key=lambda row: (-row["priority_score"], int(row["gid"])))
    members: dict[int, list[str]] = defaultdict(list)
    for row in ranked:
        members[row["cluster_id"]].append(row["gid"])
    clusters: dict[int, dict[str, Any]] = {}
    for row in tables["clusters.csv"]:
        cid = integer(row["cluster_id"], "clusters.csv cluster_id")
        require(
            cid not in clusters and cid in members,
            f"clusters.csv: кластер {cid} повторяется или отсутствует в узлах",
        )
        row["cluster_id"] = cid
        row["n_nodes"] = integer(row["n_nodes"], "clusters.csv n_nodes", 1)
        row["n_seed"] = integer(row["n_seed"], "clusters.csv n_seed")
        require(
            row["n_nodes"] == len(members[cid]) and row["n_seed"] <= row["n_nodes"],
            f"clusters.csv: неверные n_nodes/n_seed для кластера {cid}",
        )
        row["sum_kzt_internal"] = number(row["sum_kzt_internal"], "clusters.csv sum_kzt_internal")
        try:
            top = json.loads(row["top_gids"])
        except ValueError as exc:
            raise SubmissionError(
                f"clusters.csv: кластер {cid}, top_gids должен быть JSON-списком gid"
            ) from exc
        require(
            isinstance(top, list) and bool(top),
            f"clusters.csv: кластер {cid}, top_gids должен быть непустым списком",
        )
        for gid in top:
            identifier(gid, "clusters.csv top_gids")
        require(
            top == members[cid][: len(top)],
            f"clusters.csv: кластер {cid}, top_gids не соответствует членству/приоритету",
        )
        row["top_gids"] = top
        clusters[cid] = row
    require(
        clusters.keys() == members.keys(),
        "clusters.csv: перечислены не все кластеры nodes_roles.csv",
    )
    top = tables["top_nodes.csv"]
    require(
        min(20, len(nodes)) <= len(top) <= len(nodes),
        "top_nodes.csv: требуется минимум 20 узлов (или все узлы, если исходный набор меньше)",
    )
    for index, row in enumerate(top, 1):
        gid = identifier(row["gid"], "top_nodes.csv gid")
        row["rank"] = integer(row["rank"], "top_nodes.csv rank", 1)
        row["priority_score"] = number(
            row["priority_score"], "top_nodes.csv priority_score", Decimal(1)
        )
        expected = ranked[index - 1]
        require(
            row["rank"] == index and gid == expected["gid"],
            f"top_nodes.csv: место {index} не совпадает с глобальным приоритетом; при равенстве нужен числовой порядок gid",
        )
        require(
            row["role"] == expected["role"] and row["priority_score"] == expected["priority_score"],
            f"top_nodes.csv: gid {gid}, роль/скор отличаются от nodes_roles.csv",
        )
    report_path, snapshot_path = out_dir / "run_report.json", out_dir / "result.json"
    report = read_json(report_path) if report_path.exists() else None
    if report is not None:
        _report(report, nodes, clusters)
    if snapshot_path.exists():
        snapshot = read_json(snapshot_path)
        _snapshot(snapshot, tables, report)
        if report is None:
            report = snapshot.get("report")
            _report(report, nodes, clusters)
    raw = _raw(data_dir, nodes, clusters, report) if data_dir is not None else None
    return {
        "status": "success",
        "scope": "csv_and_raw_inputs" if raw is not None else "csv_only",
        "counts": {"nodes": len(nodes), "clusters": len(clusters), "top_nodes": len(top)},
        "checks": {
            "exact_csv_schemas": True,
            "unique_int64_gids": True,
            "finite_scores": True,
            "evidence_and_explanations": True,
            "cluster_membership": True,
            "global_top_ranking": True,
            "report_consistency": report is not None,
            "result_snapshot_consistency": snapshot_path.exists(),
            "raw_node_coverage_and_cluster_totals": raw is not None,
            "raw_source_hashes_match_report": raw is not None and report is not None,
        },
        "exports": {
            name: {
                "rows": len(tables[name]),
                "columns": list(columns),
                "sha256": digest(out_dir / name),
            }
            for name, columns in SCHEMAS.items()
        },
        "raw": raw,
        "limitations": [
            "Проверка контракта и согласованности не подтверждает истинность эвристических ролей или гипотез."
        ],
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Независимая проверка трёх CSV по ТЗ без повторного анализа"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("artifacts"),
        help="Каталог трёх CSV и необязательных JSON-отчётов",
    )
    parser.add_argument(
        "--data",
        type=Path,
        help="Необязательно: исходные nodes/edges/transactions.parquet; требует зависимости проекта",
    )
    args = parser.parse_args(argv)
    try:
        answer = check_submission(args.out, args.data)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(answer, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
