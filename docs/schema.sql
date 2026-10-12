-- Схема индекса документации (SQLite). Файл создаётся в output/ при сборке индекса.
-- Одна таблица на индекс: каждая строка — фрагмент документа с вектором эмбеддинга.

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,       -- strategy, project, documents, embedder, created, chunks, dim
    value TEXT NOT NULL           -- JSON
);

CREATE TABLE chunks (
    chunk_id   TEXT PRIMARY KEY,  -- source#structure-NNN, например README.md#structure-000
    strategy   TEXT NOT NULL,     -- всегда structure для дня 31
    source     TEXT NOT NULL,    -- путь файла относительно папки проекта, например docs/rag.md
    file       TEXT NOT NULL,    -- имя файла
    title      TEXT NOT NULL,    -- заголовок документа
    section    TEXT NOT NULL,    -- раздел, например «Архитектура > Компоненты»
    position   INTEGER NOT NULL, -- номер фрагмента внутри документа
    char_start INTEGER NOT NULL, -- начало фрагмента в тексте документа
    char_end   INTEGER NOT NULL, -- конец фрагмента
    n_chars    INTEGER NOT NULL, -- длина текста
    text       TEXT NOT NULL,    -- текст фрагмента
    embedding  BLOB NOT NULL     -- вектор float32, L2-нормирован; косинус = скалярное произведение
);

CREATE INDEX idx_chunks_source ON chunks(source);
