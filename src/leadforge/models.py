from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class VerificationMethod(str, Enum):
    SMTP = "smtp"
    API = "api"
    BROWSER = "browser"
    PATTERN_ONLY = "pattern_only"
    UNVERIFIED = "unverified"


class VerificationVerdict(str, Enum):
    SEND = "send"
    HOLD = "hold"
    KILL = "kill"
    UNKNOWN = "unknown"


class Contact(BaseModel):
    first_name: str
    last_name: str
    domain: str
    company: Optional[str] = None
    title: Optional[str] = None
    linkedin_url: Optional[str] = None
    source: Optional[str] = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class EmailCandidate(BaseModel):
    email: str
    contact: Contact
    pattern: Optional[str] = None
    source: str
    found_on_web: bool = False
    catch_all_domain: Optional[bool] = None
    verification_method: VerificationMethod = VerificationMethod.UNVERIFIED
    verification_verdict: VerificationVerdict = VerificationVerdict.UNKNOWN
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    notes: Optional[str] = None


class EnrichmentResult(BaseModel):
    contact: Contact
    best_email: Optional[EmailCandidate] = None
    all_candidates: list[EmailCandidate] = Field(default_factory=list)
    enriched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
