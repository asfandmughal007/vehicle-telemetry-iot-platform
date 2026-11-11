"""The platform stack: IoT Core -> Lambda -> DynamoDB, REST API, dashboard, monitoring."""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_iot as iot
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from vehicle_telemetry_cdk.config import StackConfig
from vehicle_telemetry_cdk.dashboard_site import DashboardSite
from vehicle_telemetry_cdk.monitoring import Monitoring

#: Must match the SQL the Lambda expects (see telemetry_processor.validation).
TELEMETRY_RULE_SQL = "SELECT *, topic(2) AS topic_vehicle_id FROM 'vehicles/+/telemetry'"

_API_ROUTES = (
    "/health",
    "/vehicles",
    "/vehicles/{vehicle_id}",
    "/vehicles/{vehicle_id}/latest",
    "/vehicles/{vehicle_id}/telemetry",
)
_RUNTIME = lambda_.Runtime.PYTHON_3_12
#: Keep in sync with infra/cdk/scripts/build_lambdas.py (wheels are built for this CPU).
_ARCHITECTURE = lambda_.Architecture.ARM_64


class TelemetryStack(Stack):
    """All resources of one deployment.

    Lambda code is injected (``processor_code`` / ``api_code``): production builds use the
    zip directories produced by ``scripts/build_lambdas.py``, tests use inline stubs.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StackConfig,
        processor_code: lambda_.Code,
        api_code: lambda_.Code,
        dashboard_assets_dir: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config
        prefix = f"vtp-{config.environment}"
        data_removal = RemovalPolicy.RETAIN if config.is_prod else RemovalPolicy.DESTROY

        Tags.of(self).add("project", "vehicle-telemetry-iot-platform")
        Tags.of(self).add("environment", config.environment)

        # ------------------------------------------------------------------ storage
        def table(table_id: str, **key_args: Any) -> ddb.Table:
            return ddb.Table(
                self,
                table_id,
                billing_mode=ddb.BillingMode.PAY_PER_REQUEST,  # unpredictable fleet traffic
                point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True
                ),
                removal_policy=data_removal,
                deletion_protection=config.is_prod,
                **key_args,
            )

        self.telemetry_table = table(
            "TelemetryTable",
            partition_key=ddb.Attribute(name="vehicle_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="ts", type=ddb.AttributeType.NUMBER),
            time_to_live_attribute="expires_at",
        )
        self.vehicles_table = table(
            "VehiclesTable",
            partition_key=ddb.Attribute(name="vehicle_id", type=ddb.AttributeType.STRING),
        )

        # ------------------------------------------------------- ingestion (IoT -> Lambda)
        self.dlq = sqs.Queue(
            self,
            "IngestDlq",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )

        self.processor = lambda_.Function(
            self,
            "ProcessorFunction",
            description="Validates, transforms and stores vehicle telemetry.",
            runtime=_RUNTIME,
            architecture=_ARCHITECTURE,
            handler="telemetry_processor.handler.lambda_handler",
            code=processor_code,
            memory_size=256,
            timeout=Duration.seconds(10),
            tracing=lambda_.Tracing.ACTIVE,
            # Asynchronous invocation by IoT: retry transient failures twice, then park the
            # event in the DLQ instead of losing it.
            retry_attempts=2,
            dead_letter_queue=self.dlq,
            log_group=logs.LogGroup(
                self,
                "ProcessorLogs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={
                "TELEMETRY_TABLE": self.telemetry_table.table_name,
                "VEHICLES_TABLE": self.vehicles_table.table_name,
                "DLQ_URL": self.dlq.queue_url,
                "TTL_DAYS": str(config.telemetry_ttl_days),
            },
        )
        # Least privilege: exactly the one DynamoDB action the processor uses (conditional
        # PutItem), not grant_write_data which would also allow DeleteItem/BatchWriteItem.
        self.telemetry_table.grant(self.processor, "dynamodb:PutItem")
        self.vehicles_table.grant(self.processor, "dynamodb:PutItem")
        self.dlq.grant_send_messages(self.processor)

        iot_error_role = iam.Role(
            self,
            "IotRuleErrorRole",
            assumed_by=iam.ServicePrincipal("iot.amazonaws.com"),
            description="Lets the IoT rule put undeliverable messages into the DLQ.",
        )
        self.dlq.grant_send_messages(iot_error_role)

        self.rule = iot.CfnTopicRule(
            self,
            "TelemetryRule",
            topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
                sql=TELEMETRY_RULE_SQL,
                aws_iot_sql_version="2016-03-23",
                rule_disabled=False,
                actions=[
                    iot.CfnTopicRule.ActionProperty(
                        lambda_=iot.CfnTopicRule.LambdaActionProperty(
                            function_arn=self.processor.function_arn
                        )
                    )
                ],
                error_action=iot.CfnTopicRule.ActionProperty(
                    sqs=iot.CfnTopicRule.SqsActionProperty(
                        queue_url=self.dlq.queue_url,
                        role_arn=iot_error_role.role_arn,
                        use_base64=False,
                    )
                ),
            ),
        )
        self.processor.add_permission(
            "AllowIotRule",
            principal=iam.ServicePrincipal("iot.amazonaws.com"),
            source_arn=self.rule.attr_arn,
            source_account=self.account,
        )

        self._device_registry(prefix)

        # ---------------------------------------------------------------------- API
        self.api_function = lambda_.Function(
            self,
            "ApiFunction",
            description="FastAPI read API behind API Gateway (Mangum adapter).",
            runtime=_RUNTIME,
            architecture=_ARCHITECTURE,
            handler="telemetry_api.lambda_handler.handler",
            code=api_code,
            memory_size=512,
            timeout=Duration.seconds(15),
            tracing=lambda_.Tracing.ACTIVE,
            log_group=logs.LogGroup(
                self,
                "ApiLogs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={
                "API_TELEMETRY_TABLE": self.telemetry_table.table_name,
                "API_VEHICLES_TABLE": self.vehicles_table.table_name,
            },
        )
        # Read-only and minimal: history is a Query, the fleet list a Scan, a vehicle a GetItem.
        self.telemetry_table.grant(self.api_function, "dynamodb:Query")
        self.vehicles_table.grant(self.api_function, "dynamodb:GetItem", "dynamodb:Scan")

        self.site = DashboardSite(self, "Dashboard", is_prod=config.is_prod)
        cors_origins = [self.site.origin, *config.api_cors_origins]

        self.http_api = apigw.HttpApi(
            self,
            "HttpApi",
            api_name=f"{prefix}-telemetry-api",
            description="Read API for vehicle telemetry.",
            create_default_stage=False,
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=cors_origins,
                allow_methods=[apigw.CorsHttpMethod.GET],
                allow_headers=["X-Request-ID"],
                max_age=Duration.hours(1),
            ),
        )
        integration = integrations.HttpLambdaIntegration("ApiIntegration", self.api_function)
        for path in _API_ROUTES:  # explicit GET routes only: no catch-all, no write methods
            self.http_api.add_routes(
                path=path, methods=[apigw.HttpMethod.GET], integration=integration
            )
        self.stage = apigw.HttpStage(
            self,
            "ApiStage",
            http_api=self.http_api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigw.ThrottleSettings(
                rate_limit=config.api_throttle_rate, burst_limit=config.api_throttle_burst
            ),
        )
        api_url = self.stage.url or ""

        if dashboard_assets_dir:
            self.site.deploy(dashboard_assets_dir, api_url)

        # --------------------------------------------------------------- monitoring
        self.monitoring = Monitoring(
            self,
            "Monitoring",
            config=config,
            name_prefix=prefix,
            processor=self.processor,
            api_function=self.api_function,
            dlq=self.dlq,
            api_id=self.http_api.api_id,
            iot_rule_name=self.rule.ref,
        )

        # ------------------------------------------------------------------ outputs
        CfnOutput(self, "ApiUrl", value=api_url, description="Base URL of the REST API")
        CfnOutput(self, "DashboardUrl", value=self.site.origin, description="Dashboard (HTTPS)")
        CfnOutput(self, "TelemetryTableName", value=self.telemetry_table.table_name)
        CfnOutput(self, "VehiclesTableName", value=self.vehicles_table.table_name)
        CfnOutput(self, "IngestDlqUrl", value=self.dlq.queue_url)
        CfnOutput(self, "IotPolicyName", value=f"{prefix}-vehicle-policy")
        CfnOutput(
            self,
            "AlarmTopicArn",
            value=self.monitoring.topic.topic_arn,
            description="Subscribe additional endpoints (chat, pager) here.",
        )

    # ------------------------------------------------------------------ device registry
    def _device_registry(self, prefix: str) -> None:
        """One IoT thing per vehicle plus a single policy shared by all device certificates.

        The policy uses *policy variables*, so a certificate attached to thing ``gw-VH-0001``
        can only connect with that client ID and only touch ``vehicles/VH-0001/*``. Adding a
        vehicle therefore needs a new thing, not a new policy.

        Certificates and private keys are deliberately NOT created here: a key generated by
        CloudFormation would end up in template state. Create the certificate per device
        with ``aws iot create-keys-and-certificate`` and attach policy and thing (README).
        """
        thing_name = "${iot:Connection.Thing.ThingName}"
        vehicle = "${iot:Connection.Thing.Attributes[vehicle_id]}"
        arn = f"arn:{self.partition}:iot:{self.region}:{self.account}"
        topic = f"{arn}:topic/vehicles/{vehicle}"
        topic_filter = f"{arn}:topicfilter/vehicles/{vehicle}"

        self.device_policy = iot.CfnPolicy(
            self,
            "VehiclePolicy",
            policy_name=f"{prefix}-vehicle-policy",
            policy_document={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "iot:Connect",
                        "Resource": f"{arn}:client/{thing_name}",
                        "Condition": {"Bool": {"iot:Connection.Thing.IsAttached": "true"}},
                    },
                    {
                        "Effect": "Allow",
                        "Action": "iot:Publish",
                        "Resource": [
                            f"{topic}/telemetry",
                            f"{topic}/status",
                            f"{topic}/diagnostics",
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": "iot:Subscribe",
                        "Resource": f"{topic_filter}/commands",
                    },
                    {
                        "Effect": "Allow",
                        "Action": "iot:Receive",
                        "Resource": f"{topic}/commands",
                    },
                ],
            },
        )
        self.things = [
            iot.CfnThing(
                self,
                f"Thing{index}",
                thing_name=f"gw-{vehicle_id}",
                attribute_payload=iot.CfnThing.AttributePayloadProperty(
                    attributes={"vehicle_id": vehicle_id}
                ),
            )
            for index, vehicle_id in enumerate(self.config.vehicle_ids)
        ]
