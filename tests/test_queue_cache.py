import copy
import json
import os
from pathlib import Path

import pytest

from money_graph import storage
from money_graph.demo import create_demo
from money_graph.pipeline import analyze
from money_graph.queue import node_queue
from money_graph.storage import CorruptRun, MissingRun, ObsoleteRun, ResultCache


@pytest.fixture()
def queue_result():
    rows = []
    profiles = (
        ("9007199254741001", "consolidator", 5, 2, 2, False, 400),
        ("9007199254741002", "consolidator", 3, 2, 2, True, 700),
        ("9007199254741003", "consolidator", 6, 3, 3, False, 600),
        ("2", "transit", 1, 0, 1, False, 1000),
        ("10", "transit", 2, 0, 1, True, 1000),
        ("9007199254741004", "peripheral", 4, 1, 4, False, 0),
    )
    for gid, role, rank, cluster, depth, seed, volume in profiles:
        rows.append(
            {
                "gid": gid,
                "role": role,
                "role_score": 0.5,
                "priority_score": (7 - rank) / 7,
                "rank": rank,
                "cluster_id": cluster,
                "depth": depth,
                "is_seed": seed,
                "in_deg": rank,
                "out_deg": 7 - rank,
                "in_kzt": volume,
                "out_kzt": volume / 2,
                "volume": volume,
                "evidence": "Наблюдаемая структурная гипотеза",
                "anomaly_profile": {"signals": ["signal"] if role == "transit" else []},
            }
        )
    return {"nodes": rows}


def test_queue_combines_filters_preserves_global_rank_and_returns_all_matching_nodes(queue_result):
    original = copy.deepcopy(queue_result)
    selection = node_queue(queue_result, role="consolidator", depth=2, cluster=2)
    assert selection["total"] == 6 and selection["matched"] == 2
    assert [node["rank"] for node in selection["nodes"]] == [3, 5]
    assert [node["gid"] for node in selection["nodes"]] == [
        "9007199254741002",
        "9007199254741001",
    ]
    only_seed = node_queue(queue_result, role="consolidator", depth=2, cluster=2, seeds=True)
    assert [node["gid"] for node in only_seed["nodes"]] == ["9007199254741002"]
    assert only_seed["matched"] == 1
    assert queue_result == original


def test_queue_search_retains_full_int64_strings_and_composes_with_role(queue_result):
    selected = node_queue(queue_result, search="900719925474100", role="consolidator")
    assert selected["matched"] == 3
    assert all(isinstance(node["gid"], str) for node in selected["nodes"])
    exact = node_queue(queue_result, search="9007199254741003")
    assert [node["gid"] for node in exact["nodes"]] == ["9007199254741003"]
    assert node_queue(queue_result, search="not-present")["nodes"] == []


def test_queue_pagination_matches_total_and_sorting_keeps_numeric_gid_ties(queue_result):
    page = node_queue(queue_result, sort="volume", order="desc", limit=2)
    assert [node["gid"] for node in page["nodes"]] == ["2", "10"]
    assert [node["n_anomaly_signals"] for node in page["nodes"]] == [1, 1]
    assert page["matched"] == 6 and page["offset"] == 0 and page["limit"] == 2
    second = node_queue(queue_result, sort="volume", order="desc", offset=2, limit=2)
    assert [node["gid"] for node in second["nodes"]] == [
        "9007199254741002",
        "9007199254741003",
    ]
    ascending = node_queue(queue_result, role="transit", sort="volume", order="asc")
    assert [node["gid"] for node in ascending["nodes"]] == ["2", "10"]
    assert node_queue(queue_result, offset=100)["nodes"] == []
    assert node_queue(queue_result, offset=100)["matched"] == 6


@pytest.mark.parametrize("sort", ["in_kzt", "out_kzt", "in_deg", "out_deg", "role_score"])
def test_queue_other_supported_numeric_sorts(queue_result, sort):
    for order in ("asc", "desc"):
        response = node_queue(queue_result, sort=sort, order=order)
        values = [node[sort] for node in response["nodes"]]
        assert values == sorted(values, reverse=order == "desc")


@pytest.mark.parametrize(
    "options", [{"sort": "gid"}, {"sort": "__dict__"}, {"order": "sideways"}, {"role": "fake"}]
)
def test_queue_refuses_unknown_sort_or_role(queue_result, options):
    with pytest.raises(ValueError, match="Неизвестная"):
        node_queue(queue_result, **options)


@pytest.fixture(scope="module")
def stored_result(tmp_path_factory):
    source = tmp_path_factory.mktemp("cache-source")
    create_demo(source)
    return analyze(source)


def publish(directory, run_id, result):
    path = directory / run_id / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return path


def spy_reads(monkeypatch):
    calls = []
    original = storage.read_result

    def read(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "read_result", read)
    return calls


