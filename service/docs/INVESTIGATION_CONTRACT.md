# 실제 지역 조사 계약 1.0

연구 스냅샷 API와 합성 시연 API는 기존 계약을 유지한다. 조사 API는 `/api/investigations`에 별도로 연결한다. 개인 응답 원자료를 읽거나 모델에 전송하지 않고 공개 집계 스냅샷만 사용한다.

## 요청과 응답

`POST /api/investigations` 입력: `region_code`(10자리 정확 코드), `age_group`(`20대`/`30대`), `city`(`서울`/`춘천`), `purpose`, `measurement_as_of`(`YYYY-MM`), `support_as_of`(`YYYY-MM-DD`), `sk_mode`(`provided`/`excluded`/`quality_mutation`), `idempotency_key`(8~128자). 실제 스냅샷에서 도시·코드 조합을 확인한다. 반환은 사건 상세 전체다.

`GET /api/investigations`: `{schema_version,cases:[사건 상세]}`. `GET /api/investigations/{case_id}`는 사건 상세. 상세 필드: `case_id,case_version,request,status,measurement_status,support_status,current_brief_id,created_at,updated_at,executions,briefs,reviews,events,evidence,followups`.

`GET /api/investigations/catalog/search?query=청년&limit=20`은 실제 취득한 중앙부처 461행 CSV 목록의 검색이다. 원본 CSV hash 확인 후 ID·제목·요약·부처를 검색하며 모든 hit는 `verification_level=metadata_only`, `detail_verified=false`, `eligibility_status=unknown`이다. 공식 상세를 검증한 4개 후보와 섞어 자격을 확정하지 않는다.

`POST /{case_id}/runs`: `{strategy:rule_baseline|single_search|agent,expected_case_version,idempotency_key}`. `202` 반환 `{case_id,case_version,execution_id,status:queued}`. 앱 생명주기에서 단일 로컬 worker가 자동 시작하고 SQLite 작업큐를 처리한다. UI는 사건 상세를 다시 조회한다. 사건당 queued/running/interrupted 실행은 하나다.

브리프: `brief_id,brief_version,version,observations,support_candidates,excluded_candidates,open_questions,next_actions,evidence_ids,claims,limitations,measurement_status,support_status,strategy,model_called,verification,review_status`. 후보: `id,title,url,source_url,summary,contact,conditions,eligibility_status,evidence_ids,source_hash,currently_accepting_confirmed`. 조건 상태는 `match|no_match|unknown`. 출처 없는 후보는 제외하고 개인 이용자격은 확정하지 않는다.

`POST /{case_id}/reviews`: `{brief_id,brief_version,expected_case_version,decision:accept|reject|request_info,reason,reviewer_type:user|developer_verification,idempotency_key}`. 검토를 DB에 기록하고 **동일 그래프 thread**에 `Command(resume=...)`를 전달하는 작업을 저장한다. `202` 반환 `{case_id,case_version,review_id,execution_id,status:queued}`. 수락은 브리프 검토이며 실제 의뢰·기관 연결·참여가 아니다.

`POST /{case_id}/recheck`: `{expected_case_version,idempotency_key}`. `{case_id,case_version,changes,requires_new_run,recheck_id}`. 현재 구현은 **보관 원문과 현재 manifest의 로컬 재확인**이며 실시간 HTTP 재조회가 아니다. 변경/접근불가 시 stale과 영향받는 브리프 ID를 남긴다. 기존 지원안은 덮어쓰지 않고 새로운 runs 명령으로 새 버전을 생성한다.

`POST /{case_id}/resume`: `{execution_id,expected_case_version,idempotency_key}`. 실패 실행을 같은 graph thread의 checkpoint로 재개하는 작업을 저장한다. 키 연결·일시 오류 복구 뒤 사용하며 원자료 버전 충돌은 새 실행을 요구한다. 검토 중단은 reviews 명령이 실제 `Command(resume=...)`를 전달한다.

`POST /{case_id}/followups`: `{expected_case_version,status,evidence_ids,note,idempotency_key}`. 사건 근거 없는 후속은 거부한다. 현재 집계·공고 자료로 실제 연결·참여를 확인할 수 없어 connection_confirmed/participation_confirmed 입력은 거부하고 후속 자료 계약을 요구한다.

같은 명령 키·같은 입력은 기존 결과를 반환하고 같은 키·다른 입력 또는 버전 불일치는 `409`다. 클라이언트는 명령 실패 후 새 키로 무조건 반복하기 전에 사건을 다시 읽는다.

