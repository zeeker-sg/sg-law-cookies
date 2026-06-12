from datetime import datetime, timezone
from pathlib import Path

import pytest

from sg_law_cookies import db
from sg_law_cookies.backup import BACKUP_PREFIX, BackupError, backup_db, snapshot_db
from sg_law_cookies.config import load_dotenv


class FakeS3:
    def __init__(self):
        self.uploads: list[tuple[str, str, str]] = []

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        assert Path(filename).exists()
        self.uploads.append((filename, bucket, key))


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "cookies.db"
    db.init_db(path)
    return path


def test_backup_uploads_dated_and_latest(db_path):
    fake = FakeS3()
    result = backup_db(
        db_path,
        bucket="zeeker-bucket",
        s3_client=fake,
        now=datetime(2026, 6, 12, 23, 30, 0, tzinfo=timezone.utc),
    )
    keys = [key for _, _, key in fake.uploads]
    assert keys == [
        f"{BACKUP_PREFIX}/cookies-20260612-233000.db",
        f"{BACKUP_PREFIX}/latest.db",
    ]
    assert result.size_bytes > 0
    # backups must never land under latest/ — that prefix deploys to
    # data.zeeker.sg in the Zeeker bucket
    assert not any(key.startswith("latest/") for key in keys)


def test_backup_missing_db_or_bucket(db_path, tmp_path, monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    with pytest.raises(BackupError, match="not found"):
        backup_db(tmp_path / "nope.db", bucket="b", s3_client=FakeS3())
    with pytest.raises(BackupError, match="S3_BUCKET"):
        backup_db(db_path, s3_client=FakeS3())


def test_snapshot_is_consistent_copy(db_path, tmp_path):
    conn = db.init_db(db_path)
    conn.execute(
        "INSERT INTO unresolved_terms (term, first_seen_date, count) VALUES ('x', '2026-06-12', 1)"
    )
    conn.commit()
    snap = tmp_path / "snap.db"
    snapshot_db(db_path, snap)
    import sqlite3

    rows = sqlite3.connect(snap).execute("SELECT count(*) FROM unresolved_terms").fetchone()
    assert rows[0] == 1


def test_load_dotenv_existing_env_wins(tmp_path, monkeypatch):
    envfile = tmp_path / ".env"
    envfile.write_text('S3_BUCKET="from-dotenv"\n# comment\nNEW_KEY=hello\n')
    monkeypatch.setenv("S3_BUCKET", "from-environ")
    monkeypatch.delenv("NEW_KEY", raising=False)
    load_dotenv(envfile)
    import os

    assert os.environ["S3_BUCKET"] == "from-environ"
    assert os.environ["NEW_KEY"] == "hello"
