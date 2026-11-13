# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Track the boundary violation rate alongside token count and latency.

It shows how often each boundary blocks something, which the other two cannot.
A rising rate means either that a boundary is catching bad output or that an
upstream component regressed.

Usage:
    python observability/boundary_metric.py            # emit samples and create the alarm
    python observability/boundary_metric.py --delete    # remove the alarm
"""

import argparse
import os

import boto3

REGION = os.environ.get("AWS_REGION", "us-west-2")
NAMESPACE = "AgentBoundaries"
METRIC_NAME = "BoundaryViolation"
ALARM_NAME = "bfa-sample-BoundaryViolationRate-High"

cloudwatch = boto3.client("cloudwatch", region_name=REGION)


def emit_boundary_metric(boundary_type, boundary_name, violated):
    """Record one boundary evaluation, at two granularities.

    CloudWatch does not aggregate across dimensions for custom metrics, and a
    dimension combination that was never published returns no data at all. If
    you publish only {BoundaryType, BoundaryName} and then alarm on
    {BoundaryType}, the alarm never receives a datapoint and sits in OK while
    violations accumulate. Publishing both series costs one extra datapoint and
    gives the alarm below data to evaluate.

    At high request volume, use the CloudWatch Embedded Metric Format instead of
    a synchronous call per request. EMF writes structured logs that CloudWatch
    extracts into metrics, which avoids added latency and API throttling.
    """
    value = 1 if violated else 0
    cloudwatch.put_metric_data(
        Namespace=NAMESPACE,
        MetricData=[
            {
                "MetricName": METRIC_NAME,
                "Dimensions": [
                    {"Name": "BoundaryType", "Value": boundary_type},
                    {"Name": "BoundaryName", "Value": boundary_name},
                ],
                "Value": value,
                "Unit": "Count",
            },
            {
                "MetricName": METRIC_NAME,
                "Dimensions": [{"Name": "BoundaryType", "Value": boundary_type}],
                "Value": value,
                "Unit": "Count",
            },
        ],
    )


def create_alarm(topic_arn=None):
    """Alarm on an elevated violation rate."""
    actions = [topic_arn] if topic_arn else []
    cloudwatch.put_metric_alarm(
        AlarmName=ALARM_NAME,
        AlarmDescription="Elevated intermediate boundary violation rate",
        MetricName=METRIC_NAME,
        Namespace=NAMESPACE,
        Statistic="Sum",
        Period=300,
        EvaluationPeriods=2,
        Threshold=10,
        ComparisonOperator="GreaterThanThreshold",
        Dimensions=[{"Name": "BoundaryType", "Value": "Intermediate"}],
        AlarmActions=actions,
        TreatMissingData="notBreaching",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete", action="store_true", help="remove the alarm")
    parser.add_argument(
        "--topic-arn",
        help="SNS topic ARN for alarm notifications. Encrypt the topic with a "
        "customer managed AWS KMS key and scope its topic policy.",
    )
    args = parser.parse_args()

    if args.delete:
        cloudwatch.delete_alarms(AlarmNames=[ALARM_NAME])
        print(f"Deleted alarm {ALARM_NAME}")
        return

    emit_boundary_metric("Intermediate", "validate-output", violated=True)
    emit_boundary_metric("Intermediate", "validate-output", violated=False)
    print(f"Emitted {METRIC_NAME} samples to the {NAMESPACE} namespace")

    create_alarm(args.topic_arn)
    print(f"Created alarm {ALARM_NAME}")
    if not args.topic_arn:
        print("No --topic-arn given, so the alarm has no notification action.")


if __name__ == "__main__":
    main()
