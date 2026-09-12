from datetime import date

import pymupdf
import pytest

from app.services.cv_parser import (
    CVParseError,
    build_profile,
    estimate_seniority,
    estimate_years,
    extract_roles,
    extract_skills,
    extract_text,
    infer_role_from_skills,
    split_sections,
)

TODAY = date(2026, 9, 1)

CV_LINES = [
    "Alex Rivera",
    "Senior Backend Engineer | alex@example.com | Berlin",
    "",
    "Summary",
    "Backend engineer with 6+ years of experience designing Python-based APIs.",
    "Always ready to go the extra mile.",
    "",
    "Skills",
    "Python, FastAPI, Django, postgres, Redis, Docker, k8s, AWS, Go, HTML/CSS",
    "",
    "Experience",
    "Senior Backend Engineer, Acme GmbH    Jan 2022 - Present",
    "Built event-driven services and a node.js gateway.",
    "Software Engineer, Beta Ltd    Jun 2018 - Dec 2021",
    "Maintained REST APIs and CI/CD pipelines.",
    "",
    "Education",
    "BSc Computer Science, TU Berlin    2014 - 2018",
]


def test_extract_text_reads_generated_pdf(make_pdf):
    text = extract_text(make_pdf(CV_LINES))

    assert "Alex Rivera" in text
    assert "Jun 2018 - Dec 2021" in text


def test_build_profile_end_to_end(make_pdf):
    profile = build_profile(make_pdf(CV_LINES), today=TODAY)

    assert {"Python", "FastAPI", "Django", "PostgreSQL", "Redis", "Docker", "Kubernetes", "AWS", "Go", "HTML", "CSS",
            "Node.js", "REST APIs", "CI/CD"} <= set(profile.skills)
    assert "C" not in profile.skills
    # Header title, the summary's "Backend engineer", then the previous job title (duplicates removed).
    assert profile.roles == ["Senior Backend Engineer", "Backend Engineer", "Software Engineer"]
    # Jan 2022..Sep 2026 (56 months) + Jun 2018..Dec 2021 (42 months) = 8.2 years; education is ignored.
    assert profile.years_experience == pytest.approx(8.2)
    assert profile.seniority == "senior"
    assert profile.summary_blob.startswith("Target roles: Senior Backend Engineer")
    assert "Core skills: Python" in profile.summary_blob
    assert "Summary: Backend engineer with 6+ years" in profile.summary_blob


def test_skill_aliases_and_symbols():
    text = "Skills\nk8s, postgres, Golang, C#, C++, HTML/CSS, Vue\nExperience\nShipped a node.js API in TypeScript."

    skills = extract_skills(text)

    assert {"Kubernetes", "PostgreSQL", "Go", "C#", "C++", "HTML", "CSS", "Vue.js", "Node.js", "TypeScript"} <= set(skills)
    assert "C" not in skills
    assert "JavaScript" not in skills  # "js" inside "node.js" must not count


def test_ambiguous_tokens_only_count_inside_skills_section():
    prose = "Summary\nReady to Go live. Plan C was never needed.\nExperience\nPython developer"
    listed = "Skills\nGo, C, R\nExperience\nPython developer"

    assert extract_skills(prose) == ["Python"]
    assert {"Go", "C", "R", "Python"} <= set(extract_skills(listed))


def test_inline_section_heading():
    sections = split_sections("Jane Doe\nSkills: Python, Go\nExperience\nData Engineer at X")

    assert sections["skills"] == "Python, Go"
    assert sections["experience"] == "Data Engineer at X"


def test_languages_label_inside_skills_is_not_a_new_section():
    text = (
        "Skills\nLanguages: Python, Go\nFrameworks: FastAPI\n"
        "Experience\nBackend Engineer at X\n"
        "Languages\nEnglish, Portuguese"
    )

    sections = split_sections(text)

    assert sections["skills"] == "Languages: Python, Go\nFrameworks: FastAPI"
    assert sections["languages"] == "English, Portuguese"
    assert {"Python", "Go", "FastAPI"} <= set(extract_skills(text, sections))


def test_roles_are_normalized_and_deduplicated():
    text = "Sr. Back-end Developer at Foo\nfull stack engineer at Bar\nSenior Backend Developer at Baz\nTech Lead"

    assert extract_roles(text) == ["Senior Backend Developer", "Full-Stack Engineer", "Tech Lead"]


def test_role_inferred_from_skills_when_no_title_found():
    assert infer_role_from_skills(["React", "TypeScript", "Node.js", "PostgreSQL"]) == "Full-Stack Engineer"
    assert infer_role_from_skills(["Kubernetes", "Terraform", "AWS"]) == "DevOps Engineer"
    assert infer_role_from_skills([]) == "Software Engineer"
    assert extract_roles("Skills\nReact, Vue, CSS") == ["Frontend Engineer"]


def test_years_merge_overlapping_ranges_and_skip_education():
    text = (
        "Experience\nCompany A   Jan 2020 – Dec 2022\nCompany B   06/2021 - present\n"
        "Education\nUniversity   2010 - 2014\n"
    )

    # Overlaps merge into Jan 2020..Sep 2026 = 80 months.
    assert estimate_years(text, today=TODAY) == pytest.approx(6.7)


def test_explicit_years_claim_wins_when_larger():
    assert estimate_years("Summary\nEngineer with 12 years of professional experience.", today=TODAY) == 12.0
    assert estimate_years("No dates here at all.", today=TODAY) is None


@pytest.mark.parametrize(("years", "expected"), [(1, "junior"), (3, "mid"), (6, "senior"), (12, "staff")])
def test_seniority_from_years(years, expected):
    assert estimate_seniority(years, []) == expected


def test_title_keyword_raises_seniority():
    assert estimate_seniority(3, ["Lead Software Engineer"]) == "senior"
    assert estimate_seniority(None, ["Staff Platform Engineer"]) == "staff"
    assert estimate_seniority(12, ["Junior Developer"]) == "staff"  # years outrank a stale junior title
    assert estimate_seniority(None, []) is None


def test_two_column_layout_is_read_column_by_column(make_two_column_pdf):
    pdf = make_two_column_pdf(
        "Alex Rivera - Senior Backend Engineer",
        left=["Skills", "Python", "Django", "PostgreSQL"],
        right=["Experience", "Backend Engineer at Acme", "Jan 2020 - Present"],
    )

    text = extract_text(pdf)

    assert text.index("Alex Rivera") < text.index("Skills") < text.index("PostgreSQL") < text.index("Experience")
    assert text.index("Experience") < text.index("Jan 2020 - Present")


def test_rejects_non_pdf():
    with pytest.raises(CVParseError, match="not a PDF"):
        extract_text(b"definitely not a pdf")


def test_rejects_pdf_without_text():
    doc = pymupdf.open()
    doc.new_page()
    blank = doc.tobytes()
    doc.close()

    with pytest.raises(CVParseError, match="no extractable text"):
        extract_text(blank)


def test_rejects_corrupt_pdf():
    # PyMuPDF may refuse the file outright or "repair" it into an empty document; both must be rejected.
    with pytest.raises(CVParseError, match="Could not open PDF|no extractable text"):
        extract_text(b"%PDF-1.7\n this is garbage")
