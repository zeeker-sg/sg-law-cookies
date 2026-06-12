"""S3 backup for the production database, following Zeeker's conventions.

Same env vars as the Zeeker deployer (S3_BUCKET, AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY, optional S3_ENDPOINT_URL for non-AWS endpoints),
but keys live under backups/ — never latest/, which in the Zeeker bucket
means "publish to data.zeeker.sg".
"""

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BACKUP_PREFIX = "backups/sg-law-cookies"


class BackupError(RuntimeError):
    pass


@dataclass
class BackupResult:
    bucket: str
    dated_key: str
    latest_key: str
    size_bytes: int


def _make_client():
    import boto3
    from botocore.config import Config

    access_key = os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        raise BackupError(
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are required (set them in .env)"
        )
    # Checksum settings for compatibility with non-AWS S3 endpoints (R2 etc.),
    # matching the Zeeker deployer.
    config = Config(
        response_checksum_validation="when_required",
        request_checksum_calculation="when_required",
    )
    kwargs: dict = {
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "config": config,
    }
    endpoint = os.getenv("S3_ENDPOINT_URL")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


def snapshot_db(db_path: Path, dest: Path) -> None:
    """Consistent point-in-time copy via the SQLite backup API.

    Safe against a concurrently writing pipeline (WAL mode); a plain file
    copy is not.
    """
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def backup_db(
    db_path: Path,
    bucket: str | None = None,
    s3_client=None,
    now: datetime | None = None,
) -> BackupResult:
    """Snapshot the DB and upload dated + latest copies to S3."""
    if not db_path.exists():
        raise BackupError(f"database not found: {db_path}")
    bucket = bucket or os.getenv("S3_BUCKET")
    if not bucket:
        raise BackupError("S3_BUCKET is required (set it in .env)")
    client = s3_client or _make_client()
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    dated_key = f"{BACKUP_PREFIX}/cookies-{stamp}.db"
    latest_key = f"{BACKUP_PREFIX}/latest.db"

    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "cookies-snapshot.db"
        snapshot_db(db_path, snap)
        size = snap.stat().st_size
        client.upload_file(str(snap), bucket, dated_key)
        client.upload_file(str(snap), bucket, latest_key)
    return BackupResult(bucket=bucket, dated_key=dated_key, latest_key=latest_key, size_bytes=size)
