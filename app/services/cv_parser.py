"""PDF CV -> structured CandidateProfile. Heuristic and fully local (no LLM calls)."""

import re
from collections import Counter
from datetime import date

import pymupdf as fitz  # PyMuPDF; the bare `fitz` module name is deprecated

from app.models.cv import CandidateProfile
from app.services.skills_taxonomy import BACKEND, DATA, DEVOPS, FRONTEND, ML, MOBILE, SKILL_PATTERNS

MIN_TEXT_CHARS = 30
SUMMARY_MAX_CHARS = 600
MAX_ROLES = 5
BLOB_MAX_SKILLS = 25


class CVParseError(ValueError):
    """The upload isn't a readable, text-based PDF."""


# --------------------------------------------------------------------------- text extraction


def extract_text(pdf_bytes: bytes) -> str:
    if not pdf_bytes or b"%PDF" not in pdf_bytes[:1024]:
        raise CVParseError("File is not a PDF")
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises several error types for corrupt files
        raise CVParseError(f"Could not open PDF: {exc}") from exc
    with doc:
        if doc.needs_pass:
            raise CVParseError("PDF is password-protected")
        pages = [_page_text(page) for page in doc]
    text = _normalize("\n\n".join(pages))
    if len(text) < MIN_TEXT_CHARS:
        raise CVParseError("PDF contains no extractable text (scanned CVs would need OCR, which is not supported)")
    return text


def _page_text(page: fitz.Page) -> str:
    # Block tuples: (x0, y0, x1, y1, text, block_no, block_type); type 0 = text.
    blocks = [b for b in page.get_text("blocks", sort=True) if b[6] == 0 and b[4].strip()]
    return "\n".join(b[4] for b in _reading_order(blocks, page.rect.width))


def _reading_order(blocks: list[tuple], page_width: float) -> list[tuple]:
    """Top-to-bottom, except two-column layouts are read column by column."""

    def by_position(items: list[tuple]) -> list[tuple]:
        return sorted(items, key=lambda b: (b[1], b[0]))

    mid = page_width / 2
    gutter = page_width * 0.04
    left = [b for b in blocks if b[2] <= mid + gutter]
    right = [b for b in blocks if b[0] >= mid - gutter and b[2] > mid + gutter]
    columned = {id(b) for b in left + right}
    spanning = [b for b in blocks if id(b) not in columned]
    if len(left) < 2 or len(right) < 2:
        return by_position(blocks)

    column_top = min(b[1] for b in left + right)
    header = [b for b in spanning if b[3] <= column_top + 1]
    inside = [b for b in spanning if b[3] > column_top + 1]
    # Many full-width blocks between the "columns" means it's really one column with
    # right-aligned fragments (e.g. dates), so keep natural order.
    if len(inside) > 0.2 * len(blocks):
        return by_position(blocks)
    return by_position(header) + by_position(left) + by_position(right) + by_position(inside)


_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}