## 상태와 영속성

사건: draft → investigating → awaiting_review → reviewed/held/awaiting_info. 실행: queued → running → interrupted → queued(resume) → succeeded. 모델/자료 오류는 failed와 held. `events`는 서버 실제 단계, append-only. 업무DB는 사건·이벤트·불변 브리프·리뷰·후속·도구 결과·원문 버전을 저장한다. 별도 LangGraph SQLite 체크포인트 DB는 실행 상태와 interrupt를 저장한다. 작업 lease가 만료되면 동일 thread의 checkpoint로 이어가고 이미 저장된 도구 결과를 재사용한다.

`INVESTIGATION_DB_PATH`, `INVESTIGATION_CHECKPOINT_PATH`, `OFFICIAL_SUPPORT_MANIFEST`로 경로를 설정한다. 기본 DB는 `service/backend/runtime/`이며 Git 제외. `INVESTIGATION_WORKER_ENABLED=false`는 테스트에서 worker를 수동 처리할 때만 사용한다.

## 공식 캡처와 모델 연결

manifest `sources[]`: `id,title,url,retrieved_at,published_at,source_kind,capture_path,sha256,content_text_path,conditions,regions,age_min,age_max,period,contact,summary,rights_note`. 캡처 경로는 service 루트 상대경로다. 원문 bytes hash 확인, 로컬 추출문 읽기 성공 이후 지원 후보를 사용한다. 외부 모델에는 manifest의 구조화 조건·짧은 인용과 공개 집계만 전달한다. 모델이 임의 파일·URL을 읽거나 외부 행동을 하는 도구는 없다.

원문은 브라우저 공개 폴더가 아닌 `backend/evidence/support`에 보관한다. 상세 도구는 raw hash와 `content_text_sha256`, 모든 조건의 짧은 인용이 추출문에 존재하는지 검사한다. 지원 근거에는 원문·추출문·구조화 조건 hash를 각각 저장하고 재확인에서 변경된 종류와 영향을 받는 브리프를 반환한다. 변경되거나 접근할 수 없는 자료로 이전 브리프를 수락할 수 없다.

모델은 `OPENAI_API_KEY`와 `DONGHAENG_MODEL`을 설정해야 실제 [Responses function calling](https://developers.openai.com/api/docs/guides/function-calling)을 실행한다. `/readiness`는 키 값 없이 존재 여부를 반환한다. 실제 호출 성공만 `actual_api_success=true`. 인증키 부재는 `model_key_missing`으로 실패한다. 모델 호출 5회, 도구 12회, 검색 2회, 모델 HTTP 연결5초/읽기20초, 에이전트 루프90초 예산이다.

규칙 기준선과 검색1회 기준선은 같은 원문·측정·조건 검사기를 사용한다. 검색1회는 목록 검색을 한 번 하고 상세·조건을 코드로 확인한 후 같은 모델을 한 번 호출해 요약한다. 모델 키가 없으면 검색1회와 agent 모두 명시적으로 실패하며 성능 비교에서 제외한다. B5의 실제 모델 비교는 연결과 실제 실행 후에만 완료 처리한다.

SK excluded는 수치·insight·SK 도구를 완전히 제외한다. quality_mutation은 요청 선택월 한 관측을 누락 처리하며 원본 파일을 수정하지 않는다. 캐시는 execution+step+입력 hash로 분리된다. 설문 자치구×20대/30대, 행동 동×20~39세, SK 고정거점×20대/30대는 서로 합성하지 않는다.

설문 공개 조건은 source quality=adequate_descriptive_sample, n≥30, Kish n_eff≥20이며 기준 미달이면 점수·구간·구성점수를 도구 출력에서 null로 두고 표본·품질 사유를 유지한다. SK에는 7/8월 같은 거점 기준평균 `baseline_numeric`, 기준월·거점 포함률을 별도로 반환한다. 선택월 절대량을 변화율 분모로 부르지 않는다. 10% 변화·기준평균1000 미만은 추가 확인을 정하는 개발 규칙이며 검증된 고립 위험 기준이 아니다. 7월을 선택할 때 기준평균에 8월이 포함된 회고 관측임을 명시한다.

재확인은 공식 캡처뿐 아니라 연구 스냅샷 SHA 변경·접근 실패도 기록하고 measurement_status=stale과 영향을 받은 브리프를 남긴다. 브리프 수락 전 같은 로컬 재확인을 수행한다. 외부 웹 최신성 확인과 별도다.
