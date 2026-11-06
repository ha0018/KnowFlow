"""Bounded initialization of the private S3 bucket; requires no database access."""

import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


def main():
    bucket = os.environ.get("S3_BUCKET", "knowflow")
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        region_name=os.environ.get("S3_REGION", "us-east-1"),
        config=Config(
            signature_version="s3v4",
            connect_timeout=2,
            read_timeout=3,
            retries={"max_attempts": 1},
            s3={"addressing_style": "path"},
        ),
    )
    for attempt in range(30):
        try:
            try:
                client.head_bucket(Bucket=bucket)
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in {"404", "NoSuchBucket"}:
                    raise
                client.create_bucket(Bucket=bucket)
            print("Private object bucket is ready", flush=True)
            return
        except (BotoCoreError, ClientError):
            if attempt == 29:
                raise SystemExit("Object bucket initialization failed") from None
            time.sleep(1)


if __name__ == "__main__":
    main()
