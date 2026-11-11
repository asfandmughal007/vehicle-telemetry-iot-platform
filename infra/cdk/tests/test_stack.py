"""Assertions on the synthesized CloudFormation template (no AWS account needed).

These tests encode the architecture's security and reliability decisions, so that a
refactoring cannot silently remove them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk import aws_lambda as lambda_
from aws_cdk.assertions import Match, Template

from vehicle_telemetry_cdk import StackConfig, TelemetryStack
from vehicle_telemetry_cdk.telemetry_stack import TELEMETRY_RULE_SQL

ENV = cdk.Environment(account="123456789012", region="eu-central-1")


def synth(dashboard_dir: str | None = None, **config: Any) -> Template:
    app = cdk.App()
    stack = TelemetryStack(
        app,
        "TestStack",
        config=StackConfig(**config),
        processor_code=lambda_.Code.from_inline("def lambda_handler(e, c): pass"),
        api_code=lambda_.Code.from_inline("def handler(e, c): pass"),
        dashboard_assets_dir=dashboard_dir,
        env=ENV,
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return synth(vehicle_ids=("VH-0001", "VH-0002"), alarm_email="ops@example.com")


def policy_actions(template: Template, role_prefix: str) -> set[str]:
    """All IAM actions granted to the role whose logical id starts with ``role_prefix``."""
    actions: set[str] = set()
    for policy in template.find_resources("AWS::IAM::Policy").values():
        roles = policy["Properties"]["Roles"]
        if not any(r.get("Ref", "").startswith(role_prefix) for r in roles):
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            granted = statement["Action"]
            actions.update([granted] if isinstance(granted, str) else granted)
    return actions


# --- storage -----------------------------------------------------------------


def test_telemetry_table_is_a_time_series_with_ttl_and_backups(template: Template) -> None:
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [
                {"AttributeName": "vehicle_id", "KeyType": "HASH"},
                {"AttributeName": "ts", "KeyType": "RANGE"},
            ],
            "AttributeDefinitions": Match.array_with(
                [{"AttributeName": "ts", "AttributeType": "N"}]
            ),
            "TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True},
            "BillingMode": "PAY_PER_REQUEST",
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
        },
    )


def test_vehicles_table_is_keyed_by_vehicle(template: Template) -> None:
    template.resource_count_is("AWS::DynamoDB::Table", 2)
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"KeySchema": [{"AttributeName": "vehicle_id", "KeyType": "HASH"}]},
    )


def test_dev_destroys_data_and_prod_retains_it() -> None:
    dev = synth(environment="dev")
    prod = synth(environment="prod")

    for table in dev.find_resources("AWS::DynamoDB::Table").values():
        assert table["DeletionPolicy"] == "Delete"
        assert table["Properties"].get("DeletionProtectionEnabled") in (None, False)
    for table in prod.find_resources("AWS::DynamoDB::Table").values():
        assert table["DeletionPolicy"] == "Retain"
        assert table["Properties"]["DeletionProtectionEnabled"] is True


# --- ingestion ---------------------------------------------------------------


def test_dlq_is_encrypted_and_retains_for_two_weeks(template: Template) -> None:
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"SqsManagedSseEnabled": True, "MessageRetentionPeriod": 14 * 24 * 3600},
    )
    template.has_resource_properties(
        "AWS::SQS::QueuePolicy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Deny",
                                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                            }
                        )
                    ]
                )
            }
        },
    )


def test_processor_function_configuration(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "telemetry_processor.handler.lambda_handler",
            "Runtime": "python3.12",
            "Architectures": ["arm64"],
            "Timeout": 10,
            "TracingConfig": {"Mode": "Active"},
            "DeadLetterConfig": {"TargetArn": Match.any_value()},
            "Environment": {
                "Variables": {
                    "TELEMETRY_TABLE": Match.any_value(),
                    "VEHICLES_TABLE": Match.any_value(),
                    "DLQ_URL": Match.any_value(),
                    "TTL_DAYS": "30",
                }
            },
        },
    )
    template.has_resource_properties("AWS::Lambda::EventInvokeConfig", {"MaximumRetryAttempts": 2})


def test_iot_rule_forwards_telemetry_with_topic_vehicle_id_and_has_an_error_action(
    template: Template,
) -> None:
    assert "topic(2) AS topic_vehicle_id" in TELEMETRY_RULE_SQL
    template.has_resource_properties(
        "AWS::IoT::TopicRule",
        {
            "TopicRulePayload": {
                "Sql": "SELECT *, topic(2) AS topic_vehicle_id FROM 'vehicles/+/telemetry'",
                "RuleDisabled": False,
                "Actions": [{"Lambda": {"FunctionArn": Match.any_value()}}],
                "ErrorAction": {"Sqs": Match.object_like({"UseBase64": False})},
            }
        },
    )


def test_only_the_iot_rule_may_invoke_the_processor(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {
            "Principal": "iot.amazonaws.com",
            "Action": "lambda:InvokeFunction",
            "SourceArn": Match.any_value(),
            "SourceAccount": "123456789012",
        },
    )


# --- device identity ---------------------------------------------------------


def test_one_thing_per_vehicle_with_its_vehicle_id_attribute(template: Template) -> None:
    template.resource_count_is("AWS::IoT::Thing", 2)
    for vehicle_id in ("VH-0001", "VH-0002"):
        template.has_resource_properties(
            "AWS::IoT::Thing",
            {
                "ThingName": f"gw-{vehicle_id}",
                "AttributePayload": {"Attributes": {"vehicle_id": vehicle_id}},
            },
        )


def test_device_policy_confines_each_vehicle_to_its_own_topics(template: Template) -> None:
    (policy,) = template.find_resources("AWS::IoT::Policy").values()
    document = policy["Properties"]["PolicyDocument"]
    text = json.dumps(document)

    assert '"Resource": "*"' not in text
    assert "${iot:Connection.Thing.Attributes[vehicle_id]}" in text
    assert "client/${iot:Connection.Thing.ThingName}" in text
    assert "iot:Connection.Thing.IsAttached" in text
    actions = {s["Action"] for s in document["Statement"]}
    assert actions == {"iot:Connect", "iot:Publish", "iot:Subscribe", "iot:Receive"}
    publish = next(s for s in document["Statement"] if s["Action"] == "iot:Publish")
    assert len(publish["Resource"]) == 3  # telemetry, status, diagnostics -- nothing else


def test_no_certificates_or_keys_are_created_by_the_stack(template: Template) -> None:
    template.resource_count_is("AWS::IoT::Certificate", 0)


# --- least privilege ---------------------------------------------------------


def test_processor_can_only_put_items_and_send_to_the_dlq(template: Template) -> None:
    actions = policy_actions(template, "ProcessorFunctionServiceRole")

    assert {a for a in actions if a.startswith("dynamodb:")} == {"dynamodb:PutItem"}
    assert "sqs:SendMessage" in actions


def test_api_can_only_read_with_the_three_actions_it_uses(template: Template) -> None:
    actions = policy_actions(template, "ApiFunctionServiceRole")

    dynamodb = {a for a in actions if a.startswith("dynamodb:")}
    assert dynamodb == {"dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan"}


def test_no_policy_grants_all_actions_or_all_resources_on_data(template: Template) -> None:
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            assert statement["Action"] != "*"
            if "dynamodb:PutItem" in json.dumps(statement["Action"]):
                assert statement["Resource"] != "*"


# --- API ---------------------------------------------------------------------


def test_api_exposes_exactly_the_five_get_routes(template: Template) -> None:
    routes = {
        r["Properties"]["RouteKey"]
        for r in template.find_resources("AWS::ApiGatewayV2::Route").values()
    }

    assert routes == {
        "GET /health",
        "GET /vehicles",
        "GET /vehicles/{vehicle_id}",
        "GET /vehicles/{vehicle_id}/latest",
        "GET /vehicles/{vehicle_id}/telemetry",
    }


def test_api_stage_is_throttled(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "DefaultRouteSettings": {"ThrottlingRateLimit": 20, "ThrottlingBurstLimit": 40},
            "AutoDeploy": True,
        },
    )


def test_api_cors_allows_only_get_and_the_dashboard_origin() -> None:
    template = synth(api_cors_origins=("https://dash.example.com",))

    (api,) = template.find_resources("AWS::ApiGatewayV2::Api").values()
    cors = api["Properties"]["CorsConfiguration"]
    assert cors["AllowMethods"] == ["GET"]
    assert "https://dash.example.com" in json.dumps(cors["AllowOrigins"])
    assert "*" not in cors["AllowOrigins"]


# --- dashboard hosting -------------------------------------------------------


def test_dashboard_bucket_is_private_and_served_through_cloudfront(template: Template) -> None:
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "BucketEncryption": Match.any_value(),
        },
    )
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": Match.object_like(
                {
                    "DefaultRootObject": "index.html",
                    "DefaultCacheBehavior": Match.object_like(
                        {"ViewerProtocolPolicy": "redirect-to-https"}
                    ),
                }
            )
        },
    )
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)


def test_dashboard_deployment_publishes_generated_api_config(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "config.js").write_text("// local config, must not be published")

    template = synth(dashboard_dir=str(tmp_path))

    template.resource_count_is("Custom::CDKBucketDeployment", 1)
    template.has_resource_properties("Custom::CDKBucketDeployment", {"DistributionPaths": ["/*"]})


# --- monitoring --------------------------------------------------------------


EXPECTED_ALARMS = {
    "processor-errors",
    "ingest-dlq-not-empty",
    "malformed-messages",
    "iot-rule-failures",
    "api-5xx",
    "api-function-errors",
    "no-telemetry",
}


def test_all_alarms_exist_and_notify_the_topic(template: Template) -> None:
    alarms = template.find_resources("AWS::CloudWatch::Alarm")

    names = {a["Properties"]["AlarmName"] for a in alarms.values()}
    assert names == {f"vtp-dev-{n}" for n in EXPECTED_ALARMS}
    for alarm in alarms.values():
        assert alarm["Properties"]["AlarmActions"]


def test_missing_telemetry_alarm_is_optional_and_breaches_on_missing_data() -> None:
    enabled = synth()
    disabled = synth(alarm_on_missing_telemetry=False)

    enabled.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "AlarmName": "vtp-dev-no-telemetry",
            "ComparisonOperator": "LessThanThreshold",
            "TreatMissingData": "breaching",
            "Namespace": "VehicleTelemetry",
            "MetricName": "MessagesStored",
        },
    )
    names = {
        a["Properties"]["AlarmName"]
        for a in disabled.find_resources("AWS::CloudWatch::Alarm").values()
    }
    assert "vtp-dev-no-telemetry" not in names


def test_email_subscription_only_when_an_address_is_configured(template: Template) -> None:
    template.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "ops@example.com"}
    )
    synth().resource_count_is("AWS::SNS::Subscription", 0)


def test_dashboard_widgets_use_the_processors_custom_metrics(template: Template) -> None:
    (dashboard,) = template.find_resources("AWS::CloudWatch::Dashboard").values()
    body = json.dumps(dashboard["Properties"]["DashboardBody"])

    for metric in ("MessagesStored", "DuplicateMessages", "MalformedMessages", "IngestLagMs"):
        assert metric in body
    assert "VehicleTelemetry" in body


def test_log_groups_have_a_retention_period(template: Template) -> None:
    logs = template.find_resources("AWS::Logs::LogGroup")

    assert len(logs) >= 2
    assert all(g["Properties"]["RetentionInDays"] == 30 for g in logs.values())


# --- outputs, hygiene --------------------------------------------------------


def test_outputs_needed_to_operate_the_system(template: Template) -> None:
    outputs = template.to_json()["Outputs"]

    assert {"ApiUrl", "DashboardUrl", "IngestDlqUrl", "IotPolicyName", "AlarmTopicArn"} <= set(
        outputs
    )


def test_template_contains_no_secrets(template: Template) -> None:
    text = json.dumps(template.to_json()).lower()

    for marker in ("akia", "secretaccesskey", "password", "begin private key", "aws_secret"):
        assert marker not in text


def test_resources_are_tagged() -> None:
    template = synth()

    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"Tags": Match.array_with([{"Key": "project", "Value": "vehicle-telemetry-iot-platform"}])},
    )


# --- configuration -----------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"environment": "staging"},
        {"vehicle_ids": ()},
        {"vehicle_ids": ("a/b",)},
        {"vehicle_ids": ("VH-0001", "VH-0001")},
        {"alarm_email": "not-an-email"},
        {"telemetry_ttl_days": 0},
        {"api_throttle_rate": 50, "api_throttle_burst": 10},
    ],
)
def test_invalid_configuration_is_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r".+"):
        StackConfig(**bad)


def test_config_from_cdk_context() -> None:
    config = StackConfig.from_context(
        {
            "environment": "prod",
            "vehicle_ids": "VH-0001, VH-0002",
            "alarm_email": "ops@example.com",
            "telemetry_ttl_days": "90",
            "alarm_on_missing_telemetry": "false",
            "api_cors_origins": "https://a.example,https://b.example",
        }
    )

    assert config.is_prod
    assert config.vehicle_ids == ("VH-0001", "VH-0002")
    assert config.telemetry_ttl_days == 90
    assert config.alarm_on_missing_telemetry is False
    assert config.api_cors_origins == ("https://a.example", "https://b.example")
    assert StackConfig.from_context({}) == StackConfig()
