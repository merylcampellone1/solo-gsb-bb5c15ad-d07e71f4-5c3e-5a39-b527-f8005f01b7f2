"""SQLite 存储层：连接管理与版本化 schema 迁移。

数据库路径通过环境变量 DATABASE_PATH 配置，默认 ./names.db。
表结构对 display_names (namespace, normalized_name) 做唯一约束，在数据库
层面兜底规范名重复；同形字符（confusable）判定在应用层完成。
revision 为乐观锁版本号：初始为 1，每次原子改名成功后递增，PATCH 改名
接口以 expected_revision 做条件更新。

name_reservations 保存“预占后确认注册”的预占记录：token 不可猜测且唯一，
expires_at 为 UTC 过期时刻，consumed_at 在确认注册成功后回写（NULL 表示
尚未消费）。未过期、未消费的预占与 display_names 一样参与冲突判定。

namespace_script_policies 保存命名空间级混脚本策略：每个命名空间至多一行，
blocked_script_pairs 以 JSON 文本保存规范对列表（无序、去重、字典序排列），
revision 为乐观锁版本号（初始为 1，每次成功 PUT 递增；未配置的命名空间
逻辑版本视为 0，首次 PUT 必须携带 expected_revision=0）。未配置策略的
命名空间沿用内建默认策略（Latin/Cyrillic/Greek 任意两种混用即违规）。

name_cooldowns 保存“名称释放冷却”记录：DELETE 已注册名称时在同一事务内
删除 display_names 行并写入本表，expires_at 为 UTC 冷却到期时刻。未过期的
冷却记录与 display_names / 有效预占一样参与全部冲突与混脚本判定；到期记录
在后续写事务中惰性物理删除。

版本化迁移机制（取代旧的“启动时幂等建表”）：

- 全部表结构变更定义为 MIGRATIONS 中按版本号递增的不可变迁移；每个迁移
  记录名称与自身 SQL 的 SHA-256 校验和。
- schema_migrations 表记录已应用迁移的 version / name / checksum /
  applied_at，是数据库迁移历史的唯一事实来源。
- 每次启动（init_db / migrate）对每个待应用迁移单独开启 BEGIN IMMEDIATE
  事务：持写锁后重新读取已记录版本，已被并发进程应用的迁移直接跳过，
  否则在同一事务内执行迁移 SQL 并写入记录后原子提交。两个进程并发启动
  时同一迁移只会执行一次。
- 首次接管既有数据库（schema_migrations 为空但业务表已存在）时，按当前
  表 / 列状态识别各迁移效果是否已存在并建立基线（补记已生效迁移），
  随后只执行缺失的迁移；空库则从头执行全部迁移。两条路径最终结构一致。
- 已记录迁移对应的迁移文件被改写（校验和不匹配）、记录了未知版本或版本
  历史出现缺口时，migrate 抛出 MigrationError 并指明具体版本号，应用
  启动失败、不继续服务。

命令行：
    python -m app.database migrate   # 执行全部待应用迁移（幂等）
    python -m app.database status    # 只读输出已应用 / 待应用 / 校验异常（JSON）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from typing import NamedTuple
from urllib.parse import quote

DATABASE_PATH = os.environ.get("DATABASE_PATH", "names.db")

# 迁移历史表：记录已应用迁移的版本、名称、SHA-256 校验和与应用时刻。
# 该表是迁移机制的元数据，自身不属于版本化迁移链，以 IF NOT EXISTS 幂等创建。
SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""


class Migration(NamedTuple):
    """一个不可变迁移：递增版本号、名称与按序执行的 SQL 语句组。"""

    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        """迁移内容的 SHA-256（十六进制）：记录后用于检测迁移文件被改写。"""
        digest = hashlib.sha256()
        for statement in self.statements:
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()


# 迁移链按历史演进顺序重放，保证“空库从头迁移”与“接管既有库建基线后补齐”
# 两条路径得到一致的结构（display_names 的 revision 列都由版本 3 以
# ALTER TABLE 追加，列位置与默认值完全一致）。
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "create_display_names",
        (
            """
            CREATE TABLE display_names (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace       TEXT NOT NULL,
                original_name   TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE (namespace, normalized_name)
            )
            """,
            """
            CREATE INDEX idx_display_names_namespace
                ON display_names (namespace)
            """,
        ),
    ),
    Migration(
        2,
        "create_name_reservations",
        (
            """
            CREATE TABLE name_reservations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace       TEXT NOT NULL,
                original_name   TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                token           TEXT NOT NULL UNIQUE,
                expires_at      TEXT NOT NULL,
                consumed_at     TEXT,
                created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
            # 冲突检测的热路径：按命名空间取未消费预占，再按 expires_at 过滤
            """
            CREATE INDEX idx_name_reservations_namespace_active
                ON name_reservations (namespace, consumed_at, expires_at)
            """,
        ),
    ),
    Migration(
        3,
        "add_display_names_revision",
        (
            # ALTER TABLE ADD COLUMN 带 NOT NULL 必须给出非 NULL 默认值
            """
            ALTER TABLE display_names
                ADD COLUMN revision INTEGER NOT NULL DEFAULT 1
            """,
        ),
    ),
    Migration(
        4,
        "create_namespace_script_policies",
        (
            """
            CREATE TABLE namespace_script_policies (
                namespace            TEXT PRIMARY KEY,
                blocked_script_pairs TEXT NOT NULL,
                revision             INTEGER NOT NULL DEFAULT 1,
                created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
        ),
    ),
    Migration(
        5,
        "create_name_cooldowns",
        (
            """
            CREATE TABLE name_cooldowns (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace        TEXT NOT NULL,
                original_name    TEXT NOT NULL,
                normalized_name  TEXT NOT NULL,
                cooldown_seconds INTEGER NOT NULL,
                expires_at       TEXT NOT NULL,
                created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
            # 冲突检测的热路径：按命名空间取冷却记录，再按 expires_at 过滤
            """
            CREATE INDEX idx_name_cooldowns_namespace
                ON name_cooldowns (namespace, expires_at)
            """,
        ),
    ),
)

