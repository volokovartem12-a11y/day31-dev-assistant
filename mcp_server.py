"""
День 31. MCP-сервер проекта (Model Context Protocol) на стандартной библиотеке Python.

Транспорт stdio: одно сообщение JSON-RPC 2.0 на строку в stdin, ответ — строка в stdout.
Инструменты только читают данные и не выходят за папку проекта:
  git_branch — текущая git-ветка (при отсоединённом HEAD — короткий хеш коммита);
  list_files — файлы проекта: git ls-files, а если git недоступен — обход папки;
  git_diff   — изменения относительно последнего коммита (статистика или полный diff).

Запуск вручную (для проверки):   python mcp_server.py --project "путь\\к\\проекту"
Обычно его запускает assistant.py через mcp_client.py.
"""

import argparse
import json
import os
import subprocess
import sys

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "project-git-files", "version": "1.0.0"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "output", "logs"}
MAX_FILES = 400
MAX_DIFF_CHARS = 8000


class ToolError(Exception):
    """Понятная пользователю ошибка инструмента: уходит в ответ с isError: true."""


class Project:
    def __init__(self, root: str):
        self.root = os.path.realpath(root)
        if not os.path.isdir(self.root):
            raise ValueError(f"папка проекта не найдена: {root}")

    def git(self, *args: str) -> str:
        try:
            p = subprocess.run(["git", "-C", self.root, *args], capture_output=True, timeout=15)
        except FileNotFoundError:
            raise ToolError("git не установлен или не найден в PATH")
        except subprocess.TimeoutExpired:
            raise ToolError("git не ответил за 15 секунд")
        if p.returncode != 0:
            err = p.stderr.decode("utf-8", "replace").strip()
            if "not a git repository" in err.lower():
                raise ToolError("папка проекта не является git-репозиторием")
            raise ToolError(f"git {args[0]}: {err[:300]}")
        return p.stdout.decode("utf-8", "replace")

    def inside(self, rel: str) -> str:
        """Абсолютный путь внутри папки проекта. Выход за её пределы запрещён."""
        if os.path.isabs(rel):
            raise ToolError("путь должен быть относительным, внутри проекта")
        target = os.path.realpath(os.path.join(self.root, rel))
        try:
            inside = os.path.commonpath([self.root, target]) == self.root
        except ValueError:  # другой диск в Windows
            inside = False
        if not inside:
            raise ToolError("путь выходит за пределы папки проекта")
        return target


# ---------------------------------------------------------------------------
# Инструменты
# ---------------------------------------------------------------------------

def git_branch(project: Project, args: dict) -> str:
    name = project.git("rev-parse", "--abbrev-ref", "HEAD").strip()
    if name == "HEAD":
        sha = project.git("rev-parse", "--short", "HEAD").strip()
        return f"HEAD отсоединён, коммит {sha}"
    return name


def _walk(project: Project, start: str) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            out.append(os.path.relpath(os.path.join(dirpath, name), project.root).replace("\\", "/"))
    return out


def list_files(project: Project, args: dict) -> str:
    sub = str(args.get("subdir") or "").strip()
    start = project.inside(sub) if sub else project.root
    if not os.path.isdir(start):
        raise ToolError(f"в проекте нет папки: {sub}")
    prefix = os.path.relpath(start, project.root).replace("\\", "/")
    try:
        pathspec = [] if prefix == "." else [prefix]
        files = project.git("ls-files", "--cached", "--others", "--exclude-standard", "--", *pathspec).splitlines()
        how = "git ls-files"
    except ToolError as e:  # нет git или не репозиторий — обходим папку сами
        files = _walk(project, start)
        how = f"обход папки, потому что: {e}"
    files = [f.replace("\\", "/") for f in files if f.strip()]
    files = [f for f in files if not any(part in SKIP_DIRS for part in f.split("/"))]
    total = len(files)
    head = f"Всего файлов: {total} (источник: {how})"
    if total > MAX_FILES:
        head += f"; показаны первые {MAX_FILES}"
    return "\n".join([head, *files[:MAX_FILES]])


def git_diff(project: Project, args: dict) -> str:
    stat_only = args.get("stat_only", True)
    if not isinstance(stat_only, bool):
        raise ToolError("stat_only должен быть true или false")
    out = project.git("diff", "HEAD", "--stat") if stat_only else project.git("diff", "HEAD")
    if not out.strip():
        return "Изменений относительно последнего коммита нет. (Новые файлы, которых нет в git, не показываются.)"
    if len(out) > MAX_DIFF_CHARS:
        out = out[:MAX_DIFF_CHARS] + f"\n… обрезано: лимит {MAX_DIFF_CHARS} символов"
    return out


TOOLS = {
    "git_branch": {
        "description": "Текущая git-ветка проекта.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "fn": git_branch,
    },
    "list_files": {
        "description": "Список файлов проекта без служебных папок (.git, node_modules, output). Можно указать подпапку.",
        "inputSchema": {
            "type": "object",
            "properties": {"subdir": {"type": "string", "description": "Подпапка внутри проекта, например docs"}},
            "additionalProperties": False,
        },
        "fn": list_files,
    },
    "git_diff": {
        "description": "Изменения относительно последнего коммита: статистика или полный diff. Новые файлы не видны.",
        "inputSchema": {
            "type": "object",
            "properties": {"stat_only": {"type": "boolean", "description": "true — только статистика (по умолчанию)"}},
            "additionalProperties": False,
        },
        "fn": git_diff,
    },
}


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------

def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg, project: Project):
    if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    if "id" not in msg:
        return None  # уведомление, например notifications/initialized: ответа не будет
    mid, method = msg["id"], msg["method"]
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    if method == "initialize":
        return _result(mid, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                             "serverInfo": SERVER_INFO})
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": [{"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}
                                       for n, t in TOOLS.items()]})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOLS:
            return _error(mid, -32602, f"неизвестный инструмент: {name}")
        if not isinstance(args, dict):
            return _error(mid, -32602, "arguments должен быть объектом")
        try:
            text, is_error = TOOLS[name]["fn"](project, args), False
        except ToolError as e:
            text, is_error = str(e), True
        except Exception as e:  # noqa: BLE001 — сервер не должен падать из-за одного запроса
            text, is_error = f"внутренняя ошибка инструмента: {e}", True
        return _result(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})
    return _error(mid, -32601, f"метод не поддерживается: {method}")


def serve(project: Project, lines, out) -> None:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = _error(None, -32700, "Parse error")
        else:
            resp = handle(msg, project)
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False) + "\n")
            out.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description="MCP-сервер проекта: git-ветка, файлы, diff (только чтение)")
    ap.add_argument("--project", required=True, help="папка проекта")
    a = ap.parse_args()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        project = Project(a.project)
    except ValueError as e:
        print(f"mcp_server: {e}", file=sys.stderr)
        return 2
    serve(project, sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
