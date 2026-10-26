"""Optional review fields shared by manual editing and AI suggestions."""
from copy import deepcopy
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

PLANNING_FIELDS = {
    'goal': '목적·목표', 'alternatives': '대안 비교·선택 이유',
    'service_function': '지원 기능', 'service_scope': '지역·집단 범위',
    'selection_requirements': '개인 선정 요건', 'availability': '운영·공급 가용성',
    'access_and_cost': '신청·문의 경로·이용자 부담', 'execution': '단계별 실행·인계',
    'scale': '수요·제공 예정 규모', 'budget': '비용 산출', 'funding': '재원 확보 상태',
    'evaluation': '성과 측정·점검 계획', 'decisions': '확인·결정 요청', 'sources': '출처·기준시점·갱신',
    'institutional_conditions': '제도·협의·실행 조건', 'resident_feedback': '주민·현장 의견 반영',
}
A_FIELDS = {'service_function', 'service_scope', 'selection_requirements', 'availability', 'access_and_cost'}
B_FIELDS = {'alternatives', 'scale'}


def applicable_fields(kind):
    return [key for key in PLANNING_FIELDS if key not in (B_FIELDS if kind == 'existing_program' else A_FIELDS)]


class PlanningEntry(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    status: Literal['unconfirmed', 'confirmed', 'not_applicable'] = 'unconfirmed'
    text: str = Field(default='', max_length=4000)
    evidence: str = Field(default='', max_length=2000)
    owner: str = Field(default='', max_length=300)
    next_action: str = Field(default='', max_length=2000)

    @model_validator(mode='after')
    def justified(self):
        if self.status == 'confirmed' and (not self.text.strip() or not self.evidence.strip()):
            raise ValueError('확인한 항목에는 내용과 근거가 필요합니다.')
        if self.status == 'not_applicable' and not self.text.strip():
            raise ValueError('해당 없음에는 적용하지 않는 이유가 필요합니다.')
        return self


def normalize_planning(incoming=None, previous=None):
    result = deepcopy(previous or {})
    if incoming is None:
        return result
    if not isinstance(incoming, dict) or set(incoming) - set(PLANNING_FIELDS):
        raise ValueError('invalid_planning_details')
    for key, value in incoming.items():
        result[key] = PlanningEntry.model_validate(value).model_dump()
    return result
