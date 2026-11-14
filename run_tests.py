#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Run every local test in this repository.

All tests run offline: no AWS calls, no credentials, no deployed resources. A
regression that would make a boundary report healthy while enforcing nothing
fails here.

Each suite is imported and its main() called in process.

Usage:
    python run_tests.py
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent

SUITES = [
    ("intermediate boundary validator", "intermediate/test_validator.py"),
    ("state machine retry contract", "control-safety/test_state_machine.py"),
    ("boundary metric and alarm", "observability/test_boundary_metric.py"),
    ("tool schema and Cedar policy", "gateway-authorization/test_tool_schema.py"),
    ("output fact cross-check", "endtoend/test_fact_cross_check.py"),
    ("award computed in code", "endtoend/test_compute_award.py"),
    ("deployed alarm dimensions", "endtoend/test_alarm_dimensions.py"),
    ("conditional award write", "endtoend/test_record_award.py"),
]


def load_suite(path):
    """Import a test file under a unique module name.

    A unique name matters because the suites live in sibling directories and
    several of them import a module from their own directory. Loading each under
    its own name keeps those imports from colliding.
    """
    module_name = f"_suite_{path.stem}_{abs(hash(str(path))) % 10_000}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def main():
    failed = []
    for label, relative in SUITES:
        print(f"\n{'=' * 70}\n{label}  ({relative})\n{'=' * 70}", flush=True)
        path = REPO / relative
        try:
            suite = load_suite(path)
            code = suite.main()
        except Exception as error:  # a suite that cannot run is a failure
            print(f"[FAIL] {label} raised {type(error).__name__}: {error}", flush=True)
            failed.append(label)
            continue
        if code != 0:
            failed.append(label)

    print(f"\n{'=' * 70}", flush=True)
    if failed:
        print(f"FAILED: {len(failed)} of {len(SUITES)} suites", flush=True)
        for label in failed:
            print(f"  - {label}", flush=True)
        return 1
    print(f"PASSED: {len(SUITES)} of {len(SUITES)} suites", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
