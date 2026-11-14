# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Confirm the alarm can receive data.

CloudWatch does not aggregate across dimensions for custom metrics. If the
metric is published with {BoundaryType, BoundaryName} and the alarm watches
{BoundaryType} alone, the alarm receives no data and stays in OK.

These checks compare what the code publishes against what the alarm watches. No AWS calls are made: the module's CloudWatch
client is replaced with a recorder.

Usage:
    python observability/test_boundary_metric.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import boundary_metric

failures = []


def check(ok, label, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        if detail:
            print(f"        {detail}")
        failures.append(label)
    return ok


class Recorder:
    """Stands in for the CloudWatch client and keeps the call arguments."""

    def __init__(self):
        self.metric_data = []
        self.alarms = []

    def put_metric_data(self, **kwargs):
        self.metric_data.append(kwargs)

    def put_metric_alarm(self, **kwargs):
        self.alarms.append(kwargs)


def dimension_names(dimensions):
    return frozenset(d["Name"] for d in dimensions)


def main():
    recorder = Recorder()
    boundary_metric.cloudwatch = recorder

    boundary_metric.emit_boundary_metric("Intermediate", "award-validator", True)
    boundary_metric.create_alarm()

    check(
        len(recorder.metric_data) == 1,
        "emit publishes in a single put_metric_data call",
        f"{len(recorder.metric_data)} calls; one call per evaluation avoids extra latency",
    )

    published = [
        dimension_names(series["Dimensions"])
        for call in recorder.metric_data
        for series in call["MetricData"]
    ]

    check(
        frozenset({"BoundaryType", "BoundaryName"}) in published,
        "the specific {BoundaryType, BoundaryName} series is published",
        f"published: {[sorted(p) for p in published]}",
    )

    check(
        frozenset({"BoundaryType"}) in published,
        "the aggregate {BoundaryType} series is published",
        "without this series an alarm on BoundaryType alone receives no datapoints",
    )

    check(
        len(recorder.alarms) == 1,
        "create_alarm creates exactly one alarm",
        f"{len(recorder.alarms)} alarms",
    )

    alarm = recorder.alarms[0]
    watched = dimension_names(alarm["Dimensions"])

    check(
        watched in published,
        "the alarm watches a dimension set that is published",
        f"alarm watches {sorted(watched)}, published sets are "
        f"{[sorted(p) for p in published]}; an unpublished combination can never fire",
    )

    check(
        alarm["Namespace"] == boundary_metric.NAMESPACE
        and alarm["MetricName"] == boundary_metric.METRIC_NAME,
        "the alarm points at the namespace and metric the code emits",
    )

    check(
        "TreatMissingData" in alarm,
        "the alarm sets TreatMissingData explicitly",
        "the default of missing parks the alarm in INSUFFICIENT_DATA, which reads "
        "differently from OK on a dashboard; the choice should be deliberate",
    )

    # A violated evaluation must report 1 and a clean one 0, or the rate is wrong.
    recorder.metric_data.clear()
    boundary_metric.emit_boundary_metric("Output", "guardrail", False)
    clean_values = {s["Value"] for c in recorder.metric_data for s in c["MetricData"]}
    check(
        clean_values == {0},
        "a clean evaluation emits 0 rather than nothing",
        f"values {clean_values}; emitting only on violation leaves the metric "
        "sparse and makes the rate meaningless",
    )

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