def test_cache_checks_file_metadata_on_every_hit_without_parsing_again(
    tmp_path, stored_result, monkeypatch
):
    run = "a" * 32
    path = publish(tmp_path, run, stored_result)
    calls = spy_reads(monkeypatch)
    stat_calls = []
    original_stat = Path.stat

    def stat(self, *args, **kwargs):
        if self == path:
            stat_calls.append(self)
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    cache = ResultCache(tmp_path, stored_result["report"]["rules"])
    first = cache.get(run)
    read_count = len(stat_calls)
    assert cache.get(run) is first
    assert cache.get(run) is first
    assert len(stat_calls) == read_count + 2
    assert calls == [run]


def test_cache_invalidates_in_place_edit_even_when_mtime_is_restored(
    tmp_path, stored_result, monkeypatch
):
    run = "b" * 32
    path = publish(tmp_path, run, stored_result)
    calls = spy_reads(monkeypatch)
    cache = ResultCache(tmp_path, stored_result["report"]["rules"])
    original = cache.get(run)
    before = path.stat()
    changed = copy.deepcopy(stored_result)
    changed["report"]["runtime_seconds"] += 1.0
    publish(tmp_path, run, changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    reread = cache.get(run)
    assert reread is not original
    assert reread["report"]["runtime_seconds"] == changed["report"]["runtime_seconds"]
    assert original["report"]["runtime_seconds"] == stored_result["report"]["runtime_seconds"]
    assert calls == [run, run]


def test_cache_invalidates_atomic_replacement_and_deletion(tmp_path, stored_result, monkeypatch):
    run = "c" * 32
    path = publish(tmp_path, run, stored_result)
    calls = spy_reads(monkeypatch)
    cache = ResultCache(tmp_path, stored_result["report"]["rules"])
    first = cache.get(run)
    before = path.stat()
    replacement = path.with_suffix(".pending")
    replacement.write_bytes(path.read_bytes())
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(replacement, path)
    assert cache.get(run) is not first
    assert calls == [run, run]
    path.unlink()
    with pytest.raises(MissingRun):
        cache.get(run)
    assert run not in cache.entries


@pytest.mark.parametrize("fault", ["invalid-json", "invalid-node", "obsolete-rules"])
def test_cache_never_reuses_snapshot_after_corruption_or_obsolete_rules(
    tmp_path, stored_result, fault
):
    run = "d" * 32
    path = publish(tmp_path, run, stored_result)
    cache = ResultCache(tmp_path, stored_result["report"]["rules"])
    cache.get(run)
    modified = copy.deepcopy(stored_result)
    if fault == "invalid-json":
        path.write_text("{broken", encoding="utf-8")
    elif fault == "invalid-node":
        modified["nodes"][0]["role"] = "fabricated"
        publish(tmp_path, run, modified)
    else:
        modified["report"]["rules"]["random_seed"] += 1
        publish(tmp_path, run, modified)
    with pytest.raises(ObsoleteRun if fault == "obsolete-rules" else CorruptRun):
        cache.get(run)
    assert run not in cache.entries


def test_cache_lru_keeps_two_recent_entries(tmp_path, stored_result, monkeypatch):
    ids = [f"{index:032x}" for index in range(3)]
    for run in ids:
        publish(tmp_path, run, stored_result)
    calls = spy_reads(monkeypatch)
    cache = ResultCache(tmp_path, stored_result["report"]["rules"], max_entries=2)
    first = cache.get(ids[0])
    second = cache.get(ids[1])
    assert cache.get(ids[0]) is first
    cache.get(ids[2])
    assert set(cache.entries) == {ids[0], ids[2]}
    assert cache.get(ids[1]) is not second
    assert calls == [ids[0], ids[1], ids[2], ids[1]]
    assert len(cache.entries) == 2


def test_cache_rejects_replacement_while_reading_and_does_not_cache_it(
    tmp_path, stored_result, monkeypatch
):
    run = "e" * 32
    path = publish(tmp_path, run, stored_result)
    original = storage.read_result

    def replace_while_reading(*args, **kwargs):
        result = original(*args, **kwargs)
        pending = path.with_suffix(".pending")
        pending.write_bytes(path.read_bytes())
        os.replace(pending, path)
        return result

    monkeypatch.setattr(storage, "read_result", replace_while_reading)
    cache = ResultCache(tmp_path, stored_result["report"]["rules"])
    with pytest.raises(CorruptRun):
        cache.get(run)
    assert not cache.entries


def test_cache_rejects_invalid_run_identifier_before_reading(tmp_path, stored_result, monkeypatch):
    calls = spy_reads(monkeypatch)
    cache = ResultCache(tmp_path, stored_result["report"]["rules"])
    for run in ("../outside", "a", "0" * 31, "0" * 33):
        with pytest.raises(MissingRun):
            cache.get(run)
    assert calls == []
