"""
День 31. Проверки ТЗ без интернета и без ключа AITunnel.

  1) MCP: протокол и инструменты — через настоящий процесс mcp_server.py по stdio
  2) RAG: что попадает в индекс и находит ли нужные разделы документации
  3) /help: ассистент с ИМИТАЦИЕЙ AITunnel (стаб на localhost, проверяет ключ и модель)
  4) Сквозной запуск: python assistant.py --once "/help ..."

    python test_day31.py

Отчёт: output/test_report.md. Код выхода 1, если хоть одна проверка не прошла.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from assistant import AITunnelLLM, ProjectAssistant  # noqa: E402
from index_store import Index  # noqa: E402
from mcp_client import MCPClient, MCPToolError  # noqa: E402
from project_docs import build_index, collect_docs, default_index_path  # noqa: E402

STUB_KEY = "stub-only-key-0001"
RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("ok    " if ok else "FAIL  ") + name + ("" if ok or not detail else f"  — {detail}"))


# ---------------------------------------------------------------------------
# Сырой протокол: пишем JSON-RPC прямо в процесс сервера
# ---------------------------------------------------------------------------

class RawServer:
    def __init__(self, project: str):
        env = {**os.environ, "PYTHONUTF8": "1"}
        self.p = subprocess.Popen([sys.executable, os.path.join(HERE, "mcp_server.py"), "--project", project],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)

    def send_raw(self, data: bytes) -> None:
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def send(self, msg: dict) -> None:
        self.send_raw((json.dumps(msg) + "\n").encode("utf-8"))

    def recv(self) -> dict:
        return json.loads(self.p.stdout.readline().decode("utf-8"))

    def close(self) -> None:
        self.p.stdin.close()
        self.p.wait(timeout=5)


def test_protocol() -> None:
    s = RawServer(HERE)
    try:
        s.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t"}}})
        r = s.recv()
        check("MCP initialize отвечает версией протокола и именем сервера",
              r.get("result", {}).get("protocolVersion") == "2024-11-05"
              and r.get("result", {}).get("serverInfo", {}).get("name") == "project-git-files", str(r)[:200])

        s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})  # уведомление: ответа быть не должно
        s.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        r = s.recv()
        check("уведомление без id не получает ответа (следующий ответ — на ping)", r.get("id") == 2, str(r)[:200])

        s.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = {t["name"] for t in s.recv().get("result", {}).get("tools", [])}
        check("tools/list содержит git_branch, list_files, git_diff",
              {"git_branch", "list_files", "git_diff"} <= names, str(names))

        s.send_raw("это не json\n".encode("utf-8"))
        r = s.recv()
        check("битый JSON → ошибка -32700, сервер жив", r.get("error", {}).get("code") == -32700, str(r)[:200])

        s.send({"jsonrpc": "2.0", "id": 5, "method": "foo/bar"})
        r = s.recv()
        check("неизвестный метод → -32601", r.get("error", {}).get("code") == -32601, str(r)[:200])

        s.send({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "rm_rf", "arguments": {}}})
        r = s.recv()
        check("неизвестный инструмент → -32602", r.get("error", {}).get("code") == -32602, str(r)[:200])

        s.send({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "git_branch", "arguments": {}}})
        r = s.recv()
        check("git_branch возвращает content и isError", "content" in r.get("result", {}) and "isError" in r.get("result", {}),
              str(r)[:200])
    finally:
        s.close()


# ---------------------------------------------------------------------------
# MCP-инструменты через клиент: git-репозиторий и папка без git
# ---------------------------------------------------------------------------

def git(*args: str, cwd: str) -> None:
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


def test_git_tools(tmp: str) -> None:
    plain = os.path.join(tmp, "plain")
    os.makedirs(os.path.join(plain, "node_modules", "lib"))
    with open(os.path.join(plain, "a.txt"), "w", encoding="utf-8") as f:
        f.write("привет")
    with open(os.path.join(plain, "node_modules", "lib", "x.js"), "w", encoding="utf-8") as f:
        f.write("x")
    with MCPClient(plain) as m:
        try:
            m.call("git_branch")
            check("папка без git: git_branch сообщает об этом (isError)", False, "не было ошибки")
        except MCPToolError as e:
            check("папка без git: git_branch сообщает об этом (isError)",
                  "не является git-репозиторием" in str(e), str(e))
        files = m.call("list_files")
        check("папка без git: list_files обходит папку и пропускает node_modules",
              "a.txt" in files and "node_modules" not in files, files[:200])
        try:
            m.call("list_files", {"subdir": "../"})
            check("защита: list_files не выходит за папку проекта", False, "ошибки не было")
        except MCPToolError as e:
            check("защита: list_files не выходит за папку проекта", "за пределы" in str(e), str(e))

    if not shutil.which("git"):
        check("git-репозиторий: ветка, файлы, diff", True, "пропущено: git не найден в этой среде")
        return
    repo = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(repo, "docs"))
    subprocess.run(["git", "init", "-b", "feature/mcp-test", repo], check=True, capture_output=True)
    with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as f:
        f.write("# Тест\n")
    with open(os.path.join(repo, "docs", "a.md"), "w", encoding="utf-8") as f:
        f.write("# Док\n")
    git("add", ".", cwd=repo)
    git("commit", "-m", "init", cwd=repo)
    with MCPClient(repo) as m:
        branch = m.call("git_branch")
        check("git-репозиторий: git_branch возвращает ветку", branch == "feature/mcp-test", branch)
        files = m.call("list_files")
        check("git-репозиторий: list_files через git ls-files",
              "git ls-files" in files and "docs/a.md" in files, files[:200])
        sub = m.call("list_files", {"subdir": "docs"})
        check("list_files с подпапкой показывает только её файлы",
              "docs/a.md" in sub and "README.md" not in sub, sub[:200])
        check("git_diff пусто, если изменений нет", "нет" in m.call("git_diff"), "")
        with open(os.path.join(repo, "README.md"), "a", encoding="utf-8") as f:
            f.write("новая строка\n")
        diff = m.call("git_diff", {"stat_only": True})
        check("git_diff показывает изменённый файл", "README.md" in diff, diff[:200])
        git("checkout", "--detach", cwd=repo)
        det = m.call("git_branch")
        check("отсоединённый HEAD: git_branch возвращает хеш коммита", det.startswith("HEAD отсоединён"), det)


# ---------------------------------------------------------------------------
# RAG по документации проекта
# ---------------------------------------------------------------------------

def test_rag(tmp: str) -> str:
    docs = collect_docs(HERE)
    srcs = {d.source for d in docs}
    check("индекс: README.md попадает в документы", "README.md" in srcs, str(sorted(srcs)))
    check("индекс: папка docs/ попадает целиком",
          {"docs/architecture.md", "docs/mcp.md", "docs/rag.md", "docs/commands.md"} <= srcs, str(sorted(srcs)))
    check("индекс: схемы и API (schema.sql, mcp_tools.json) попадают",
          {"docs/schema.sql", "docs/mcp_tools.json"} <= srcs, str(sorted(srcs)))
    check("индекс: код .py и папка output/ не попадают",
          not any(s.endswith(".py") or s.startswith("output/") for s in srcs), str(sorted(srcs)))

    idx_path = os.path.join(tmp, "docs-index.sqlite")
    info = build_index(HERE, idx_path, "local")
    check("индекс собирается и содержит фрагменты", info["chunks"] >= 10, str(info["chunks"]))

    idx = Index(idx_path)
    cases = [
        ("как запустить ассистента", {"README.md", "docs/commands.md"}),
        ("архитектура проекта компоненты", {"docs/architecture.md"}),
        ("схема таблицы индекса chunks", {"docs/schema.sql", "docs/rag.md"}),
        ("какие инструменты MCP есть", {"docs/mcp.md", "docs/mcp_tools.json"}),
        ("как работает команда /help", {"docs/commands.md", "README.md", "docs/architecture.md"}),
        ("что индексируется из схем API", {"docs/rag.md"}),
    ]
    hits = 0
    for q, want in cases:
        top = [r["source"] for r in idx.search(q, k=5)]
        ok = bool(set(top) & want)
        hits += ok
        check(f"поиск «{q}» находит нужный документ в топ-5", ok, f"топ-5: {top}")
    check("поиск по документации: не меньше 5 из 6 вопросов", hits >= 5, f"{hits} из {len(cases)}")

    empty = os.path.join(tmp, "empty")
    os.makedirs(empty)
    try:
        build_index(empty, os.path.join(tmp, "e.sqlite"), "local")
        check("пустой проект: понятная ошибка, а не падение", False, "ошибки не было")
    except RuntimeError as e:
        check("пустой проект: понятная ошибка, а не падение", "нет документации" in str(e), str(e))
    return idx_path


# ---------------------------------------------------------------------------
# Имитация AITunnel: проверяет ключ и модель, отвечает по контексту
# ---------------------------------------------------------------------------

class StubHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def _send(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        StubHandler.requests.append({"path": self.path, "auth": self.headers.get("Authorization", ""), "body": body})
        if self.path != "/v1/chat/completions":
            return self._send(404, {"error": {"message": "not found"}})
        if self.headers.get("Authorization", "") != f"Bearer {STUB_KEY}":
            return self._send(401, {"error": {"message": "invalid api key"}})
        user = body["messages"][-1]["content"]
        branch = re.search(r"Git-ветка: (.+)", user)
        parts = ["[имитация AITunnel]"]
        parts.append("Ветка: " + branch.group(1).strip() + "." if branch else "Ветки в контексте нет.")
        parts.append("В контексте есть mcp_server.py." if "mcp_server.py" in user else "Списка файлов в контексте нет.")
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": " ".join(parts)},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


def start_stub() -> tuple:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


def test_assistant(idx_path: str, base: str) -> None:
    StubHandler.requests.clear()
    with MCPClient(HERE) as mcp:
        index = Index(idx_path)

        try:
            AITunnelLLM(api_key="", base_url=base).chat("s", "u")
            check("без ключа: понятная ошибка, запрос не уходит", False, "ошибки не было")
        except RuntimeError as e:
            check("без ключа: понятная ошибка, запрос не уходит",
                  "Не задан ключ" in str(e) and not StubHandler.requests, str(e))

        try:
            AITunnelLLM(api_key="wrong-key", base_url=base).chat("s", "u")
            check("неверный ключ: сообщение про 401", False, "ошибки не было")
        except RuntimeError as e:
            check("неверный ключ: сообщение про 401", "отклонил ключ" in str(e), str(e))

        llm = AITunnelLLM(api_key=STUB_KEY, base_url=base)
        asst = ProjectAssistant(HERE, index, llm, mcp)

        res = asst.answer("какая структура проекта", with_structure=True)
        req = StubHandler.requests[-1]
        user = req["body"]["messages"][-1]["content"]
        check("/help: запрос уходит с заголовком Bearer и моделью gpt-5.6-luna",
              req["auth"] == f"Bearer {STUB_KEY}" and req["body"]["model"] == "gpt-5.6-luna",
              f"auth={req['auth'][:12]}… model={req['body'].get('model')}")
        check("/help: в контекст попадает список файлов через MCP",
              "=== Файлы проекта" in user and "mcp_server.py" in user, "")
        check("/help: в контекст попадает git-ветка через MCP",
              "Git-ветка:" in user, "")
        check("/help: ответ модели возвращается и содержит данные из контекста",
              "mcp_server.py" in res["answer"], res["answer"][:200])
        check("/help: список источников не пустой и содержит MCP", "MCP: git_branch, list_files" in res["sources"],
              str(res["sources"]))

        asst.answer("как запустить ассистента", with_structure=False)
        plain = StubHandler.requests[-1]["body"]["messages"][-1]["content"]
        check("обычный вопрос без /help: списка файлов в контексте нет",
              "=== Файлы проекта" not in plain, "")

        leaked = any(STUB_KEY in json.dumps(r["body"], ensure_ascii=False) for r in StubHandler.requests)
        check("ключ не попадает в тело запросов (только в заголовок авторизации)", not leaked, "")


def test_cli(idx_path: str, base: str) -> None:
    env = {**os.environ, "PYTHONUTF8": "1", "AITUNNEL_BASE_URL": base, "AITUNNEL_API_KEY": STUB_KEY}
    p = subprocess.run([sys.executable, os.path.join(HERE, "assistant.py"), "--project", HERE,
                        "--index", idx_path, "--once", "/help какая структура проекта"],
                       capture_output=True, env=env, timeout=120)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    check("сквозной запуск --once /help: код выхода 0", p.returncode == 0, out[-300:])
    check("сквозной запуск: ответ имитации и блок источников",
          "[имитация AITunnel]" in out and "Источники:" in out, out[-300:])
    check("сквозной запуск: ключ не виден в выводе", STUB_KEY not in out, "")


def write_report(started: str) -> None:
    os.makedirs(os.path.join(HERE, "output"), exist_ok=True)
    ok = sum(1 for _, good, _ in RESULTS if good)
    lines = [
        "# Отчёт проверок дня 31 (имитация)",
        "",
        f"Запуск: {started}. Результат: **{ok} из {len(RESULTS)}** проверок пройдено.",
        "",
        "Что проверено на имитации: протокол MCP (через настоящий процесс mcp_server.py), инструменты в папке с git и без,",
        "защита путей, индекс документации (лексические эмбеддинги), /help с имитацией AITunnel, сквозной запуск.",
        "",
        "Что НЕ проверено здесь: настоящий AITunnel и ключ, модель gpt-5.6-luna на сервере, Ollama, эмбеддинги bge-m3,",
        "официальный MCP SDK, работа на ПК пользователя с его репозиторием.",
        "",
        "| № | Проверка | Результат |",
        "|---|---|---|",
    ]
    for i, (name, good, _) in enumerate(RESULTS, 1):
        lines.append(f"| {i} | {name} | {'пройдено' if good else '**НЕ пройдено**'} |")
    with open(os.path.join(HERE, "output", "test_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    started = datetime.now().strftime("%Y-%m-%d %H:%M")
    tmp = tempfile.mkdtemp(prefix="day31-test-")
    srv, base = start_stub()
    try:
        print("== 1. MCP: протокол")
        test_protocol()
        print("\n== 2. MCP: инструменты (папка без git и git-репозиторий)")
        test_git_tools(tmp)
        print("\n== 3. RAG: документация проекта")
        idx_path = test_rag(tmp)
        print("\n== 4. /help с имитацией AITunnel")
        test_assistant(idx_path, base)
        print("\n== 5. Сквозной запуск assistant.py")
        test_cli(idx_path, base)
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    write_report(started)
    failed = [n for n, good, _ in RESULTS if not good]
    print(f"\nИтог: {len(RESULTS) - len(failed)} из {len(RESULTS)}. Отчёт: output\\test_report.md")
    if failed:
        print("Не пройдено:\n  " + "\n  ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
