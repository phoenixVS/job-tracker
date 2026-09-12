import hashlib
import re
from collections.abc import Sequence

import numpy as np
import pymupdf
import pytest

from app.services.matcher import Matcher


class FakeEncoder:
    """Deterministic hashed bag-of-words embeddings: texts sharing words are similar. No model download."""

    dim = 512

    def __init__(self) -> None:
        self.calls = 0
        self.texts_encoded = 0

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        self.calls += 1
        self.texts_encoded += len(texts)
        matrix = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for token in re.findall(r"[a-z0-9+#.]+", text.lower()):
                matrix[row, int(hashlib.md5(token.encode()).hexdigest(), 16) % self.dim] += 1.0
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.where(norms == 0, 1.0, norms)


@pytest.fixture
def fake_encoder() -> FakeEncoder:
    return FakeEncoder()


@pytest.fixture
def matcher(fake_encoder: FakeEncoder) -> Matcher:
    return Matcher(fake_encoder, model_name="fake-bow")


def _make_pdf(lines: list[str], fontsize: float = 10, line_height: float = 14) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 50.0
    for line in lines:
        if y > page.rect.height - 50:
            page = doc.new_page()
            y = 50.0
        if line:
            page.insert_text((50, y), line, fontsize=fontsize)
        y += line_height
    data = doc.tobytes()
    doc.close()
    return data


def _make_two_column_pdf(header: str, left: list[str], right: list[str]) -> bytes:
    """Lines are spaced far apart so each is its own block, like many real two-column CV templates."""
    doc = pymupdf.open()
    page = doc.new_page()  # A4-ish: 595 x 842
    page.insert_text((50, 50), header, fontsize=20)
    for index, line in enumerate(left):
        page.insert_text((50, 120 + index * 40), line, fontsize=10)
    for index, line in enumerate(right):
        page.insert_text((330, 120 + index * 40), line, fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def make_pdf():
    return _make_pdf


@pytest.fixture
def make_two_column_pdf():
    return _make_two_column_pdf
