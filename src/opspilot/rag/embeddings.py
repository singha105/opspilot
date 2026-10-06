"""Text embeddings with fastembed, cached on disk by chunk-text hash."""

import hashlib
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

Vector = list[float]
BATCH_SIZE = 16


class Embedder(Protocol):
    """Turns passages and queries into vectors of a fixed dimension."""

    @property
    def dimension(self) -> int: ...

    def embed_passages(self, texts: Sequence[str]) -> list[Vector]: ...

    def embed_query(self, text: str) -> Vector: ...


class EmbeddingCache:
    """SQLite cache of passage vectors keyed by sha1(model + text)."""

    def __init__(self, path: Path, model: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vec BLOB)")
        self._model = model

    def key(self, text: str) -> str:
        return hashlib.sha1(f"{self._model}\x1f{text}".encode()).hexdigest()  # noqa: S324

    def get_many(self, keys: Sequence[str]) -> dict[str, Vector]:
        found: dict[str, Vector] = {}
        for start in range(0, len(keys), 500):
            batch = list(keys[start : start + 500])
            marks = ",".join("?" * len(batch))
            rows = self._db.execute(
                f"SELECT key, vec FROM vectors WHERE key IN ({marks})",  # noqa: S608
                batch,
            )
            for key, blob in rows:
                found[key] = np.frombuffer(blob, dtype=np.float32).tolist()
        return found

    def put_many(self, items: dict[str, Vector]) -> None:
        rows = [(k, np.asarray(v, dtype=np.float32).tobytes()) for k, v in items.items()]
        self._db.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?)", rows)
        self._db.commit()

    def __len__(self) -> int:
        (count,) = self._db.execute("SELECT COUNT(*) FROM vectors").fetchone()
        return int(count)


class FastEmbedEmbedder:
    """fastembed model (default ``BAAI/bge-small-en-v1.5``, 384 dimensions).

    Documents use passage embeddings and queries use query embeddings (bge adds its
    retrieval instruction to queries). Only passage vectors are cached; query time
    is part of measured retrieval latency.
    """

    def __init__(
        self,
        model: str = "BAAI/bge-small-en-v1.5",
        cache_path: Path | None = None,
        model_dir: Path | None = None,
    ) -> None:
        from fastembed import TextEmbedding  # heavy import, done lazily

        self.model = model
        self._model = TextEmbedding(
            model_name=model, cache_dir=str(model_dir) if model_dir else None
        )
        self._cache = EmbeddingCache(cache_path, model) if cache_path else None
        self._dimension = len(self.embed_query("dimension probe"))

    @property
    def dimension(self) -> int:
        return self._dimension

    def _embed(self, texts: Sequence[str]) -> list[Vector]:
        """Embed in small length-sorted batches.

        Every batch is padded to its longest text, so large batches of long chunks
        need gigabytes for attention and push an 8 GB machine into swap. Sorting by
        length keeps padding (and memory) low.
        """
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        sorted_vectors = self._model.passage_embed([texts[i] for i in order], batch_size=BATCH_SIZE)
        vectors: list[Vector] = [[] for _ in texts]
        for i, vector in zip(order, sorted_vectors, strict=True):
            vectors[i] = _to_list(vector)
        return vectors

    def embed_passages(self, texts: Sequence[str]) -> list[Vector]:
        if self._cache is None:
            return self._embed(texts)
        keys = [self._cache.key(t) for t in texts]
        cached = self._cache.get_many(keys)
        missing = [(k, t) for k, t in zip(keys, texts, strict=True) if k not in cached]
        if missing:
            vectors = self._embed([t for _, t in missing])
            fresh = {k: v for (k, _), v in zip(missing, vectors, strict=True)}
            self._cache.put_many(fresh)
            cached.update(fresh)
        return [cached[k] for k in keys]

    def embed_query(self, text: str) -> Vector:
        return _to_list(next(iter(self._model.query_embed(text))))


def _to_list(vector: NDArray[Any]) -> Vector:
    return [float(x) for x in vector]
