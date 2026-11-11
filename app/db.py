"""SQLite 连接与表结构。

公告的业务身份是「列表入口 + 公告编号」的组合：不同采购目录（list_url）
允许出现相同公告编号，二者互不覆盖。旧版库以 notice_no 单列唯一，
init_db 会自动把旧表迁移为组合唯一，迁移后历史公告仍可正常查询。
"""
import sqlite3
from pathlib import Path

from . import config

# 新表：UNIQUE(list_url, notice_no) 组合业务主键
TABLE_SQL = """
CREATE TABLE IF NOT EXISTS announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_no       TEXT NOT NULL,                  -- 公告编号（同一列表入口内唯一）
    project_no      TEXT,                           -- 项目编号
    notice_type     TEXT,                           -- first/correction/revocation
    publish_time    TEXT,                           -- 发布时间（ISO 风格字符串）
    deadline        TEXT,                           -- 截止时间（ISO 风格字符串）
    ref_notice_no   TEXT,                           -- 引用编号（仅在本列表入口内解析）
    source_url      TEXT,                           -- 来源（详情页）链接
    list_url        TEXT NOT NULL DEFAULT '',       -- 所属列表页（列表入口）
    content_hash    TEXT,                           -- 内容指纹，用于识别变更
    chain_id        TEXT,                           -- 所属链（本入口内首次公告编号）；孤立公告为 NULL
    chain_status    TEXT,                           -- ok / conflict / cancelled / orphan
    conflict_reasons TEXT,                          -- 冲突原因，JSON 数组字符串
    effective_deadline TEXT,                        -- 有效截止时间（计算结果）
    last_synced_at  TEXT NOT NULL,                  -- 最近一次同步时间
    last_fetch_error TEXT,                          -- 最近一次详情抓取错误（成功后清空）
    UNIQUE(list_url, notice_no)
);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_ann_list ON announcements(list_url);
CREATE INDEX IF NOT EXISTS idx_ann_project ON announcements(project_no);
CREATE INDEX IF NOT EXISTS idx_ann_chain ON announcements(list_url, chain_id);
CREATE INDEX IF NOT EXISTS idx_ann_ref ON announcements(list_url, ref_notice_no);
"""

SCHEMA = TABLE_SQL + INDEX_SQL + """
CREATE TABLE IF NOT EXISTS sync_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL UNIQUE,
    list_url        TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,                  -- success / partial / failed
    total_listed    INTEGER NOT NULL DEFAULT 0,
    fetched_ok      INTEGER NOT NULL DEFAULT 0,
    failed_count    INTEGER NOT NULL DEFAULT 0,
    kept_last       INTEGER NOT NULL DEFAULT 0,
    message         TEXT
);
"""

# 迁移时按列名拷贝，新旧表列集合保持一致
_COLUMNS = (
    "notice_no", "project_no", "notice_type", "publish_time", "deadline",
    "ref_notice_no", "source_url", "list_url", "content_hash", "chain_id",
    "chain_status", "conflict_reasons", "effective_deadline",
    "last_synced_at", "last_fetch_error",
)


def get_db_path() -> Path:
    return config.DB_PATH


def connect() -> sqlite3.Connection:
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _has_legacy_notice_unique(conn: sqlite3.Connection) -> bool:
    """旧版 announcements 表在 notice_no 单列上有 UNIQUE 约束/索引。"""
    for idx in conn.execute("PRAGMA index_list('announcements')").fetchall():
        if not idx["unique"]:
            continue
        info = conn.execute(
            f'PRAGMA index_info("{idx["name"]}")'
        ).fetchall()
        if [r["name"] for r in info] == ["notice_no"]:
            return True
    return False


def _migrate_legacy_announcements(conn: sqlite3.Connection) -> bool:
    """把 notice_no 单列唯一的旧表重建为 (list_url, notice_no) 组合唯一。

    历史数据原样保留（list_url 缺失时补空串），返回是否执行了迁移。
    """
    if not _table_exists(conn, "announcements"):
        return False
    if not _has_legacy_notice_unique(conn):
        return False

    # 重命名后，旧表的具名索引仍占用原名，先显式删除，避免新表的
    # CREATE INDEX IF NOT EXISTS 被同名旧索引跳过。
    conn.execute("ALTER TABLE announcements RENAME TO announcements_legacy")
    for legacy_index in ("idx_ann_project", "idx_ann_chain", "idx_ann_ref"):
        conn.execute(f"DROP INDEX IF EXISTS {legacy_index}")
    conn.executescript(TABLE_SQL + INDEX_SQL)
    col_list = ", ".join(_COLUMNS)
    select_list = ", ".join(
        ("COALESCE(list_url, '')" if col == "list_url" else col)
        for col in _COLUMNS
    )
    conn.execute(
        f"INSERT INTO announcements ({col_list}) "
        f"SELECT {select_list} FROM announcements_legacy"
    )
    conn.execute("DROP TABLE announcements_legacy")
    return True


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_legacy_announcements(conn)
    conn.commit()
