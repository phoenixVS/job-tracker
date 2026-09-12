"""Local semantic scoring with sentence-transformers (CPU, zero cost)."""

import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from typing import Protocol, TypeVar

import numpy as np

from app.core.config import get_settings
from app.models.job import RawJob

logger = logging.getLogger(__name__)

T = TypeVar("T")
MAX_DESCRIPTION_CHARS = 2000


class Encoder(Protocol):
    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """Return a (len(texts), dim) float32 matrix."""


class SentenceTransformerEncoder:
    def __init__(self, model_name: str, device: str = "cpu", batch_size: int = 64) -> None:
        from sentence_transformers import SentenceTransformer  # heavy import, keep lazy

        logger.info("Loading embedding model %s on %s", model_name, device)
        self._model = SentenceTransformer(model_name, device=device)
        self._batch_size = batch_size

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        return self._model.encode(
            list(texts),
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        ).astype(np.float32)


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    return matrix / np.where(norms == 0, 1.0, norms)


def vector_to_bytes(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def bytes_to_vector(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.float32).copy()


def metadata_text(job: RawJob) -> str:
    """Stage-1 text: what search results give us without a detail call."""
    parts = [job.title]
    if job.company:
        parts.append(f"at {job.company}")
    if job.seniority:
        parts.append(f"Seniority: {job.seniority}.")
    if job.skills:
        parts.append(f"Skills: {', '.join(job.skills)}.")
    if job.location:
        parts.append(f"Location: {job.location}.")
    return " ".join(parts)


def full_text(job: RawJob) -> str:
    """Stage-2 text: title + description; falls back to metadata when no description is known."""
    description = " ".join(job.description.split())[:MAX_DESCRIPTION_CHARS]
    if not description:
        return metadata_text(job)
    return f"{job.title} {description}"


class Matcher:
    def __init__(self, encoder: Encoder, model_name: str = "custom") -> None:
        self.encoder = encoder
        self.model_name = model_name
        self._profile_vector: np.ndarray | None = None
        self._lock = threading.Lock()

    @property
    def has_profile(self) -> bool:
        return self._profile_vector is not None

    def encode_profile(self, summary_blob: str) -> np.ndarray:
        with self._lock:
            return _l2_normalize(self.encoder.encode([summary_blob]))[0]

    def set_profile_vector(self, vector: np.ndarray) -> None:
        self._profile_vector = _l2_normalize(vector)

    def set_profile(self, summary_blob: str) -> np.ndarray:
        """Encode the candidate once and cache the vector for every later batch."""
        vector = self.encode_profile(summary_blob)
        self._profile_vector = vector
        return vector

    def score_texts(self, texts: Sequence[str]) -> list[float]:
        """Cosine similarity to the profile, clipped to [0, 1]."""
        if self._profile_vector is None:
            raise RuntimeError("No candidate profile loaded; upload a CV first")
        if not texts:
            return []
        with self._lock:
            embeddings = _l2_normalize(self.encoder.encode(list(texts)))
        similarities = np.clip(embeddings @ self._profile_vector, 0.0, 1.0)
        return [round(float(s), 4) for s in similarities]

    def score_jobs(self, jobs: Sequence[RawJob], text_fn: Callable[[RawJob], str] = full_text) -> list[float]:
        return self.score_texts([text_fn(job) for job in jobs])

    @staticmethod
    def filter_above(scored: Iterable[tuple[T, float]], threshold: float) -> list[tuple[T, float]]:
        """Keep items scoring >= threshold, best first."""
        kept = [(item, score) for item, score in scored if score >= threshold]
        return sorted(kept, key=lambda pair: pair[1], reverse=True)


_matcher: Matcher | None = None
_matcher_lock = threading.Lock()


def get_matcher() -> Matcher:
    """Process-wide singleton; the model is loaded once (warmed in the app lifespan)."""
    global _matcher
    if _matcher is None:
        with _matcher_lock:
            if _matcher is None:
                model_name = get_settings().embedding_model
                _matcher = Matcher(SentenceTransformerEncoder(model_name), model_name=model_name)
    return _matcher


def set_matcher(matcher: Matcher | None) -> None:
    """Override the singleton (tests inject a fake encoder)."""
    global _matcher
    _matcher = matcher
