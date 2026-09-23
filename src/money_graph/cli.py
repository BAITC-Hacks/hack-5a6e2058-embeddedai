import argparse
import json
import os
from pathlib import Path

from .demo import create_demo
from .loader import DataError
from .pipeline import EXPORTS, analyze, write_result


def output_directory(path: Path, sources: tuple[Path, ...] = (), *, demo: bool = False) -> None:
    """Only replace our own exports; never erase a source or an unrelated directory."""
    if path.is_symlink():
        raise DataError("Папка результата не должна быть символической ссылкой")
    resolved = path.resolve()
    protected = (Path.cwd().resolve(), *(source.resolve() for source in sources))
    if any(resolved == source or resolved in source.parents for source in protected):
        raise DataError(
            "Папка результата не должна заменять исходные данные, рабочий каталог или их родителей"
        )
    if resolved.exists():
        if not resolved.is_dir():
            raise DataError("Путь результата уже занят файлом; укажите отдельную папку")
        entries = list(resolved.iterdir())
        allowed = set() if demo else {*EXPORTS, "run_report.json", "result.json"}
        if any(entry.name not in allowed or not entry.is_file() for entry in entries):
            raise DataError(
                "Папка результата содержит посторонние файлы. Укажите новую или пустую папку"
            )


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("PORT/--port должен быть целым числом") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("PORT/--port должен быть от 1 до 65535")
    return port


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
    serve_cmd.add_argument("--port", type=port_number, default=os.getenv("PORT", "3000"))
    args = parser.parse_args()
    try:
        if args.command == "demo":
            output_directory(args.out, demo=True)
            create_demo(args.out)
            print(f"Синтетический набор: {args.out}")
        elif args.command == "analyze":
            sources = (args.data, args.rules) if args.rules is not None else (args.data,)
            output_directory(args.out, sources)
            result = analyze(args.data, args.rules)
            write_result(result, args.out)
            print(json.dumps(result["report"], ensure_ascii=False, indent=2))
        else:
            import uvicorn

            from .api import create_app

            uvicorn.run(create_app(initial_data=args.data), host=args.host, port=args.port)
    except DataError as exc:
        parser.exit(2, f"Ошибка данных: {exc}\n")
    except OSError as exc:
        parser.exit(2, f"Ошибка файлов или запуска: {exc}\n")


if __name__ == "__main__":
    main()
