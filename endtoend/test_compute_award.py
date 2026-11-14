# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The award formula in code: pin every rule's result, then every edge case.

Offline. The evaluation imports this same formula, and this suite fails if
the evaluation defines its own copy.
"""

import ast
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent
EVALUATION = REPO / "evaluation" / "measure_reliability.py"

# compute_award.py reads AWS_REGION at import and creates a client, but makes
# no call.
os.environ.setdefault("AWS_REGION", "us-west-2")
sys.path.insert(0, str(HERE / "lambdas"))

from compute_award import STATUTORY_MAXIMUM, compute  # noqa: E402


def load_evaluation():
    """Read CASES from the evaluation without importing it, because importing
    it checks a live model. Also report whether it imports the deployed formula
    rather than defining its own copy."""
    tree = ast.parse(EVALUATION.read_text())
    cases = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", None) == "CASES" for t in n.targets))
    imports = any(isinstance(n, ast.ImportFrom) and n.module == "compute_award"
                  and any(a.name == "compute" for a in n.names) for n in tree.body)
    own_copy = any(isinstance(n, ast.FunctionDef) and n.name == "compute_award" for n in tree.body)
    return cases, imports and not own_copy


CASES, evaluation_uses_deployed_formula = load_evaluation()
EXPECTED = {case["id"]: case["expected_amount"] for case in CASES}


def facts(income=None, household=None, rent=None, months=None, requested=None, award_type=None):
    return {
        "monthly_income": income,
        "household_size": household,
        "monthly_rent": rent,
        "months_requested": months,
        "requested_amount": requested,
        "award_type": award_type,
    }


# Correct facts for each evaluation case, read from its record, with the
# category the rules assign. The expected award comes from CASES itself.
GROUND_TRUTH = [
    ("C01", facts(1800, 3, 1150), "housing"),
    ("C02", facts(None, 4), "housing"),
    ("C03", facts(900, 2, 6200, 12), "housing"),
    ("C04", facts(1400, 2), "food_assistance"),
    ("C05", facts(1600, 2), None),
    ("C06", facts(), "housing"),
    ("C07", facts(700, 5, requested=51000, award_type="one_time"), "housing"),
    ("C08", facts(1200, 2, 800), "food_assistance"),
]

# Inputs a model can return that the cases above never exercise.
# (label, facts, category, expected amount, expected citation)
EDGES = [
    ("no period stated uses 12 months", facts(1800, 3, 1150), "housing", 7320, "HA-3.2"),
    ("a stated 6-month period is used", facts(1800, 3, 1150, 6), "housing", 3660, "HA-3.2"),
    ("a stated zero-month period escalates", facts(1800, 3, 1150, 0), "housing", None, "G-0.1"),
    ("a negative period escalates", facts(1800, 3, 1150, -3), "housing", None, "G-0.1"),
    ("an absurd period is capped by G-0.2", facts(900, 2, 6200, 999), "housing",
     STATUTORY_MAXIMUM, "G-0.2"),
    ("rent covered by income awards zero", facts(5000, 2, 1000), "housing", 0, "HA-3.2"),
    ("negative income escalates", facts(-1500, 2, 0), "housing", None, "G-0.1"),
    ("negative rent escalates", facts(1500, 2, -400), "housing", None, "G-0.1"),
    ("zero household escalates", facts(1200, 0), "food_assistance", None, "G-0.1"),
    ("one-time grant under the cap", facts(700, 5, requested=3200, award_type="one_time"),
     "housing", 3200, "HA-4.1"),
    ("one-time grant with no amount escalates", facts(700, 5, award_type="one_time"),
     "housing", None, "G-0.1"),
    ("food assistance caps at 800 a month", facts(1200, 6), "food_assistance", 9600, "FA-1.4"),
    ("energy credit is flat", facts(1200, 2), "energy", 400, "EN-2.1"),
    ("an unknown category escalates", facts(1200, 2), "childcare", None, "G-0.3"),
    ("a string income escalates", facts("1800", 3, 1150), "housing", None, "G-0.1"),
]

failures = []


def check(ok, label, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"        {detail}")
        failures.append(label)


def main():
    check(set(EXPECTED) == {case_id for case_id, _, _ in GROUND_TRUTH},
          "every evaluation case has ground-truth facts here", sorted(EXPECTED))
    for case_id, extracted, category in GROUND_TRUTH:
        amount, citation = compute(extracted, category)
        check(amount == EXPECTED[case_id],
              f"{case_id}: award matches the evaluation's expected {EXPECTED[case_id]}",
              f"got {amount} ({citation})")
    for label, extracted, category, amount, citation in EDGES:
        got = compute(extracted, category)
        check(got == (amount, citation), label, f"got {got}, expected {(amount, citation)}")
    check(evaluation_uses_deployed_formula,
          "the evaluation imports the deployed formula instead of keeping a copy",
          "measure_reliability.py defines its own compute_award or does not import compute")
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
