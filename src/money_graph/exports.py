"""The evaluator-facing CSV contract, checked before publishing an export."""

import csv
import hashlib
from pathlib import Path

from .loader import DataError

EXPORT_SCHEMAS = {
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


def export_manifest(result: dict, out_dir: Path) -> dict:
    """Confirm the files an evaluator receives, after the serializer has finished."""
    expected_counts = {
        "nodes_roles.csv": len(result["nodes"]),
        "clusters.csv": len(result["clusters"]),
        "top_nodes.csv": min(50, len(result["nodes"])),
    }
    manifest = {}
    for name, columns in EXPORT_SCHEMAS.items():
        path = (out_dir / name).resolve()
        try:
            with path.open(encoding="utf-8", newline="") as source:
                reader = csv.DictReader(source, strict=True)
                if reader.fieldnames != list(columns):
                    raise DataError(f"Выгрузка {name}: схема CSV не соответствует ТЗ")
                count = 0
                for row in reader:
                    count += 1
                    if None in row or any(
                        not (row.get(column) or "").strip() for column in columns
                    ):
                        raise DataError(
                            f"Выгрузка {name}: незаполненная или некорректная строка {count}"
                        )
                if count != expected_counts[name]:
                    raise DataError(
                        f"Выгрузка {name}: ожидалось {expected_counts[name]} строк, получено {count}"
                    )
        except (csv.Error, UnicodeError) as exc:
            raise DataError(f"Выгрузка {name}: некорректный CSV в UTF-8") from exc
        with path.open("rb") as source_bytes:
            digest = hashlib.file_digest(source_bytes, "sha256").hexdigest()
        manifest[name] = {
            "path": str(path),
            "rows": count,
            "columns": list(columns),
            "sha256": digest,
        }
    return manifest
