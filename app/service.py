"""业务逻辑：名称分析、注册与“预占后确认注册”。

判定顺序（单条注册 / 预占时）：
1. 混脚本违规（Latin/Cyrillic/Greek 出现两种及以上）-> 422
2. 与同命名空间已有名称及未过期、未消费预占逐条比较：规范名相同或
   confusables 判定为同形 -> 409，并返回全部冲突记录
3. 落库；display_names 的 (namespace, normalized_name) 唯一约束兜底并发写入

预占（create_reservation）：
1. 先做与注册一致的只读分析（混脚本 -> 422，冲突 -> 409）
2. BEGIN IMMEDIATE 取写锁，惰性清理过期预占后复查冲突，生成不可猜测
   token 并写入 name_reservations

确认注册（claim_reservation）：
1. BEGIN IMMEDIATE 单事务内按序校验：token 存在且命名空间匹配
   （不匹配/未知 -> 404）、有效期（过期 -> 410，顺手惰性删除）、
   消费状态（已消费 -> 409）、当前冲突（与已注册名称或其他有效预占
   冲突 -> 409）
2. 通过则在同一事务写入 display_names、回写 consumed_at；唯一约束
   兜底并发，整段失败即回滚，同一 token 并发或重复确认只会成功一次

批量注册（analyze_batch / insert_batch）：
1. 整批先复用单条分析（已含有效预占冲突），再对批内两两做规范名 /
   同形判定，命中时按原请求索引互相标注在双方 batch_conflicts 中
2. 任一项混脚本或存在（库内 / 预占 / 批内）冲突 -> 422 汇总明细，不落库
3. 全部通过 -> BEGIN IMMEDIATE 单事务落库，事务内复查冲突以锁定
   判定；并发竞争使判定失效 -> 回滚 -> 由调用方重新分析并返回 409

命名空间合并（find_merge_conflicts / merge_namespaces）：
1. 单个 BEGIN IMMEDIATE 事务内先惰性清理两侧过期预占，再对源与目标的
   已注册名称及有效预占做跨空间规范名 / 同形复查（同一命名空间内部在
   写入时已保证互不冲突，只需检查跨空间组合）
2. 任一冲突 -> 回滚并抛 MergeConflictError（携带双方记录），不修改数据
3. 无冲突 -> 同一事务内把源命名空间的 display_names 与 name_reservations
   整体 UPDATE 到目标并返回迁移数量；唯一约束 / 锁竞争等并发异常同样
   整体回滚，由调用方重新检测后返回 409

原子改名（rename_name）：
1. 单个 BEGIN IMMEDIATE 事务内按序校验：记录存在且命名空间匹配
   （不匹配/未知 -> NameNotFoundError -> 404）、expected_revision 与当前
   revision 一致（过期 -> StaleRevisionError -> 409）、新名称不混脚本
   （违规 -> MixedScriptNameError -> 422）、新规范名与同空间其他已注册
   名称及有效预占无冲突（排除当前记录自身，预占全部参与 -> 409）
2. 全部通过才在同一事务内 UPDATE original_name / normalized_name 并
   revision = revision + 1；条件 UPDATE（AND revision = ?）兜底并发，
   id / created_at 保持不变，任何失败都回滚

原子交换（swap_names）：
1. 单个 BEGIN IMMEDIATE 事务内校验两条记录都存在且同属 URL 命名空间
   （任一缺失/跨空间 -> SwapRecordNotFoundError -> 404），双方 revision
   都等于各自 expected_revision（任一过期 -> SwapStaleRevisionError ->
   409，明细同时携带双方当前 revision）
2. UNIQUE(namespace, normalized_name) 是即时约束，不能直接互改：在同一
   事务内用随机临时名先腾出一条记录的规范名槽位（临时名仅存在于未提交
   事务中，其他连接只能读到交换前快照），再三步条件 UPDATE 成对交换
   original_name / normalized_name，仅后两步令双方 revision 各 + 1；
   id / created_at 不变，任何失败整体回滚，不存在部分更新

命名空间混脚本策略（namespace_script_policies）：
1. 每个命名空间至多一条策略记录：blocked_script_pairs 为无序去重的规范
   脚本对（JSON 存储），revision 初始为 1、每次成功 PUT 递增；未配置的
   命名空间逻辑版本为 0，并沿用内建默认策略（Latin/Cyrillic/Greek 任意
   两种混用即违规）
2. PUT 在单个 BEGIN IMMEDIATE 事务内先校验 expected_revision（与行存在
   性组合：未配置时当前版本按 0 计；不一致 -> StalePolicyRevisionError
   -> 409），再用新策略扫描该空间全部已注册名称与有效预占；任一现存
   名称违规 -> ScriptPolicyConflictError（携带逐条明细）-> 409，策略
   不更新；全部通过才写入并把 revision + 1。由此维持不变量：策略生效后，
   空间内现存名称与有效预占始终合规
3. 注册 / 批量 / 预占 / 改名 / check 全部按“所属命名空间”的生效策略
   判定混脚本；写入类操作在写锁事务内重新加载策略并复查，使并发的策略
   修改与名称写入互不破坏。预占确认（claim）无需复查：策略 PUT 会扫描
   全部有效预占，确认只是把已合规的预占转成已注册名称；交换（swap）不
   引入新名称，同样无需复查
4. 合并（merge）以目标命名空间的策略检查源、目标两侧的已注册名称与有效
   预占；目标侧数据在不变量下必然合规（实际违规集为空，但仍按要求扫描），
   源侧任一记录违规即随跨空间冲突一起 409 且不迁移任何数据

名称释放冷却（name_cooldowns，DELETE 接口）：
1. DELETE /namespaces/{namespace}/names/{id} 携带 expected_revision 与
   cooldown_seconds（60–86400），在单个 BEGIN IMMEDIATE 事务内校验归属与
   版本，通过后删除 display_names 行并写入一条带 expires_at 的冷却记录
2. 未过期冷却记录与已注册名称、有效预占完全等价地参与单条 / 批量注册、
   预占、改名、check 的规范名 / 同形判定（冲突明细 source="released"，
   附 expires_at），也参与命名空间合并的跨空间复查 / 整体迁移，以及
   脚本策略 PUT 的影响面扫描（违规明细 kind="released"）
3. 过期冷却记录不参与任何判定，在每个写事务开始时惰性物理删除；
   GET /namespaces/{namespace}/cooldowns 只读列出仍有效的冷却记录

跨空间同形风险簇（find_name_risk_clusters，POST /name-risk-clusters）：
1. 接收 2–20 个互不相同的命名空间与 max_names（1–500），在单个只读
   事务（BEGIN 读事务）中依次读取这些空间的已注册名称、未过期未消费
   预占与未过期冷却；同一事务保证三类数据来自同一份数据库快照，并发
   写入在事务期间提交也不改变本函数看到的节点集合。只读路径不做任何
   惰性清理，过期 / 已消费记录在 Python 侧过滤
2. 快照节点总量超过 max_names 时抛 RiskSnapshotTooLargeError（-> 413），
   绝不截断快照后继续计算
3. 只对不同命名空间的节点建边：normalized_name 相同记 normalized_match
   （先按规范名分组、组内跨空间两两连边），否则 is_confusable 同形记
   confusable（按规范名去重后两两判定，同规范名不再重复比较）；同空间
   内部不建边。并查集求连通分量，只保留至少含 2 个节点的分量：A-B、
   B-C 即使 A-C 不直接冲突也归入同一簇
4. 节点稳定标识为 (namespace, kind, id)，kind 排序 registered <
   reservation < released，预占与冷却附 expires_at；边端点小者为
   source 并附 reasons；簇按最小节点键排序、cluster_id 从 1 编号，
   簇内节点与边均确定性排序

快照 Merkle 包含证明（build_snapshot_proof，
POST /namespaces/{namespace}/snapshot-proof）：
1. 接收 1–20 个由 kind/id 表示的节点与 max_nodes（1–500），在单个只读
   事务（BEGIN 读事务）中固定命名空间的生效脚本策略、已注册名称、未过期
   未消费预占与未过期冷却；四类数据共享同一数据库快照，只读路径不做惰性
   清理，过期 / 已消费记录在 Python 侧过滤
2. 业务快照节点总量（不含策略叶）超过 max_nodes 即抛
   SnapshotTooLargeError（-> 413），不截断、不建树；随后校验请求节点全部
   存在于快照，任一缺失抛 SnapshotNodesNotFoundError（-> 404，附全部
   缺失项）
3. 叶集合为 1 个恒存在的 script_policy 叶（未配置时固定内建默认策略，
   revision=0；下标恒为 0）加全部业务叶，按 (类型序, id) 排序后把业务
   字段编码为规范 JSON（键排序、紧凑分隔、UTF-8 原样非 ASCII），以
   SHA-256 构建 Merkle 树：叶 H(0x00||json)，父 H(0x01||left||right)，
   奇数层复制末节点。证明为自叶向上的兄弟哈希 + left/right 方向，只由
   排序后下标决定——相同快照下请求节点顺序变化不改变根与各自证明

候选集原子注册（allocate_names，
POST /namespaces/{namespace}/names/allocate）：
1. 接收 1–20 个申请项（client_id 请求内唯一，各带 1–10 个按优先级排列
   的候选名），全部工作在单个 BEGIN IMMEDIATE 写锁事务内：先惰性清理
   过期预占与冷却，再重新加载命名空间生效脚本策略、一次性读取全部既有
   占用（已注册名称 + 有效预占 + 未过期冷却）
2. 对每个候选做与单条注册一致的静态判定（混脚本 / 规范名 / 同形；同形
   判定按无序规范名对在请求内缓存），不通过只标记该候选不可选并记录
   逐候选原因；再以 app.allocation.AllocationModel 预计算项间冲突位掩码
   （不同项选中候选规范名相等 / 同形即冲突）
3. 回溯搜索字典序最小的完整分配（候选按序号升序试探 + 前向检查剪枝）；
   搜索状态超过 10000 抛 AllocationSearchTooLargeError（-> 413），无解
   抛 AllocationImpossibleError（-> 409，附无可选项及逐候选静态原因与
   项间冲突），两者均回滚不写库；有解才在同一事务一次性 INSERT 全部
   名称并按请求顺序返回记录（附 client_id / 中选 candidate_index）
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from confusables import is_confusable

from .allocation import (
    MAX_CANDIDATES_PER_ITEM,
    AllocationModel,
    AllocationSearchTooLargeError,
    CandidateSpec,
)
from .analysis import (
    DEFAULT_BLOCKED_PAIRS,
    analyze_scripts,
    normalize_name,
)
from .merkle import inclusion_proof, leaf_hash, merkle_root

REASON_NORMALIZED_MATCH = "normalized_match"
REASON_CONFUSABLE = "confusable"

SOURCE_REGISTERED = "registered"
SOURCE_RESERVATION = "reservation"
SOURCE_RELEASED = "released"

# token 使用 URL 安全随机字节；仅含 A-Za-z0-9_-，可直接放在路径中
TOKEN_BYTES = 32

# 预占 TTL 边界（与 schemas.ReservationRequest 的校验保持一致）
MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 600

# 释放冷却时长边界（与 schemas.ReleaseRequest 的校验保持一致）
MIN_COOLDOWN_SECONDS = 60
MAX_COOLDOWN_SECONDS = 86400


class NameConflictError(Exception):
    """数据库唯一约束冲突或事务内复查发现冲突（并发写入）。

    conflicts 可选：事务内复查发现冲突时带上最新冲突明细，供调用方直接
    返回 409，避免回滚后再查一次。
    """

    def __init__(self, value: str | None = None, conflicts: list[dict] | None = None):
        super().__init__(value or "name_conflict")
        self.conflicts = conflicts


class ReservationNotFoundError(Exception):
    """token 不存在或不属于该命名空间。"""


class ReservationExpiredError(Exception):
    """预占已过期（HTTP 410 Gone）。"""


class ReservationConsumedError(Exception):
    """预占已被确认消费：重复或并发确认（HTTP 409 Conflict）。"""


class MergeConflictError(Exception):
    """合并命名空间时发现跨空间冲突 / 目标策略脚本违规，或并发写入导致
    合并事务失败。

    conflicts 为冲突对列表，每项含 reason 与双方记录（source_record /
    target_record）；script_violations 为按目标命名空间策略扫描两侧数据
    得到的逐条违规明细，每项带 side（source / target）与记录信息。
    唯一约束 / 锁竞争等并发异常兜底时二者均为空，由调用方在回滚后重新检测。
    """

    def __init__(
        self,
        conflicts: list[dict] | None = None,
        script_violations: list[dict] | None = None,
    ):
        super().__init__("merge_conflict")
        self.conflicts = conflicts or []
        self.script_violations = script_violations or []


class NameNotFoundError(Exception):
    """已注册名称记录不存在，或不属于指定命名空间（HTTP 404）。"""


class StaleRevisionError(Exception):
    """expected_revision 与当前 revision 不一致（HTTP 409 Conflict）。

    current_revision 为数据库中的最新版本，供调用方提示客户端重读后重试。
    """

    def __init__(self, current_revision: int):
        super().__init__("stale_revision")
        self.current_revision = current_revision


class MixedScriptNameError(Exception):
    """新名称混用 Latin/Cyrillic/Greek 中两种及以上脚本（HTTP 422）。

    analysis 携带脚本分析明细，供接口层复用 _mixed_script_error 的响应结构。
    """

    def __init__(self, analysis: dict):
        super().__init__("mixed_script_violation")
        self.analysis = analysis


class SwapRecordNotFoundError(Exception):
    """交换双方中有记录不存在，或不属于指定命名空间（HTTP 404）。

    missing_ids 为缺失（不存在 / 跨命名空间）的记录 id 列表，按请求顺序给出。
    """

    def __init__(self, missing_ids: list[int]):
        super().__init__("name_not_found")
        self.missing_ids = missing_ids


class SwapStaleRevisionError(Exception):
    """交换双方中有记录的 revision 与 expected_revision 不一致（HTTP 409）。

    records 始终按请求顺序同时给出双方的 id / expected_revision /
    current_revision，供接口层一次性返回双方当前版本，客户端重读后可
    整体重试。
    """

    def __init__(self, records: list[dict]):
        super().__init__("stale_revision")
        self.records = records


class ScriptPolicyNotFoundError(Exception):
    """命名空间尚未配置混脚本策略（GET 时 HTTP 404）。"""


class StalePolicyRevisionError(Exception):
    """策略 PUT 的 expected_revision 与当前版本不一致（HTTP 409 Conflict）。

    未配置时 current_revision 为 0；已配置时为库中最新 revision。
    """

    def __init__(self, current_revision: int):
        super().__init__("stale_revision")
        self.current_revision = current_revision


class ScriptPolicyConflictError(Exception):
    """新策略会使命名空间内现存名称 / 有效预占违规（HTTP 409 Conflict）。

    violations 为逐条违规明细，每项含记录（id / kind / original_name /
    normalized_name，预占附 expires_at）与命中的 violating_script_pairs。
    """

    def __init__(self, violations: list[dict]):
        super().__init__("script_policy_conflict")
        self.violations = violations


class BatchMixedScriptError(Exception):
    """批量写事务内复查发现混脚本违规（并发策略修改使批次前判定失效）。

    items 为违规项的事务内重新分析结果（含 index 与脚本明细），由接口层
    以 409 返回，语义与并发冲突回滚后的重新分析一致。
    """

    def __init__(self, items: list[dict]):
        super().__init__("mixed_script_violation")
        self.items = items


class RiskSnapshotTooLargeError(Exception):
    """跨空间风险簇快照节点总量超过请求方给定的 max_names（HTTP 413）。

    snapshot_count 为同一读事务快照内的实际节点总数（已注册名称 + 有效预占
    + 未过期冷却），limit 为请求方给定的上限；超限后不做任何分簇计算，
    绝不为了凑上限而截断快照。
    """

    def __init__(self, snapshot_count: int, limit: int):
        super().__init__("risk_snapshot_too_large")
        self.snapshot_count = snapshot_count
        self.limit = limit


class SnapshotTooLargeError(Exception):
    """命名空间快照业务节点总量超过请求方给定的 max_nodes（HTTP 413）。

    snapshot_count 为同一读事务快照内的实际业务节点总数（已注册名称 +
    有效预占 + 未过期冷却，不含脚本策略叶），limit 为请求方给定的上限；
    超限后不构建树、不生成任何证明，绝不为了凑上限而截断快照。
    """

    def __init__(self, snapshot_count: int, limit: int):
        super().__init__("snapshot_too_large")
        self.snapshot_count = snapshot_count
        self.limit = limit


class SnapshotNodesNotFoundError(Exception):
    """请求包含证明的节点不存在于快照中（不存在 / 已过期 / 已消费，HTTP 404）。

    missing 为缺失节点引用列表，每项 {"kind": ..., "id": ...}，按请求顺序给出。
    """

    def __init__(self, missing: list[dict]):
        super().__init__("snapshot_nodes_not_found")
        self.missing = missing


class AllocationImpossibleError(Exception):
    """候选集原子注册不存在可行组合（HTTP 409 allocation_impossible）。

    items 为“无可选项”的申请项明细（按请求索引升序），每项给出 client_id 与
    逐候选阻断原因；searched_states 为搜索访问的状态数。该异常只在写锁事务
    回滚后由接口层读取，数据库未发生任何写入。
    """

    def __init__(self, items: list[dict], searched_states: int):
        super().__init__("allocation_impossible")
        self.items = items
        self.searched_states = searched_states


def utc_now() -> datetime:
    """当前 UTC 时刻（naive，按 UTC 解释）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_now_iso() -> str:
    """当前 UTC 时刻的 ISO 8601 字符串（毫秒精度，Z 后缀）。"""
    return utc_now().isoformat(timespec="milliseconds") + "Z"


