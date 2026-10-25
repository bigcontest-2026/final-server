"""Verify the published national I5 bundle, or explicitly rebuild a frozen input."""
import argparse
import json
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE / "backend"))
from app.config import Settings
from app.final_model import FinalModel, model_modules


def main():
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Explicit frozen V3 input to rebuild; omitted uses published national data")
    parser.add_argument("--output-root", type=Path, default=settings.model_runtime_root)
    args = parser.parse_args()
    if args.source is None:
        model = FinalModel(settings)
        catalog = model.regions()
        counts = {level: model.survey_area(level=level, limit=1)["total_matched"]
                  for level in ("sgg", "administrative_dong")}
        print(json.dumps({"bundle": str(model.bundle_path("survey")), "verified_rows": counts,
                          "geographic_scope": catalog["geographic_scope"], "regions": len(catalog["regions"])}, ensure_ascii=False))
        return
    _, survey, _, _ = model_modules(settings.model_root)
    bundle = survey.build(source=args.source, output_root=args.output_root)
    counts = {level: survey.query(bundle, level=level, limit=500)["total_matched"]
              for level in ("sgg", "administrative_dong")}
    print(json.dumps({"bundle": str(bundle), "verified_rows": counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