def _normalize(text: str) -> str:
    for ligature, replacement in _LIGATURES.items():
        text = text.replace(ligature, replacement)
    text = text.replace("­", "").replace(" ", " ")
    text = re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", text)  # re-join words hyphenated across lines
    text = re.sub(r"[•●▪◦■►‣∙]", "-", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --------------------------------------------------------------------------- sections

_SECTION_HEADINGS: list[tuple[str, str]] = [
    ("skills", r"(?:technical |core |key |professional )?(?:skills|competencies|expertise)(?: (?:&|and) [a-z]+)?"
               r"|tech(?:nical)? stack|technologies|tools(?: (?:&|and) technologies)?|programming languages"),
    ("summary", r"(?:professional |career )?(?:summary|profile)|about(?: me)?|objective"),
    ("experience", r"(?:professional |work |relevant )?experience|employment(?: history)?|work history|career history"),
    ("education", r"education(?: (?:&|and) [a-z]+)?|academic background"),
    ("projects", r"(?:personal |side |selected |key )?projects"),
    ("certifications", r"certifications?|licenses(?: (?:&|and) certifications)?|awards"),
    ("languages", r"languages"),
    ("other", r"interests|hobbies|references|publications|volunteering"),
]
_HEADING_RES = [
    (name, re.compile(rf"^(?:{pattern})\s*(?::\s*(?P<rest>.*))?$", re.IGNORECASE))
    for name, pattern in _SECTION_HEADINGS
]


def _match_heading(candidate: str, current: str) -> tuple[str, str | None] | None:
    """Return (section name, inline content) if the line is a section heading."""
    for name, regex in _HEADING_RES:
        match = regex.match(candidate)
        if not match:
            continue
        rest = match.group("rest")
        if rest is None and len(candidate) > 40:
            continue
        if rest is not None and name == "languages" and current == "skills":
            return None  # "Languages: Python, Go" inside Skills is a sub-list, not spoken languages
        return name, rest
    return None


def split_sections(text: str) -> dict[str, str]:
    """Split CV text by headings. Text before the first heading goes to "header"."""
    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    for line in text.split("\n"):
        heading = _match_heading(line.strip().strip("-–—|#*").strip(), current)
        if heading is None:
            sections.setdefault(current, []).append(line)
            continue
        current, rest = heading
        sections.setdefault(current, [])
        if rest:
            sections[current].append(rest)
    joined = {name: "\n".join(lines).strip() for name, lines in sections.items()}
    return {name: body for name, body in joined.items() if body}


# --------------------------------------------------------------------------- skills


def extract_skills(text: str, sections: dict[str, str] | None = None) -> list[str]:
    """Canonical skills found in the CV, most frequently mentioned first."""
    sections = split_sections(text) if sections is None else sections
    skills_text = sections.get("skills", "")
    counts: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for pattern in SKILL_PATTERNS:
        target = skills_text if pattern.skills_section_only else text
        if not target:
            continue
        positions = [m.start() for m in pattern.regex.finditer(target)]
        if positions:
            counts[pattern.canonical] += len(positions)
            first_seen[pattern.canonical] = min(first_seen.get(pattern.canonical, positions[0]), positions[0])
    return sorted(counts, key=lambda skill: (-counts[skill], first_seen[skill]))


# --------------------------------------------------------------------------- roles

_LEVEL_WORDS = r"Senior|Sr\.?|Lead|Staff|Principal|Junior|Jr\.?|Mid[- ]Level|Intermediate"
_DOMAIN_WORDS = (
    r"Full[\s-]?Stack|Back[\s-]?End|Front[\s-]?End|Software|Web|Data|Machine\s+Learning|ML|AI|DevOps|Platform"
    r"|Cloud|Mobile|iOS|Android|Site\s+Reliability|QA|Test|Security|Infrastructure|Systems|Application|Embedded"
    r"|Game|Blockchain|Python|Java|JavaScript|TypeScript|Golang|Go|Ruby|PHP|Rust|React|Node(?:\.js)?"
)
_ROLE_RE = re.compile(
    rf"\b(?:(?P<level>{_LEVEL_WORDS})\s+)?(?P<domain>{_DOMAIN_WORDS})\s+"
    r"(?P<kind>Engineer|Developer|Architect|Programmer|Scientist|Analyst)\b",
    re.IGNORECASE,
)
_LEAD_RE = re.compile(r"\b(?:Tech(?:nical)?\s+Lead|Engineering\s+Manager|Head\s+of\s+Engineering|CTO)\b", re.IGNORECASE)

_DOMAIN_CANON = {
    "fullstack": "Full-Stack", "backend": "Backend", "frontend": "Frontend", "machinelearning": "Machine Learning",
    "ml": "ML", "ai": "AI", "devops": "DevOps", "ios": "iOS", "qa": "QA", "php": "PHP", "javascript": "JavaScript",
    "typescript": "TypeScript", "golang": "Go", "node": "Node.js", "node.js": "Node.js",
    "sitereliability": "Site Reliability",
}
_LEVEL_CANON = {"sr": "Senior", "sr.": "Senior", "jr": "Junior", "jr.": "Junior", "mid-level": "Mid-Level",
                "mid level": "Mid-Level"}
_LEAD_CANON = {"cto": "CTO", "technical lead": "Tech Lead", "tech lead": "Tech Lead"}


def _canonical_role(match: re.Match[str]) -> str:
    level = match.group("level")
    domain = match.group("domain")
    parts = []
    if level:
        parts.append(_LEVEL_CANON.get(level.lower(), level.capitalize()))
    domain_key = re.sub(r"[\s-]+", "", domain.lower())
    parts.append(_DOMAIN_CANON.get(domain_key, " ".join(domain.split()).title()))
    parts.append(match.group("kind").capitalize())
    return " ".join(parts)


def infer_role_from_skills(skills: list[str]) -> str:
    owned = set(skills)
    scores = {
        "Frontend Engineer": len(owned & FRONTEND),
        "Backend Engineer": len(owned & BACKEND),
        "Mobile Engineer": len(owned & MOBILE),
        "DevOps Engineer": len(owned & DEVOPS),
        "Data Engineer": len(owned & DATA),
        "Machine Learning Engineer": len(owned & ML),
    }
    if scores["Frontend Engineer"] >= 2 and scores["Backend Engineer"] >= 2:
        return "Full-Stack Engineer"
    best = max(scores, key=lambda role: scores[role])
    return best if scores[best] >= 2 else "Software Engineer"


def extract_roles(text: str, sections: dict[str, str] | None = None, skills: list[str] | None = None) -> list[str]:
    """Job titles held/targeted, in document order (usually most recent first)."""
    sections = split_sections(text) if sections is None else sections
    scope = "\n".join(sections.get(name, "") for name in ("header", "summary", "experience")).strip() or text
    found: list[tuple[int, str]] = [(m.start(), _canonical_role(m)) for m in _ROLE_RE.finditer(scope)]
    for match in _LEAD_RE.finditer(scope):
        raw = " ".join(match.group(0).split())
        found.append((match.start(), _LEAD_CANON.get(raw.lower(), raw.title())))

    roles: list[str] = []
    for _, role in sorted(found):
        if role.lower() not in {r.lower() for r in roles}:
            roles.append(role)
    if not roles:
        roles = [infer_role_from_skills(skills if skills is not None else extract_skills(text, sections))]
    return roles[:MAX_ROLES]


# --------------------------------------------------------------------------- experience & seniority

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH_RE = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"


def _date_point(prefix: str) -> str:
    return (rf"(?:(?P<{prefix}_mon>{_MONTH_RE})\s+|(?P<{prefix}_num>0?[1-9]|1[0-2])[/.])?"
            rf"(?P<{prefix}_year>(?:19|20)\d{{2}})")


_RANGE_RE = re.compile(
    rf"{_date_point('s')}\s*(?:-|–|—|to|until|till)\s*"
    rf"(?:{_date_point('e')}|(?P<present>present|current|now|today|ongoing|date))",
    re.IGNORECASE,
)
_EXPLICIT_YEARS_RE = re.compile(
    r"(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)\.?(?:\s+of)?(?:\s+[\w.+#/-]+){0,4}?\s+(?:experience|expertise)",
    re.IGNORECASE,
)
_LEVEL_RANK = {"junior": 0, "mid": 1, "senior": 2, "staff": 3}


def _month_index(match: re.Match[str], prefix: str) -> int:
    month_name, month_num = match.group(f"{prefix}_mon"), match.group(f"{prefix}_num")
    if month_name:
        month = _MONTHS[month_name[:3].lower()]
    elif month_num:
        month = int(month_num)
    else:
        month = 1  # year-only dates: "2018 - 2021" counts as 3 years
    return int(match.group(f"{prefix}_year")) * 12 + month - 1


def estimate_years(text: str, sections: dict[str, str] | None = None, today: date | None = None) -> float | None:
    """Years of experience: the larger of an explicit "N+ years" claim and merged employment date ranges."""
    sections = split_sections(text) if sections is None else sections
    today = today or date.today()
    now_index = today.year * 12 + today.month - 1

    explicit = [float(m.group(1)) for m in _EXPLICIT_YEARS_RE.finditer(text)]
    explicit = [years for years in explicit if 0 < years <= 50]

    scope = sections.get("experience") or "\n".join(
        body for name, body in sections.items() if name not in {"education", "certifications", "projects"}
    )
    intervals: list[tuple[int, int]] = []
    for match in _RANGE_RE.finditer(scope):
        start = _month_index(match, "s")
        end = now_index if match.group("present") else _month_index(match, "e")
        end = min(end, now_index)
        if start < end <= start + 50 * 12:
            intervals.append((start, end))

    total_months = 0
    current_start = current_end = None
    for start, end in sorted(intervals):
        if current_end is None or start > current_end:
            if current_end is not None:
                total_months += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None:
        total_months += current_end - current_start

    candidates = []
    if explicit:
        candidates.append(max(explicit))
    if intervals:
        candidates.append(round(total_months / 12, 1))
    return max(candidates) if candidates else None


def seniority_from_years(years: float) -> str:
    if years < 2:
        return "junior"
    if years < 5:
        return "mid"
    if years < 9:
        return "senior"
    return "staff"


def seniority_from_title(title: str) -> str | None:
    lowered = title.lower()
    if re.search(r"\b(staff|principal|head of|cto|engineering manager)\b", lowered):
        return "staff"
    if re.search(r"\b(senior|lead)\b", lowered):
        return "senior"
    if re.search(r"\bmid-level\b", lowered):
        return "mid"
    if re.search(r"\bjunior\b", lowered):
        return "junior"
    return None


def estimate_seniority(years: float | None, roles: list[str]) -> str | None:
    levels = []
    if years is not None:
        levels.append(seniority_from_years(years))
    if roles and (title_level := seniority_from_title(roles[0])):
        levels.append(title_level)
    return max(levels, key=_LEVEL_RANK.__getitem__) if levels else None


# --------------------------------------------------------------------------- profile


def _truncate_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def build_summary_blob(
    roles: list[str], seniority: str | None, years: float | None, skills: list[str], summary_text: str
) -> str:
    """Dense, compact text for embedding; kept short to fit MiniLM's 256-token window."""
    parts = []
    if roles:
        parts.append(f"Target roles: {', '.join(roles[:3])}.")
    if seniority:
        experience = f" ({years:g} years of experience)" if years else ""
        parts.append(f"Seniority: {seniority}{experience}.")
    if skills:
        parts.append(f"Core skills: {', '.join(skills[:BLOB_MAX_SKILLS])}.")
    summary = _truncate_words(" ".join(summary_text.split()), SUMMARY_MAX_CHARS)
    if summary:
        parts.append(f"Summary: {summary}")
    return " ".join(parts)


def profile_from_text(text: str, today: date | None = None) -> CandidateProfile:
    sections = split_sections(text)
    skills = extract_skills(text, sections)
    roles = extract_roles(text, sections, skills)
    years = estimate_years(text, sections, today)
    seniority = estimate_seniority(years, roles)
    summary_text = sections.get("summary") or sections.get("experience", "")
    return CandidateProfile(
        skills=skills,
        roles=roles,
        seniority=seniority,
        years_experience=years,
        summary_blob=build_summary_blob(roles, seniority, years, skills, summary_text),
    )


def build_profile(pdf_bytes: bytes, today: date | None = None) -> CandidateProfile:
    return profile_from_text(extract_text(pdf_bytes), today)
