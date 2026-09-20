"""Per-source limits in the sync pipeline (e.g. Worklittle's small job and detail-call caps)."""

from app.core.config import Settings
from app.models.job import RawJob
from app.services.job_fetcher import SearchParams
from app.services.sync import _fetch_all, _score_two_stage

QUERIES = ["Backend Engineer", "Software Engineer", "Platform Engineer"]


class FakeSource:
    def __init__(self, name: str, jobs: list[RawJob], max_jobs: int | None = None,
                 max_details: int | None = None) -> None:
        self.name = name
        self.jobs = jobs
        self.max_jobs = max_jobs
        self.max_details = max_details
        self.search_limits: list[int] = []
        self.detailed: list[str] = []

    async def search(self, params: SearchParams) -> list[RawJob]:
        self.search_limits.append(params.limit)
        return self.jobs[: params.limit]

    async def fetch_details(self, job: RawJob) -> RawJob:
        self.detailed.append(job.external_id)
        return job.model_copy(update={"description": f"{job.title}: build Python backend services"})

    async def aclose(self) -> None:
        return None


def make_jobs(source: str, count: int) -> list[RawJob]:
    return [RawJob(external_id=f"{source}-{n}", source=source, title=f"Backend Engineer {n}") for n in range(count)]


async def test_search_limit_uses_per_source_cap_when_set():
    settings = Settings(_env_file=None, max_jobs_per_source=150)
    jobdatalake = FakeSource("jobdatalake", make_jobs("jobdatalake", 5))
    worklittle = FakeSource("worklittle", make_jobs("worklittle", 5), max_jobs=30)

    batch, status, any_success = await _fetch_all([jobdatalake, worklittle], QUERIES, None, settings)

    assert jobdatalake.search_limits == [50, 50, 50]
    assert worklittle.search_limits == [10, 10, 10]
    assert any_success and status == {"jobdatalake": "ok (5 jobs)", "worklittle": "ok (5 jobs)"}
    assert len(batch) == 10


async def test_detail_calls_respect_per_source_cap(matcher):
    settings = Settings(_env_file=None, detail_enrich_top_n=4)
    jdl_jobs, wl_jobs = make_jobs("jobdatalake", 3), make_jobs("worklittle", 3)
    jobdatalake = FakeSource("jobdatalake", jdl_jobs)
    worklittle = FakeSource("worklittle", wl_jobs, max_details=1)
    matcher.set_profile("Target roles: Backend Engineer. Core skills: Python.")

    scores, jobs, enriched = await _score_two_stage(jdl_jobs + wl_jobs, [jobdatalake, worklittle], matcher, settings)

    assert len(worklittle.detailed) == 1  # capped even though more Worklittle jobs are in the overall top 4
    assert len(jobdatalake.detailed) == 3
    assert enriched == 4
    assert len(jobs) == 6 and set(scores) == {job.storage_key for job in jobs}


async def test_zero_detail_cap_disables_detail_calls_for_that_source(matcher):
    settings = Settings(_env_file=None, detail_enrich_top_n=50)
    wl_jobs = make_jobs("worklittle", 3)
    worklittle = FakeSource("worklittle", wl_jobs, max_details=0)
    matcher.set_profile("Backend Engineer")

    _, _, enriched = await _score_two_stage(wl_jobs, [worklittle], matcher, settings)

    assert worklittle.detailed == [] and enriched == 0
