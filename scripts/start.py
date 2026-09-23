"""Install locked dependencies, build the frontend and run the production app."""

import sys

from _runner import RunnerError, require_node, run


def main() -> int:
    try:
        require_node()
        run(["uv", "sync", "--frozen", "--no-dev", "--python", "3.12"])
        run(["npm", "--prefix", "web", "ci", "--no-audit", "--no-fund"])
        run(["npm", "--prefix", "web", "run", "build"])
        run(["uv", "run", "--frozen", "--no-dev", "money-graph", "serve", *sys.argv[1:]])
        return 0
    except RunnerError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
