"""Validate and gate Customer QA datasets before they become golden data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ci.generate_customer_qa_dataset import validate_cases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--require-reviewed", action="store_true")
    args = parser.parse_args()
    cases = json.loads(args.dataset.read_text())
    errors = validate_cases(cases, require_reviewed=args.require_reviewed)
    result = {"passed": not errors, "cases": len(cases), "errors": errors}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
