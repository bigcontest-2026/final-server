# 동행 조사 에이전트

## 복지 추천 구현 진행

2026-10-25부터 [단계별 구현 README](docs/implementation/README.md)를 따라 매핑 코어 → 데이터/DB 어댑터 → 그래프 → 서비스 순으로 진행한다. 현재 Step 01 코어와 합성 시험 8개를 완료했으며 실제 DB·기존 조사 그래프 연결은 후속 단계다.

LangGraph 조사 흐름, 읽기 도구, Codex 구독·Responses 제공자, 업무 저장소, 중단·재개 worker를 담은 독립 Python 패키지다. FastAPI와 화면은 형제 `service/`, 연구 스냅샷 계약과 지수·학습 산출은 형제 `model/`이 소유한다.

기능·데이터 피처·아키텍처·입출력 계약·저장 구조·구현 제약은 [개발자 상세 명세](docs/AGENT_FEATURES_AND_ARCHITECTURE.md)를 참고한다.

향후 구현 순서는 [최신 에이전트 개발 계획](docs/AGENT_ENTERPRISE_PLAN.md)을 따른다. LangGraph 업무 흐름에 Codex 구독 실행과 Phoenix 추적·평가를 연결하고 기관 내부망 운영으로 확장한다. 구독 어댑터·서버 도구 루프·로컬 Phoenix 연동과 기관 인증 기반을 구현했다. 실제 실행과 남은 범위는 [구현·피드백 기록](docs/IMPLEMENTATION_FEEDBACK_20261021.md)에 구분한다.

## 전국 I5 참고지수 입력 (2026-10-17)

`InvestigationTools(..., i5_bundle=검증된_전국_I5_디렉터리)`로 별도 번들을 주입한다. `get_survey_area_isolation_index(region_code="26110", month="202512")`는 5자리 지역 참고지수, `dong_code="2611051000"` 지정은 동별 참고지수를 반환한다. 형제 model 패키지를 설치해야 한다.

서비스를 통해 실행하면 최신 `model/data/i5/national_2025/`에 Git으로 포함된 데이터를 기본 주입한다. `model`, `agent`, `service`를 같은 부모 폴더에 두면 `I5_BUNDLE_PATH`를 따로 설정할 필요가 없다. 직접 도구를 생성하는 코드는 위 `i5_bundle` 인자를 지정한다. 경로를 변경할 때만 서비스 환경변수를 사용하며 서버에는 업데이트·재시작이 필요하다.

실제 `measurements()`는 `survey_i5_population_reference`를 독립 observation/evidence로 추가한다. 정확한 동 코드·월·공식 지역명 조합을 검증한다. 기존 서울 417개 통신 행동지수·직접 설문 근거는 보존하며 I5와 합산하지 않는다. I5는 20세 이상 전체 인구구성 참고지수이며 요청자의 20대/30대 전용 점수가 아니다.

I5 manifest 해시는 execution 버전에 고정된다. 대기/실행 중 버전 변경이나 파일 무결성 오류를 거부하고 brief의 `quality_status=hold`, `operational_alert_allowed=false`를 유지한다. 미설정 시 기존 스냅샷 도구는 그대로 작동한다.

## 구조

- `src/donghaeng_agent/`: graph, tools, provider, repository, worker, contracts.
- `src/donghaeng_agent/mock_graph.py`, `mock_contracts.py`: 기존 합성 데모 흐름과 계약. 조사 에이전트 결과와 구분한다.
- `resources/support/`: 공식 지원 조건 manifest와 목록 검색 자료.
- `evidence/support/raw/`, `text/`: manifest 해시와 대조하는 로컬 원문·추출문. Git 제외다.
- `sources/`: 공식 취득·권리·검사 기록과 새 빈 폴더로 취득하는 도구. `official_raw/` 제외.
- `docs/`: 최신 에이전트 개발 계획, 현재 구현 상세, 출처 장부.
- `evaluation/`: 보존한 개발 평가 기록·프로토콜. 새 replay는 `evaluation/replays/`에 생성한다.
- `tests/`: 서비스 없이 실행하는 합성 그래프·자료 경로 검증.

## 설치 및 연결

`bigcontest-2026` 루트에서 Python 3.12 이상으로 실행한다.

```sh
python -m pip install -e ./model -e './agent[dev]'
```

서비스의 구성 코드는 다음 값을 주입한다.

```python
from donghaeng_agent.tools import InvestigationTools

tools = InvestigationTools(
    research_path=model_snapshot_path,
    support_path=agent_root / "resources/support/manifest.json",
    resource_root=agent_root,
    capture_root=agent_root / "evidence/support",
)
```

상대 원문 경로는 전달한 `resource_root`를 기준으로 해석한다. 읽기를 허용하는 위치는 manifest 디렉터리와 전달한 `capture_root`다. 패키지 설치 위치나 서비스 코드 경로로 원문 위치를 결정하지 않는다. 저장소와 체크포인트 경로 역시 서비스가 주입하며 로컬 DB를 Git에 넣지 않는다.

HTTP 요청·응답은 [서비스 API 계약](../service/docs/INVESTIGATION_CONTRACT.md)을 따른다. `DONGHAENG_PROVIDER=codex_subscription`이 기본이며 검증한 Codex CLI 0.163.0의 ChatGPT 로그인이 필요하다. API 호출은 `DONGHAENG_PROVIDER=openai_responses`를 명시할 때만 `OPENAI_API_KEY`·`DONGHAENG_MODEL`을 사용한다. 인증·설정 실패는 성공으로 대체하지 않는다. 기관 모드에서는 개인 구독 실행을 차단한다.

## 검증 및 평가

```sh
cd agent
python -m pytest -q
python evaluation/evaluate_donghaeng_workflow.py
```

평가 스크립트는 형제 서비스의 실제 FastAPI 경계를 사용한다. API 의존성도 설치되어 있어야 한다. 원문·추출문은 로컬에 있는 경우에만 공식 상세 조건을 재현할 수 있다. 단위 테스트는 패키지 이전과 경로 가드를 확인한다. 기존 평가 산출은 이전 실행의 근거이며 현재 폴더 구성의 새 실행으로 취급하지 않는다. 실제 외부 모델 호출·잠금 평가·현장 지원 효과를 검증한 기록은 아니다.

## 로컬 Phoenix와 실제 구독 검증

```sh
python3 -m venv agent/.venv
agent/.venv/bin/pip install -r service/backend/requirements-dev.txt -e ./model -e './agent[dev,observability,phoenix]' pandas==3.0.6
agent/.venv/bin/python agent/evaluation/serve_phoenix_local.py --port 6006
# 별도 터미널: 고유 iteration 이름을 사용한다. 실제 구독 한도를 사용한다.
agent/.venv/bin/python agent/evaluation/run_feedback_iteration.py --strategy single_search --iteration local-review-001 --publish
# 모델을 호출하지 않는 통합 검사
agent/.venv/bin/python service/scripts/check_backend_ci.py
```

`runtime/enterprise-feedback/`는 실제 실행·평가 근거, `evaluation/phoenix_regression_cases_v1_1.json`은 현재 합성 반례 12개이며 구별한다. Phoenix는 로컬 개발 전용이고 업무 DB·체크포인트와 분리한다. 자세한 설치·접근·보존·업무 DB 복원 절차는 [운영 문서](docs/PHOENIX_OPERATIONS.md)를 따른다.
