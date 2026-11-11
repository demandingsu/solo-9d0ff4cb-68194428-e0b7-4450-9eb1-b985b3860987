"""旧版 SQLite（notice_no 单列唯一）自动迁移测试。"""
from __future__ import annotations

import sqlite3

from app import db

LEGACY_SCHEMA = """
CREATE TABLE announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_no       TEXT NOT NULL UNIQUE,
    project_no      TEXT,
    notice_type     TEXT,
    publish_time    TEXT,
    deadline        TEXT,
    ref_notice_no   TEXT,
    source_url      TEXT,
    list_url        TEXT,
    content_hash    TEXT,
    chain_id        TEXT,
    chain_status    TEXT,
    conflict_reasons TEXT,
    effective_deadline TEXT,
    last_synced_at  TEXT NOT NULL,
    last_fetch_error TEXT
);
CREATE INDEX idx_ann_project ON announcements(project_no);
CREATE INDEX idx_ann_chain ON announcements(chain_id);
CREATE INDEX idx_ann_ref ON announcements(ref_notice_no);
CREATE TABLE sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    list_url TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    total_listed INTEGER NOT NULL DEFAULT 0,
    fetched_ok INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    kept_last INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""


def _build_legacy_db(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO announcements (notice_no, project_no, notice_type, "
        "deadline, list_url, chain_id, chain_status, last_synced_at, "
        "last_fetch_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "X-001", "PRJ-OLD", "first", "2026-11-20 09:30:00",
            "http://old/announcements", "X-001", "ok",
            "2026-11-01 00:00:00", None,
        ),
    )
    conn.execute(
        "INSERT INTO sync_runs (run_id, list_url, started_at, status) "
        "VALUES (?, ?, ?, ?)",
        ("run-1", "http://old/announcements", "2026-11-01 00:00:00", "success"),
    )
    conn.commit()
    conn.close()


def test_legacy_db_migrates_and_remains_queryable(tmp_path, monkeypatch):
    db_file = tmp_path / "legacy.db"
    _build_legacy_db(db_file)

    monkeypatch.setenv("DB_PATH", str(db_file))
    from app import config
    config.DB_PATH = db_file

    conn = db.connect()
    db.init_db(conn)  # 触发迁移

    # 迁移后旧数据原样可查
    row = conn.execute(
        "SELECT * FROM announcements WHERE notice_no = ? AND list_url = ?",
        ("X-001", "http://old/announcements"),
    ).fetchone()
    assert row is not None
    assert row["project_no"] == "PRJ-OLD"
    assert row["chain_status"] == "ok"
    run = conn.execute("SELECT run_id, status FROM sync_runs").fetchone()
    assert run["run_id"] == "run-1"

    # 新表为组合唯一：同编号、不同入口可以并存
    conn.execute(
        "INSERT INTO announcements (notice_no, list_url, last_synced_at) "
        "VALUES (?, ?, ?)",
        ("X-001", "http://new/announcements", "2026-11-02 00:00:00"),
    )
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM announcements WHERE notice_no = 'X-001'"
    ).fetchone()["n"]
    assert count == 2

    # 旧的单列唯一约束已不存在：同入口重复编号必须被拒绝
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO announcements (notice_no, list_url, last_synced_at) "
            "VALUES (?, ?, ?)",
            ("X-001", "http://old/announcements", "2026-11-03 00:00:00"),
        )

    # 迁移幂等：再跑一次 init_db 不报错、不重复迁移
    db.init_db(conn)
    db.init_db(conn)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM announcements"
    ).fetchone()["n"] == 2
    conn.close()


def test_legacy_row_with_null_list_url_migrates_with_default(tmp_path, monkeypatch):
    db_file = tmp_path / "legacy-null.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO announcements (notice_no, last_synced_at, list_url) "
        "VALUES ('N-001', '2026-11-01 00:00:00', NULL)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(db_file))
    from app import config
    config.DB_PATH = db_file

    conn = db.connect()
    db.init_db(conn)
    row = conn.execute(
        "SELECT notice_no, list_url FROM announcements WHERE notice_no = 'N-001'"
    ).fetchone()
    assert row["list_url"] == ""
    conn.close()
