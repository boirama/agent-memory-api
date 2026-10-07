"""Embedding providers, chosen by EMBEDDING_PROVIDER.

  hashing (default) — no API key, no model download, no cold start, ~0.1ms.
        Signed feature hashing over word unigrams, bigrams and character
        4-grams. Captures lexical and morphological overlap well but has NO
        semantic understanding: "car" and "automobile" score near zero.
        Hybrid ranking compensates somewhat. Be honest with yourself.

  openai — text-embedding-3-small requested at 768 dimensions so it drops
        into the same vector(768) column. Real paraphrase matching. Costs
        ~$0.00002 per store, which is 2% of your $0.001 store price.

Ship on hashing to get live; switch to openai before taking real money.
Both are L2-normalised so cosine distance behaves.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from abc import ABC, abstractmethod

from .config import settings

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class EmbeddingProvider(ABC):
    dim: int

    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]


class HashingEmbedder(EmbeddingProvider):
    def __init__(self, dim: int = 768) -> None:
        self.dim = dim

    @staticmethod
    def _features(text: str) -> list[str]:
        lowered = text.lower()
        words = _TOKEN_RE.findall(lowered)

        feats: list[str] = [f"w:{w}" for w in words]
        feats += [f"b:{words[i]}_{words[i + 1]}" for i in range(len(words) - 1)]

        # Character 4-grams give partial credit for typos and inflections
        # ("invoicing" vs "invoiced") that word tokens alone would miss.
        compact = " ".join(words)
        feats += [f"c:{compact[i:i + 4]}" for i in range(max(0, len(compact) - 3))]
        return feats

    def embed_sync(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        if not text or not text.strip():
            vec[0] = 1.0  # cosine distance hates all-zero vectors
            return vec

        counts: dict[str, int] = {}
        for f in self._features(text):
            counts[f] = counts.get(f, 0) + 1

        for feature, count in counts.items():
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            slot = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            # Sublinear: a word repeated 10x shouldn't dominate 10x.
            vec[slot] += sign * (1.0 + math.log(count))

        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]

    async def embed(self, text: str) -> list[float]:
        return self.embed_sync(text)


class OpenAIEmbedder(EmbeddingProvider):
    def __init__(self, api_key: str, model: str, dim: int = 768) -> None:
        self.dim = dim
        self.model = model
        self._api_key = api_key
        self._fallback = HashingEmbedder(dim)
        self._client = None

    def _get_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                base_url="https://api.openai.com/v1",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=httpx.Timeout(10.0, connect=5.0),
            )
        return self._client

    async def _call(self, inputs: list[str]) -> list[list[float]]:
        client = self._get_client()
        resp = await client.post(
            "/embeddings",
            json={"input": inputs, "model": self.model, "dimensions": self.dim},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        data.sort(key=lambda d: d["index"])
        return [d["embedding"] for d in data]

    async def embed(self, text: str) -> list[float]:
        try:
            return (await self._call([text or " "]))[0]
        except Exception as exc:  # noqa: BLE001
            # Never fail a write because OpenAI hiccuped. Degrade instead,
            # but log loudly: mixing embedding spaces silently poisons recall.
            logger.error("openai embedding failed, falling back to hashing: %s", exc)
            return await self._fallback.embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            return await self._call([t or " " for t in texts])
        except Exception as exc:  # noqa: BLE001
            logger.error("openai batch embedding failed: %s", exc)
            return [await self._fallback.embed(t) for t in texts]


_provider: EmbeddingProvider | None = None


def get_embedder() -> EmbeddingProvider:
    global _provider
    if _provider is not None:
        return _provider

    if settings.embedding_provider == "openai":
        if not settings.openai_api_key:
            logger.warning(
                "EMBEDDING_PROVIDER=openai but OPENAI_API_KEY is unset - "
                "using hashing embedder"
            )
            _provider = HashingEmbedder(settings.embedding_dim)
        else:
            _provider = OpenAIEmbedder(
                settings.openai_api_key,
                settings.openai_embedding_model,
                settings.embedding_dim,
            )
    else:
        _provider = HashingEmbedder(settings.embedding_dim)

    logger.info("embedder: %s (dim=%d)", type(_provider).__name__, _provider.dim)
    return _provider


def to_pgvector(vec: list[float]) -> str:
    """pgvector's text input format. Passing a Python list through asyncpg
    needs the pgvector codec registered; the string form is one less part."""
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"
