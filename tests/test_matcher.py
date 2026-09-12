import numpy as np
import pytest

from app.models.job import RawJob
from app.services.matcher import (
    MAX_DESCRIPTION_CHARS,
    Matcher,
    bytes_to_vector,
    full_text,
    metadata_text,
    vector_to_bytes,
)

PROFILE = "Target roles: Backend Engineer. Seniority: senior. Core skills: Python, FastAPI, PostgreSQL, Docker, AWS."


def make_job(external_id: str, title: str, description: str = "", **extra) -> RawJob:
    return RawJob(external_id=external_id, source="mock", title=title, description=description, **extra)


MOCK_JOBS = [
    make_job("sales", "Account Executive", "Close SaaS sales deals, manage pipeline and hit quota"),
    make_job("backend", "Senior Backend Engineer", "Build Python FastAPI services on PostgreSQL, Docker and AWS"),
    make_job("frontend", "Frontend Engineer", "React TypeScript CSS component library and design system"),
]


def test_relevant_job_ranks_above_irrelevant(matcher):
    matcher.set_profile(PROFILE)
    scores = dict(zip((job.external_id for job in MOCK_JOBS), matcher.score_jobs(MOCK_JOBS)))

    assert scores["backend"] > scores["frontend"] > scores["sales"]


def test_scores_are_normalized_to_unit_interval(matcher):
    matcher.set_profile(PROFILE)
    scores = matcher.score_texts([PROFILE, *(full_text(job) for job in MOCK_JOBS)])

    assert all(0.0 <= score <= 1.0 for score in scores)
    assert scores[0] == pytest.approx(1.0, abs=1e-3)


def test_negative_cosine_is_clipped_to_zero():
    class OppositeEncoder:
        def encode(self, texts):
            return np.array([[1.0, 0.0] if text == "profile" else [-1.0, 0.0] for text in texts], dtype=np.float32)

    matcher = Matcher(OppositeEncoder())
    matcher.set_profile("profile")

    assert matcher.score_texts(["job"]) == [0.0]


def test_filter_above_threshold_keeps_best_first():
    scored = [("a", 0.30), ("b", 0.91), ("c", 0.45), ("d", 0.449)]

    assert Matcher.filter_above(scored, 0.45) == [("b", 0.91), ("c", 0.45)]


def test_profile_is_encoded_once_across_batches(matcher, fake_encoder):
    matcher.set_profile(PROFILE)
    matcher.score_jobs(MOCK_JOBS[:2])
    matcher.score_jobs(MOCK_JOBS[2:])

    assert fake_encoder.calls == 3
    assert fake_encoder.texts_encoded == 1 + len(MOCK_JOBS)


def test_empty_batch_skips_encoder(matcher, fake_encoder):
    matcher.set_profile(PROFILE)

    assert matcher.score_texts([]) == []
    assert fake_encoder.calls == 1


def test_scoring_without_profile_raises(matcher):
    with pytest.raises(RuntimeError, match="upload a CV"):
        matcher.score_texts(["anything"])


def test_stored_profile_vector_gives_identical_scores(matcher, fake_encoder):
    vector = matcher.set_profile(PROFILE)
    expected = matcher.score_jobs(MOCK_JOBS)

    restored = Matcher(type(fake_encoder)())
    restored.set_profile_vector(bytes_to_vector(vector_to_bytes(vector)))

    assert restored.score_jobs(MOCK_JOBS) == expected


def test_job_text_builders():
    bare = make_job("1", "Backend Engineer", skills=["Python", "Go"], company="Acme", location="Remote")
    assert full_text(bare) == metadata_text(bare) == "Backend Engineer at Acme Skills: Python, Go. Location: Remote."

    described = make_job("2", "Backend Engineer", "x" * (MAX_DESCRIPTION_CHARS + 500))
    assert full_text(described) == "Backend Engineer " + "x" * MAX_DESCRIPTION_CHARS


@pytest.mark.slow
def test_real_minilm_prefers_semantically_close_job():
    from app.services.matcher import SentenceTransformerEncoder

    matcher = Matcher(SentenceTransformerEncoder("all-MiniLM-L6-v2"), model_name="all-MiniLM-L6-v2")
    matcher.set_profile("Senior backend engineer. Core skills: Python, Django, PostgreSQL, REST APIs, AWS, Docker.")
    backend, sales = matcher.score_texts([
        "Python Developer: build Django REST APIs backed by Postgres, deployed with Docker on AWS.",
        "Account Executive: close enterprise software deals and exceed quarterly sales quota.",
    ])

    assert backend > sales + 0.2
    assert 0.0 <= sales < backend <= 1.0
