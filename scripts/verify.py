"""Cross-platform quality gate, with a Node-free backend-only mode for the jury."""

import argparse
import sys

from _runner import CHECK_PATHS, RunnerError, require_node, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка воспроизводимости Графа денег")
    parser.add_argument(
        "--backend-only", action="store_true", help="Python/CLI/API без Node.js и браузера"
    )
    args = parser.parse_args(argv)
    try:
        if not args.backend_only:
            require_node()
        run(["uv", "sync", "--frozen", "--python", "3.12"])
        run(["uv", "run", "--frozen", "ruff", "check", *CHECK_PATHS])
        run(["uv", "run", "--frozen", "ruff", "format", "--check", *CHECK_PATHS])
        run(["uv", "run", "--frozen", "mypy", "src"])
        run(["uv", "run", "--frozen", "pytest", "-q"])
        if not args.backend_only:
            run(["npm", "--prefix", "web", "ci", "--no-audit", "--no-fund"])
            run(["npm", "--prefix", "web", "run", "check"])
            run(["npm", "--prefix", "web", "run", "build"])
            run(["uv", "run", "--frozen", "python", "scripts/smoke.py"])
            run(["npm", "--prefix", "web", "exec", "--", "playwright", "install", "chromium"])
            run(["npm", "--prefix", "web", "run", "test:e2e"])
        return 0
    except RunnerError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
