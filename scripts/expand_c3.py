"""Recalculate adult-age C3 into a separate verified bundle; never publish implicitly."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "model"))

from final_model.src.c3_expansion import regenerate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=ROOT / "bundles/activity")
    parser.add_argument("--card-zip", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New output directory outside the base bundle")
    args = parser.parse_args()
    print(json.dumps(regenerate(args.base, args.card_zip, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
