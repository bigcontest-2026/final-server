from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Scenario = Literal["normal", "risk_spike", "missing_data", "expired_policy", "no_policy"]
RiskLevel = Literal["low", "watch", "high", "unknown"]


class SourceModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class Signal(SourceModel):
    key: str
    label: str
    value: float | None
    unit: str
    change_pct: float | None = None


class District(SourceModel):
    code: str = Field(min_length=1)
    name: str
    district: str
    risk_score: float | None = Field(ge=0, le=100)
    risk_level: RiskLevel
    previous_score: float | None = Field(default=None, ge=0, le=100)
    quality: str
    population: int | None = Field(default=None, ge=0)
    signals: list[Signal]
    trend: list[dict]
    summary: str


class Policy(SourceModel):
    id: str
    title: str
    districts: list[str]
    target: str
    tags: list[str]
    description: str
    start_date: date
    end_date: date
    source_url: str | None = None
    is_mock: Literal[True]
    contact: str

    @model_validator(mode="after")
    def valid_period(self):
        if self.start_date > self.end_date:
            raise ValueError("policy start_date must not follow end_date")
        return self


class Dataset(SourceModel):
    schema_version: Literal["1.0"]
    mode: Literal["mock"]
    as_of: datetime
    disclaimer: str = Field(min_length=1)
    districts: list[District]
    policies: list[Policy]

    @model_validator(mode="after")
    def unique_identifiers(self):
        for values in ([d.code for d in self.districts], [p.id for p in self.policies]):
            if len(values) != len(set(values)):
                raise ValueError("dataset identifiers must be unique")
        return self


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    district_code: str = Field(min_length=1, max_length=64)
    scenario: Scenario = "normal"


class Step(BaseModel):
    id: str
    label: str
    status: Literal["completed", "held"]
    detail: str


class RunResult(BaseModel):
    run_id: str
    mode: Literal["mock"] = "mock"
    district_code: str
    scenario: Scenario
    status: Literal["completed", "held"]
    risk_score: float | None
    risk_level: RiskLevel
    summary: str
    policies: list[Policy]
    warnings: list[str]
    steps: list[Step]
    completed_at: datetime
