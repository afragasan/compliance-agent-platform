"""Runtime configuration.

Values come from environment variables (or a local ``.env``). In AWS the database
credentials are resolved from Secrets Manager when ``DB_SECRET_ARN`` is set; locally
the plain ``DATABASE_URL`` is used against the docker-compose Postgres.
"""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database -----------------------------------------------------------
    database_url: str = Field(
        default="postgresql://cap:cap@localhost:5432/cap",
        description="libpq DSN used by the LangGraph PostgresSaver.",
    )
    db_secret_arn: str | None = Field(
        default=None,
        description="If set, a Secrets Manager secret with username/password/host/port/dbname.",
    )

    # --- AWS / Bedrock ----------------------------------------------------------
    aws_region: str = Field(default="us-east-1")
    bedrock_model_id: str = Field(default="us.anthropic.claude-sonnet-4-6")

    # --- Screening policy -----------------------------------------------------
    prompt_version: str = Field(default="sanctions-eval-v1")
    clear_confidence_threshold: float = Field(default=0.85)
    queue: str = Field(default="sanctions_screening")

    # --- Audit export (S3 Object Lock) -----------------------------------------
    audit_export_bucket: str | None = Field(
        default=None,
        description="S3 bucket `cap audit export` writes hash-chained audit batches to.",
    )
    audit_export_prefix: str = Field(default="screening-audit")
    audit_retention_days: int = Field(
        default=30,
        description=(
            "Object Lock COMPLIANCE-mode default retention, in days, for the export "
            "bucket. 30 (1 month) here is a POC default — production deployments should "
            "set this per the applicable regulatory retention requirement (e.g. AML "
            "recordkeeping rules commonly call for multi-year retention)."
        ),
    )

    def resolved_dsn(self) -> str:
        """Return the effective libpq DSN, pulling creds from Secrets Manager if configured."""
        if not self.db_secret_arn:
            return self.database_url

        import boto3  # local import: only needed in AWS

        client = boto3.client("secretsmanager", region_name=self.aws_region)
        secret = json.loads(client.get_secret_value(SecretId=self.db_secret_arn)["SecretString"])
        return (
            f"postgresql://{secret['username']}:{secret['password']}"
            f"@{secret['host']}:{secret.get('port', 5432)}/{secret['dbname']}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
