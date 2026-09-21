"""请求 / 响应模型。"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from .analysis import (
    MAX_BLOCKED_PAIRS,
    normalize_blocked_pairs,
)


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("name must not be blank")
    return value


# 单个显示名的统一约束：1–64 字符、非空白（注册 / 检查 / 批量接口共用）
NameStr = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64),
    AfterValidator(_not_blank),
]

# 命名空间的统一约束：1–64 字符、非空白（合并接口的请求体使用）
NamespaceStr = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64),
    AfterValidator(_not_blank),
]


class NameRequest(BaseModel):
    """注册 / 检查请求体。"""

    name: NameStr = Field(examples=["Аdmin"])


class RenameRequest(BaseModel):
    """原子改名请求体：新名称 + 调用方持有的乐观锁版本。"""

    name: NameStr = Field(examples=["Аdmin"])
    expected_revision: int = Field(
        ge=1,
        examples=[1],
        description=(
            "调用方上次读取到的 revision；与当前值一致才执行改名，"
            "成功后 revision 递增"
        ),
    )


class BatchNameRequest(BaseModel):
    """批量注册请求体：1–50 个名称，全部通过才落库。"""

    names: list[NameStr] = Field(
        min_length=1,
        max_length=50,
        examples=[["paypal", "Alice"]],
    )


class SwapNameEntry(BaseModel):
    """原子交换中的一方：记录 id 与调用方持有的乐观锁版本。"""

    id: int = Field(ge=1, examples=[1])
    expected_revision: int = Field(
        ge=1,
        examples=[1],
        description="调用方上次读取到的该记录 revision；双方版本都一致才执行交换",
    )


class SwapNamesRequest(BaseModel):
    """原子交换请求体：恰好两条不同的记录，各自携带 expected_revision。"""

    records: list[SwapNameEntry] = Field(
        min_length=2,
        max_length=2,
        examples=[{"id": 1, "expected_revision": 1}],
    )

    @model_validator(mode="after")
    def _require_distinct_ids(self) -> "SwapNamesRequest":
        if self.records[0].id == self.records[1].id:
            raise ValueError("records 必须是两条不同的记录（id 不能相同）")
        return self


class ReservationRequest(BaseModel):
    """预占请求体：名称 + 有效期（30–600 秒）。"""

    name: NameStr = Field(examples=["Аdmin"])
    ttl_seconds: int = Field(
        ge=30,
        le=600,
        examples=[120],
        description="预占有效期（秒），范围 30–600；到期未确认即失效",
    )


# 释放冷却时长边界（与 cooldown_seconds 的接口约束保持一致）
MIN_COOLDOWN_SECONDS = 60
MAX_COOLDOWN_SECONDS = 86400


class ReleaseRequest(BaseModel):
    """删除（释放）已注册名称请求体：乐观锁版本 + 冷却时长（60–86400 秒）。"""

    expected_revision: int = Field(
        ge=1,
        examples=[1],
        description=(
            "调用方上次读取到的 revision；与当前值一致才执行删除，"
            "删除与冷却记录写入在同一个事务内完成"
        ),
    )
    cooldown_seconds: int = Field(
        ge=MIN_COOLDOWN_SECONDS,
        le=MAX_COOLDOWN_SECONDS,
        examples=[3600],
        description=(
            "释放冷却时长（秒），范围 60–86400；冷却到期前该名称的规范名"
            "与同形名仍阻止注册 / 预占 / 改名 / 合并"
        ),
    )


class ScriptDetail(BaseModel):
    script: str
    characters: list[str]
    code_points: list[str]


class ScriptPolicyRequest(BaseModel):
    """命名空间混脚本策略 PUT 请求体：阻断脚本对 + 乐观锁版本。

    blocked_script_pairs 无序且去重：输入顺序无关，大小写敏感，重复对只
    保留一次；每一项为恰好两个不同的合法 Unicode 脚本名，空列表表示
    “不拦截任何脚本组合”。
    """

    blocked_script_pairs: list[list[str]] = Field(
        max_length=MAX_BLOCKED_PAIRS,
        examples=[[["Latin", "Cyrillic"], ["Greek", "Latin"]]],
        description=(
            "被阻断的无序脚本对：名称同时出现一对中的两个脚本即违规。"
            "顺序无关、自动去重；空列表表示不拦截任何混脚本组合"
        ),
    )
    expected_revision: int = Field(
        ge=0,
        examples=[0],
        description=(
            "调用方上次读取到的策略 revision；命名空间尚未配置策略时为 0，"
            "首次配置必须传 0，成功后 revision 变为 1，之后每次 PUT 递增"
        ),
    )

    @model_validator(mode="after")
    def _canonicalize_pairs(self) -> "ScriptPolicyRequest":
        # 逐项校验脚本名（未知 / Common / Inherited / Unknown / 同名对 -> 422），
        # 再无序去重并按字典序排列，使服务层只需处理规范对
        normalized = normalize_blocked_pairs(self.blocked_script_pairs)
        object.__setattr__(self, "blocked_script_pairs", [list(pair) for pair in normalized])
        return self


class ScriptPolicy(BaseModel):
    """命名空间混脚本策略（GET 与 PUT 成功的响应）。"""

    namespace: str
    blocked_script_pairs: list[list[str]] = Field(
        description="生效中的阻断脚本对（无序、已去重，按字典序排列）"
    )
    revision: int = Field(description="策略版本号：首次配置为 1，每次成功 PUT 递增")
    created_at: str
    updated_at: str


class ConflictRecord(BaseModel):
    id: int
    original_name: str
    normalized_name: str
    reason: Literal["normalized_match", "confusable"]
    # 冲突来源：已注册名称（默认，向后兼容）、仍有效的预占，或释放冷却记录
    source: Literal["registered", "reservation", "released"] = "registered"
    # 仅 source 为 reservation / released 时出现：有效期 / 冷却到期时刻
    expires_at: str | None = None


class AnalysisResult(BaseModel):
    """名称分析结果（检查接口的响应，也是注册前的内部判定结果）。"""

    namespace: str
    original_name: str
    normalized_name: str
    scripts: list[str]
    script_details: list[ScriptDetail]
    restricted_scripts_used: list[str]
    blocked_script_pairs: list[list[str]] = Field(
        description="本次判定生效的命名空间混脚本策略（未配置时为内建默认策略）"
    )
    violating_script_pairs: list[list[str]] = Field(
        description="名称实际命中的阻断脚本对；非空即 mixed_script_violation"
    )
    mixed_script_violation: bool
    conflicts: list[ConflictRecord]


class NameRecord(BaseModel):
    id: int
    namespace: str
    original_name: str
    normalized_name: str
    revision: int
    created_at: str


class ReservationRecord(BaseModel):
    """预占成功响应：不可猜测的确认 token 与 UTC 过期时刻。"""

    id: int
    namespace: str
    original_name: str
    normalized_name: str
    token: str
    ttl_seconds: int
    expires_at: str
    created_at: str


class ClaimedNameRecord(NameRecord):
    """确认预占成功响应：已注册名称记录 + 消费掉的预占信息。"""

    token: str
    expires_at: str
    consumed_at: str


class CooldownRecord(BaseModel):
    """释放冷却记录：DELETE 成功响应，也是冷却列表接口的列表项。"""

    id: int
    namespace: str
    original_name: str
    normalized_name: str
    cooldown_seconds: int = Field(description="本次释放设置的冷却时长（秒）")
    expires_at: str = Field(description="冷却到期时刻（UTC ISO 8601）；到期前同名 / 同形名仍被拦截")
    created_at: str


class BatchItemRecord(NameRecord):
    """批量注册成功的单项记录：落库字段 + 原请求索引。"""

    index: int


class BatchRegisterResponse(BaseModel):
    """批量注册成功响应：items 按原请求顺序返回。"""

    namespace: str
    items: list[BatchItemRecord]


class SwapNameItemRecord(SwapNameEntry):
    """原子交换成功的单项记录：请求中的 id / expected_revision + 交换后的完整记录。"""

    index: int
    namespace: str
    original_name: str
    normalized_name: str
    revision: int
    created_at: str


class SwapNamesResponse(BaseModel):
    """原子交换成功响应：items 按请求顺序返回交换后的两条记录。"""

    namespace: str
    items: list[SwapNameItemRecord]


class MergeRequest(BaseModel):
    """合并请求体：目标命名空间（源命名空间在路径中）。"""

    target_namespace: NamespaceStr = Field(
        examples=["chat-v2"],
        description="目标命名空间：源命名空间的全部名称与预占记录将迁入此处",
    )


# 跨空间风险簇分析：命名空间数量与快照节点总量上限
MIN_RISK_NAMESPACES = 2
MAX_RISK_NAMESPACES = 20
MAX_RISK_SNAPSHOT = 500


class NameRiskClustersRequest(BaseModel):
    """跨空间同形风险簇请求体：2–20 个互不相同的命名空间 + 快照节点上限（≤500）。"""

    namespaces: list[NamespaceStr] = Field(
        min_length=MIN_RISK_NAMESPACES,
        max_length=MAX_RISK_NAMESPACES,
        examples=[["chat", "forum", "marketplace"]],
        description=(
            "待联合分析的命名空间列表：恰好 2–20 个且互不相同；只对不同命名空间"
            "之间的名称建边，同一命名空间内部不建边"
        ),
    )
    max_names: int = Field(
        ge=1,
        le=MAX_RISK_SNAPSHOT,
        examples=[500],
        description=(
            "这些命名空间内已注册名称、有效预占与未过期冷却快照节点的总量上限；"
            "实际快照总量超过该值时返回 413 且不做截断计算"
        ),
    )

    @model_validator(mode="after")
    def _require_distinct_namespaces(self) -> "NameRiskClustersRequest":
        if len(set(self.namespaces)) != len(self.namespaces):
            raise ValueError("namespaces 中的命名空间必须互不相同")
        return self


class MergeResult(BaseModel):
    """合并成功响应：迁入目标命名空间的记录数量。"""

    source_namespace: str
    target_namespace: str
    merged_names: int = Field(description="迁移的已注册名称数量")
    merged_reservations: int = Field(
        description="迁移的预占记录数量（含已消费的历史记录；过期记录已惰性清理，不迁移）"
    )
    merged_cooldowns: int = Field(
        description="迁移的有效释放冷却记录数量（过期冷却已惰性清理，不迁移）"
    )


# 跨空间同形风险簇的三类节点：kind 同时作为判别联合的 discriminator
RiskNodeKind = Literal["registered", "reservation", "released"]
RiskEdgeReason = Literal["normalized_match", "confusable"]


class RiskNodeRef(BaseModel):
    """簇中边端点的节点稳定标识：namespace + kind + id 唯一确定一个快照节点。"""

    namespace: str
    kind: RiskNodeKind
    id: int


class RiskNodeBase(BaseModel):
    """风险簇节点的公共字段（节点稳定标识 + 原名 / 规范名）。"""

    namespace: str
    id: int
    original_name: str
    normalized_name: str


class RegisteredRiskNode(RiskNodeBase):
    """已注册名称节点：无 expires_at。"""

    kind: Literal["registered"] = "registered"


class ExpiringRiskNode(RiskNodeBase):
    """有效预占 / 未过期释放冷却节点：附 expires_at。"""

    kind: Literal["reservation", "released"]
    expires_at: str = Field(description="预占有效期 / 冷却到期时刻（UTC ISO 8601）")


RiskNode = Annotated[
    RegisteredRiskNode | ExpiringRiskNode, Field(discriminator="kind")
]


class RiskEdge(BaseModel):
    """风险簇中的一条跨空间边：两端节点稳定标识 + 建边原因。"""

    source: RiskNodeRef = Field(description="按 (namespace, kind, id) 排序后的较小端")
    target: RiskNodeRef = Field(description="按 (namespace, kind, id) 排序后的较大端")
    reasons: list[RiskEdgeReason] = Field(
        description="建边原因：normalized_match（规范名相同）或 confusable（同形），排序后给出"
    )


class RiskCluster(BaseModel):
    """跨空间同形风险簇：一个连通分量，至少含 2 个节点且至少一条跨空间边。"""

    cluster_id: int = Field(description="簇编号：按簇内最小节点标识确定性地从 1 开始编号")
    nodes: list[RiskNode] = Field(
        description="簇内全部节点，按 (namespace, kind, id) 升序排列"
    )
    edges: list[RiskEdge] = Field(
        description="簇内全部跨空间边，按 (source, target, reasons) 升序排列"
    )


class NameRiskClustersResponse(BaseModel):
    """跨空间同形风险簇分析结果。"""

    namespaces: list[str] = Field(
        description="本次分析的命名空间（按字典序规范化后的输入集合）"
    )
    max_names: int = Field(description="请求给定的快照节点总量上限")
    snapshot_count: int = Field(
        description=(
            "同一读事务快照内命中的节点总数（已注册名称 + 有效预占 + 未过期冷却）；"
            "该值不超过 max_names 时才会进行分簇计算"
        )
    )
    clusters: list[RiskCluster] = Field(
        description="至少含 2 个节点的风险簇，按簇内最小节点标识升序排列"
    )


# ---------- 快照 Merkle 包含证明 ----------

# 一次快照证明请求的节点数上限
MIN_SNAPSHOT_PROOF_NODES = 1
MAX_SNAPSHOT_PROOF_NODES = 20
# 单命名空间业务快照节点（已注册名称 + 有效预占 + 未过期冷却）上限
MAX_SNAPSHOT_NODES = 500

# 三类业务节点；script_policy 为快照中恒存在的特殊叶
SnapshotProofNodeKind = Literal["registered", "reservation", "released"]


class SnapshotProofNodeRef(BaseModel):
    """请求包含证明的节点引用：kind + id（同一命名空间内定位一个快照节点）。"""

    kind: SnapshotProofNodeKind
    id: int = Field(ge=1)


class SnapshotProofRequest(BaseModel):
    """快照包含证明请求体：1–20 个互不相同的 kind/id 节点 + 业务快照上限。"""

    nodes: list[SnapshotProofNodeRef] = Field(
        min_length=MIN_SNAPSHOT_PROOF_NODES,
        max_length=MAX_SNAPSHOT_PROOF_NODES,
        examples=[[{"kind": "registered", "id": 1}]],
        description=(
            "待取得包含证明的节点引用，1–20 个且 (kind, id) 互不相同；"
            "请求顺序不影响根与各节点证明（证明只由按类型、id 排序后的位置决定）"
        ),
    )
    max_nodes: int = Field(
        ge=1,
        le=MAX_SNAPSHOT_NODES,
        examples=[500],
        description=(
            "该命名空间已注册名称、有效预占与未过期冷却快照节点的总量上限；"
            "实际业务快照总量超过该值时返回 413 且不截断、不构建树"
        ),
    )

    @model_validator(mode="after")
    def _require_distinct_nodes(self) -> "SnapshotProofRequest":
        keys = [(node.kind, node.id) for node in self.nodes]
        if len(set(keys)) != len(keys):
            raise ValueError("nodes 中的 (kind, id) 必须互不相同")
        return self


class SnapshotEffectivePolicy(BaseModel):
    """快照固定的生效脚本策略（未配置时回显内建默认策略，revision=0）。"""

    kind: Literal["script_policy"] = "script_policy"
    revision: int = Field(
        description="策略版本：已配置为库中 revision；未配置时为 0（内建默认策略）"
    )
    blocked_script_pairs: list[list[str]] = Field(
        description="快照时点生效的阻断脚本对（按字典序排列）"
    )
    configured: bool = Field(
        description="命名空间是否显式配置过策略；false 时 blocked_script_pairs 为内建默认策略"
    )


class SnapshotProofLeaf(BaseModel):
    """证明项回显：节点稳定标识 + 叶业务字段 + 叶哈希（0x00 前缀）。"""

    kind: SnapshotProofNodeKind
    id: int
    namespace: str
    original_name: str
    normalized_name: str
    revision: int | None = Field(
        default=None,
        description="仅 registered 节点出现：乐观锁版本",
    )
    expires_at: str | None = Field(
        default=None,
        description="仅 reservation / released 节点出现：预占有效期 / 冷却到期时刻",
    )
    cooldown_seconds: int | None = Field(
        default=None,
        description="仅 released 节点出现：本次释放设置的冷却时长（秒）",
    )
    created_at: str
    leaf_hash: str = Field(description="该叶的 SHA-256 哈希（十六进制）：SHA-256(0x00 || 规范 JSON)")


class SnapshotProofStep(BaseModel):
    """包含证明的一层：兄弟节点哈希与它相对当前节点的方向。"""

    direction: Literal["left", "right"] = Field(
        description=(
            "兄弟节点的方向：left 表示 parent=H(0x01||sibling||current)；"
            "right 表示 parent=H(0x01||current||sibling)"
        )
    )
    hash: str = Field(description="兄弟节点哈希（十六进制）")


class SnapshotProofItem(BaseModel):
    """单个请求节点的包含证明：按请求顺序回显，内容与请求顺序无关。"""

    kind: SnapshotProofNodeKind
    id: int
    leaf: SnapshotProofLeaf = Field(description="该节点在快照中的叶载荷（规范 JSON 的内容视图）")
    leaf_index: int = Field(
        description="叶在排序后 Merkle 树中的下标（script_policy 叶恒为 0）"
    )
    proof: list[SnapshotProofStep] = Field(
        description="自叶向上的包含证明步骤；单叶树为空列表"
    )


class SnapshotProofResponse(BaseModel):
    """快照 Merkle 包含证明响应。"""

    namespace: str
    root: str = Field(description="Merkle 根哈希（十六进制）")
    leaf_count: int = Field(
        description=(
            "Merkle 树叶节点总数：业务快照节点（已注册名称 + 有效预占 + 未过期冷却）"
            "外加恒存在的 1 个生效脚本策略叶"
        )
    )
    snapshot_count: int = Field(
        description="业务快照节点总数（leaf_count - 1）；413 判定即该值是否超过 max_nodes"
    )
    max_nodes: int = Field(description="请求给定的业务快照节点总量上限")
    policy: SnapshotEffectivePolicy = Field(description="快照固定的生效脚本策略（排序后的第 0 个叶）")
    items: list[SnapshotProofItem] = Field(
        description="每个请求节点的包含证明，按请求顺序返回（证明内容与请求顺序无关）"
    )


class ErrorDetail(BaseModel):
    error: str
    message: str