_MIGRATIONS_BY_VERSION = {migration.version: migration for migration in MIGRATIONS}

# 迁移版本必须严格递增且无缺口（开发期防线，导入时即失败）
assert [m.version for m in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1)), (
    "MIGRATIONS 版本号必须从 1 开始连续递增"
)


class MigrationError(RuntimeError):
    """迁移校验或执行失败：错误信息指明具体版本号，应用必须中止启动。"""


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _detect_legacy_applied(conn: sqlite3.Connection) -> set[int]:
    """识别既有数据库（无迁移历史表）中各迁移效果是否已存在，用于建立基线。

    按表 / 列的存在性判定：表已存在视为对应建表迁移已生效；
    display_names 已含 revision 列视为列迁移已生效。
    """
    tables = _table_names(conn)
    applied: set[int] = set()
    if "display_names" in tables:
        applied.add(1)
        if "revision" in _column_names(conn, "display_names"):
            applied.add(3)
    if "name_reservations" in tables:
        applied.add(2)
    if "namespace_script_policies" in tables:
        applied.add(4)
    if "name_cooldowns" in tables:
        applied.add(5)
    return applied


def _read_recorded(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    """读取 schema_migrations 中已记录的迁移（按版本升序），须在建表之后调用。"""
    rows = conn.execute(
        "SELECT version, name, checksum, applied_at "
        "FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {row["version"]: row for row in rows}


def _validate_recorded(
    recorded: dict[int, sqlite3.Row], *, check_gaps: bool
) -> None:
    """校验已记录迁移与迁移文件一致：未被改写、无未知版本、可选地检查版本缺口。"""
    for version in sorted(recorded):
        row = recorded[version]
        migration = _MIGRATIONS_BY_VERSION.get(version)
        if migration is None:
            highest = max(_MIGRATIONS_BY_VERSION)
            raise MigrationError(
                f"schema_migrations 记录了未知迁移版本 {version}"
                f"（当前迁移文件最高版本为 {highest}）：数据库可能由更新版本的"
                "应用创建，拒绝启动"
            )
        if row["name"] != migration.name or row["checksum"] != migration.checksum:
            raise MigrationError(
                f"迁移版本 {version}（{migration.name}）的记录与迁移文件不一致："
                "迁移文件在应用后被改写（名称或 SHA-256 校验和不匹配），拒绝启动"
            )
    if check_gaps and recorded:
        highest_recorded = max(recorded)
        for migration in MIGRATIONS:
            if migration.version < highest_recorded and migration.version not in recorded:
                raise MigrationError(
                    f"迁移版本出现缺口：版本 {migration.version}"
                    f"（{migration.name}）未应用，但更高的版本 {highest_recorded}"
                    " 已记录，迁移历史不一致，拒绝启动"
                )


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    """在 BEGIN IMMEDIATE 事务内幂等创建迁移历史表（并发启动安全）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(SCHEMA_MIGRATIONS_DDL)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _baseline_if_first_takeover(conn: sqlite3.Connection) -> bool:
    """schema_migrations 为空时按既有表/列状态建立基线；返回是否执行了基线。

    整个判定与写入在单个 BEGIN IMMEDIATE 事务内完成：并发进程同时首次接管
    同一数据库时，后到者在持锁重读后看到基线已存在，直接跳过不重复写入
    （其后的逐迁移应用会校验这些记录的校验和）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        recorded = _read_recorded(conn)
        if recorded:
            # 并发进程已抢先建立基线：本事务不再写入
            conn.commit()
            return False
        for version in sorted(_detect_legacy_applied(conn)):
            migration = _MIGRATIONS_BY_VERSION[version]
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum) "
                "VALUES (?, ?, ?)",
                (migration.version, migration.name, migration.checksum),
            )
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise


