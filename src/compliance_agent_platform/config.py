"""Runtime configuration.

Values come from environment variables (or a local ``.env``). In AWS the database
credentials are resolved from Secrets Manager when ``DB_SECRET_ARN`` is set; locally
the plain ``DATABASE_URL`` is used against the docker-compose Postgres.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

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

    # --- Retrieval (RAG) --------------------------------------------------------
    vector_backend: Literal["faiss", "pgvector"] = Field(
        default="faiss",
        description="'faiss' for local iteration (no DB round-trip); 'pgvector' for deployed.",
    )
    bedrock_embedding_model_id: str = Field(default="amazon.titan-embed-text-v2:0")
    embedding_dimension: int = Field(
        default=1024, description="Titan Embed Text v2's default output dimension."
    )
    faiss_index_path: str = Field(default="var/faiss/regulatory_corpus.json")
    retrieval_top_k: int = Field(default=5)
    regulatory_corpus_dir: str = Field(default="docs/regulatory-corpus")

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