def parse_expires_at(value: str) -> datetime:
    """解析 expires_at（Z 结尾的 UTC ISO 8601）为 naive datetime。"""
    return datetime.fromisoformat(value.removesuffix("Z")).replace(tzinfo=None)


# ---------- 命名空间混脚本策略 ----------


def get_script_policy(conn: sqlite3.Connection, namespace: str) -> dict | None:
    """读取命名空间已配置的混脚本策略行；未配置返回 None。"""
    row = conn.execute(
        "SELECT * FROM namespace_script_policies WHERE namespace = ?",
        (namespace,),
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["blocked_script_pairs"] = [
        tuple(pair) for pair in json.loads(record["blocked_script_pairs"])
    ]
    return record


def get_blocked_pairs(
    conn: sqlite3.Connection, namespace: str
) -> tuple[tuple[str, str], ...]:
    """读取命名空间生效中的阻断脚本对；未配置时返回内建默认策略。

    已配置但 blocked_script_pairs 为空列表时返回空元组——表示显式配置为
    “不拦截任何脚本组合”，与未配置（默认策略）语义不同。
    """
    policy = get_script_policy(conn, namespace)
    if policy is None:
        return DEFAULT_BLOCKED_PAIRS
    return tuple(policy["blocked_script_pairs"])


def _script_violation_entry(row, kind: str, pairs) -> dict | None:
    """按策略 pairs 分析单条记录的原名；违规则返回明细，否则 None。

    row 需含 id / original_name / normalized_name；kind 区分已注册名称
    （registered）、有效预占（reservation）与未过期释放冷却（released），
    后两者附 expires_at。
    """
    analysis = analyze_scripts(row["original_name"], pairs)
    if not analysis["mixed_script_violation"]:
        return None
    entry = {
        "id": row["id"],
        "kind": kind,
        "original_name": row["original_name"],
        "normalized_name": row["normalized_name"],
        "violating_script_pairs": analysis["violating_script_pairs"],
        "script_details": analysis["script_details"],
    }
    if kind in (SOURCE_RESERVATION, SOURCE_RELEASED):
        entry["expires_at"] = row["expires_at"]
    return entry


def find_script_violations(
    conn: sqlite3.Connection,
    namespace: str,
    pairs,
    now: datetime | None = None,
) -> list[dict]:
    """扫描命名空间内违反给定脚本策略的已注册名称、有效预占与冷却记录。

    已注册名称按 id 排序在前，有效预占按 id 排序居中，未过期冷却按 id
    排序在后；过期 / 已消费预占与过期冷却不参与（调用方一般已在写事务内
    完成惰性清理）。
    """
    moment = now or utc_now()
    violations: list[dict] = []
    for row in conn.execute(
        """
        SELECT id, original_name, normalized_name
        FROM display_names
        WHERE namespace = ?
        ORDER BY id
        """,
        (namespace,),
    ).fetchall():
        entry = _script_violation_entry(row, SOURCE_REGISTERED, pairs)
        if entry is not None:
            violations.append(entry)
    for row in _active_reservation_rows(conn, namespace, moment):
        entry = _script_violation_entry(row, SOURCE_RESERVATION, pairs)
        if entry is not None:
            violations.append(entry)
    for row in _active_cooldown_rows(conn, namespace, moment):
        entry = _script_violation_entry(row, SOURCE_RELEASED, pairs)
        if entry is not None:
            violations.append(entry)
    return violations


def get_script_policy_or_404(
    conn: sqlite3.Connection, namespace: str
) -> dict:
    """供 GET 接口使用：未配置即抛 ScriptPolicyNotFoundError。"""
    policy = get_script_policy(conn, namespace)
    if policy is None:
        raise ScriptPolicyNotFoundError(namespace)
    policy["blocked_script_pairs"] = [
        list(pair) for pair in policy["blocked_script_pairs"]
    ]
    return policy


def put_script_policy(
    conn: sqlite3.Connection,
    namespace: str,
    blocked_pairs: list[tuple[str, str]],
    expected_revision: int,
) -> dict:
    """单个 BEGIN IMMEDIATE 事务内校验版本并更新命名空间混脚本策略。

    事务内按序校验：
    1. 版本：未配置时当前版本按 0 计；库中 revision（或 0）必须等于
       expected_revision，否则 StalePolicyRevisionError -> 409
    2. 影响面：用新策略扫描该空间全部已注册名称与有效预占，任一现存
       名称违规 -> ScriptPolicyConflictError（携带逐条明细）-> 409，
       策略不更新

    全部通过后写入（首次配置为 INSERT，revision=1；之后为条件 UPDATE，
    revision + 1），任何失败均回滚。并发插入同命名空间策略（唯一约束）时
    回滚后重读：行已存在则归类为 StalePolicyRevisionError；锁竞争且行仍
    不存在时抛 ScriptPolicyConflictError（violations 为空），均由调用方
    按 409 处理，客户端重读后可重试。
    """
    serialized = json.dumps(
        [list(pair) for pair in blocked_pairs], separators=(",", ":"), sort_keys=True
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 持锁期间惰性清理过期预占与冷却，保证影响面扫描只含有效记录
        purge_expired_reservations(conn, namespace)
        purge_expired_cooldowns(conn, namespace)

        row = conn.execute(
            "SELECT revision FROM namespace_script_policies WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        current_revision = row["revision"] if row is not None else 0

        # 1. 乐观锁版本校验（未配置时逻辑版本为 0）
        if current_revision != expected_revision:
            raise StalePolicyRevisionError(current_revision)

        # 2. 新策略不得使现存已注册名称或有效预占违规
        violations = find_script_violations(conn, namespace, blocked_pairs)
        if violations:
            raise ScriptPolicyConflictError(violations)

        if current_revision == 0:
            cursor = conn.execute(
                """
                INSERT INTO namespace_script_policies
                    (namespace, blocked_script_pairs, revision)
                VALUES (?, ?, 1)
                """,
                (namespace, serialized),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE namespace_script_policies
                   SET blocked_script_pairs = ?, revision = revision + 1,
                       updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                 WHERE namespace = ? AND revision = ?
                """,
                (serialized, namespace, expected_revision),
            )
            if cursor.rowcount != 1:
                # 条件更新兜底（持写锁后理论上不会发生）：重读当前版本
                refreshed = conn.execute(
                    "SELECT revision FROM namespace_script_policies WHERE namespace = ?",
                    (namespace,),
                ).fetchone()
                raise StalePolicyRevisionError(
                    refreshed["revision"] if refreshed else 0
                )
        conn.commit()
    except (StalePolicyRevisionError, ScriptPolicyConflictError):
        conn.rollback()
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # 并发插入同命名空间策略（唯一约束）/ 锁竞争：回滚后重读状态，
        # 尽量归类为 stale_revision（行已存在），由客户端重读后重试
        conn.rollback()
        refreshed = conn.execute(
            "SELECT revision FROM namespace_script_policies WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        if refreshed is not None:
            raise StalePolicyRevisionError(refreshed["revision"]) from exc
        raise ScriptPolicyConflictError([]) from exc

    return get_script_policy_or_404(conn, namespace)


def _conflict_entry(row, source: str, reason: str) -> dict:
    entry = {
        "id": row["id"],
        "original_name": row["original_name"],
        "normalized_name": row["normalized_name"],
        "reason": reason,
        "source": source,
    }
    if source in (SOURCE_RESERVATION, SOURCE_RELEASED):
        entry["expires_at"] = row["expires_at"]
    return entry


def _active_reservation_rows(
    conn: sqlite3.Connection, namespace: str, now: datetime | None = None
) -> list[sqlite3.Row]:
    """读取未消费预占并在 Python 侧剔除已过期记录。

    expires_at 由应用以 UTC ISO 8601 写入，直接按字符串比较即可；放在
    Python 侧过滤可避免 SQLite 内置 strftime 毫秒格式（%f）差异带来的
    边界问题。
    """
    moment = now or utc_now()
    rows = conn.execute(
        """
        SELECT id, original_name, normalized_name, expires_at
        FROM name_reservations
        WHERE namespace = ? AND consumed_at IS NULL
        ORDER BY id
        """,
        (namespace,),
    ).fetchall()
    return [
        row
        for row in rows
        if parse_expires_at(row["expires_at"]) > moment
    ]


def _active_cooldown_rows(
    conn: sqlite3.Connection, namespace: str, now: datetime | None = None
) -> list[sqlite3.Row]:
    """读取释放冷却记录并在 Python 侧剔除已过期记录（与预占同构）。"""
    moment = now or utc_now()
    rows = conn.execute(
        """
        SELECT id, original_name, normalized_name, expires_at
        FROM name_cooldowns
        WHERE namespace = ?
        ORDER BY id
        """,
        (namespace,),
    ).fetchall()
    return [
        row
        for row in rows
        if parse_expires_at(row["expires_at"]) > moment
    ]


def purge_expired_reservations(
    conn: sqlite3.Connection, namespace: str | None = None, now: datetime | None = None
) -> None:
    """惰性清理：删除已过期且尚未消费的预占记录。

    必须在已取得写锁的事务中调用（调用方负责 BEGIN IMMEDIATE）。
    过期判定在 Python 侧完成（见 _active_reservation_rows），按 id 删除。
    namespace 为 None 时清理所有命名空间。
    """
    moment = now or utc_now()
    if namespace is None:
        rows = conn.execute(
            "SELECT id, expires_at FROM name_reservations WHERE consumed_at IS NULL"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, expires_at FROM name_reservations "
            "WHERE namespace = ? AND consumed_at IS NULL",
            (namespace,),
        ).fetchall()
    expired_ids = [
        row["id"]
        for row in rows
        if parse_expires_at(row["expires_at"]) <= moment
    ]
    if expired_ids:
        conn.executemany(
            "DELETE FROM name_reservations WHERE id = ?",
            [(id_,) for id_ in expired_ids],
        )


def purge_expired_cooldowns(
    conn: sqlite3.Connection, namespace: str | None = None, now: datetime | None = None
) -> None:
    """惰性清理：删除已过期的释放冷却记录（必须在写锁事务中调用）。

    与 purge_expired_reservations 同构；namespace 为 None 时清理所有命名空间。
    """
    moment = now or utc_now()
    sql = "SELECT id, expires_at FROM name_cooldowns"
    params: tuple = ()
    if namespace is not None:
        sql += " WHERE namespace = ?"
        params = (namespace,)
    rows = conn.execute(sql, params).fetchall()
    expired_ids = [
        row["id"]
        for row in rows
        if parse_expires_at(row["expires_at"]) <= moment
    ]
    if expired_ids:
        conn.executemany(
            "DELETE FROM name_cooldowns WHERE id = ?",
            [(id_,) for id_ in expired_ids],
        )


def find_conflicts(
    conn: sqlite3.Connection,
    namespace: str,
    normalized_name: str,
    exclude_reservation_id: int | None = None,
    exclude_name_id: int | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """与同命名空间内已注册名称、有效预占及未过期冷却逐条做规范名/同形判定。

    已注册名称冲突排在前（按 id），有效预占冲突居中（按 id），未过期冷却
    冲突排在后（按 id）。
    exclude_reservation_id：确认注册时排除正在确认的那条预占自身。
    exclude_name_id：改名时排除正在改名的那条已注册记录自身（新规范名
    与自身相等不算冲突）；有效预占与冷却不排除，仍全部参与判定。
    """
    conflicts: list[dict] = []
    rows = conn.execute(
        """
        SELECT id, original_name, normalized_name
        FROM display_names
        WHERE namespace = ?
        ORDER BY id
        """,
        (namespace,),
    ).fetchall()
    for row in rows:
        if exclude_name_id is not None and row["id"] == exclude_name_id:
            continue
        existing_normalized = row["normalized_name"]
        if existing_normalized == normalized_name:
            reason = REASON_NORMALIZED_MATCH
        elif is_confusable(normalized_name, existing_normalized):
            reason = REASON_CONFUSABLE
        else:
            continue
        conflicts.append(_conflict_entry(row, SOURCE_REGISTERED, reason))

    for row in _active_reservation_rows(conn, namespace, now):
        if exclude_reservation_id is not None and row["id"] == exclude_reservation_id:
            continue
        existing_normalized = row["normalized_name"]
        if existing_normalized == normalized_name:
            reason = REASON_NORMALIZED_MATCH
        elif is_confusable(normalized_name, existing_normalized):
            reason = REASON_CONFUSABLE
        else:
            continue
        conflicts.append(_conflict_entry(row, SOURCE_RESERVATION, reason))

    for row in _active_cooldown_rows(conn, namespace, now):
        existing_normalized = row["normalized_name"]
        if existing_normalized == normalized_name:
            reason = REASON_NORMALIZED_MATCH
        elif is_confusable(normalized_name, existing_normalized):
            reason = REASON_CONFUSABLE
        else:
            continue
        conflicts.append(_conflict_entry(row, SOURCE_RELEASED, reason))
    return conflicts


def analyze_name(conn: sqlite3.Connection, namespace: str, name: str) -> dict:
    """完整分析：规范名 + 脚本分析 + 冲突检测（只读，不落库）。

    冲突同时覆盖已注册名称、未过期预占与未过期释放冷却；过期记录不参与，
    也不会在此被删除（只读路径不做惰性清理）。混脚本按所属命名空间的
    生效策略判定（未配置时使用内建默认策略）。
    """
    normalized = normalize_name(name)
    pairs = get_blocked_pairs(conn, namespace)
    return {
        "namespace": namespace,
        "original_name": name,
        "normalized_name": normalized,
        **analyze_scripts(name, pairs),
        "conflicts": find_conflicts(conn, namespace, normalized),
    }


def analyze_batch(conn: sqlite3.Connection, namespace: str, names: list[str]) -> list[dict]:
    """整批联合分析：逐项复用单条分析，再对批内两两做规范名 / 同形判定。

    返回与请求顺序一致的分析结果列表；批内冲突按原请求索引互相标注在
    双方的 batch_conflicts 中（字段与库内冲突记录一致，只是以索引定位）。
    """
    analyses = [
        {"index": index, **analyze_name(conn, namespace, name), "batch_conflicts": []}
        for index, name in enumerate(names)
    ]
    for left in range(len(analyses)):
        for right in range(left + 1, len(analyses)):
            a, b = analyses[left], analyses[right]
            if a["normalized_name"] == b["normalized_name"]:
                reason = REASON_NORMALIZED_MATCH
            elif is_confusable(a["normalized_name"], b["normalized_name"]):
                reason = REASON_CONFUSABLE
            else:
                continue
            a["batch_conflicts"].append(
                {
                    "index": right,
                    "original_name": b["original_name"],
                    "normalized_name": b["normalized_name"],
                    "reason": reason,
                }
            )
            b["batch_conflicts"].append(
                {
                    "index": left,
                    "original_name": a["original_name"],
                    "normalized_name": a["normalized_name"],
                    "reason": reason,
                }
            )
    return analyses


def insert_name(conn: sqlite3.Connection, namespace: str, name: str, normalized_name: str) -> dict:
    """写入新名称，返回完整记录。唯一约束冲突时抛 NameConflictError。"""
    try:
        cursor = conn.execute(
            """
            INSERT INTO display_names (namespace, original_name, normalized_name)
            VALUES (?, ?, ?)
            """,
            (namespace, name, normalized_name),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise NameConflictError(normalized_name) from exc

    row = conn.execute(
        "SELECT * FROM display_names WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return dict(row)


def register_name(
    conn: sqlite3.Connection, namespace: str, name: str, normalized_name: str
) -> dict:
    """持写锁注册：BEGIN IMMEDIATE 事务内惰性清理过期预占并复查。

    与预占 / 批量 / 确认共用同一套并发判定：有效预占与已注册名称一样
    阻止注册。事务内重新加载所属命名空间的混脚本策略并复查（防止只读分析
    通过后策略被并发改严），混脚本 -> MixedScriptNameError；复查不通过、
    唯一约束冲突或锁竞争时回滚并抛 NameConflictError。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        purge_expired_reservations(conn, namespace)
        purge_expired_cooldowns(conn, namespace)
        script_analysis = analyze_scripts(name, get_blocked_pairs(conn, namespace))
        if script_analysis["mixed_script_violation"]:
            raise MixedScriptNameError(script_analysis)
        conflicts = find_conflicts(conn, namespace, normalized_name)
        if conflicts:
            raise NameConflictError(normalized_name, conflicts)
        cursor = conn.execute(
            """
            INSERT INTO display_names (namespace, original_name, normalized_name)
            VALUES (?, ?, ?)
            """,
            (namespace, name, normalized_name),
        )
        new_id = cursor.lastrowid
        conn.commit()
    except (NameConflictError, MixedScriptNameError):
        conn.rollback()
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # 唯一约束兜底 / 锁竞争：回滚
        conn.rollback()
        raise NameConflictError(str(exc)) from exc

    row = conn.execute(
        "SELECT * FROM display_names WHERE id = ?", (new_id,)
    ).fetchone()
    return dict(row)


def rename_name(
    conn: sqlite3.Connection,
    namespace: str,
    name_id: int,
    name: str,
    expected_revision: int,
) -> dict:
    """原子改名：在单个 BEGIN IMMEDIATE 事务内校验并更新已注册名称。

    事务内校验顺序：
    1. 记录存在且命名空间匹配，否则 NameNotFoundError -> 404
    2. revision == expected_revision，否则 StaleRevisionError -> 409
    3. 新名称不混脚本，否则 MixedScriptNameError -> 422
    4. 与同空间其他已注册名称（排除自身）及全部有效预占无冲突，
       否则 NameConflictError -> 409

    全部通过后在同一事务更新 original_name / normalized_name 并令
    revision + 1；id / created_at 保持不变。条件 UPDATE 以 revision
    兜底并发：影响 0 行视为版本过期。任何失败均回滚。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 规范化在写锁事务内进行，与后续策略 / 冲突复查同生共死
        normalized_name = normalize_name(name)

        row = conn.execute(
            "SELECT * FROM display_names WHERE id = ?",
            (name_id,),
        ).fetchone()

        # 1. 记录未知或不属于 URL 中的命名空间
        if row is None or row["namespace"] != namespace:
            raise NameNotFoundError(name_id)

        # 2. 乐观锁版本校验
        current_revision = row["revision"]
        if current_revision != expected_revision:
            raise StaleRevisionError(current_revision)

        # 3. 混脚本拦截：按所属命名空间当前生效策略判定（持锁加载，
        #    与并发的策略 PUT 互不破坏）
        script_analysis = analyze_scripts(name, get_blocked_pairs(conn, namespace))
        if script_analysis["mixed_script_violation"]:
            raise MixedScriptNameError(script_analysis)

        # 持锁期间惰性清理过期预占与冷却，使冲突判定只含有效记录
        purge_expired_reservations(conn, namespace)
        purge_expired_cooldowns(conn, namespace)

        # 4. 事务内复查冲突：排除当前记录自身，有效预占与冷却仍全部参与
        conflicts = find_conflicts(
            conn,
            namespace,
            normalized_name,
            exclude_name_id=name_id,
        )
        if conflicts:
            raise NameConflictError(normalized_name, conflicts)

        cursor = conn.execute(
            """
            UPDATE display_names
               SET original_name = ?, normalized_name = ?, revision = revision + 1
             WHERE id = ? AND namespace = ? AND revision = ?
            """,
            (name, normalized_name, name_id, namespace, expected_revision),
        )
        # 条件更新兜底：持锁期间版本被改动（理论上被写锁阻止），按版本过期处理
        if cursor.rowcount == 0:
            refreshed = conn.execute(
                "SELECT revision FROM display_names WHERE id = ? AND namespace = ?",
                (name_id, namespace),
            ).fetchone()
            raise StaleRevisionError(refreshed["revision"] if refreshed else current_revision)
        conn.commit()
    except (
        NameNotFoundError,
        StaleRevisionError,
        MixedScriptNameError,
        NameConflictError,
    ):
        conn.rollback()
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # 唯一约束兜底 / 锁竞争：回滚，按冲突处理（持写锁后复查会给出最新明细）
        conn.rollback()
        raise NameConflictError(str(exc)) from exc

    updated = conn.execute(
        "SELECT * FROM display_names WHERE id = ?", (name_id,)
    ).fetchone()
    return dict(updated)


def swap_names(
    conn: sqlite3.Connection,
    namespace: str,
    entries: list[tuple[int, int]],
) -> list[dict]:
    """原子交换同命名空间内两条已注册名称的 original_name / normalized_name。

    entries 为 [(id, expected_revision), (id, expected_revision)]，顺序即
    返回顺序。单个 BEGIN IMMEDIATE 事务内按序校验：
    1. 两条记录都存在且命名空间匹配，任一不满足 -> SwapRecordNotFoundError
       （携带全部缺失 id）-> 404
    2. 双方当前 revision 都等于各自 expected_revision，任一过期 ->
       SwapStaleRevisionError（携带双方当前 revision）-> 409

    SQLite 的 UNIQUE(namespace, normalized_name) 为即时约束：交换的两个
    规范名都被对方占着，直接互改会在第一条 UPDATE 即触发约束。因此在
    同一事务内借助一个随机临时名先腾出槽位再成对互换（三次条件 UPDATE，
    全部带 revision 乐观锁谓词）：

        UPDATE id1 -> 临时名（revision 不变）
        UPDATE id2 -> id1 的规范名（revision + 1）
        UPDATE id1 -> id2 的规范名（revision + 1）

    临时名仅存在于未提交事务中：BEGIN IMMEDIATE 已取库级写锁，其他连接
    读到的始终是交换前快照，提交后才呈现最终交换结果，故临时名不会被
    任何并发请求观察到，也不会与并发注册 / 改名相互破坏。交换不改变
    命名空间内已占用规范名的集合，天然不与有效预占产生新冲突。

    成功后双方 id / created_at 保持不变、revision 各 + 1；任何失败都
    整体回滚，绝不产生部分更新。返回按请求顺序排列的两条完整记录。
    """
    (left_id, left_expected), (right_id, right_expected) = entries
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 写操作前惰性清理过期预占与冷却（交换不改变占用规范名集合，无需复查）
        purge_expired_reservations(conn, namespace)
        purge_expired_cooldowns(conn, namespace)
        rows = {
            row["id"]: row
            for row in conn.execute(
                "SELECT * FROM display_names WHERE id IN (?, ?)",
                (left_id, right_id),
            ).fetchall()
        }

        def _row_or_raise(target_id: int):
            row = rows.get(target_id)
            if row is None or row["namespace"] != namespace:
                missing = [
                    rid
                    for rid, _ in entries
                    if rid not in rows or rows[rid]["namespace"] != namespace
                ]
                raise SwapRecordNotFoundError(missing)
            return row

        # 1. 归属校验：任一记录未知或不属于 URL 中的命名空间 -> 404
        left_row = _row_or_raise(left_id)
        right_row = _row_or_raise(right_id)

        def _current(target_id: int, expected: int) -> dict:
            """重读单条记录的当前 revision，组装 stale 明细（持锁期间状态稳定）。"""
            row = rows.get(target_id)
            if row is None:
                refreshed = conn.execute(
                    "SELECT revision FROM display_names WHERE id = ? AND namespace = ?",
                    (target_id, namespace),
                ).fetchone()
                current = refreshed["revision"] if refreshed else expected
            else:
                current = row["revision"]
            return {"id": target_id, "expected_revision": expected, "current_revision": current}

        def _stale_if_needed() -> SwapStaleRevisionError | None:
            details = [
                _current(left_id, left_expected),
                _current(right_id, right_expected),
            ]
            if any(d["current_revision"] != d["expected_revision"] for d in details):
                # 明细始终携带双方当前 revision，供客户端整体重读后重试
                return SwapStaleRevisionError(details)
            return None

        # 2. 乐观锁版本校验：任一方版本过期 -> 409，附双方当前 revision
        if stale := _stale_if_needed():
            raise stale

        # 取双方原名 / 规范名对（交换的是完整成对名称，而不是只换规范名）
        left_name, left_norm = left_row["original_name"], left_row["normalized_name"]
        right_name, right_norm = right_row["original_name"], right_row["normalized_name"]

        # 随机临时名：secrets 32 字节十六进制，纯 ASCII（NFKC/casefold 稳定），
        # 长度远低于 64 字符上限；持写锁后再复查一次唯一约束碰撞（概率可忽略）
        temp_original = f"__swap__/{secrets.token_hex(32)}"
        temp_norm = normalize_name(temp_original)
        clash = conn.execute(
            "SELECT 1 FROM display_names WHERE namespace = ? AND normalized_name = ?",
            (namespace, temp_norm),
        ).fetchone()
        if clash is not None:
            # 极小概率随机碰撞：视为并发异常，调用方整体重试即可
            raise NameConflictError(temp_norm)

        def _update(
            target_id: int,
            expected: int,
            original_name: str,
            normalized_name: str,
            bump_revision: bool,
        ) -> None:
            revision_sql = "revision + 1" if bump_revision else "revision"
            cursor = conn.execute(
                f"""
                UPDATE display_names
                   SET original_name = ?, normalized_name = ?, revision = {revision_sql}
                 WHERE id = ? AND namespace = ? AND revision = ?
                """,
                (original_name, normalized_name, target_id, namespace, expected),
            )
            if cursor.rowcount != 1:
                # 条件更新兜底（持写锁后理论上不会发生）：按实际状态归类异常
                missing = [
                    rid
                    for rid, _ in entries
                    if conn.execute(
                        "SELECT 1 FROM display_names WHERE id = ? AND namespace = ?",
                        (rid, namespace),
                    ).fetchone()
                    is None
                ]
                if missing:
                    raise SwapRecordNotFoundError(missing)
                stale_error = _stale_if_needed()
                raise stale_error or NameConflictError("swap_update_failed")

        # 3. 三步成对交换：临时名腾槽 -> right 接管 left 的名称 -> left 接管 right 的名称
        _update(left_id, left_expected, temp_original, temp_norm, bump_revision=False)
        _update(right_id, right_expected, left_name, left_norm, bump_revision=True)
        _update(left_id, left_expected, right_name, right_norm, bump_revision=True)
        conn.commit()
    except (SwapRecordNotFoundError, SwapStaleRevisionError, NameConflictError):
        conn.rollback()
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # 唯一约束兜底 / 锁竞争（busy_timeout 内未抢到写锁）：整体回滚，
        # 不写入任何数据，交由调用方按 409 处理（客户端可重试）
        conn.rollback()
        raise NameConflictError(str(exc)) from exc

    placeholders = ", ".join("?" for _ in entries)
    updated_rows = conn.execute(
        f"SELECT * FROM display_names WHERE id IN ({placeholders})",
        [rid for rid, _ in entries],
    ).fetchall()
    by_id = {row["id"]: dict(row) for row in updated_rows}
    return [by_id[rid] for rid, _ in entries]


def insert_batch(conn: sqlite3.Connection, namespace: str, names: list[str]) -> list[dict]:
    """单个事务批量落库，返回与请求顺序一致的完整记录。

    BEGIN IMMEDIATE 锁定写入后在事务内复查混脚本（重新加载所属命名空间
    策略，防止批次分析后策略被并发改严）与冲突（已注册名称 + 有效预占），
    保证并发下此前的分析判定仍然成立；复查不通过、唯一约束冲突或锁竞争时
    整批回滚：混脚本抛 BatchMixedScriptError（携带违规项明细），冲突抛
    NameConflictError，由调用方重新分析。
    """
    normalized_names = [normalize_name(name) for name in names]
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 持锁期间顺带惰性清理过期预占与冷却
        purge_expired_reservations(conn, namespace)
        purge_expired_cooldowns(conn, namespace)
        # 事务内复查混脚本：策略可能在批次只读分析后被并发改严
        pairs = get_blocked_pairs(conn, namespace)
        script_violations = []
        for index, name in enumerate(names):
            script_analysis = analyze_scripts(names[index], pairs)
            if script_analysis["mixed_script_violation"]:
                script_violations.append(
                    {
                        "index": index,
                        "original_name": names[index],
                        "normalized_name": normalized_names[index],
                        **script_analysis,
                    }
                )
        if script_violations:
            raise BatchMixedScriptError(script_violations)
        # 事务内复查冲突：持锁期间状态不变，判定结果可信
        for normalized_name in normalized_names:
            conflicts = find_conflicts(conn, namespace, normalized_name)
            if conflicts:
                raise NameConflictError(normalized_name, conflicts)
        ids = []
        for name, normalized_name in zip(names, normalized_names):
            cursor = conn.execute(
                """
                INSERT INTO display_names (namespace, original_name, normalized_name)
                VALUES (?, ?, ?)
                """,
                (namespace, name, normalized_name),
            )
            ids.append(cursor.lastrowid)
        conn.commit()
    except (NameConflictError, BatchMixedScriptError):
        conn.rollback()
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # 唯一约束兜底 / 锁竞争：整批回滚
        conn.rollback()
        raise NameConflictError(str(exc)) from exc

    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM display_names WHERE id IN ({placeholders}) ORDER BY id",
        ids,
    ).fetchall()
    return [dict(row) for row in rows]


def create_reservation(
    conn: sqlite3.Connection, namespace: str, name: str, ttl_seconds: int
) -> dict:
    """预占名称：返回含不可猜测 token 与 expires_at 的预占记录。

    调用方需先完成混脚本 / 冲突的只读分析；本函数在 BEGIN IMMEDIATE
    事务中惰性清理过期预占，再重新加载命名空间策略复查混脚本与冲突后
    写入（防止只读分析通过后策略被并发改严），复查不通过分别抛
    MixedScriptNameError / NameConflictError。
    """
    normalized_name = normalize_name(name)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    created = utc_now()
    expires_at = (created + timedelta(seconds=ttl_seconds)).isoformat(
        timespec="milliseconds"
    ) + "Z"

    try:
        conn.execute("BEGIN IMMEDIATE")
        purge_expired_reservations(conn, namespace)
        purge_expired_cooldowns(conn, namespace)
        script_analysis = analyze_scripts(name, get_blocked_pairs(conn, namespace))
        if script_analysis["mixed_script_violation"]:
            raise MixedScriptNameError(script_analysis)
        conflicts = find_conflicts(conn, namespace, normalized_name, now=created)
        if conflicts:
            raise NameConflictError(normalized_name, conflicts)
        cursor = conn.execute(
            """
            INSERT INTO name_reservations
                (namespace, original_name, normalized_name, token, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (namespace, name, normalized_name, token, expires_at),
        )
        new_id = cursor.lastrowid
        conn.commit()
    except (NameConflictError, MixedScriptNameError):
        conn.rollback()
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # token 极小概率碰撞（唯一约束）/ 锁竞争：回滚由调用方重试或报冲突
        conn.rollback()
        raise NameConflictError(str(exc)) from exc

    row = conn.execute(
        "SELECT * FROM name_reservations WHERE id = ?", (new_id,)
    ).fetchone()
    record = dict(row)
    record["ttl_seconds"] = ttl_seconds
    return record


def claim_reservation(conn: sqlite3.Connection, namespace: str, token: str) -> dict:
    """确认预占：单事务校验并注册，返回 ClaimedNameRecord 形状的记录。

    事务内校验顺序：
    1. token 存在且命名空间匹配，否则 ReservationNotFoundError -> 404
    2. 未过期，否则 ReservationExpiredError -> 410（惰性删除过期记录）
    3. 未消费，否则 ReservationConsumedError -> 409
    4. 与当前已注册名称 / 其他有效预占无冲突，否则 NameConflictError -> 409

    全部通过后在同一事务写入 display_names 并回写 consumed_at；
    唯一约束兜底并发，任何失败都回滚，同一 token 至多成功一次。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 写操作前惰性清理过期冷却（过期预占仅按 token 单独惰性删除，
        # 避免确认无关空间的预占）
        purge_expired_cooldowns(conn, namespace)
        row = conn.execute(
            "SELECT * FROM name_reservations WHERE token = ?",
            (token,),
        ).fetchone()

        # 1. token 未知或不属于该命名空间
        if row is None or row["namespace"] != namespace:
            raise ReservationNotFoundError(token)

        reservation_id = row["id"]

        # 2. 过期：惰性删除后提交清理结果，返回 410
        now = utc_now()
        if parse_expires_at(row["expires_at"]) <= now:
            conn.execute("DELETE FROM name_reservations WHERE id = ?", (reservation_id,))
            conn.commit()
            raise ReservationExpiredError(token)

        # 3. 已消费：并发 / 重复确认
        if row["consumed_at"] is not None:
            raise ReservationConsumedError(token)

        # 4. 事务内复查当前冲突（排除自身预占）
        conflicts = find_conflicts(
            conn,
            namespace,
            row["normalized_name"],
            exclude_reservation_id=reservation_id,
            now=now,
        )
        if conflicts:
            raise NameConflictError(row["normalized_name"], conflicts)

        cursor = conn.execute(
            """
            INSERT INTO display_names (namespace, original_name, normalized_name)
            VALUES (?, ?, ?)
            """,
            (namespace, row["original_name"], row["normalized_name"]),
        )
        name_id = cursor.lastrowid
        consumed_at = utc_now_iso()
        mark_cursor = conn.execute(
            "UPDATE name_reservations SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (consumed_at, reservation_id),
        )
        # 条件更新兜底：若影响行数为 0，说明并发确认抢先，整体回滚
        if mark_cursor.rowcount == 0:
            raise ReservationConsumedError(token)
        conn.commit()
    except (ReservationNotFoundError, ReservationConsumedError, NameConflictError):
        # 只读校验失败 / 冲突：回滚无副作用
        conn.rollback()
        raise
    except ReservationExpiredError:
        # 过期分支已提交惰性删除，保持提交即可
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # 唯一约束兜底（注册名被抢先）/ 锁竞争：回滚，按冲突处理
        conn.rollback()
        raise NameConflictError(str(exc)) from exc

    name_row = conn.execute(
        "SELECT * FROM display_names WHERE id = ?", (name_id,)
    ).fetchone()
    record = dict(name_row)
    record["token"] = token
    record["expires_at"] = row["expires_at"]
    record["consumed_at"] = consumed_at
    return record


def _merge_entry(row, kind: str) -> dict:
    """合并冲突记录：kind 区分已注册名称 / 有效预占 / 未过期冷却。"""
    entry = {
        "id": row["id"],
        "original_name": row["original_name"],
        "normalized_name": row["normalized_name"],
        "kind": kind,
    }
    if kind in (SOURCE_RESERVATION, SOURCE_RELEASED):
        entry["expires_at"] = row["expires_at"]
    return entry


def _namespace_merge_entries(
    conn: sqlite3.Connection, namespace: str, now: datetime | None = None
) -> list[dict]:
    """读取命名空间内参与合并冲突判定的全部记录。

    顺序：已注册名称 + 有效预占 + 未过期释放冷却。
    """
    entries = [
        _merge_entry(row, SOURCE_REGISTERED)
        for row in conn.execute(
            """
            SELECT id, original_name, normalized_name
            FROM display_names
            WHERE namespace = ?
            ORDER BY id
            """,
            (namespace,),
        ).fetchall()
    ]
    entries.extend(
        _merge_entry(row, SOURCE_RESERVATION)
        for row in _active_reservation_rows(conn, namespace, now)
    )
    entries.extend(
        _merge_entry(row, SOURCE_RELEASED)
        for row in _active_cooldown_rows(conn, namespace, now)
    )
    return entries


def find_merge_conflicts(
    conn: sqlite3.Connection,
    source_namespace: str,
    target_namespace: str,
    now: datetime | None = None,
) -> list[dict]:
    """源与目标命名空间之间的跨空间冲突检测（规范名相同 / 同形）。

    两侧都覆盖已注册名称、有效预占与未过期释放冷却；同一命名空间内部的
    记录在写入时已保证互不冲突，只需检查跨空间组合。返回的每项冲突都带
    双方记录（source_record 来自源命名空间，target_record 来自目标命名空间，
    kind 区分 registered / reservation / released）。
    """
    moment = now or utc_now()
    source_entries = _namespace_merge_entries(conn, source_namespace, moment)
    target_entries = _namespace_merge_entries(conn, target_namespace, moment)
    conflicts: list[dict] = []
    for source_entry in source_entries:
        for target_entry in target_entries:
            if source_entry["normalized_name"] == target_entry["normalized_name"]:
                reason = REASON_NORMALIZED_MATCH
            elif is_confusable(
                source_entry["normalized_name"], target_entry["normalized_name"]
            ):
                reason = REASON_CONFUSABLE
            else:
                continue
            conflicts.append(
                {
                    "reason": reason,
                    "source_record": source_entry,
                    "target_record": target_entry,
                }
            )
    return conflicts


def _merge_script_violations(entries: list[dict], pairs, side: str) -> list[dict]:
    """按目标策略 pairs 检查单侧合并记录（已注册名称 + 有效预占 + 冷却）。

    每项违规明细额外带 side（"source" / "target"），记录结构与跨空间
    冲突明细一致（kind 区分 registered / reservation / released，后两者
    附 expires_at）。
    """
    violations: list[dict] = []
    for entry in entries:
        analysis = analyze_scripts(entry["original_name"], pairs)
        if not analysis["mixed_script_violation"]:
            continue
        expires_field = (
            {"expires_at": entry["expires_at"]}
            if entry["kind"] in (SOURCE_RESERVATION, SOURCE_RELEASED)
            else {}
        )
        violations.append(
            {
                "side": side,
                "record": {
                    "id": entry["id"],
                    "kind": entry["kind"],
                    "original_name": entry["original_name"],
                    "normalized_name": entry["normalized_name"],
                    **expires_field,
                },
                "violating_script_pairs": analysis["violating_script_pairs"],
                "script_details": analysis["script_details"],
            }
        )
    return violations


def find_merge_script_violations(
    conn: sqlite3.Connection,
    source_namespace: str,
    target_namespace: str,
    now: datetime | None = None,
) -> list[dict]:
    """以目标命名空间的混脚本策略检查源、目标两侧数据。

    返回逐条违规明细（source 侧在前、target 侧在后，各自按合并记录顺序）。
    目标侧在“策略生效后现存数据始终合规”的不变量下实际不会有违规，但
    仍按要求显式扫描；源侧记录此前只受源空间策略约束，是主要的拒绝来源。
    """
    moment = now or utc_now()
    pairs = get_blocked_pairs(conn, target_namespace)
    source_entries = _namespace_merge_entries(conn, source_namespace, moment)
    target_entries = _namespace_merge_entries(conn, target_namespace, moment)
    violations = _merge_script_violations(source_entries, pairs, "source")
    violations.extend(_merge_script_violations(target_entries, pairs, "target"))
    return violations


def merge_namespaces(
    conn: sqlite3.Connection, source_namespace: str, target_namespace: str
) -> dict:
    """把源命名空间的全部已注册名称、预占记录与有效冷却合并到目标命名空间。

    调用方需保证 source_namespace != target_namespace。单个 BEGIN IMMEDIATE
    事务内完成：惰性清理两侧过期预占与冷却 -> 跨空间冲突复查（已注册名称 +
    有效预占 + 未过期冷却）-> 以目标命名空间的混脚本策略扫描源、目标两侧
    数据 -> 无冲突且两侧数据均合规才整体 UPDATE 迁移。复查发现冲突或脚本
    违规抛 MergeConflictError（携带双方记录 / 逐条违规明细），不修改任何
    数据；唯一约束 / 锁竞争等并发异常同样整体回滚，抛 MergeConflictError
    （明细为空，由调用方重新检测）。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        purge_expired_reservations(conn, source_namespace)
        purge_expired_reservations(conn, target_namespace)
        purge_expired_cooldowns(conn, source_namespace)
        purge_expired_cooldowns(conn, target_namespace)
        # 事务内复查：持锁期间状态不变，判定结果可信
        conflicts = find_merge_conflicts(conn, source_namespace, target_namespace)
        script_violations = find_merge_script_violations(
            conn, source_namespace, target_namespace
        )
        if conflicts or script_violations:
            raise MergeConflictError(conflicts, script_violations)
        names_cursor = conn.execute(
            "UPDATE display_names SET namespace = ? WHERE namespace = ?",
            (target_namespace, source_namespace),
        )
        reservations_cursor = conn.execute(
            "UPDATE name_reservations SET namespace = ? WHERE namespace = ?",
            (target_namespace, source_namespace),
        )
        cooldowns_cursor = conn.execute(
            "UPDATE name_cooldowns SET namespace = ? WHERE namespace = ?",
            (target_namespace, source_namespace),
        )
        merged_names = names_cursor.rowcount
        merged_reservations = reservations_cursor.rowcount
        merged_cooldowns = cooldowns_cursor.rowcount
        conn.commit()
    except MergeConflictError:
        conn.rollback()
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # 唯一约束兜底 / 锁竞争：整体回滚，由调用方重新检测冲突后返回 409
        conn.rollback()
        raise MergeConflictError() from exc

    return {
        "source_namespace": source_namespace,
        "target_namespace": target_namespace,
        "merged_names": merged_names,
        "merged_reservations": merged_reservations,
        "merged_cooldowns": merged_cooldowns,
    }


def list_names(conn: sqlite3.Connection, namespace: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM display_names WHERE namespace = ? ORDER BY id",
        (namespace,),
    ).fetchall()
    return [dict(row) for row in rows]


def release_name(
    conn: sqlite3.Connection,
    namespace: str,
    name_id: int,
    expected_revision: int,
    cooldown_seconds: int,
) -> dict:
    """删除已注册名称并写入释放冷却：单个 BEGIN IMMEDIATE 事务内完成。

    事务内校验顺序：
    1. 记录存在且命名空间匹配，否则 NameNotFoundError -> 404
    2. revision == expected_revision，否则 StaleRevisionError -> 409

    全部通过后先惰性清理过期冷却，再在同一事务删除 display_names 行并写入
    name_cooldowns（expires_at = 当前时刻 + cooldown_seconds）。冷却记录
    即刻占位：提交后该名称的规范名 / 同形名在冷却到期前阻止一切注册、
    预占、改名与跨空间合并。条件 DELETE（AND revision = ?）兜底并发，
    影响 0 行按版本过期处理；任何失败均回滚，不存在“名称已删除但无冷却”
    的部分更新。返回新写入的冷却记录。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT * FROM display_names WHERE id = ?",
            (name_id,),
        ).fetchone()

        # 1. 记录未知或不属于 URL 中的命名空间
        if row is None or row["namespace"] != namespace:
            raise NameNotFoundError(name_id)

        # 2. 乐观锁版本校验
        current_revision = row["revision"]
        if current_revision != expected_revision:
            raise StaleRevisionError(current_revision)

        # 持锁期间惰性清理过期冷却，使后续记录集只含有效冷却
        purge_expired_cooldowns(conn, namespace)

        created = utc_now()
        expires_at = (created + timedelta(seconds=cooldown_seconds)).isoformat(
            timespec="milliseconds"
        ) + "Z"

        cursor = conn.execute(
            """
            DELETE FROM display_names
             WHERE id = ? AND namespace = ? AND revision = ?
            """,
            (name_id, namespace, expected_revision),
        )
        if cursor.rowcount != 1:
            # 条件删除兜底（持写锁后理论上不会发生）：重读当前版本
            refreshed = conn.execute(
                "SELECT revision FROM display_names WHERE id = ? AND namespace = ?",
                (name_id, namespace),
            ).fetchone()
            raise StaleRevisionError(
                refreshed["revision"] if refreshed else current_revision
            )

        cursor = conn.execute(
            """
            INSERT INTO name_cooldowns
                (namespace, original_name, normalized_name, cooldown_seconds, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                namespace,
                row["original_name"],
                row["normalized_name"],
                cooldown_seconds,
                expires_at,
            ),
        )
        new_id = cursor.lastrowid
        conn.commit()
    except (NameNotFoundError, StaleRevisionError):
        conn.rollback()
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # 锁竞争：回滚，按并发冲突处理（客户端重读后重试）
        conn.rollback()
        raise NameConflictError(str(exc)) from exc

    record = conn.execute(
        "SELECT * FROM name_cooldowns WHERE id = ?", (new_id,)
    ).fetchone()
    return dict(record)


def list_cooldowns(conn: sqlite3.Connection, namespace: str) -> list[dict]:
    """只读列出命名空间内仍有效的释放冷却记录（按 id 升序）。

    过期记录不出现在结果中，但只读接口不做物理删除（惰性清理统一由写
    事务负责）。
    """
    moment = utc_now()
    rows = conn.execute(
        "SELECT * FROM name_cooldowns WHERE namespace = ? ORDER BY id",
        (namespace,),
    ).fetchall()
    return [
        dict(row)
        for row in rows
        if parse_expires_at(row["expires_at"]) > moment
    ]


# ---------- 跨空间同形风险簇 ----------


# 节点类型在排序键 / 稳定标识中的先后顺序（registered < reservation < released）
RISK_KIND_ORDER = {
    SOURCE_REGISTERED: 0,
    SOURCE_RESERVATION: 1,
    SOURCE_RELEASED: 2,
}


def _registered_snapshot_rows(
    conn: sqlite3.Connection, namespaces: tuple[str, ...]
) -> list[dict]:
    """读取多个命名空间的全部已注册名称（单独函数，便于并发测试注入）。"""
    placeholders = ", ".join("?" for _ in namespaces)
    rows = conn.execute(
        f"""
        SELECT namespace, id, original_name, normalized_name
        FROM display_names
        WHERE namespace IN ({placeholders})
        ORDER BY namespace, id
        """,
        namespaces,
    ).fetchall()
    return [dict(row) for row in rows]


def _reservation_snapshot_rows(
    conn: sqlite3.Connection, namespaces: tuple[str, ...], now: datetime
) -> list[dict]:
    """读取多个命名空间的未消费、未过期预占（单独函数，便于并发测试注入）。"""
    placeholders = ", ".join("?" for _ in namespaces)
    rows = conn.execute(
        f"""
        SELECT namespace, id, original_name, normalized_name, expires_at
        FROM name_reservations
        WHERE namespace IN ({placeholders}) AND consumed_at IS NULL
        ORDER BY namespace, id
        """,
        namespaces,
    ).fetchall()
    return [
        dict(row)
        for row in rows
        if parse_expires_at(row["expires_at"]) > now
    ]


def _cooldown_snapshot_rows(
    conn: sqlite3.Connection, namespaces: tuple[str, ...], now: datetime
) -> list[dict]:
    """读取多个命名空间的未过期释放冷却记录（单独函数，便于并发测试注入）。"""
    placeholders = ", ".join("?" for _ in namespaces)
    rows = conn.execute(
        f"""
        SELECT namespace, id, original_name, normalized_name, expires_at
        FROM name_cooldowns
        WHERE namespace IN ({placeholders})
        ORDER BY namespace, id
        """,
        namespaces,
    ).fetchall()
    return [
        dict(row)
        for row in rows
        if parse_expires_at(row["expires_at"]) > now
    ]


def _node_key(node: dict) -> tuple[str, int, int]:
    """节点稳定排序 / 标识键：namespace、kind 序值、id。"""
    return node["namespace"], RISK_KIND_ORDER[node["kind"]], node["id"]


class _UnionFind:
    """按秩合并的并查集：以快照节点下标为元素，用于求跨空间边的连通分量。"""

    def __init__(self, size: int):
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


def find_name_risk_clusters(
    conn: sqlite3.Connection,
    namespaces: list[str],
    max_names: int,
    now: datetime | None = None,
) -> dict:
    """跨命名空间同形风险簇：在读事务快照上固定各空间的三类名称并分簇。

    在**同一个只读事务（BEGIN 读事务）**中依次读取这些命名空间的已注册
    名称、有效预占与未过期冷却，保证后续判定基于同一份数据库快照（并发
    写入在事务期间提交也不改变本函数看到的节点集合）；只读路径不做任何
    惰性清理，过期 / 已消费记录在 Python 侧过滤，与 check 等只读接口一致。

    快照节点总数（三类记录之和）超过 max_names 时立即抛
    RiskSnapshotTooLargeError（-> 413），绝不截断快照后继续计算。

    建边规则：仅对**不同命名空间**的两个节点，按
      1. normalized_name 完全相等 -> normalized_match
      2. 否则 confusables 同形 -> confusable
    建边；同一命名空间内部不建边（空间内写入时已保证互不冲突）。再按
    并查集求连通分量，只保留至少含 2 个节点的分量：因此 A-B、B-C 建边
    而 A-C 不直接冲突时三者仍归入同一簇。簇、节点、边全部确定性排序：
      - 节点键 (namespace, kind 序, id)，registered < reservation < released
      - 边按 (source 键, target 键) 排序，端点小者为 source
      - 簇按簇内最小节点键排序，cluster_id 从 1 顺序编号
    """
    moment = now or utc_now()
    ordered_namespaces = tuple(sorted(set(namespaces)))

    # 单一读事务固定全部快照：事务期间其他连接的提交对本连接不可见
    conn.execute("BEGIN")
    try:
        registered = _registered_snapshot_rows(conn, ordered_namespaces)
        reservations = _reservation_snapshot_rows(conn, ordered_namespaces, moment)
        cooldowns = _cooldown_snapshot_rows(conn, ordered_namespaces, moment)

        nodes: list[dict] = []
        for row in registered:
            nodes.append({**row, "kind": SOURCE_REGISTERED})
        for row in reservations:
            nodes.append({**row, "kind": SOURCE_RESERVATION})
        for row in cooldowns:
            nodes.append({**row, "kind": SOURCE_RELEASED})

        snapshot_count = len(nodes)
        if snapshot_count > max_names:
            raise RiskSnapshotTooLargeError(snapshot_count, max_names)

        union_find = _UnionFind(snapshot_count)
        # edges[(i, j)] = {"normalized_match", "confusable"}（i < j）
        edges: dict[tuple[int, int], set[str]] = {}

        def link(left: int, right: int, reason: str) -> None:
            if left > right:
                left, right = right, left
            union_find.union(left, right)
            edges.setdefault((left, right), set()).add(reason)

        # 1. 规范名相同：先按 normalized_name 分组，组内跨空间两两连边。
        #    同一空间内的记录在写入时已保证规范名唯一，故组内同空间至多一条。
        by_normalized: dict[str, list[int]] = {}
        for index, node in enumerate(nodes):
            by_normalized.setdefault(node["normalized_name"], []).append(index)
        for indices in by_normalized.values():
            if len(indices) < 2:
                continue
            for left_pos in range(len(indices)):
                for right_pos in range(left_pos + 1, len(indices)):
                    left, right = indices[left_pos], indices[right_pos]
                    if nodes[left]["namespace"] != nodes[right]["namespace"]:
                        link(left, right, REASON_NORMALIZED_MATCH)

        # 2. 同形：只比较规范名不同的跨空间节点对；同规范名上一步已建边。
        #    按规范名去重后两两判定（is_confusable 结果按无序规范名对缓存），
        #    一个规范名可能被多个空间的多个节点（registered/reservation/released）持有。
        normalized_names = sorted(by_normalized)
        confusable_pairs: set[tuple[str, str]] = set()
        for left_pos in range(len(normalized_names)):
            for right_pos in range(left_pos + 1, len(normalized_names)):
                left_norm, right_norm = (
                    normalized_names[left_pos],
                    normalized_names[right_pos],
                )
                if is_confusable(left_norm, right_norm):
                    confusable_pairs.add((left_norm, right_norm))
        for left_norm, right_norm in confusable_pairs:
            for left in by_normalized[left_norm]:
                for right in by_normalized[right_norm]:
                    if nodes[left]["namespace"] != nodes[right]["namespace"]:
                        link(left, right, REASON_CONFUSABLE)
    finally:
        conn.rollback()

    # 3. 连通分量：只保留至少 2 个节点的分量（无跨空间边的孤立节点丢弃）
    members: dict[int, list[int]] = {}
    for index in range(snapshot_count):
        members.setdefault(union_find.find(index), []).append(index)
    components = [
        sorted(indices, key=lambda idx: _node_key(nodes[idx]))
        for indices in members.values()
        if len(indices) >= 2
    ]

    # 簇按簇内最小节点键确定性排序，cluster_id 从 1 顺序编号
    components.sort(key=lambda indices: _node_key(nodes[indices[0]]))

    clusters: list[dict] = []
    for cluster_id, component in enumerate(components, start=1):
        component_set = set(component)
        cluster_edges: list[dict] = []
        for (left, right), reasons in edges.items():
            if left not in component_set:
                continue
            # 边端点按节点键 (namespace, kind, id) 确定小者为 source
            if _node_key(nodes[left]) <= _node_key(nodes[right]):
                source_node, target_node = nodes[left], nodes[right]
            else:
                source_node, target_node = nodes[right], nodes[left]
            cluster_edges.append(
                {
                    "source": _node_ref(source_node),
                    "target": _node_ref(target_node),
                    "reasons": sorted(
                        reasons,
                        key={
                            REASON_NORMALIZED_MATCH: 0,
                            REASON_CONFUSABLE: 1,
                        }.get,
                    ),
                }
            )
        cluster_edges.sort(key=_edge_sort_key)
        clusters.append(
            {
                "cluster_id": cluster_id,
                "nodes": [_risk_node_payload(nodes[index]) for index in component],
                "edges": cluster_edges,
            }
        )

    return {
        "namespaces": list(ordered_namespaces),
        "max_names": max_names,
        "snapshot_count": snapshot_count,
        "clusters": clusters,
    }


def _node_ref(node: dict) -> dict:
    """边端点的稳定标识：仅 namespace / kind / id。"""
    return {"namespace": node["namespace"], "kind": node["kind"], "id": node["id"]}


def _risk_node_payload(node: dict) -> dict:
    """簇节点响应载荷：稳定标识 + 原名 / 规范名，预占与冷却附 expires_at。"""
    payload = {
        "namespace": node["namespace"],
        "kind": node["kind"],
        "id": node["id"],
        "original_name": node["original_name"],
        "normalized_name": node["normalized_name"],
    }
    if node["kind"] in (SOURCE_RESERVATION, SOURCE_RELEASED):
        payload["expires_at"] = node["expires_at"]
    return payload


def _edge_sort_key(edge: dict) -> tuple:
    """边的确定性排序键：source 节点键、target 节点键、reasons 序列。"""
    source, target = edge["source"], edge["target"]
    return (
        (source["namespace"], RISK_KIND_ORDER[source["kind"]], source["id"]),
        (target["namespace"], RISK_KIND_ORDER[target["kind"]], target["id"]),
        tuple(edge["reasons"]),
    )


# ---------- 快照 Merkle 包含证明（POST .../snapshot-proof） ----------


# 快照叶的类型序：脚本策略叶恒在最前，随后与风险簇一致
# registered < reservation < released；排序先按该序值再按 id
SNAPSHOT_KIND_ORDER = {
    "script_policy": 0,
    SOURCE_REGISTERED: 1,
    SOURCE_RESERVATION: 2,
    SOURCE_RELEASED: 3,
}

# 脚本策略叶的固定 id（策略不是业务记录，没有自增 id）
SCRIPT_POLICY_LEAF_ID = 0


def _proof_registered_rows(
    conn: sqlite3.Connection, namespace: str
) -> list[dict]:
    """读取命名空间全部已注册名称（单独函数，便于并发测试注入）。"""
    rows = conn.execute(
        """
        SELECT id, namespace, original_name, normalized_name, revision, created_at
        FROM display_names
        WHERE namespace = ?
        ORDER BY id
        """,
        (namespace,),
    ).fetchall()
    return [dict(row) for row in rows]


def _proof_reservation_rows(
    conn: sqlite3.Connection, namespace: str, now: datetime
) -> list[dict]:
    """读取命名空间未消费、未过期预占（单独函数，便于并发测试注入）。"""
    rows = conn.execute(
        """
        SELECT id, namespace, original_name, normalized_name, expires_at, created_at
        FROM name_reservations
        WHERE namespace = ? AND consumed_at IS NULL
        ORDER BY id
        """,
        (namespace,),
    ).fetchall()
    return [
        dict(row)
        for row in rows
        if parse_expires_at(row["expires_at"]) > now
    ]


def _proof_cooldown_rows(
    conn: sqlite3.Connection, namespace: str, now: datetime
) -> list[dict]:
    """读取命名空间未过期释放冷却记录（单独函数，便于并发测试注入）。"""
    rows = conn.execute(
        """
        SELECT id, namespace, original_name, normalized_name,
               cooldown_seconds, expires_at, created_at
        FROM name_cooldowns
        WHERE namespace = ?
        ORDER BY id
        """,
        (namespace,),
    ).fetchall()
    return [
        dict(row)
        for row in rows
        if parse_expires_at(row["expires_at"]) > now
    ]


def _effective_policy_leaf(conn: sqlite3.Connection, namespace: str) -> dict:
    """组装生效脚本策略叶载荷：已配置用库中策略；未配置用内建默认策略。

    未配置时 configured=False、revision=0、blocked_script_pairs 为内建默认
    三对——与各写入 / check 接口实际生效的判定一致，使默认策略也被快照固定。
    """
    policy = get_script_policy(conn, namespace)
    if policy is None:
        return {
            "kind": "script_policy",
            "id": SCRIPT_POLICY_LEAF_ID,
            "namespace": namespace,
            "configured": False,
            "revision": 0,
            "blocked_script_pairs": [list(pair) for pair in DEFAULT_BLOCKED_PAIRS],
        }
    return {
        "kind": "script_policy",
        "id": SCRIPT_POLICY_LEAF_ID,
        "namespace": namespace,
        "configured": True,
        "revision": policy["revision"],
        "blocked_script_pairs": [list(pair) for pair in policy["blocked_script_pairs"]],
    }


def _business_leaf_payload(row: dict, kind: str) -> dict:
    """把业务行组装为叶载荷（规范 JSON 的内容视图）。

    字段集合跨类型固定（不适用的字段显式为 None），与响应中回显的叶视图
    逐键一致，使验证方可以直接把响应 JSON 规范编码后重算叶哈希；规范编码
    时键按字典序排列，故此处字段书写顺序无关哈希。
    """
    return {
        "kind": kind,
        "id": row["id"],
        "namespace": row["namespace"],
        "original_name": row["original_name"],
        "normalized_name": row["normalized_name"],
        "revision": row["revision"] if kind == SOURCE_REGISTERED else None,
        "cooldown_seconds": (
            row["cooldown_seconds"] if kind == SOURCE_RELEASED else None
        ),
        "expires_at": (
            row["expires_at"] if kind in (SOURCE_RESERVATION, SOURCE_RELEASED) else None
        ),
        "created_at": row["created_at"],
    }


def build_snapshot_proof(
    conn: sqlite3.Connection,
    namespace: str,
    nodes: list[dict],
    max_nodes: int,
    now: datetime | None = None,
) -> dict:
    """单个只读事务内固定快照，构建 SHA-256 Merkle 树并返回包含证明。

    在**同一个只读事务（BEGIN）**中依次读取命名空间的生效脚本策略、已注册
    名称、未消费未过期预占与未过期释放冷却，使四类数据来自同一份数据库
    快照（事务期间其他连接的提交对本连接不可见）；只读路径不做惰性清理，
    过期 / 已消费记录在 Python 侧过滤，与风险簇 / check 只读路径一致。

    业务节点总量（已注册 + 预占 + 冷却，不含策略叶）超过 max_nodes 时立即
    抛 SnapshotTooLargeError（-> 413），**不截断、不构建树**。随后校验请求
    的全部 (kind, id) 都存在于快照，任一缺失抛 SnapshotNodesNotFoundError
    （-> 404，按请求顺序附全部缺失引用）。

    叶集合 = 1 个恒存在的 script_policy 叶（排序后下标 0）+ 全部业务叶，
    按 (类型序, id) 排序后以规范 JSON 叶哈希构建 Merkle 树（叶 0x00、父
    0x01、奇数层复制末节点）。请求节点的证明只由其排序后位置决定，故相同
    快照下请求顺序变化不改变根，也不改变各节点自身的证明步骤。
    """
    moment = now or utc_now()

    # 单一读事务固定全部快照：事务期间其他连接的提交对本连接不可见
    conn.execute("BEGIN")
    try:
        registered = _proof_registered_rows(conn, namespace)
        reservations = _proof_reservation_rows(conn, namespace, moment)
        cooldowns = _proof_cooldown_rows(conn, namespace, moment)
        policy_leaf = _effective_policy_leaf(conn, namespace)
    finally:
        conn.rollback()

    snapshot_count = len(registered) + len(reservations) + len(cooldowns)
    if snapshot_count > max_nodes:
        raise SnapshotTooLargeError(snapshot_count, max_nodes)

    # 业务叶按 (类型序, id) 排序：SQL 已按 id 升序，按类型顺序拼接即可
    leaves: list[dict] = [policy_leaf]
    for row in registered:
        leaves.append(_business_leaf_payload(row, SOURCE_REGISTERED))
    for row in reservations:
        leaves.append(_business_leaf_payload(row, SOURCE_RESERVATION))
    for row in cooldowns:
        leaves.append(_business_leaf_payload(row, SOURCE_RELEASED))

    leaf_key = {(leaf["kind"], leaf["id"]): index for index, leaf in enumerate(leaves)}

    # 全部请求节点都必须存在于快照（按请求顺序收集缺失项）
    missing = [
        {"kind": node["kind"], "id": node["id"]}
        for node in nodes
        if (node["kind"], node["id"]) not in leaf_key
    ]
    if missing:
        raise SnapshotNodesNotFoundError(missing)

    hashes = [leaf_hash(leaf) for leaf in leaves]
    root = merkle_root(hashes)

    # items 按请求顺序回显；证明只由排序后下标决定，与请求顺序无关
    items: list[dict] = []
    for node in nodes:
        index = leaf_key[(node["kind"], node["id"])]
        items.append(
            {
                "kind": node["kind"],
                "id": node["id"],
                "leaf": {**leaves[index], "leaf_hash": hashes[index].hex()},
                "leaf_index": index,
                "proof": inclusion_proof(hashes, index),
            }
        )

    return {
        "namespace": namespace,
        "root": root.hex(),
        "leaf_count": len(leaves),
        "snapshot_count": snapshot_count,
        "max_nodes": max_nodes,
        "policy": {key: value for key, value in policy_leaf.items() if key != "id"},
        "items": items,
    }


# ---------- 候选集原子注册（POST .../names/allocate） ----------


def _existing_occupants(conn: sqlite3.Connection, namespace: str, now: datetime) -> list[dict]:
    """一次性读取命名空间内参与候选判定的全部既有占用（已注册 + 有效预占 +
    未过期冷却），供候选集分配的逐项逐候选静态判定复用，避免每个候选重复查询。

    顺序与 find_conflicts 一致：已注册名称（id 升序）-> 有效预占（id 升序）
    -> 未过期冷却（id 升序）。
    """
    occupants: list[dict] = []
    for row in conn.execute(
        """
        SELECT id, original_name, normalized_name
        FROM display_names
        WHERE namespace = ?
        ORDER BY id
        """,
        (namespace,),
    ).fetchall():
        occupants.append({**dict(row), "source": SOURCE_REGISTERED})
    for row in _active_reservation_rows(conn, namespace, now):
        occupants.append({**dict(row), "source": SOURCE_RESERVATION})
    for row in _active_cooldown_rows(conn, namespace, now):
        occupants.append({**dict(row), "source": SOURCE_RELEASED})
    return occupants


def _occupant_conflicts(
    normalized_name: str,
    occupants: list[dict],
    confusable_cache: dict[tuple[str, str], bool],
    occupant_conflict_cache: dict[str, list[dict]],
) -> list[dict]:
    """候选规范名与既有占用集合的逐条冲突（结构与 find_conflicts 完全一致）。

    两层缓存均仅限单次请求：is_confusable 按无序规范名对缓存（候选两两之间
    及候选与既有占用之间的同形判定不重复计算）；相同候选规范名（如不同
    申请项携带同一候选名）的完整既有冲突明细也直接复用——各记录 id /
    source 不随候选位置变化。
    """
    cached = occupant_conflict_cache.get(normalized_name)
    if cached is not None:
        return cached
    conflicts: list[dict] = []
    for occupant in occupants:
        existing_normalized = occupant["normalized_name"]
        if existing_normalized == normalized_name:
            reason = REASON_NORMALIZED_MATCH
        else:
            cache_key = tuple(sorted((normalized_name, existing_normalized)))
            hit = confusable_cache.get(cache_key)
            if hit is None:
                hit = is_confusable(normalized_name, existing_normalized)
                confusable_cache[cache_key] = hit
            if not hit:
                continue
            reason = REASON_CONFUSABLE
        conflicts.append(_conflict_entry(occupant, occupant["source"], reason))
    occupant_conflict_cache[normalized_name] = conflicts
    return conflicts


def _candidate_specs_against_state(
    items: list[dict],
    pairs,
    occupants: list[dict],
    confusable_cache: dict[tuple[str, str], bool],
    occupant_conflict_cache: dict[str, list[dict]],
) -> list[list[CandidateSpec]]:
    """在写事务快照上为每个申请项的每个候选产出静态判定 CandidateSpec。

    静态约束与单条注册完全一致：先按所属命名空间生效策略判定混脚本，再与
    全部既有占用（已注册名称 / 有效预占 / 未过期冷却）做规范名相等 / 同形
    判定；任一不通过即 valid=False，reasons 给出全部命中原因（混脚本一项 +
    逐条既有冲突）。
    """
    grouped: list[list[CandidateSpec]] = []
    for item_index, item in enumerate(items):
        specs: list[CandidateSpec] = []
        for candidate_index, name in enumerate(item["candidates"]):
            normalized = normalize_name(name)
            script_analysis = analyze_scripts(name, pairs)
            reasons: list[dict] = []
            if script_analysis["mixed_script_violation"]:
                reasons.append(
                    {
                        "reason": "mixed_script",
                        "restricted_scripts_used": script_analysis[
                            "restricted_scripts_used"
                        ],
                        "blocked_script_pairs": script_analysis["blocked_script_pairs"],
                        "violating_script_pairs": script_analysis[
                            "violating_script_pairs"
                        ],
                        "script_details": script_analysis["script_details"],
                    }
                )
            reasons.extend(
                _occupant_conflicts(
                    normalized,
                    occupants,
                    confusable_cache,
                    occupant_conflict_cache,
                )
            )
            specs.append(
                CandidateSpec(
                    item_index=item_index,
                    candidate_index=candidate_index,
                    client_id=item["client_id"],
                    original_name=name,
                    normalized_name=normalized,
                    valid=not reasons,
                    reasons=tuple(reasons),
                )
            )
        grouped.append(specs)
    return grouped


def _positions_conflict(left: CandidateSpec, right: CandidateSpec, cache) -> str | None:
    """两个不同申请项的候选之间是否冲突；返回原因或 None。

    规则与既有名称间冲突一致：规范名相等为 normalized_match，否则按
    无序规范名对缓存的 confusables 同形判定为 confusable。
    """
    if left.normalized_name == right.normalized_name:
        return REASON_NORMALIZED_MATCH
    cache_key = tuple(sorted((left.normalized_name, right.normalized_name)))
    hit = cache.get(cache_key)
    if hit is None:
        hit = is_confusable(left.normalized_name, right.normalized_name)
        cache[cache_key] = hit
    return REASON_CONFUSABLE if hit else None


def _build_allocation_model(
    items: list[dict],
    grouped: list[list[CandidateSpec]],
    confusable_cache: dict[tuple[str, str], bool] | None = None,
) -> AllocationModel:
    """由静态判定结果构建位掩码约束模型：有效候选位 + 项间冲突位掩码。

    复用静态判定阶段的 confusables 无序规范名对缓存，避免重复同形计算。
    """
    width = MAX_CANDIDATES_PER_ITEM
    valid_positions: list[int] = []
    for specs in grouped:
        mask = 0
        for spec in specs:
            if spec.valid:
                mask |= 1 << spec.candidate_index
        valid_positions.append(mask)

    model = AllocationModel(
        items=items,
        candidates=grouped,
        valid_positions=valid_positions,
        conflict_masks={},
    )
    cache: dict[tuple[str, str], bool] = (
        confusable_cache if confusable_cache is not None else {}
    )
    for i, left_specs in enumerate(grouped):
        for left_spec in left_specs:
            mask = 0
            # 静态无效候选不会被搜索选中，其禁用位掩码用不到（409 明细也只
            # 导出指向静态有效候选的冲突）；跳过可避免一半以上的同形判定。
            if not left_spec.valid:
                model.conflict_masks[(i, left_spec.candidate_index)] = mask
                continue
            for j, right_specs in enumerate(grouped):
                if j == i:
                    continue
                for right_spec in right_specs:
                    if not right_spec.valid:
                        continue
                    if _positions_conflict(left_spec, right_spec, cache):
                        mask |= 1 << (j * width + right_spec.candidate_index)
            model.conflict_masks[(i, left_spec.candidate_index)] = mask
    return model


def _candidate_blocker_entry(spec: CandidateSpec, model: AllocationModel) -> dict:
    """组装 409 明细中的逐候选阻断原因载荷。

    静态阻断原因（混脚本 / 与既有占用冲突）始终给出；并附该候选与请求中
    其他项**静态有效**候选的项间冲突（candidate_conflicts，按
    (项索引, 候选索引, 原因) 升序，直接由搜索模型的冲突位掩码导出）。
    与静态无效候选的冲突不列出——该候选自身已在其所属项下带静态原因，
    且永不会被选中，不构成真实约束。
    """
    entry = {
        "candidate_index": spec.candidate_index,
        "original_name": spec.original_name,
        "normalized_name": spec.normalized_name,
        "reasons": [dict(reason) for reason in spec.reasons],
    }
    width = MAX_CANDIDATES_PER_ITEM
    mask = model.conflict_masks[(spec.item_index, spec.candidate_index)]
    request_conflicts: list[dict] = []
    for other_index, other_specs in enumerate(model.candidates):
        if other_index == spec.item_index:
            continue
        for other_spec in other_specs:
            bit = 1 << (other_index * width + other_spec.candidate_index)
            if not (mask & bit) or not other_spec.valid:
                continue
            reason = (
                REASON_NORMALIZED_MATCH
                if spec.normalized_name == other_spec.normalized_name
                else REASON_CONFUSABLE
            )
            request_conflicts.append(
                {
                    "item_index": other_index,
                    "client_id": other_spec.client_id,
                    "candidate_index": other_spec.candidate_index,
                    "original_name": other_spec.original_name,
                    "normalized_name": other_spec.normalized_name,
                    "reason": reason,
                }
            )
    request_conflicts.sort(
        key=lambda conflict: (
            conflict["item_index"],
            conflict["candidate_index"],
            conflict["reason"],
        )
    )
    entry["candidate_conflicts"] = request_conflicts
    return entry


def _impossible_items(model: AllocationModel) -> list[dict]:
    """无解时组装“无可选项”的申请项明细（409 allocation_impossible）。

    列出**根因项**：每个候选都被静态约束（脚本 / 规范名 / 同形）或与请求
    中其他项某个静态有效候选的两两冲突直接阻断的申请项——其任意选择都
    无法避开一个具体的、已列出的阻断原因。已证明整体无解时本集合必非空
    （若每项都至少有一个静态有效且与其他项任何候选都不冲突的候选，这些
    候选合起来即构成完整解，与无解矛盾）。极端争用结构下若根因项集合
    意外为空（防御性分支），回退为按请求顺序列出全部申请项，逐候选给出
    静态原因与项间冲突，供客户端整体调整后重试。
    """
    root_items: list[dict] = []
    for item_index, specs in enumerate(model.candidates):
        candidates = [
            blocker
            for spec in specs
            if (
                blocker := _candidate_blocker_entry(spec, model)
            )["reasons"]
            or blocker["candidate_conflicts"]
        ]
        if len(candidates) == len(specs):
            root_items.append(
                {
                    "index": item_index,
                    "client_id": model.items[item_index]["client_id"],
                    "candidates": candidates,
                }
            )

    if root_items:
        return root_items

    # 防御性回退：理论上不可达（见函数说明），仍保证 409 明细完整
    return [
        {
            "index": item_index,
            "client_id": model.items[item_index]["client_id"],
            "candidates": [
                _candidate_blocker_entry(spec, model)
                for spec in model.candidates[item_index]
            ],
        }
        for item_index in range(len(model.items))
    ]


def allocate_names(
    conn: sqlite3.Connection, namespace: str, items: list[dict]
) -> dict:
    """候选集原子注册：单个 BEGIN IMMEDIATE 事务内求解并一次性注册。

    items 为 [{"client_id": ..., "candidates": [name, ...]}]，长度 1–20，
    每项 1–10 个按优先级排列的候选名（client_id 唯一性由 Pydantic 保证）。

    事务流程（任何失败都整体回滚，绝不写入部分结果）：
    1. BEGIN IMMEDIATE 取库级写锁，惰性清理过期预占与释放冷却；
    2. 重新加载命名空间生效脚本策略（与注册 / 批量 / 预占一致，防止只读
       分析后策略被并发改严），对每个候选做静态判定（混脚本 -> 原因里带
       脚本明细；规范名 / 同形 -> 带既有占用冲突明细，覆盖已注册名称、
       有效预占与未过期冷却）；
    3. 预计算项间冲突（不同项选中规范名相等 / 同形即冲突），以位掩码回溯
       搜索字典序最小的完整分配；搜索状态超过 10000 ->
       AllocationSearchTooLargeError（-> 413，事务回滚，不写库）；
    4. 不存在可行组合 -> AllocationImpossibleError（-> 409，附无可选项与
       逐候选阻断原因，事务回滚，不写库）；
    5. 找到解后在同一事务一次性 INSERT 全部已注册名称，返回与请求顺序
       一致的逐项记录（含 client_id 与实际选中的 candidate_index）。
    """
    moment = utc_now()
    grouped: list[list[CandidateSpec]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        purge_expired_reservations(conn, namespace)
        purge_expired_cooldowns(conn, namespace)
        # 事务内持锁加载生效策略与既有占用快照：持写锁期间状态不会被并发改写
        pairs = get_blocked_pairs(conn, namespace)
        occupants = _existing_occupants(conn, namespace, moment)
        confusable_cache: dict[tuple[str, str], bool] = {}
        occupant_conflict_cache: dict[str, list[dict]] = {}
        grouped = _candidate_specs_against_state(
            items,
            pairs,
            occupants,
            confusable_cache,
            occupant_conflict_cache,
        )
        model = _build_allocation_model(items, grouped, confusable_cache)
        solution = model.solve()
        if solution is None:
            raise AllocationImpossibleError(
                _impossible_items(model), model.visited_states
            )
        records: list[dict] = []
        for item_index, candidate_index in enumerate(solution):
            spec = grouped[item_index][candidate_index]
            cursor = conn.execute(
                """
                INSERT INTO display_names (namespace, original_name, normalized_name)
                VALUES (?, ?, ?)
                """,
                (namespace, spec.original_name, spec.normalized_name),
            )
            records.append((cursor.lastrowid, item_index, candidate_index))
        conn.commit()
    except (AllocationImpossibleError, AllocationSearchTooLargeError):
        conn.rollback()
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # 锁竞争 / 唯一约束兜底（持写锁后理论上不会发生）：整体回滚
        conn.rollback()
        raise NameConflictError(str(exc)) from exc

    result_items: list[dict] = []
    for new_id, item_index, candidate_index in records:
        row = conn.execute(
            "SELECT * FROM display_names WHERE id = ?", (new_id,)
        ).fetchone()
        result_items.append(
            {
                "index": item_index,
                "client_id": items[item_index]["client_id"],
                "candidate_index": candidate_index,
                **dict(row),
            }
        )
    return {"namespace": namespace, "items": result_items}
