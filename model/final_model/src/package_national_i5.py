"""Package the verified nationwide I5 values and reviewable three-repository changes."""
import json
import subprocess
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED

from .common import dump,sha
from .survey_area_index import query

FINAL=Path(__file__).resolve().parents[1]
WORKSPACE=FINAL.parents[1]


def git(repo,*args):
    return subprocess.check_output(["git","-c",f"safe.directory={repo.as_posix()}","-C",str(repo),*args])


def package():
    pointer=FINAL/"outputs/survey_area_index_latest.json"
    bundle=pointer.parent/json.loads(pointer.read_text(encoding="utf-8"))["run_id"]
    if query(bundle)["geographic_scope"]!="nationwide":raise ValueError("national_I5_bundle_required")
    target=FINAL/"outputs"/("national_i5_agent_handoff_"+bundle.name+".zip")
    instructions="""전국 I5 실제 값 + agent/service 연결 인계

1. model/final_model/outputs/survey_area_index_*/ 디렉터리가 실제 전국 I5 번들입니다.
2. model/agent/service 변경 파일과 changes/*.patch를 포함합니다. BASE_COMMITS.json과 현재 팀 버전을 비교한 후 반영하세요.
3. model wheel은 dist/에 있습니다. 기존 동일 버전이 설치돼 있다면 pip install --force-reinstall --no-deps <wheel>로 교체하세요. 서비스의 의존성 설치가 별도로 필요합니다.
4. 서버 환경 I5_BUNDLE_PATH에 압축 해제한 I5 번들의 절대 경로를 지정합니다. 기존 RESEARCH_PATH는 유지합니다.
5. APP_MODE=research에서 GET /api/research/i5?region_code=26110&month=202512 로 조회합니다.
6. 실제 조사 measurements/graph/brief에는 survey_i5_population_reference라는 별도 근거로 전달됩니다.
7. I5는 20세 이상 인구구성 가중 참고지수입니다. 지역 설문 실측/고립률/개인 확률 또는 20대/30대 전용 점수가 아닙니다.
8. hold 및 operational_alert_allowed=false를 유지합니다. 서울 417개 통신 행동지수와 합산하지 않습니다.
9. 로컬 HTTP+실제 LangGraph 경로를 검증했습니다. 외부 LLM 호출·원격 운영 배포를 수행한 인계가 아닙니다.
10. 공식 원표에 인구가 0인 읍면동·월 16개는 0점 대신 미산출 사유를 제공합니다. 상위 시와 하위 구의 인구를 중복 합산하지 마세요.
"""
    bases={}
    with ZipFile(target,"w",ZIP_DEFLATED,compresslevel=6) as archive:
        for path in sorted(bundle.iterdir()):
            if path.is_file():archive.write(path,path.relative_to(WORKSPACE).as_posix())
        archive.write(pointer,pointer.relative_to(WORKSPACE).as_posix())
        for name in ("model","agent","service"):
            repo=WORKSPACE/name
            bases[name]=git(repo,"rev-parse","HEAD").decode().strip()
            changed=set(git(repo,"diff","--name-only").decode().splitlines())
            changed.update(git(repo,"ls-files","--others","--exclude-standard").decode().splitlines())
            for relative in sorted(changed):
                path=repo/relative
                if path.is_file():archive.write(path,name+"/"+relative)
            archive.writestr("changes/"+name+".patch",git(repo,"diff","--binary"))
        for wheel in (FINAL/"outputs/wheels").glob("*.whl"):archive.write(wheel,"dist/"+wheel.name)
        for path in (FINAL/"outputs").glob("national_i5*test_results.xml"):archive.write(path,"validation/"+path.name)
        archive.write(WORKSPACE/"data-set/mois_national/download_manifest.json","validation/official_download_manifest.json")
        archive.writestr("BASE_COMMITS.json",json.dumps(bases,indent=2))
        archive.writestr("README.txt",instructions)
    with ZipFile(target) as archive:
        if archive.testzip():raise ValueError("national_handoff_CRC_failed")
        manifest=json.loads(archive.read(bundle.relative_to(WORKSPACE).as_posix()+"/manifest.json"))
        import hashlib
        for name,digest in manifest["outputs"].items():
            if hashlib.sha256(archive.read(bundle.relative_to(WORKSPACE).as_posix()+"/"+name)).hexdigest()!=digest:
                raise ValueError("national_handoff_hash_mismatch:"+name)
    result=dict(zip=target.name,bytes=target.stat().st_size,sha256=sha(target),national_bundle=bundle.name,
        source_base_commits=bases,raw_official_csv_included=False,upload_performed=False)
    dump(target.with_suffix('.manifest.json'),result)
    print(json.dumps(result),flush=True)
    return target


if __name__=="__main__":package()
