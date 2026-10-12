"""
День 31. Ассистент разработчика: RAG по документации проекта + контекст проекта через MCP + команда /help.

    python assistant.py                                  # чат о папке, где лежит сам ассистент
    python assistant.py --project "путь\\к\\проекту"      # чат о другом проекте
    python assistant.py --project ... --once "/help какая структура проекта"   # один вопрос и выход
    python assistant.py --project ... --build            # только собрать индекс документации

В чате:
    /help <вопрос>  — ответ о проекте: документация + git-ветка + список файлов (через MCP)
    /branch         — текущая git-ветка (через MCP)
    /files [папка]  — список файлов проекта (через MCP)
    /reindex        — пересобрать индекс документации
    /exit           — выход
Обычный вопрос без /help ищет ответ только по документации.

LLM по умолчанию — AITunnel, модель gpt-5.6-luna. Ключ AITUNNEL_API_KEY берётся из окружения,
а если его нет — спрашивается скрытым вводом и хранится только в памяти процесса.
Локальный вариант: --llm ollama (нужна запущенная Ollama).
"""

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request

from index_store import Index
from mcp_client import MCPClient, MCPToolError
from project_docs import build_index, default_index_path

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_AITUNNEL_MODEL = "gpt-5.6-luna"
DEFAULT_AITUNNEL_URL = "https://api.aitunnel.ru/v1"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"
FILES_IN_CONTEXT = 150
CHUNKS_IN_CONTEXT = 5
CHUNK_CHARS = 1200

SYSTEM_PROMPT = (
    "Ты — ассистент разработчика. Отвечай на вопросы о проекте только по блокам контекста ниже: "
    "данные проекта (папка, git-ветка, список файлов) и фрагменты документации. "
    "Если ответа в этих блоках нет, так и напиши: в документации и файлах этого не нашёл. "
    "Не выдумывай файлы, функции, команды и настройки, которых нет в контексте. "
    "Отвечай по-русски, коротко и по делу. В конце перечисли источники в квадратных скобках: [путь] или [путь: раздел]."
)

HELP_TEXT = """Команды:
  /help <вопрос>  — ответ о проекте: документация, git-ветка и структура файлов (через MCP)
  /branch         — текущая git-ветка (через MCP)
  /files [папка]  — список файлов проекта (через MCP)
  /reindex        — пересобрать индекс документации
  /exit           — выход
Обычный вопрос без /help ищет ответ только по документации."""


class AITunnelLLM:
    """OpenAI-совместимый чат AITunnel. Только стандартная библиотека."""

    name = "aitunnel"

    def __init__(self, model: str = DEFAULT_AITUNNEL_MODEL, api_key: str = None, base_url: str = None,
                 timeout: float = 120):
        self.model = model
        self.api_key = api_key if api_key is not None else os.environ.get("AITUNNEL_API_KEY", "")
        self.base_url = (base_url or os.environ.get("AITUNNEL_BASE_URL") or DEFAULT_AITUNNEL_URL).rstrip("/")
        self.timeout = timeout

    @property
    def label(self) -> str:
        return f"aitunnel:{self.model}"

    def chat(self, system: str, user: str, history: list = None) -> tuple:
        if not self.api_key:
            raise RuntimeError("Не задан ключ AITunnel. Запустите ассистента в обычном окне: он спросит ключ скрытым вводом.")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}] + (history or [])
        body = json.dumps({"model": self.model, "messages": messages}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base_url + "/chat/completions", data=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError("AITunnel отклонил ключ (401). Проверьте ключ и запустите снова.") from e
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"AITunnel: HTTP {e.code} {detail}") from e
        except OSError as e:  # URLError — подкласс OSError
            raise RuntimeError(f"AITunnel недоступен: {e}") from e
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError("AITunnel вернул ответ неожиданного вида") from e
        usage = data.get("usage") or {}
        return text.strip(), {"prompt_tokens": usage.get("prompt_tokens", 0),
                              "completion_tokens": usage.get("completion_tokens", 0)}


def make_llm(kind: str, model: str = None):
    if kind == "ollama":
        from local_llm import OllamaLLM
        return OllamaLLM(model or DEFAULT_OLLAMA_MODEL, temperature=0.2, num_ctx=8192, num_predict=900)
    llm = AITunnelLLM(model or DEFAULT_AITUNNEL_MODEL)
    if not llm.api_key and "AITUNNEL_BASE_URL" not in os.environ:
        try:
            llm.api_key = getpass.getpass("Ключ AITunnel (ввод скрыт, в файлы не сохраняется): ").strip()
        except (EOFError, KeyboardInterrupt):
            raise RuntimeError("Ключ не введён. Запустите ассистента в обычном окне PowerShell.")
    return llm


