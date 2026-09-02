"""Smoke-test Amazon Bedrock access for the Strands agent — no robot involved.

Verifies, in order:
  1. AWS credentials resolve (default profile / env / role).
  2. A Strands agent on the configured Bedrock model can complete one request.

Run via ``test_bedrock.sh`` (installs deps), or directly once deps are present:

    python test_bedrock.py

Configuration (environment variables):
    AWS_REGION         AWS region for Bedrock        (default: us-east-1)
    BEDROCK_MODEL_ID   Bedrock inference-profile id  (default: Amazon Nova 2 Lite)
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    region = os.environ.get("AWS_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-2-lite-v1:0")

    # 1. Confirm AWS credentials resolve before touching Bedrock.
    try:
        import boto3

        ident = boto3.client("sts", region_name=region).get_caller_identity()
        print(f"[ok] AWS credentials resolved for account {ident['Account']} ({ident['Arn']})")
    except Exception as e:  # noqa: BLE001
        print(f"[fail] Could not resolve AWS credentials: {e}")
        print("       Configure a default profile (aws configure) or set AWS_PROFILE.")
        return 1

    # 2. One real Strands -> Bedrock round trip.
    try:
        from strands import Agent
        from strands.models import BedrockModel

        agent = Agent(model=BedrockModel(model_id=model_id, region_name=region))
        print(f"[..] Invoking Bedrock model '{model_id}' in {region} ...")
        result = agent("Reply with exactly: Bedrock OK")
        print(f"[ok] Model responded: {str(result).strip()}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"[fail] Bedrock invocation failed: {e}")
        print("       Common causes: model access not enabled for this ID in the AWS")
        print(f"       console (Bedrock > Model access), or wrong region. ID={model_id}, region={region}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
