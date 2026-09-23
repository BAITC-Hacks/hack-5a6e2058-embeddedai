import argparse
import json
import os
import shutil
import sys
import tempfile
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


def analyze_inputs(args: argparse.Namespace) -> dict:
    """Normalize explicit files without changing or duplicating the calculation path."""
    sources = (args.data,) if args.data is not None else (args.nodes, args.edges, args.transactions)
    if args.rules is not None:
        sources += (args.rules,)
    output_directory(args.out, sources)
    if args.data is not None:
        return analyze(args.data, args.rules)
    paths = {"nodes": args.nodes, "edges": args.edges, "transactions": args.transactions}
    if len({path.resolve() for path in paths.values()}) != 3:
        raise DataError("Укажите три разных входных Parquet-файла")
    for name, path in paths.items():
        if not path.is_file():
            raise DataError(f"Файл --{name} не найден: {path}")
    # Copies work without symlink privileges on Windows and retain the exact input bytes/hashes.
    with tempfile.TemporaryDirectory(prefix="money-graph-input-") as temporary:
        source = Path(temporary)
        for name, path in paths.items():
            shutil.copyfile(path, source / f"{name}.parquet")
        return analyze(source, args.rules)


def main(argv: list[str] | None = None, *, analyze_only: bool = False) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Граф денег — воспроизводимый анализ")
    if analyze_only:
        parser.set_defaults(command="analyze")
        analyze_cmd = parser
    else:
        commands = parser.add_subparsers(dest="command", required=True)
        analyze_cmd = commands.add_parser("analyze")
    source = analyze_cmd.add_mutually_exclusive_group(required=True)
    source.add_argument("--data", type=Path, help="Папка nodes/edges/transactions.parquet")
    source.add_argument(
        "--nodes", type=Path, help="Путь к файлу узлов; вместе с --edges и --transactions"
    )
    analyze_cmd.add_argument("--edges", type=Path, help="Путь к файлу рёбер")
    analyze_cmd.add_argument("--transactions", type=Path, help="Путь к файлу транзакций")
    analyze_cmd.add_argument("--out", type=Path, default=Path("artifacts"))
    analyze_cmd.add_argument("--rules", type=Path)
    if not analyze_only:
        demo_cmd = commands.add_parser("demo")
        demo_cmd.add_argument("--out", type=Path, default=Path("var/demo-data"))
        serve_cmd = commands.add_parser("serve")
        serve_cmd.add_argument("--data", type=Path)
        serve_cmd.add_argument("--host", default="127.0.0.1")
        serve_cmd.add_argument("--port", type=port_number, default=os.getenv("PORT", "3000"))
    args = parser.parse_args(argv)
    if args.command == "analyze":
        if args.data is not None and (args.edges is not None or args.transactions is not None):
            parser.error("--data нельзя совмещать с --nodes, --edges или --transactions")
        if args.nodes is not None and (args.edges is None or args.transactions is None):
            parser.error("При --nodes также обязательны --edges и --transactions")
    try:
        if args.command == "demo":
            output_directory(args.out, demo=True)
            create_demo(args.out)
            print(f"Синтетический набор: {args.out}")
        elif args.command == "analyze":
            result = analyze_inputs(args)
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
