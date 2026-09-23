"""Read the organizer schema without converting identifiers through float."""

import re
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

FILES = ("nodes", "edges", "transactions")
COLUMNS = {
    "nodes": {"gid", "depth", "is_seed"},
    "edges": {"src", "dst", "sum_kzt", "n_tx", "depth"},
    "transactions": {"src", "dst", "sum_kzt", "date"},
}
LIMITS = {"nodes": 10_000, "edges": 50_000, "transactions": 100_000}
# Larger totals cannot retain individual tiyn when represented by float64.
MAX_TOTAL_KZT = (2**53 - 1) / 100


class DataError(ValueError):
    """Input cannot be analyzed reliably."""


def load(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    tables = {}
    for name in FILES:
        path = data_dir / f"{name}.parquet"
        if not path.is_file():
            raise DataError(f"Отсутствует {name}.parquet в папке данных")
        try:
            meta = pq.read_metadata(path)
            if meta.num_rows > LIMITS[name]:
                raise DataError(f"{name}: превышен предел {LIMITS[name]} строк для демо")
            if (
                sum(meta.row_group(i).total_byte_size for i in range(meta.num_row_groups))
                > 128_000_000
            ):
                raise DataError(f"{name}: распакованный файл превышает 128 МБ")
            columns = meta.schema.to_arrow_schema().names
            if len(columns) != len(set(columns)):
                raise DataError(f"{name}: повторяющиеся имена столбцов")
            missing = COLUMNS[name] - set(columns)
            if missing:
                raise DataError(f"{name}: отсутствуют столбцы {', '.join(sorted(missing))}")
            # Extra fields must not override computed metrics or leak client attributes.
            tables[name] = pd.read_parquet(path, columns=sorted(COLUMNS[name]))[
                sorted(COLUMNS[name])
            ]
        except DataError:
            raise
        except Exception as exc:
            raise DataError(
                f"Не удалось прочитать {name}.parquet: проверьте формат Parquet"
            ) from exc
    nodes, edges, tx = (tables[name] for name in FILES)
    validate(nodes, edges, tx)
    return (
        nodes.sort_values("gid").reset_index(drop=True),
        edges.sort_values(["src", "dst"]).reset_index(drop=True),
        tx.sort_values(["date", "src", "dst", "sum_kzt"]).reset_index(drop=True),
    )


def validate(nodes: pd.DataFrame, edges: pd.DataFrame, tx: pd.DataFrame) -> None:
    for name, df in zip(FILES, (nodes, edges, tx), strict=True):
        if not df.columns.is_unique:
            raise DataError(f"{name}: повторяющиеся имена столбцов")
        missing = COLUMNS[name] - set(df.columns)
        if missing:
            raise DataError(f"{name}: отсутствуют столбцы {', '.join(sorted(missing))}")
        if df[list(COLUMNS[name])].isna().any().any():
            raise DataError(f"{name}: пропуски в обязательных полях")
        for col in ("gid", "src", "dst", "depth", "n_tx"):
            if col in COLUMNS[name] and not pd.api.types.is_integer_dtype(df[col]):
                raise DataError(f"{name}.{col}: ожидается целочисленный тип, не float/string")
        for col in ("gid", "src", "dst"):
            if col in COLUMNS[name]:
                if (df[col] < -(2**63)).any() or (df[col] > 2**63 - 1).any():
                    raise DataError(f"{name}.{col}: ID должен помещаться в signed int64")
                df[col] = df[col].astype("int64")
        if "sum_kzt" in COLUMNS[name]:
            if (
                not (
                    pd.api.types.is_integer_dtype(df.sum_kzt)
                    or pd.api.types.is_float_dtype(df.sum_kzt)
                )
                or not np.isfinite(df.sum_kzt).all()
            ):
                raise DataError(f"{name}: некорректные суммы")
            if (df.sum_kzt <= 0).any():
                raise DataError(f"{name}: суммы должны быть положительными")
            # Normalize before groupby: summing integer columns can silently overflow.
            df["sum_kzt"] = df.sum_kzt.astype("float64")
            with np.errstate(over="ignore", invalid="ignore"):
                total = float(df.sum_kzt.sum())
            if not np.isfinite(total) or total > MAX_TOTAL_KZT:
                raise DataError(f"{name}: общий оборот слишком велик для точного расчёта в тиынах")
    if nodes.empty or not nodes.gid.is_unique:
        raise DataError("nodes: нужен непустой список уникальных gid")
    if not pd.api.types.is_bool_dtype(nodes.is_seed):
        raise DataError("nodes.is_seed: ожидается bool")
    if not nodes.depth.between(0, 4).all() or not edges.depth.between(1, 4).all():
        raise DataError("Недопустимая глубина обхода")
    if not (nodes.is_seed == (nodes.depth == 0)).all():
        raise DataError("Seed должен соответствовать depth=0")
    if edges.duplicated(["src", "dst"]).any() or (edges.n_tx < 1).any():
        raise DataError("edges: повторяющиеся пары или неверное n_tx")
    known = set(nodes.gid)
    for name, df in (("edges", edges), ("transactions", tx)):
        if (set(df.src) | set(df.dst)) - known:
            raise DataError(f"{name}: ссылка на отсутствующий в nodes узел")
    tx["date"] = _dates(tx.date)
    agg = tx.groupby(["src", "dst"]).agg(total=("sum_kzt", "sum"), count=("sum_kzt", "size"))
    merged = edges.merge(agg, on=["src", "dst"], how="outer", indicator=True)
    if not (merged["_merge"] == "both").all():
        raise DataError("Пары edges и transactions не совпадают")
    if not np.allclose(merged.sum_kzt, merged.total, atol=0.01, rtol=0):
        raise DataError("Суммы edges не совпадают с transactions (допуск 0,01 KZT)")
    if not (merged.n_tx == merged["count"]).all():
        raise DataError("Количество переводов n_tx не совпадает с transactions")


def _dates(values: pd.Series) -> pd.Series:
    """The contract contains calendar dates, not Unix offsets or intraday timestamps."""
    try:
        for value in values:
            if isinstance(value, str):
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
                    raise ValueError("Use an ISO calendar date")
            elif not isinstance(value, (date, datetime, pd.Timestamp)):
                raise ValueError("Expected a calendar date")
        parsed = pd.to_datetime(values, errors="raise")
        if (
            parsed.isna().any()
            or parsed.dt.tz is not None
            or not (parsed == parsed.dt.normalize()).all()
        ):
            raise ValueError("Time and timezone are not part of the input schema")
        return parsed
    except (TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise DataError(
            "transactions.date: некорректная дата; ожидается YYYY-MM-DD без времени и часового пояса"
        ) from exc