def _apply_one(conn: sqlite3.Connection, migration: Migration) -> bool:
    """在单个 BEGIN IMMEDIATE 事务内应用一个迁移；已被并发应用则跳过。

    持写锁后重新读取已记录版本并校验（防改写 / 防未知版本），该迁移已记录
    则直接提交返回；否则在同一事务内执行全部迁移语句并写入历史记录后原子
    提交。任一语句失败整体回滚，不会留下半个迁移。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        recorded = _read_recorded(conn)
        _validate_recorded(recorded, check_gaps=False)
        if migration.version in recorded:
            conn.commit()
            return False
        for statement in migration.statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum) "
            "VALUES (?, ?, ?)",
            (migration.version, migration.name, migration.checksum),
        )
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise


def migrate(path: str | None = None) -> list[int]:
    """执行全部待应用迁移（幂等），返回本次实际应用的版本号列表。

    首次接管既有数据库时先按表/列状态建立基线；空库从头执行全部迁移。
    每个迁移在独立的 BEGIN IMMEDIATE 事务中原子提交，并发启动的多个进程
    不会重复执行同一迁移。迁移文件被改写、出现未知版本或版本缺口时抛出
    MigrationError（含具体版本号），调用方应中止启动。
    """
    target = path or DATABASE_PATH
    if target != ":memory:":
        directory = os.path.dirname(os.path.abspath(target))
        os.makedirs(directory, exist_ok=True)
    # isolation_level=None：关闭隐式事务，BEGIN IMMEDIATE / COMMIT 全部显式控制；
    # timeout=30：并发迁移时等待对端提交，而不是立刻 SQLITE_BUSY
    conn = sqlite3.connect(target, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_migrations_table(conn)
        # 已有迁移历史（非首次接管）：先在 BEGIN IMMEDIATE 中重读并严格校验
        # （迁移文件被改写 / 未知版本 / 版本缺口 -> MigrationError，拒绝启动）
        conn.execute("BEGIN IMMEDIATE")
        try:
            recorded = _read_recorded(conn)
            if recorded:
                _validate_recorded(recorded, check_gaps=True)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if not recorded:
            # 首次接管：既有库按表/列状态建立基线，空库不记录任何基线
            _baseline_if_first_takeover(conn)
        applied = []
        for migration in MIGRATIONS:
            if _apply_one(conn, migration):
                applied.append(migration.version)
        return applied
    finally:
        conn.close()


def migration_status(path: str | None = None) -> dict:
    """只读返回迁移状态：已应用、待应用与校验异常。不创建/修改任何数据库对象。

    以只读模式打开数据库（文件不存在时不创建），不建表、不建基线、不执行
    迁移；schema_migrations 尚不存在的既有库视为全部待应用。
    """
    target = path or DATABASE_PATH
    pending = [
        {"version": m.version, "name": m.name, "checksum": m.checksum}
        for m in MIGRATIONS
    ]
    result: dict = {
        "database": target,
        "applied": [],
        "pending": pending,
        "issues": [],
    }
    if target == ":memory:" or not os.path.exists(target):
        return result

    # 只读模式打开（mode=ro）：不会创建/修改文件，也不会触发日志恢复写入
    conn = sqlite3.connect(
        f"file:{quote(os.path.abspath(target))}?mode=ro", uri=True
    )
    conn.row_factory = sqlite3.Row
    try:
        if "schema_migrations" not in _table_names(conn):
            return result
        recorded = _read_recorded(conn)
    finally:
        conn.close()

    applied = []
    issues = result["issues"]
    for version in sorted(recorded):
        row = recorded[version]
        migration = _MIGRATIONS_BY_VERSION.get(version)
        if migration is None:
            issues.append(
                {
                    "version": version,
                    "type": "unknown_version",
                    "message": (
                        f"schema_migrations 记录了未知迁移版本 {version}"
                        f"（当前迁移文件最高版本为 {max(_MIGRATIONS_BY_VERSION)}）"
                    ),
                }
            )
            continue
        applied.append(
            {
                "version": version,
                "name": row["name"],
                "checksum": row["checksum"],
                "applied_at": row["applied_at"],
            }
        )
        if row["name"] != migration.name or row["checksum"] != migration.checksum:
            issues.append(
                {
                    "version": version,
                    "type": "checksum_mismatch",
                    "message": (
                        f"迁移版本 {version}（{migration.name}）的记录与迁移文件"
                        "不一致：迁移文件在应用后被改写"
                    ),
                }
            )
    if recorded:
        highest_recorded = max(recorded)
        for migration in MIGRATIONS:
            if migration.version < highest_recorded and migration.version not in recorded:
                issues.append(
                    {
                        "version": migration.version,
                        "type": "version_gap",
                        "message": (
                            f"迁移版本 {migration.version}（{migration.name}）未应用，"
                            f"但更高的版本 {highest_recorded} 已记录"
                        ),
                    }
                )
    result["applied"] = applied
    result["pending"] = [
        item for item in pending if item["version"] not in recorded
    ]
    return result


def init_db(path: str | None = None) -> str:
    """执行版本化迁移（建库建表、基线接管，幂等），返回实际使用的数据库路径。"""
    target = path or DATABASE_PATH
    migrate(target)
    return target


def get_connection() -> sqlite3.Connection:
    """新建一个连接（按行名字典化返回），由调用方负责关闭。

    check_same_thread=False：FastAPI 依赖与端点可能运行在不同线程池
    线程中；每个连接仍只属一个请求、串行使用，跨线程访问是安全的。
    """
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    # 写事务（BEGIN IMMEDIATE）竞争时等待对端提交，而不是立刻 SQLITE_BUSY
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.database",
        description="版本化数据库迁移：migrate 执行待应用迁移，status 只读查看状态",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "migrate", help="执行全部待应用迁移（幂等；校验失败退出码为 1）"
    )
    subparsers.add_parser(
        "status", help="只读输出已应用 / 待应用迁移与校验异常（JSON）"
    )
    args = parser.parse_args(argv)
    command = args.command or "migrate"

    if command == "status":
        print(json.dumps(migration_status(), ensure_ascii=False, indent=2))
        return 0

    try:
        applied = migrate()
    except MigrationError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    if applied:
        print(f"database migrated: {DATABASE_PATH} (applied versions: {applied})")
    else:
        print(f"database up to date: {DATABASE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
