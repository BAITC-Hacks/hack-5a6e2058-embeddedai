"""Support python -m money_graph with the same commands as money-graph."""

import sys

try:
    from .cli import main
except ModuleNotFoundError as exc:
    print(
        f"Не установлена зависимость {exc.name}. Из checkout запустите:\n"
        "uv run --frozen --no-dev python -m money_graph --help",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

if __name__ == "__main__":
    main()
