"""Pydantic models: the candidate profile, blueprints, jobs and application state."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Localized = Union[str, Dict[str, str]]


class Strict(BaseModel):
    """Profile/blueprint files are hand-edited: unknown keys are almost always typos -> report them."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _blank_means_default(cls, data: Any) -> Any:
        """`phone:` left empty in YAML (null) should simply mean 'not set'."""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None}
        return data


# =========================================================================== profile
class Bullet(Strict):
    id: str = ""
    text: Localized
    tags: List[str] = Field(default_factory=list)


def _coerce_bullets(v: Any) -> Any:
    if v is None:
        return []
    out = []
    for b in v:
        out.append({"text": b} if isinstance(b, str) else b)
    return out


class Identity(Strict):
    name: str
    title: Localized = ""  # e.g. "Software engineering student"
    email: str = ""
    phone: str = ""
    location: str = ""
    links: Dict[str, str] = Field(default_factory=dict)  # linkedin / github / website ...
    gender: str = ""  # optional: "f" | "m" - only used if your own fixed texts need agreement


class Availability(Strict):
    start_date: date
    start_flex_days: int = 45
    min_months: int = 4
    max_months: int = 6
    internship_label: Localized = Field(
        default_factory=lambda: {"en": "end-of-studies internship", "fr": "stage de fin d'études"}
    )
    programme: Localized = ""  # "my final year of the engineering degree at ..."
    note: Localized = ""  # extra logistics sentence (internship agreement, mobility ...)
    target_countries: List[str] = Field(default_factory=lambda: ["France", "Germany"])


class Education(Strict):
    id: str = ""
    school: str
    degree: Localized
    period: str = ""
    location: str = ""
    details: List[Localized] = Field(default_factory=list)


class Experience(Strict):
    id: str = ""
    org: str
    role: Localized
    period: str = ""
    location: str = ""
    tags: List[str] = Field(default_factory=list)
    bullets: List[Bullet] = Field(default_factory=list)

    _b = field_validator("bullets", mode="before")(_coerce_bullets)


class Project(Strict):
    id: str = ""
    name: str
    tagline: Localized = ""
    stack: List[str] = Field(default_factory=list)
    period: str = ""
    link: str = ""
    tags: List[str] = Field(default_factory=list)
    bullets: List[Bullet] = Field(default_factory=list)

    _b = field_validator("bullets", mode="before")(_coerce_bullets)


class SkillGroup(Strict):
    category: Localized
    items: List[str]


class LanguageSkill(Strict):
    name: Localized
    level: Localized


class Profile(Strict):
    identity: Identity
    availability: Availability
    education: List[Education] = Field(default_factory=list)
    experience: List[Experience] = Field(default_factory=list)
    projects: List[Project] = Field(default_factory=list)
    skills: List[SkillGroup] = Field(default_factory=list)
    languages: List[LanguageSkill] = Field(default_factory=list)
    certifications: List[Localized] = Field(default_factory=list)
    interests: List[Localized] = Field(default_factory=list)
    example: bool = False  # set to false (or delete) once you've replaced the sample data

    def finalize(self) -> "Profile":
        """Fill in missing ids and make sure they are unique."""
        seen: set[str] = set()

        def uniq(base: str) -> str:
            cand, i = base, 2
            while cand in seen:
                cand, i = f"{base}-{i}", i + 1
            seen.add(cand)
            return cand

        for i, e in enumerate(self.education, 1):
            e.id = uniq(e.id or f"edu-{i}")
        for i, x in enumerate(self.experience, 1):
            x.id = uniq(x.id or f"exp-{i}")
            for j, b in enumerate(x.bullets, 1):
                b.id = uniq(b.id or f"{x.id}-b{j}")
        for i, p in enumerate(self.projects, 1):
            p.id = uniq(p.id or f"proj-{i}")
            for j, b in enumerate(p.bullets, 1):
                b.id = uniq(b.id or f"{p.id}-b{j}")
        return self


# =========================================================================== blueprints
class ParagraphSpec(Strict):
    id: str
    kind: Literal["generated", "fixed"] = "generated"
    label: Localized = ""
    goal: Localized = ""  # instruction to the model (generated)
    example: Localized = ""  # style example (generated) - facts in it must NOT be reused
    text: Localized = ""  # fixed text with {placeholders}
    max_words: int = 90


