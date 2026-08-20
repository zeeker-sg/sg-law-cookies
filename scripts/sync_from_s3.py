#!/usr/bin/env python3
"""Download latest.db from S3 and overwrite local cookies.db."""
import os, boto3, sys
from pathlib import Path

s3 = boto3.client("s3",
    endpoint_url=os.environ["S3_ENDPOINT_URL"],
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)

bucket = os.environ["S3_BUCKET"]
key = "cookies/latest.db"
local = Path(__file__).with_name("cookies.db")

s3.download_file(bucket, key, str(local))
size = local.stat().st_size
print(f"Downloaded s3://{bucket}/{key} ({size:,} bytes) -> {local}")

# Quick sanity check: verify it's a valid SQLite DB
import sqlite3
conn = sqlite3.connect(local)
cursor = conn.cursor()
cursor.execute("SELECT COUNT(*) FROM cookies")
count = cursor.fetchone()[0]
cursor.execute("SELECT MAX(date) FROM cookies")
latest = cursor.fetchone()[0]
conn.close()
print(f"Verified: {count} cookies, latest date: {latest}")
