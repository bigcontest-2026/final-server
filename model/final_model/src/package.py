"""Package generated evidence and query code for internal agent handoff."""
import argparse,json
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED
from .common import dump,sha,verify_bundle
from .pipeline import FINAL,stage

def package(bundle):
    bundle=Path(bundle).resolve()
    manifest=verify_bundle(bundle)
    # Capture the current query/schema implementation; preserve original execution hashes.
    manifest["handoff_code_sha256"]={p.relative_to(FINAL).as_posix():sha(p) for folder in ("src","schemas") for p in sorted((FINAL/folder).rglob("*")) if p.is_file() and "__pycache__" not in str(p)}
    dump(bundle/"manifest.json",manifest)
    verify_bundle(bundle)
    target=FINAL/"outputs"/("agent_handoff_"+manifest["run_id"]+".zip")
    with ZipFile(target,"w",compression=ZIP_DEFLATED,compresslevel=6) as z:
        for p in sorted(bundle.rglob("*")):
            if p.is_file():z.write(p,"final_model/outputs/"+manifest["run_id"]+"/"+p.relative_to(bundle).as_posix())
        for folder in ("src","schemas","reports"):
            for p in sorted((FINAL/folder).rglob("*")):
                if p.is_file() and "__pycache__" not in str(p) and p.name != "tests.xml":
                    z.write(p,"final_model/"+p.relative_to(FINAL).as_posix())
        for name in ("__init__.py","README.md","requirements.lock.txt","configs/example.json"):
            z.write(FINAL/name,"final_model/"+name)
    with ZipFile(target) as z:
        bad=z.testzip()
        if bad:raise ValueError("archive_crc_error:"+bad)
    info={"run_id":manifest["run_id"],"zip":target.name,"sha256":sha(target),"bytes":target.stat().st_size,
          "raw_inputs_included":False,"local_config_included":False,"failed_runs_included":False,
          "actual_aggregate_evidence_included":True,"upload_performed":False,"agent_service_live_integration":False}
    dump(target.with_suffix(".manifest.json"),info)
    stage(7,f"팀 내부 인계 ZIP 생성·CRC/해시 검증: {target.name}. 원자료·실패 실행·로컬 설정 제외.")
    print(json.dumps(info,ensure_ascii=False))
    return target

def main():
    p=argparse.ArgumentParser();p.add_argument("--bundle");args=p.parse_args()
    bundle=Path(args.bundle) if args.bundle else FINAL/"outputs"/json.loads((FINAL/"outputs/latest.json").read_text(encoding="utf-8"))["run_id"]
    package(bundle)
if __name__=="__main__":main()
