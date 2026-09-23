"""Small subprocess helpers shared by the portable development entry points."""

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK_PATHS = ["src", "tests", "analyze.py", "scripts"]


class RunnerError(Exception):
    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def executable(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        hint = (
            "Установите uv: https://docs.astral.sh/uv/getting-started/installation/"
            if name == "uv"
            else "Для веб-интерфейса установите Node.js 22.12+ вместе с npm."
        )
        raise RunnerError(f"Не найдена команда {name}. {hint}")
    return found


def run(arguments: list[str]) -> None:
    command = [executable(arguments[0]), *arguments[1:]]
    print("+ " + " ".join(arguments), flush=True)
    try:
        subprocess.run(command, cwd=ROOT, env=os.environ | {"PYTHONUTF8": "1"}, check=True)
    except subprocess.CalledProcessError as exc:
        raise RunnerError(
            f"Команда {arguments[0]} завершилась с кодом {exc.returncode}; см. сообщение выше.",
            exc.returncode if exc.returncode > 0 else 1,
        ) from exc
    except OSError as exc:
        raise RunnerError(f"Не удалось запустить {arguments[0]}: {exc}") from exc


def require_node() -> None:
    node = executable("node")
    executable("npm")
    try:
        version = subprocess.run(
            [node, "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
        major, minor, *_ = (int(part) for part in version.removeprefix("v").split("."))
        if (major, minor) < (22, 12):
            raise ValueError
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise RunnerError(
            "Нужен Node.js 22.12+; проверьте node --version и обновите Node.js."
        ) from exc
