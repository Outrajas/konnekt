from typing import Literal

from pydantic import BaseModel, Field

RoleClass = Literal["recruiter", "eng_manager", "founder", "engineer", "other"]
PageKind = Literal[
    "team", "contact", "personal_site", "profile", "blog_post", "talk", "careers", "other"
]
NextExpected = Literal["email", "bio", "talks", "repos", "contact_page", "team_list"]


class PersonMention(BaseModel):
    name: str
    title: str | None = None
    role_class: RoleClass = "other"
    emails_published: list[str] = Field(default_factory=list)
    profile_links: list[str] = Field(default_factory=list)
    professional: bool


class NextLink(BaseModel):
    # candidate_id is the only link selector exposed to the model; url is filled by validation.
    candidate_id: int | None = None
    url: str | None = None
    reason: str
    expected: NextExpected


class PageExtraction(BaseModel):
    page_kind: PageKind
    people: list[PersonMention] = Field(default_factory=list)
    org_emails: list[str] = Field(default_factory=list)
    next_links: list[NextLink] = Field(default_factory=list)
    pattern_hint: str | None = None


class PingResponse(BaseModel):
    ok: bool
