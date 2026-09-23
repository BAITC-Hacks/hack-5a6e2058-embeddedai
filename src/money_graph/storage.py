"""Published runs, recoverable bootstrap metadata and post-success retention."""

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .loader import DataError
from .pipeline import validate_result

LOGGER = logging.getLogger(__name__)
RUN_ID = re.compile(r"[a-f0-9]{32}")


class MissingRun(Exception):
    pass


class CorruptRun(Exception):
    pass


class ObsoleteRun(Exception):
    pass


class ResultCache:
    """Bounded read-only snapshots, invalidated on replacement, edits or deletion.

    Callers must not mutate returned dictionaries. Fingerprints are checked on
    every request, so caching never bypasses rules or corrupt-file recovery.
    """

    def __init__(self, directory: Path, rules: dict[str, Any], max_entries: int = 2):
        self.directory = directory
        self.rules = rules
        self.max_entries = max_entries
        self.entries: OrderedDict[str, tuple[tuple[int, ...], dict[str, Any]]] = OrderedDict()
        self.lock = threading.RLock()

    def get(self, run_id: str) -> dict[str, Any]:
        path = run_path(self.directory, run_id) / "result.json"
        with self.lock:
            try:
                stat = path.stat()
            except FileNotFoundError as exc:
                self.entries.pop(run_id, None)
                raise MissingRun from exc
            except OSError as exc:
                raise CorruptRun from exc
            fingerprint = (
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            )
            cached = self.entries.get(run_id)
            if cached is not None and cached[0] == fingerprint:
                self.entries.move_to_end(run_id)
                return cached[1]
            self.entries.pop(run_id, None)
            result = read_result(self.directory, run_id, self.rules)
            try:
                after = path.stat()
            except OSError as exc:
                raise CorruptRun from exc
            if fingerprint != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise CorruptRun
            # Avoid retaining an unusually large upload in addition to its active request.
            if stat.st_size <= 16 * 1024 * 1024:
                self.entries[run_id] = (fingerprint, result)
                while len(self.entries) > self.max_entries:
                    self.entries.popitem(last=False)
            return result


def run_path(directory: Path, run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise MissingRun
    return directory / run_id


def read_result(
    directory: Path, run_id: str, expected_rules: dict[str, Any] | None = None
) -> dict[str, Any]:
    try:
        result = json.loads(
            (run_path(directory, run_id) / "result.json").read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise MissingRun from exc
    except (OSError, ValueError) as exc:
        raise CorruptRun from exc
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("report"), dict)
        or not isinstance(result["report"].get("rules"), dict)
        or any(
            not isinstance(result.get(key), list)
            for key in ("nodes", "edges", "clusters", "cluster_edges", "top")
        )
    ):
        raise CorruptRun
    if expected_rules is not None and (
        result["report"].get("rules") != expected_rules
        or result["report"].get("rules_version") != expected_rules["version"]
    ):
        raise ObsoleteRun
    try:
        validate_result(result)
    except (DataError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise CorruptRun from exc
    return result


def read_bootstrap(directory: Path) -> dict[str, Any] | None:
    try:
        initial = json.loads((directory / "bootstrap.json").read_text(encoding="utf-8"))
        if (
            not isinstance(initial, dict)
            or not isinstance(initial.get("run_id"), str)
            or type(initial.get("synthetic")) is not bool
        ):
            return None
        run_path(directory, initial["run_id"])
        return initial
    except (OSError, ValueError, MissingRun):
        return None


def write_bootstrap(directory: Path, initial: dict[str, Any]) -> None:
    # A restart sees either the old complete pointer or the new complete pointer.
    descriptor, name = tempfile.mkstemp(prefix=".bootstrap-", dir=directory)
    pending = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(initial, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, directory / "bootstrap.json")
    finally:
        pending.unlink(missing_ok=True)


def prune(directory: Path, bootstrap_id: str, published_id: str) -> None:
    """Never run on failed upload; keep the bootstrap and at most 20 uploaded runs."""
    now = time.time()
    try:
        saved = sorted(
            (
                p
                for p in directory.iterdir()
                if p.is_dir() and RUN_ID.fullmatch(p.name) and p.name != bootstrap_id
            ),
            key=lambda p: (p.stat().st_mtime, p.name),
        )
        for index, path in enumerate(saved):
            if path.name != published_id and (
                now - path.stat().st_mtime > 86400 or index < len(saved) - 20
            ):
                shutil.rmtree(path)
    except OSError:
        # The newly published calculation is still usable if maintenance fails.
        LOGGER.exception("Could not prune old run directories")
