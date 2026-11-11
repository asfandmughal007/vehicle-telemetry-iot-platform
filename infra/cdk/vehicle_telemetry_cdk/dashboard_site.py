"""Static dashboard hosting: private S3 bucket behind CloudFront (origin access control)."""

from __future__ import annotations

from aws_cdk import RemovalPolicy
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

# Files of the dashboard directory that must not be published.
_EXCLUDE = ["tests", "tests/*", "Dockerfile", "nginx.conf", "config.js"]


class DashboardSite(Construct):
    """The bucket is never public: only this CloudFront distribution may read from it."""

    def __init__(self, scope: Construct, construct_id: str, *, is_prod: bool) -> None:
        super().__init__(scope, construct_id)
        self.bucket = s3.Bucket(
            self,
            "Bucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN if is_prod else RemovalPolicy.DESTROY,
            auto_delete_objects=not is_prod,
        )
        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_root_object="index.html",
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
            ),
        )

    @property
    def origin(self) -> str:
        """HTTPS origin of the site (a deploy-time token), e.g. for the API's CORS list."""
        return f"https://{self.distribution.distribution_domain_name}"

    def deploy(self, assets_dir: str, api_url: str) -> None:
        """Upload the dashboard and a generated ``config.js`` that points to the API."""
        config_js = (
            "window.DASHBOARD_CONFIG = "
            f'{{ apiBaseUrl: "{api_url.rstrip("/")}", refreshSeconds: 5, historyPoints: 120 }};\n'
        )
        s3deploy.BucketDeployment(
            self,
            "Deployment",
            sources=[
                s3deploy.Source.asset(assets_dir, exclude=_EXCLUDE),
                s3deploy.Source.data("config.js", config_js),
            ],
            destination_bucket=self.bucket,
            distribution=self.distribution,
            distribution_paths=["/*"],
        )
