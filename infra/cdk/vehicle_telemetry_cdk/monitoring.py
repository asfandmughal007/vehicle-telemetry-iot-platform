"""CloudWatch dashboard and alarms (all notifications go to one SNS topic)."""

from __future__ import annotations

from aws_cdk import Duration
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from vehicle_telemetry_cdk.config import StackConfig

# Must match telemetry_processor.metrics (EMF namespace and dimension).
METRIC_NAMESPACE = "VehicleTelemetry"
METRIC_SERVICE = "telemetry-processor"


_AT_LEAST = cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD


def _custom(name: str, statistic: str = "Sum", period_min: int = 5) -> cw.Metric:
    return cw.Metric(
        namespace=METRIC_NAMESPACE,
        metric_name=name,
        dimensions_map={"Service": METRIC_SERVICE},
        statistic=statistic,
        period=Duration.minutes(period_min),
    )


def _api_metric(api_id: str, name: str, statistic: str = "Sum") -> cw.Metric:
    return cw.Metric(
        namespace="AWS/ApiGateway",
        metric_name=name,
        dimensions_map={"ApiId": api_id},
        statistic=statistic,
        period=Duration.minutes(5),
    )


class Monitoring(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StackConfig,
        name_prefix: str,
        processor: lambda_.IFunction,
        api_function: lambda_.IFunction,
        dlq: sqs.IQueue,
        api_id: str,
        iot_rule_name: str,
    ) -> None:
        super().__init__(scope, construct_id)

        self.topic = sns.Topic(self, "AlarmTopic", display_name=f"{name_prefix} alarms")
        if config.alarm_email:
            self.topic.add_subscription(subscriptions.EmailSubscription(config.alarm_email))
        action = cw_actions.SnsAction(self.topic)

        def alarm(
            alarm_id: str,
            description: str,
            metric: cw.IMetric,
            *,
            threshold: float,
            comparison: cw.ComparisonOperator = _AT_LEAST,
            periods: int = 1,
            missing: cw.TreatMissingData = cw.TreatMissingData.NOT_BREACHING,
        ) -> cw.Alarm:
            created = cw.Alarm(
                self,
                alarm_id,
                alarm_name=f"{name_prefix}-{alarm_id}",
                alarm_description=description,
                metric=metric,
                threshold=threshold,
                evaluation_periods=periods,
                comparison_operator=comparison,
                treat_missing_data=missing,
            )
            created.add_alarm_action(action)
            return created

        alarm(
            "processor-errors",
            "Telemetry processor Lambda failed (transient AWS errors or bugs).",
            processor.metric_errors(period=Duration.minutes(5), statistic="Sum"),
            threshold=1,
        )
        alarm(
            "ingest-dlq-not-empty",
            "Messages reached the ingest dead-letter queue: inspect and redrive.",
            dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5), statistic="Maximum"
            ),
            threshold=1,
        )
        alarm(
            "malformed-messages",
            "Many malformed telemetry messages: a vehicle may run incompatible firmware.",
            _custom("MalformedMessages"),
            threshold=10,
        )
        alarm(
            "iot-rule-failures",
            "The IoT rule failed to deliver messages to the processor.",
            cw.Metric(
                namespace="AWS/IoT",
                metric_name="Failure",
                dimensions_map={"RuleName": iot_rule_name},
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=1,
        )
        alarm(
            "api-5xx",
            "The API returned server errors.",
            _api_metric(api_id, "5xx"),
            threshold=5,
        )
        alarm(
            "api-function-errors",
            "The API Lambda raised errors.",
            api_function.metric_errors(period=Duration.minutes(5), statistic="Sum"),
            threshold=1,
        )
        if config.alarm_on_missing_telemetry:
            alarm(
                "no-telemetry",
                "No telemetry has been stored for 15 minutes.",
                _custom("MessagesStored", period_min=15),
                threshold=1,
                comparison=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
                missing=cw.TreatMissingData.BREACHING,
            )

        self.dashboard = cw.Dashboard(self, "Dashboard", dashboard_name=f"{name_prefix}-overview")
        self.dashboard.add_widgets(
            cw.GraphWidget(
                title="Ingestion (messages / 5 min)",
                width=12,
                left=[
                    _custom("MessagesStored"),
                    _custom("DuplicateMessages"),
                    _custom("MalformedMessages"),
                ],
            ),
            cw.GraphWidget(
                title="Ingest lag: gateway sampling -> stored (ms)",
                width=12,
                left=[_custom("IngestLagMs", "Average"), _custom("IngestLagMs", "p95")],
            ),
            cw.GraphWidget(
                title="Processor Lambda",
                width=8,
                left=[
                    processor.metric_invocations(period=Duration.minutes(5)),
                    processor.metric_errors(period=Duration.minutes(5)),
                    processor.metric_throttles(period=Duration.minutes(5)),
                ],
            ),
            cw.GraphWidget(
                title="Processing latency (ms)",
                width=8,
                left=[
                    _custom("ProcessingLatencyMs", "Average"),
                    _custom("ProcessingLatencyMs", "p99"),
                ],
            ),
            cw.GraphWidget(
                title="Dead-letter queue depth",
                width=8,
                left=[
                    dlq.metric_approximate_number_of_messages_visible(
                        period=Duration.minutes(5), statistic="Maximum"
                    )
                ],
            ),
            cw.GraphWidget(
                title="API requests and errors",
                width=12,
                left=[
                    _api_metric(api_id, "Count"),
                    _api_metric(api_id, "4xx"),
                    _api_metric(api_id, "5xx"),
                ],
            ),
            cw.GraphWidget(
                title="API latency (ms)",
                width=12,
                left=[
                    _api_metric(api_id, "Latency", "p95"),
                    _api_metric(api_id, "Latency", "Average"),
                ],
            ),
        )
