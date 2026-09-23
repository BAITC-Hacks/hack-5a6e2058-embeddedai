from datetime import date

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from money_graph.loader import COLUMNS, DataError, load, validate
from money_graph.pipeline import analyze


@pytest.fixture()
def tables():
    first, second, isolate = -(2**63), 2**63 - 1, 2**53 + 1
    return {
        "nodes": pd.DataFrame(
            {"gid": [first, second, isolate], "depth": [0, 1, 0], "is_seed": [True, False, True]}
        ),
        "edges": pd.DataFrame(
            {"src": [first], "dst": [second], "sum_kzt": [5000.0], "n_tx": [1], "depth": [1]}
        ),
        "transactions": pd.DataFrame(
            {"src": [first], "dst": [second], "sum_kzt": [5000.0], "date": ["2026-07-01"]}
        ),
    }


def save(directory, tables):
    for name, frame in tables.items():
        frame.to_parquet(directory / f"{name}.parquet", index=False)


def test_entire_signed_int64_domain_and_nullable_integers_are_preserved(tmp_path, tables):
    for frame in tables.values():
        for column in ("gid", "src", "dst"):
            if column in frame:
                frame[column] = frame[column].astype("Int64")
    save(tmp_path, tables)
    nodes, edges, transactions = load(tmp_path)
    assert nodes.gid.dtype == np.dtype("int64")
    assert edges.src[0] == transactions.src[0] == -(2**63)
    assert edges.dst[0] == transactions.dst[0] == 2**63 - 1
    result = analyze(tmp_path)
    assert {node["gid"] for node in result["nodes"]} == {str(g) for g in tables["nodes"].gid}


def test_extra_columns_cannot_leak_attributes_or_override_metrics(tmp_path, tables):
    tables["nodes"]["customer_name"] = "private information"
    tables["nodes"]["role"] = "fabricated"
    tables["edges"]["total"] = 999
    tables["edges"]["_merge"] = "spoofed"
    save(tmp_path, tables)
    loaded = load(tmp_path)
    for (name, _), frame in zip(tables.items(), loaded, strict=True):
        assert set(frame.columns) == COLUMNS[name]
    result = analyze(tmp_path)
    assert all("customer_name" not in node for node in result["nodes"])


@pytest.mark.parametrize("value", [2**63, 2**64 - 1])
def test_unsigned_identifier_overflow_is_rejected(tmp_path, tables, value):
    tables["nodes"]["gid"] = pd.Series([1, value, 3], dtype="uint64")
    save(tmp_path, tables)
    with pytest.raises(DataError, match="int64"):
        load(tmp_path)


def test_unsigned_identifiers_inside_signed_range_are_supported(tmp_path, tables):
    mapping = {-(2**63): 1, 2**63 - 1: 2**63 - 1, 2**53 + 1: 2**53 + 1}
    for frame in tables.values():
        for column in ("gid", "src", "dst"):
            if column in frame:
                frame[column] = pd.Series([mapping[v] for v in frame[column]], dtype="uint64")
    save(tmp_path, tables)
    nodes, edges, _ = load(tmp_path)
    assert max(nodes.gid) == 2**63 - 1
    assert edges.dst[0] == 2**63 - 1


@pytest.mark.parametrize(
    "value",
    [20260701, 0, True, "NaT", "07/01/2026", "2026-02-30", "2026-07-01T12:00:00"],
)
def test_invalid_date_encodings_are_not_silently_reinterpreted(tmp_path, tables, value):
    tables["transactions"]["date"] = [value]
    save(tmp_path, tables)
    with pytest.raises(DataError, match="дата"):
        load(tmp_path)


@pytest.mark.parametrize(
    "value", [pd.Timestamp("2026-07-01T01:00:00"), pd.Timestamp("2026-07-01", tz="UTC")]
)
def test_intraday_and_timezone_timestamps_require_explicit_date_conversion(tmp_path, tables, value):
    tables["transactions"]["date"] = [value]
    save(tmp_path, tables)
    with pytest.raises(DataError, match="дата"):
        load(tmp_path)


@pytest.mark.parametrize("value", ["2026-07-01", date(2026, 7, 1), pd.Timestamp("2026-07-01")])
def test_schema_calendar_date_encodings_are_supported(tmp_path, tables, value):
    tables["transactions"]["date"] = [value]
    save(tmp_path, tables)
    _, _, transactions = load(tmp_path)
    assert transactions.date[0] == pd.Timestamp("2026-07-01")


@pytest.mark.parametrize("amount", [True, 0, -5000, np.inf, np.nan, 1e308, 2**62])
def test_invalid_or_overflowing_amounts_are_rejected(tmp_path, tables, amount):
    for name in ("edges", "transactions"):
        tables[name]["sum_kzt"] = [amount]
    save(tmp_path, tables)
    with pytest.raises(DataError):
        load(tmp_path)


def test_complex_amounts_are_a_data_error_instead_of_an_uncaught_exception(tables):
    tables["transactions"]["sum_kzt"] = [5000 + 2j]
    with pytest.raises(DataError, match="суммы"):
        validate(*tables.values())


def test_duplicate_parquet_columns_fail_actionably(tmp_path, tables):
    save(tmp_path, tables)
    pq.write_table(
        pa.Table.from_arrays(
            [pa.array([1]), pa.array([1]), pa.array([0]), pa.array([True])],
            names=["gid", "gid", "depth", "is_seed"],
        ),
        tmp_path / "nodes.parquet",
    )
    with pytest.raises(DataError, match="повторяющиеся"):
        load(tmp_path)


def test_missing_required_column_names_are_reported(tmp_path, tables):
    tables["transactions"] = tables["transactions"].drop(columns="date")
    save(tmp_path, tables)
    with pytest.raises(DataError, match="transactions: отсутствуют столбцы date"):
        load(tmp_path)


@pytest.mark.parametrize(
    "table,column,values,message",
    [
        ("nodes", "gid", [1, 1, 3], "уникальных"),
        ("nodes", "gid", pd.Series([1, None, 3], dtype="Int64"), "пропуски"),
        ("nodes", "depth", [0, 5, 0], "глубина"),
        ("nodes", "is_seed", [True, True, True], "Seed"),
        ("edges", "src", [42], "отсутствующий"),
        ("edges", "depth", [0], "глубина"),
        ("edges", "n_tx", [0], "n_tx"),
        ("edges", "n_tx", [1.0], "целочисленный"),
        ("transactions", "dst", [42], "отсутствующий"),
    ],
)
def test_inconsistent_graph_contract_fails_before_analysis(
    tmp_path, tables, table, column, values, message
):
    tables[table][column] = values
    save(tmp_path, tables)
    with pytest.raises(DataError, match=message):
        load(tmp_path)


def test_empty_edges_and_transactions_preserve_all_isolates(tmp_path, tables):
    tables["edges"] = tables["edges"].iloc[:0]
    tables["transactions"] = tables["transactions"].iloc[:0]
    save(tmp_path, tables)
    result = analyze(tmp_path)
    assert result["report"]["n_nodes"] == result["report"]["n_isolates"] == 3
    assert result["report"]["n_edges"] == result["report"]["n_transactions"] == 0
    assert all(node["role"] == "peripheral" for node in result["nodes"])
    assert all(node["priority_score"] == 0 for node in result["nodes"])
