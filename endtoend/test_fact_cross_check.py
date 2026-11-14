# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The output boundary in classify.py: every extracted number must be in the record.

Offline. The correct extractions below are what the model returned for each
ground-truth case, so a check that blocks them would send every legitimate
case to a human. The wrong extractions are the failure this boundary exists to
catch: an altered figure, an invented one, and a value that is not a number.
"""

import ast
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent

# classify.py reads these at import and creates clients, but makes no call.
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("BEDROCK_MODEL_ID", "test")
os.environ.setdefault("GUARDRAIL_ID", "test")
os.environ.setdefault("GUARDRAIL_VERSION", "DRAFT")
sys.path.insert(0, str(HERE / "lambdas"))

from classify import ungrounded_numbers  # noqa: E402


def load_cases():
    """Read CASES from the evaluation without importing it, because importing
    it checks a live model and this suite must run offline."""
    tree = ast.parse((REPO / "evaluation" / "measure_reliability.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "CASES" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError("CASES not found in measure_reliability.py")


RECORDS = {case["id"]: case["record"] for case in load_cases()}


def facts(income=None, household=None, rent=None, months=None, requested=None):
    return {
        "monthly_income": income,
        "household_size": household,
        "monthly_rent": rent,
        "months_requested": months,
        "requested_amount": requested,
    }


CORRECT = [
    ("C01", facts(1800, 3, 1150)),
    ("C02", facts(None, 4)),
    ("C03", facts(900, 2, 6200, 12)),
    ("C05", facts(1600, 2)),
    ("C06", facts()),
    ("C07", facts(700, 5, requested=51000)),
    ("C01", facts("1,800", 3, 1150)),
]
WRONG = [
    ("C01", facts(900, 3, 1150), ["monthly_income"]),
    ("C01", facts(1800, 7, 4200), ["household_size", "monthly_rent"]),
    ("C02", facts(2100, 4), ["monthly_income"]),
    ("C01", facts(1800, True, 1150), ["household_size"]),
    ("C01", facts("unknown", 3, 1150), ["monthly_income"]),
]

failures = []


def check(ok, label, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"        {detail}")
        failures.append(label)


def main():
    for case_id, extracted in CORRECT:
        found = ungrounded_numbers(extracted, RECORDS[case_id])
        check(not found, f"{case_id}: a correct extraction passes", f"flagged {found}")
    for case_id, extracted, expected in WRONG:
        found = ungrounded_numbers(extracted, RECORDS[case_id])
        check(found == expected, f"{case_id}: a wrong extraction is caught ({expected})",
              f"flagged {found}")
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
