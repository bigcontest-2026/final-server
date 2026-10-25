from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

class SourceModel(BaseModel):
    model_config = ConfigDict(extra="allow")

AgeGroup = Literal["20대", "30대"]
Index = float | None


class SurveyRecord(SourceModel):
    district: str = Field(min_length=1)
    age_group: AgeGroup
    score: Index = Field(ge=0, le=100)
    ci_low: Index = Field(ge=0, le=100)
    ci_high: Index = Field(ge=0, le=100)
    support_absence: Index = Field(ge=0, le=100)
    loneliness: Index = Field(ge=0, le=100)
    n: int = Field(ge=0)
    n_eff: float | None = Field(ge=0)
    quality: str = Field(min_length=1)
    notes: list[str]

    @model_validator(mode="after")
    def consistent_measurement(self):
        if self.ci_low is not None and self.ci_high is not None and self.ci_low > self.ci_high:
            raise ValueError("confidence interval endpoints must be ordered")
        if self.score is None and (self.ci_low is not None or self.ci_high is not None):
            raise ValueError("missing survey score must not have confidence interval")
        if self.score is not None:
            if self.support_absence is None or self.loneliness is None:
                raise ValueError("published index requires both survey components")
            if abs(self.score - (self.support_absence + self.loneliness) / 2) > 0.11:
                raise ValueError("index must be the normative 50/50 survey construction")
        return self


class Survey(SourceModel):
    year: int
    area_level: Literal["district"]
    population: str
    formula: str
    ci_method: str | None = None
    records: list[SurveyRecord] = Field(min_length=1)


class MobilityRecord(SourceModel):
    city: str
    district: str | None
    dong_code: str = Field(pattern=r"^\d{10}$")
    dong_name: str
    age_group: AgeGroup
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    flow_total: float | None = Field(ge=0)
    change_pct: float | None
    quality: str
    notes: list[str]

    @model_validator(mode="after")
    def missing_flow_is_not_change(self):
        if self.flow_total is None and self.change_pct is not None:
            raise ValueError("missing flow must not produce a change percentage")
        return self


class Mobility(SourceModel):
    area_level: Literal["dong"]
    unit: str = "제공 유동량 합계"
    periods: list[str]
    records: list[MobilityRecord]


class BehaviorDimensions(SourceModel):
    economic_instability: Index = Field(ge=0, le=100)
    social_activity: Index = Field(ge=0, le=100)
    isolated_lifestyle: Index = Field(ge=0, le=100)
    social_relations: Index = Field(ge=0, le=100)


class BehaviorRecord(SourceModel):
    city: Literal["서울"]
    district: str = Field(min_length=1)
    dong_code: str = Field(pattern=r"^\d{10}$")
    dong_name: str = Field(min_length=1)
    age_group: Literal["20~39세"]
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    score: Index = Field(ge=0, le=100)
    dimensions: BehaviorDimensions
    quality: str = Field(min_length=1)
    notes: list[str]

    @model_validator(mode="after")
    def published_score_requires_all_dimensions(self):
        if self.score is not None and any(
            getattr(self.dimensions, dimension) is None for dimension in (
                "economic_instability", "social_activity", "isolated_lifestyle", "social_relations"
            )
        ):
            raise ValueError("published behavior score requires four observed dimensions")
        return self


class PaperBehaviorIndex(SourceModel):
    area_level: Literal["dong"]
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    population: str = Field(min_length=1)
    method: str = Field(min_length=1)
    normalization: str = Field(min_length=1)
    source_url: str | None = None
    report_url: str | None = None
    full_paper_reproduction: Literal[False] = False
    records: list[BehaviorRecord]


class Source(SourceModel):
    title: str
    url: str | None = None
    path: str | None = None
    description: str | None = None


class ResearchSnapshot(SourceModel):
    schema_version: Literal["research-1.0"]
    mode: Literal["research"]
    as_of: datetime
    disclaimer: str = Field(min_length=1)
    survey: Survey
    mobility: Mobility
    paper_behavior_index: PaperBehaviorIndex | None = None
    sources: list[Source]
    limitations: list[str]

    @model_validator(mode="after")
    def unique_and_compatible_keys(self):
        survey_keys = [(row.district, row.age_group) for row in self.survey.records]
        mobility_keys = [(row.city, row.dong_code, row.age_group, row.period) for row in self.mobility.records]
        if len(survey_keys) != len(set(survey_keys)) or len(mobility_keys) != len(set(mobility_keys)):
            raise ValueError("research measurement keys must be unique")
        if any(row.period not in self.mobility.periods for row in self.mobility.records):
            raise ValueError("mobility periods must be declared")
        if self.paper_behavior_index is not None:
            behavior_keys = [(row.city, row.dong_code, row.age_group, row.period)
                             for row in self.paper_behavior_index.records]
            if len(behavior_keys) != len(set(behavior_keys)):
                raise ValueError("behavior measurement keys must be unique")
            if any(row.period != self.paper_behavior_index.period for row in self.paper_behavior_index.records):
                raise ValueError("behavior observations must match the declared reference period")
        return self


def load_research_snapshot(path: Path) -> ResearchSnapshot:
    return ResearchSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
