from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class VerificationMethod(str, Enum):  # noqa: UP042 - preserve str(Enum) behavior
    SMTP = "smtp"
    API = "api"
    BROWSER = "browser"
    PATTERN_ONLY = "pattern_only"
    UNVERIFIED = "unverified"


class VerificationVerdict(str, Enum):  # noqa: UP042 - preserve str(Enum) behavior
    SEND = "send"
    HOLD = "hold"
    KILL = "kill"
    UNKNOWN = "unknown"


class Contact(BaseModel):
    first_name: str
    last_name: str
    domain: str
    company: str | None = None
    title: str | None = None
    linkedin_url: str | None = None
    source: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class EmailCandidate(BaseModel):
    email: str
    contact: Contact
    pattern: str | None = None
    source: str
    found_on_web: bool = False
    catch_all_domain: bool | None = None
    verification_method: VerificationMethod = VerificationMethod.UNVERIFIED
    verification_verdict: VerificationVerdict = VerificationVerdict.UNKNOWN
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    notes: str | None = None


class EnrichmentResult(BaseModel):
    contact: Contact
    best_email: EmailCandidate | None = None
    all_candidates: list[EmailCandidate] = Field(default_factory=list)
    enriched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