class Blueprint(Strict):
    name: str = "Blueprint"
    kind: Literal["letter", "email"] = "letter"
    tone: Localized = ""
    avoid: Dict[str, List[str]] = Field(default_factory=dict)  # lang -> phrases to avoid
    subject: Localized = ""
    subject_no_role: Localized = ""
    salutations: Dict[str, Dict[str, str]] = Field(default_factory=dict)  # lang -> style -> text
    closing: Localized = ""
    signature_lines: List[str] = Field(default_factory=list)  # e.g. ["{name}", "{phone} | {email}"]
    paragraphs: List[ParagraphSpec]


# =========================================================================== job & analysis
class Job(BaseModel):
    url: str = ""
    source: str = "pasted"  # jsonld | html | browser | pasted | bookmarklet
    platform: str = ""
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    language: str = "unknown"
    employment_type: str = ""
    date_posted: str = ""
    valid_through: str = ""
    company_url: str = ""
    notes: List[str] = Field(default_factory=list)


class JobAnalysis(BaseModel):
    role_title: str = ""
    company_name: str = ""
    mission: str = ""
    must_have: List[str] = Field(default_factory=list)
    nice_to_have: List[str] = Field(default_factory=list)
    domains: List[str] = Field(default_factory=list)
    company_description: str = ""
    terms: Dict[str, int] = Field(default_factory=dict)  # canonical tech term -> occurrences
    stack: List[str] = Field(default_factory=list)  # display names, most frequent first


class FitItem(BaseModel):
    key: str
    status: Literal["ok", "warn", "bad", "info", "unknown"]
    label: str
    detail: str = ""


class CompanyFact(BaseModel):
    fact: str
    quote: str
    source: str = ""


class Flag(BaseModel):
    level: Literal["error", "warn", "info"]
    code: str
    message: str


class Evidence(BaseModel):
    id: str
    kind: str  # project | experience
    title: str
    subtitle: str = ""
    period: str = ""
    stack: List[str] = Field(default_factory=list)
    bullets: List[str] = Field(default_factory=list)
    score: float = 0.0
    matched: List[str] = Field(default_factory=list)


# =========================================================================== drafts
class ParagraphDraft(BaseModel):
    id: str
    kind: str = "generated"
    label: str = ""
    text: str = ""
    max_words: int = 90
    flags: List[Flag] = Field(default_factory=list)
    edited: bool = False


class DocDraft(BaseModel):
    kind: Literal["letter", "email"] = "letter"
    subject: str = ""
    salutation: str = ""
    paragraphs: List[ParagraphDraft] = Field(default_factory=list)
    closing: str = ""
    signature: List[str] = Field(default_factory=list)
    recipient: List[str] = Field(default_factory=list)
    place_date: str = ""


class CVPlan(BaseModel):
    lang: str = "en"
    headline: str = ""
    project_ids: List[str] = Field(default_factory=list)  # selected, in display order
    experience_ids: List[str] = Field(default_factory=list)
    bullet_ids: Dict[str, List[str]] = Field(default_factory=dict)  # item id -> ordered bullet ids
    skills: List[Dict[str, Any]] = Field(default_factory=list)  # [{category, items}] reordered
    scores: Dict[str, float] = Field(default_factory=dict)
    matched: Dict[str, List[str]] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)
    extra_keywords: List[str] = Field(default_factory=list)  # keywords added to the skills block (tailor CV)


class ApplicationState(BaseModel):
    id: str
    kind: Literal["job", "spontaneous", "cv"] = "job"
    created_at: str = ""
    updated_at: str = ""
    status: str = "drafted"  # drafted | sent | replied | interview | offer | rejected | withdrawn
    lang: str = "en"
    company: str = ""
    role: str = ""
    url: str = ""
    contact: Dict[str, str] = Field(default_factory=dict)
    note: str = ""
    job: Optional[Job] = None
    analysis: Optional[JobAnalysis] = None
    fit: List[FitItem] = Field(default_factory=list)
    facts: List[CompanyFact] = Field(default_factory=list)
    evidence: List[Evidence] = Field(default_factory=list)
    doc: Optional[DocDraft] = None
    cv_plan: Optional[CVPlan] = None
    target: Dict[str, Any] = Field(default_factory=dict)  # spontaneous: domains, role text, company_url
    files: Dict[str, str] = Field(default_factory=dict)
    messages: List[str] = Field(default_factory=list)
    model: str = ""
    sent_at: str = ""
    follow_up_on: str = ""
    notes_log: List[str] = Field(default_factory=list)
