"""Research investigations: separate from the synthetic RunResult contract."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InvestigationRequest(StrictModel):
    region_code: str = Field(pattern=r"^\d{10}$")
    age_group: Literal["20대", "30대"]
    city: str = Field(min_length=1, max_length=60)
    purpose: str = Field(min_length=1, max_length=500)
    measurement_as_of: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    support_as_of: date
    sk_mode: Literal["provided", "excluded", "quality_mutation"] = "provided"
    idempotency_key: str = Field(min_length=8, max_length=128)


class RunCommand(StrictModel):
    strategy: Literal["rule_baseline", "single_search", "agent"] = "rule_baseline"
    expected_case_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class ReviewCommand(StrictModel):
    brief_id: str
    brief_version: int = Field(ge=1)
    expected_case_version: int = Field(ge=1)
    decision: Literal["accept", "reject", "request_info"]
    reason: str = Field(min_length=1, max_length=2000)
    reviewer_type: Literal["user", "developer_verification"] = "user"
    idempotency_key: str = Field(min_length=8, max_length=128)


class RecheckCommand(StrictModel):
    expected_case_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class RetryCommand(StrictModel):
    execution_id: str
    expected_case_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class FollowupCommand(StrictModel):
    expected_case_version: int = Field(ge=1)
    status: Literal["referral_reported", "connection_confirmed", "participation_confirmed", "closed"]
    evidence_ids: list[str] = Field(min_length=1)
    note: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=128)
