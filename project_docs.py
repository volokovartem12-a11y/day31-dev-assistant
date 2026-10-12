"""
День 31. RAG по документации проекта: README, папка docs/, схемы данных и описания API.

В индекс попадают:
  • в корне: README*, CONTRIBUTING*, CHANGELOG*, любые *.md;
  • всё из папки docs/: .md, .txt, .sql, .json, .yaml, .yml, .graphql;
  • файлы схем и API в любой папке: *.sql, а также *.json / *.yaml / *.yml / *.graphql,
    в имени которых есть schema, openapi, swagger, api или migration.
Код (.py и другие языки) сюда не входит: ТЗ просит документацию, схемы и описания API.

Разбиение — стратегия structure из дней 21–28 (по заголовкам Markdown, по абзацам для остального).
Эмбеддинги — local (без сети, лексические) или ollama (семантические, на этом ПК).
"""

import hashlib
import os

from chunking import structure_chunks
from embeddings import make_embedder
from index_store import save_index
from loaders import Document, _title

BASE = os.path.dirname(os.path.abspath(__file__))
TEXT_EXT = {".md": "markdown", ".txt": "text", ".sql": "text", ".json": "text",
            ".yaml": "text", ".yml": "text", ".graphql": "text"}
SCHEMA_EXT = {".sql", ".json", ".yaml", ".yml", ".graphql"}
SCHEMA_HINTS = ("schema", "openapi", "swagger", "api", "migration")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "output", "logs"}
ROOT_NAMES = ("readme", "contributing", "changelog")
MAX_FILE_BYTES = 300_000


def _wanted(rel: str, name: str) -> bool:
    ext = os.path.splitext(name)[1].lower()
    low = name.lower()
    parts = rel.split("/")
    if len(parts) == 1:  # файлы в корне проекта
        return ext in TEXT_EXT and (ext == ".md" or low.startswith(ROOT_NAMES))
    if parts[0] == "docs" and ext in TEXT_EXT:
        return True
    if ext == ".sql":
        return True
    return ext in SCHEMA_EXT and any(h in low for h in SCHEMA_HINTS)


def collect_docs(root: str) -> list:
    root = os.path.abspath(root)
    docs = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            if not _wanted(rel, name) or os.path.getsize(path) > MAX_FILE_BYTES:
                continue
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                text = f.read().replace("\r\n", "\n").replace("\r", "\n").strip()
            if not text:
                continue
            kind = TEXT_EXT[os.path.splitext(name)[1].lower()]
            docs.append(Document(rel, name, kind, _title(kind, text, name), text))
    return docs


def default_index_path(root: str) -> str:
    real = os.path.realpath(root)
    key = hashlib.md5(real.lower().encode("utf-8")).hexdigest()[:8]
    return os.path.join(BASE, "output", f"index-{os.path.basename(real)}-{key}.sqlite")


def build_index(root: str, index_path: str, backend: str = "local", model: str = None) -> dict:
    docs = collect_docs(root)
    if not docs:
        raise RuntimeError("В проекте нет документации: добавьте README.md, папку docs/ "
                           "или файлы схем (например schema.sql, openapi.yaml).")
    chunks = [c for d in docs for c in structure_chunks(d)]
    if not chunks:
        raise RuntimeError("Документы пустые: нечего индексировать.")
    # В вектор попадают путь файла и раздел: вопрос «схема indeks» найдёт schema.sql, даже если в тексте этого слова нет.
    # В индекс и в ответы уходит сам текст фрагмента — без шапки.
    texts = [f"{c.source} {c.section}\n{c.text}" for c in chunks]
    embedder = make_embedder(backend, model).fit(texts)
    vectors = embedder.embed(texts)
    save_index(index_path, chunks, vectors, {
        "strategy": "structure",
        "project": os.path.basename(os.path.abspath(root)),
        "documents": [d.source for d in docs],
        "embedder": embedder.state(),
    })
    return {"documents": len(docs), "chunks": len(chunks), "sources": [d.source for d in docs],
            "embedder": embedder.label, "index": index_path}
