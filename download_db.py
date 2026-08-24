#!/usr/bin/env python3
"""Download cookies DB from S3 using credentials from ~/.config/zeeker/.env"""
import os
from pathlib import Path

# Read the zeeker .env directly
env_path = Path.home() / ".config" / "zeeker" / ".env"
env_vars = {}
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            key, value = line.split('=', 1)
            env_vars[key] = value

# Set env vars for boto3
os.environ['AWS_ACCESS_KEY_ID'] = env_vars['AWS_ACCESS_KEY_ID']
os.environ['AWS_SECRET_ACCESS_KEY'] = env_vars['AWS_SECRET_ACCESS_KEY']
os.environ['S3_BUCKET'] = env_vars['S3_BUCKET']
os.environ['S3_ENDPOINT_URL'] = env_vars['S3_ENDPOINT_URL']
os.environ['AWS_REGION'] = env_vars.get('AWS_REGION', 'default')

import boto3
from botocore.config import Config

s3 = boto3.client(
    's3',
    endpoint_url=os.environ['S3_ENDPOINT_URL'],
    aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
    region_name=os.environ['AWS_REGION'],
    config=Config(
        response_checksum_validation="when_required",
        request_checksum_calculation="when_required",
    )
)

bucket = os.environ['S3_BUCKET']

# List objects under cookies/
print("Listing S3 objects under cookies/...")
for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix='cookies/'):
    for obj in page.get('Contents', []):
        print(f"  {obj['Key']:60s} {obj['Size']:>12,} bytes")

# Download the DB
keys_to_try = ['cookies/latest.db', 'cookies/sg-law-cookies.db', 'backups/sg-law-cookies/latest.db']
for key in keys_to_try:
    try:
        dest = Path('cookies.db')
        print(f"\nTrying to download s3://{bucket}/{key} -> {dest}")
        s3.download_file(bucket, key, str(dest))
        size = dest.stat().st_size
        print(f"SUCCESS: Downloaded {size:,} bytes to {dest}")
        break
    except Exception as e:
        print(f"  FAILED: {e}")
else:
    print("\nCould not find a DB to download. Starting fresh...")