class ProjectAssistant:
    def __init__(self, project: str, index: Index, llm, mcp: MCPClient):
        self.project = project
        self.name = os.path.basename(project)
        self.index = index
        self.llm = llm
        self.mcp = mcp

    def _mcp(self, tool: str, args: dict = None) -> str:
        try:
            return self.mcp.call(tool, args)
        except MCPToolError as e:
            return f"недоступно: {e}"

    def branch(self) -> str:
        return self._mcp("git_branch")

    def files(self, subdir: str = "") -> str:
        return self._mcp("list_files", {"subdir": subdir} if subdir else {})

    def answer(self, question: str, with_structure: bool = False) -> dict:
        question = question.strip()
        if not question:
            raise ValueError("Пустой вопрос.")
        hits = self.index.search(question, k=CHUNKS_IN_CONTEXT)
        blocks = [f"Проект: {self.name}"]
        sources = []
        if with_structure:
            blocks.append(f"Git-ветка: {self.branch()}")
            file_lines = self.files().splitlines()
            blocks.append("=== Файлы проекта (MCP list_files) ===\n" + "\n".join(file_lines[:FILES_IN_CONTEXT + 1]))
            sources.append("MCP: git_branch, list_files")
        blocks.append("=== Документация (фрагменты RAG) ===")
        for h in hits:
            blocks.append(f"[{h['source']} — {h['section']}]\n{h['text'][:CHUNK_CHARS]}")
            if h["source"] not in sources:
                sources.append(h["source"])
        user = f"Вопрос: {question}\n\n" + "\n\n".join(blocks)
        text, usage = self.llm.chat(SYSTEM_PROMPT, user)
        return {"answer": text, "sources": sources, "usage": usage, "with_structure": with_structure}


def show(res: dict) -> None:
    print(f"\n{res['answer']}\n")
    if res["sources"]:
        print("Источники: " + "; ".join(res["sources"]))
    print()


def run_chat(asst: ProjectAssistant, reindex) -> None:
    print(f"Ассистент разработчика — проект «{asst.name}». /help — команды и вопросы о структуре.\n")
    while True:
        try:
            line = input("Вы: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        try:
            if line in ("/exit", "/quit"):
                break
            if line == "/help":
                print(HELP_TEXT + "\n")
            elif line.startswith("/help "):
                show(asst.answer(line[6:], with_structure=True))
            elif line == "/branch":
                print(f"Git-ветка: {asst.branch()}\n")
            elif line.startswith("/files"):
                print(asst.files(line[6:].strip()) + "\n")
            elif line == "/reindex":
                asst.index = reindex()
                print("Индекс пересобран.\n")
            elif line.startswith("/"):
                print("Неизвестная команда. /help — список команд.\n")
            else:
                show(asst.answer(line))
        except RuntimeError as e:  # MCPError и ошибки LLM — подклассы RuntimeError
            print(f"[ошибка] {e}\n")
        except ValueError as e:
            print(f"[ошибка] {e}\n")


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Ассистент разработчика: RAG по документации и MCP-контекст проекта")
    ap.add_argument("--project", default=BASE, help="папка проекта (по умолчанию — папка самого ассистента)")
    ap.add_argument("--index", help="файл индекса документации (по умолчанию — в output/)")
    ap.add_argument("--llm", choices=["aitunnel", "ollama"], default="aitunnel")
    ap.add_argument("--model", help=f"модель: для aitunnel по умолчанию {DEFAULT_AITUNNEL_MODEL}, для ollama {DEFAULT_OLLAMA_MODEL}")
    ap.add_argument("--embed", choices=["local", "ollama"], default="local", help="эмбеддинги документации")
    ap.add_argument("--build", action="store_true", help="только собрать индекс документации и выйти")
    ap.add_argument("--rebuild", action="store_true", help="пересобрать индекс перед запуском")
    ap.add_argument("--once", metavar="ВОПРОС", help="ответить один раз и выйти; начните с /help для структуры проекта")
    a = ap.parse_args(argv)

    project = os.path.realpath(a.project)
    if not os.path.isdir(project):
        print(f"Папка проекта не найдена: {a.project}")
        return 2
    index_path = a.index or default_index_path(project)
    try:
        if a.build or a.rebuild or not os.path.exists(index_path):
            print("Индексирую документацию...")
            info = build_index(project, index_path, a.embed)
            print(f"Готово: документов {info['documents']}, фрагментов {info['chunks']}, эмбеддинги: {info['embedder']}")
            print("Документы: " + ", ".join(info["sources"]) + "\n")
            if a.build:
                return 0
        index = Index(index_path)
        llm = make_llm(a.llm, a.model)
        with MCPClient(project) as mcp:
            asst = ProjectAssistant(project, index, llm, mcp)
            if a.once is not None:
                q = a.once.strip()
                with_structure = q.startswith("/help")
                if with_structure:
                    q = q[5:].strip()
                show(asst.answer(q, with_structure=with_structure))
                return 0

            def reindex():
                build_index(project, index_path, a.embed)
                return Index(index_path)

            run_chat(asst, reindex)
    except (RuntimeError, ValueError, FileNotFoundError) as e:
        print(f"[ошибка] {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
