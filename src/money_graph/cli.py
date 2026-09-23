import argparse
import json
import os
from pathlib import Path

from .demo import create_demo
from .loader import DataError
from .pipeline import analyze, write_result


def main() -> None:
    parser = argparse.ArgumentParser(description="Граф денег — воспроизводимый анализ")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze_cmd = commands.add_parser("analyze")
    analyze_cmd.add_argument("--data", type=Path, required=True)
    analyze_cmd.add_argument("--out", type=Path, default=Path("artifacts"))
    analyze_cmd.add_argument("--rules", type=Path)
    demo_cmd = commands.add_parser("demo")
    demo_cmd.add_argument("--out", type=Path, default=Path("var/demo-data"))
    serve_cmd = commands.add_parser("serve")
    serve_cmd.add_argument("--data", type=Path)
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=int(os.getenv("PORT", "3000")))
    args = parser.parse_args()
    try:
        if args.command == "demo":
            create_demo(args.out)
            print(f"Синтетический набор: {args.out}")
        elif args.command == "analyze":
            if (
                args.out.resolve() == args.data.resolve()
                or args.out.resolve() in args.data.resolve().parents
            ):
                raise DataError(
                    "Папка результата не должна заменять исходные данные или их родительский каталог"
                )
            result = analyze(args.data, args.rules)
            write_result(result, args.out)
            print(json.dumps(result["report"], ensure_ascii=False, indent=2))
        else:
            import uvicorn

            from .api import create_app

            uvicorn.run(create_app(initial_data=args.data), host=args.host, port=args.port)
    except DataError as exc:
        parser.exit(2, f"Ошибка данных: {exc}\n")


if __name__ == "__main__":
    main()
