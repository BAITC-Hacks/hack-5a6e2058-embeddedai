"""Portable files-to-CSV entry point; the installed dependencies come from uv.lock."""

import sys
from pathlib import Path


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if sys.version_info[:2] != (3, 12):
        print(
            "Требуется Python 3.12. uv установит нужную версию автоматически:\n"
            "uv run --frozen --no-dev --python 3.12 python analyze.py --data data/data --out output",
            file=sys.stderr,
        )
        raise SystemExit(2)
    # Also support a pinned pip environment without installing an editable package.
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    try:
        from money_graph.cli import main as cli_main
    except ModuleNotFoundError as exc:
        print(
            f"Не установлена зависимость {exc.name}. Запустите из корня репозитория:\n"
            "uv run --frozen --no-dev python analyze.py --data data/data --out output\n"
            "Или установите зависимости: python -m pip install --require-hashes -r requirements.txt",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    cli_main(["analyze", *sys.argv[1:]])


if __name__ == "__main__":
    main()
