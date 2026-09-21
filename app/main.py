"""FastAPI 应用入口。

接口一览：
- POST   /namespaces/{namespace}/names                              注册显示名（201 / 409 / 422）
- POST   /namespaces/{namespace}/names/allocate                     候选集原子注册（201 / 409 / 413 / 422，单事务）
- POST   /namespaces/{namespace}/names/batch                        批量注册（201 / 409 / 422，单事务）
- POST   /namespaces/{namespace}/names/swap                         原子交换两条已注册名称（200 / 404 / 409 / 422，单事务）
- PATCH  /namespaces/{namespace}/names/{id}                         原子改名（200 / 404 / 409 / 422，单事务）
- DELETE /namespaces/{namespace}/names/{id}                         删除名称并进入释放冷却（200 / 404 / 409 / 422，单事务）
- GET    /namespaces/{namespace}/cooldowns                          列出命名空间内有效释放冷却记录
- POST   /namespaces/{namespace}/reservations                       预占名称（201 / 409 / 422）
- POST   /namespaces/{namespace}/reservations/{token}/claim         确认预占并注册（201 / 404 / 409 / 410）
- POST   /namespaces/{source}/merge                                 合并命名空间（200 / 409 / 422，单事务）
- PUT    /namespaces/{namespace}/script-policy                      配置命名空间混脚本策略（200 / 409 / 422，单事务）
- GET    /namespaces/{namespace}/script-policy                      读取命名空间混脚本策略（200 / 404）
- POST   /namespaces/{namespace}/check                              只分析不落库（200）
- GET    /namespaces/{namespace}/names                              列出命名空间内已注册名称
- POST   /name-risk-clusters                                        跨空间同形风险簇（200 / 413 / 422，单一读事务快照）
- POST   /namespaces/{namespace}/snapshot-proof                     快照 Merkle 包含证明（200 / 404 / 413 / 422，单一读事务快照）
- GET    /health                                                    健康检查
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
import sqlite3

from fastapi import Depends, FastAPI, HTTPException, status

from . import database, service
from .analysis import normalize_name
from .schemas import (
    AllocationRequest,
    AllocationResponse,
    AnalysisResult,
    BatchNameRequest,
    BatchRegisterResponse,
    ClaimedNameRecord,
    CooldownRecord,
    MergeRequest,
    MergeResult,
    NameRecord,
    NameRequest,
    NameRiskClustersRequest,
    NameRiskClustersResponse,
    ReleaseRequest,
    RenameRequest,
    ReservationRecord,
    ReservationRequest,
    ScriptPolicy,
    ScriptPolicyRequest,
    SnapshotProofRequest,
    SnapshotProofResponse,
    SwapNamesRequest,
    SwapNamesResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时执行版本化数据库迁移（幂等；迁移被改写 / 版本缺口时抛错，应用不启动）
    database.init_db()
    yield


app = FastAPI(
    title="Unicode Display Name Anti-Spoofing API",
    version="1.9.0",
    description=(
        "命名空间内的显示名防仿冒：NFKC+casefold 规范名查重、"
        "confusables 同形字符判定；混脚本拦截支持命名空间级策略"
        "（PUT/GET script-policy，未配置时默认 Latin/Cyrillic/Greek "
        "任意两种混用即违规）；支持预占后确认注册（reservations / claim），"
        "未过期预占与已注册名称一样参与全部冲突判定；支持把源命名空间的"
        "名称、预占与释放冷却记录整体合并到目标命名空间（merge，以目标策略"
        "检查两侧数据，冲突或违规时不修改任何数据）；支持凭 expected_revision 对"
        "已注册名称做原子改名（PATCH），改名在单个写锁事务内完成版本校验"
        "与冲突复查；支持同命名空间内两条已注册名称的原子交换（names/swap），"
        "单事务内校验双方归属与版本后成对互换名称并各递增 revision；"
        "支持删除名称并设置释放冷却（DELETE）：单事务内校验归属与版本后删除"
        "名称并生成带 expires_at 的冷却记录，冷却到期前与已注册名称、有效预占"
        "同等参与全部规范名、同形与混脚本判定（冲突明细 source=released）；"
        "支持候选集原子注册（names/allocate）：1–20 个申请项各带唯一 "
        "client_id 与按优先级排列的 1–10 个候选名，在单个 BEGIN IMMEDIATE "
        "事务内按脚本策略、规范名、同形、有效预占与释放冷却约束求解互不冲突"
        "的字典序最小组合并一次性注册，无解返回 409（附逐候选阻断原因）、"
        "搜索状态超过 10000 返回 413，均不写入部分结果；"
        "支持跨命名空间同形风险簇分析（POST /name-risk-clusters）：在单个"
        "SQLite 读事务快照上固定 2–20 个命名空间的已注册名称、有效预占与"
        "未过期冷却，只对不同空间的名称按规范名相同或同形建边，按连通分量"
        "返回至少含 2 个节点的风险簇（快照总量超过 max_names 时返回 413，"
        "不截断计算）；支持对单个命名空间生成快照 Merkle 包含证明"
        "（POST /namespaces/{namespace}/snapshot-proof）：在单个 SQLite 读事务"
        "快照上固定生效脚本策略、已注册名称、有效预占与未过期冷却，按类型与 id "
        "排序后以规范 JSON + SHA-256 构建 Merkle 树（叶 0x00、父 0x01、奇数层"
        "复制末节点），为 1–20 个请求节点返回带左右方向的包含证明。"
    ),
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = database.get_connection()
    try:
        yield conn
    finally:
        conn.close()


def _mixed_script_error(analysis: dict) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "error": "mixed_script_violation",
            "message": (
                "名称同时使用了所属命名空间混脚本策略所阻断的脚本组合，"
                "存在仿冒风险，已拒绝"
            ),
            "restricted_scripts_used": analysis["restricted_scripts_used"],
            "blocked_script_pairs": analysis["blocked_script_pairs"],
            "violating_script_pairs": analysis["violating_script_pairs"],
            "script_details": analysis["script_details"],
        },
    )


def _conflict_error(normalized_name: str, conflicts: list[dict], message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "name_conflict",
            "message": message,
            "normalized_name": normalized_name,
            "conflicts": conflicts,
        },
    )


CONFLICT_MESSAGE = "规范名相同或与命名空间内已有名称（含未过期预占/释放冷却）同形"


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/name-risk-clusters",
    response_model=NameRiskClustersResponse,
    responses={
        413: {"description": "这些命名空间的快照节点总量超过 max_names，未进行任何分簇计算"},
        422: {"description": "namespaces 数量不在 2–20 个范围、含重复项，或 max_names 超出 1–500"},
    },
)
def name_risk_clusters(
    payload: NameRiskClustersRequest, conn=Depends(get_db)
) -> dict:
    """跨命名空间同形风险簇：在同一个 SQLite 读事务快照上联合分析多个命名空间。

    接收 2–20 个互不相同的命名空间与 max_names（1–500）。在单个读事务中
    固定这些空间的**已注册名称、未过期未消费预占与未过期释放冷却**快照
    （只读、不清理过期记录）；快照节点总量超过 max_names 时直接返回
    `413 risk_snapshot_too_large`（附 snapshot_count 与 max_names），
    **不截断快照、不做分簇计算**。

    建边仅发生在**不同命名空间**的节点之间：规范名（NFKC+casefold）相同
    记为 `normalized_match`，否则 confusables 同形记为 `confusable`；同一
    命名空间内部不建边。随后按连通分量分簇，只返回至少含 2 个节点的簇——
    因而 A-B、B-C 即使 A-C 并不直接冲突也会归入同一簇。节点以
    `namespace` / `kind`（registered/reservation/released）/ `id` 稳定标识，
    预占与冷却节点附 `expires_at`；边给出排序后的两端节点标识与 `reasons`。
    簇、节点、边全部确定性排序（节点键 namespace、kind、id；簇按最小节点键，
    cluster_id 从 1 开始）。
    """
    try:
        return service.find_name_risk_clusters(
            conn, payload.namespaces, payload.max_names
        )
    except service.RiskSnapshotTooLargeError as exc:
        raise HTTPException(
            status_code=413,  # 413 Payload Too Large（新旧 starlette 常量名不同，直接用字面值）
            detail={
                "error": "risk_snapshot_too_large",
                "message": (
                    "这些命名空间的已注册名称、有效预占与未过期冷却快照总量"
                    f"为 {exc.snapshot_count}，超过请求给定的 max_names="
                    f"{exc.limit}；未截断快照，也未进行分簇计算，请收窄命名空间"
                    "或提高 max_names 后重试"
                ),
                "snapshot_count": exc.snapshot_count,
                "max_names": exc.limit,
            },
        )


@app.post(
    "/namespaces/{namespace}/snapshot-proof",
    response_model=SnapshotProofResponse,
    responses={
        404: {"description": "请求的节点在当前快照中不存在（不存在 / 已过期 / 已消费），附全部缺失引用"},
        413: {"description": "命名空间业务快照节点总量超过 max_nodes，未截断快照、未构建树"},
        422: {"description": "nodes 数量不在 1–20 个范围、含重复 (kind, id)，或 max_nodes 超出 1–500"},
    },
)
def snapshot_proof(
    namespace: str, payload: SnapshotProofRequest, conn=Depends(get_db)
) -> dict:
    """快照 Merkle 包含证明：在同一个 SQLite 读事务快照上固定并承诺数据。

    接收 1–20 个由 `kind`（registered/reservation/released）与 `id` 表示的
    节点及 `max_nodes`（1–500）。在**单个只读事务**中固定该命名空间的
    **生效脚本策略、已注册名称、未过期未消费预占与未过期释放冷却**（只读、
    不清理过期记录）；业务快照节点总量超过 `max_nodes` 时直接返回
    `413 snapshot_too_large`（附 snapshot_count 与 max_nodes），**不截断
    快照、不构建树**。随后校验请求节点全部存在于快照，任一缺失返回
    `404 snapshot_nodes_not_found`（按请求顺序附全部缺失的 kind/id）。

    Merkle 叶集合为 1 个恒存在的 `script_policy` 叶（排序后下标恒为 0；
    命名空间未配置策略时固定内建默认策略，revision=0）加全部业务叶，按
    **（类型序, id）**排序（script_policy < registered < reservation <
    released）。每个叶把业务字段编码为**规范 JSON**（键按字典序、紧凑分隔、
    UTF-8 原样保留非 ASCII），叶哈希 `SHA-256(0x00 || json)`，父哈希
    `SHA-256(0x01 || left || right)`，每层奇数个节点时复制末节点与自身配对。

    响应给出 `root`、`leaf_count`（业务节点数 + 1 个策略叶）以及每个请求
    节点的叶载荷、叶下标与包含证明：证明步骤自叶向上，`direction` 为兄弟
    节点方向（left 时 `parent=H(0x01||sibling||current)`，right 时反之），
    `hash` 为兄弟哈希的十六进制。证明只由节点排序后的位置决定，故**相同
    快照下请求节点顺序变化不改变根，也不改变各节点自身的证明**；`items`
    仅按请求顺序回显以便客户端对齐。
    """
    nodes = [{"kind": item.kind, "id": item.id} for item in payload.nodes]
    try:
        return service.build_snapshot_proof(
            conn, namespace, nodes, payload.max_nodes
        )
    except service.SnapshotTooLargeError as exc:
        raise HTTPException(
            status_code=413,  # 413 Payload Too Large（新旧 starlette 常量名不同，直接用字面值）
            detail={
                "error": "snapshot_too_large",
                "message": (
                    "该命名空间的已注册名称、有效预占与未过期冷却快照总量"
                    f"为 {exc.snapshot_count}，超过请求给定的 max_nodes="
                    f"{exc.limit}；未截断快照，也未构建 Merkle 树，请收窄"
                    "命名空间数据范围或提高 max_nodes 后重试"
                ),
                "snapshot_count": exc.snapshot_count,
                "max_nodes": exc.limit,
            },
        )
    except service.SnapshotNodesNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "snapshot_nodes_not_found",
                "message": (
                    "请求的部分节点在当前快照中不存在（记录不存在，"
                    "或预占 / 冷却已过期、预占已被消费）"
                ),
                "missing": exc.missing,
            },
        )


@app.post(
    "/namespaces/{namespace}/names",
    status_code=status.HTTP_201_CREATED,
    response_model=NameRecord,
    responses={
        409: {"description": "规范名相同或与已有名称/有效预占/释放冷却同形（confusable）"},
        422: {"description": "混用 Latin/Cyrillic/Greek 中两种及以上脚本"},
    },
)
def register_name(namespace: str, payload: NameRequest, conn=Depends(get_db)) -> dict:
    analysis = service.analyze_name(conn, namespace, payload.name)

    if analysis["mixed_script_violation"]:
        raise _mixed_script_error(analysis)

    if analysis["conflicts"]:
        raise _conflict_error(
            analysis["normalized_name"], analysis["conflicts"], CONFLICT_MESSAGE
        )

    try:
        return service.register_name(
            conn, namespace, payload.name, analysis["normalized_name"]
        )
    except service.MixedScriptNameError as exc:
        # 并发策略改严：只读分析通过、写事务内复查命中新策略
        raise _mixed_script_error(exc.analysis)
    except service.NameConflictError as exc:
        # 并发下唯一约束/事务复查兜底：优先用事务内复查结果，否则重新查出冲突
        conflicts = exc.conflicts or service.find_conflicts(
            conn, namespace, analysis["normalized_name"]
        )
        raise _conflict_error(
            analysis["normalized_name"], conflicts, CONFLICT_MESSAGE
        )


@app.patch(
    "/namespaces/{namespace}/names/{name_id}",
    response_model=NameRecord,
    responses={
        404: {"description": "记录不存在，或不属于该命名空间"},
        409: {"description": "expected_revision 已过期（stale_revision），或新名称与其他记录/有效预占/释放冷却冲突"},
        422: {"description": "混用 Latin/Cyrillic/Greek 中两种及以上脚本"},
    },
)
def rename_registered_name(
    namespace: str, name_id: int, payload: RenameRequest, conn=Depends(get_db)
) -> dict:
    """原子改名：在单个 BEGIN IMMEDIATE 事务内完成乐观锁校验与改名。

    事务内按序校验：记录存在且命名空间匹配（否则 404）-> revision 与
    expected_revision 一致（否则 409 stale_revision，附 current_revision）
    -> 新名称不混脚本（否则 422）-> 新规范名与同空间其他已注册名称
    （排除当前记录）、全部有效预占及未过期释放冷却不冲突（否则 409，沿用
    注册接口的冲突明细结构）。成功后保留 id / created_at，更新原名与规范
    名，revision + 1。
    """
    try:
        return service.rename_name(
            conn, namespace, name_id, payload.name, payload.expected_revision
        )
    except service.NameNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "name_not_found",
                "message": "已注册名称不存在，或不属于该命名空间",
            },
        )
    except service.StaleRevisionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "stale_revision",
                "message": "名称记录已被其他请求修改，请重新读取最新版本后重试",
                "current_revision": exc.current_revision,
            },
        )
    except service.MixedScriptNameError as exc:
        raise _mixed_script_error(exc.analysis)
    except service.NameConflictError as exc:
        normalized_name = normalize_name(payload.name)
        # 并发下锁/唯一约束兜底：优先用事务内复查结果，否则回滚后重新查出冲突
        conflicts = exc.conflicts or service.find_conflicts(
            conn, namespace, normalized_name, exclude_name_id=name_id
        )
        raise _conflict_error(normalized_name, conflicts, CONFLICT_MESSAGE)


@app.delete(
    "/namespaces/{namespace}/names/{name_id}",
    response_model=CooldownRecord,
    responses={
        200: {"description": "名称已删除，返回新生成的释放冷却记录（含 expires_at）"},
        404: {"description": "记录不存在，或不属于该命名空间"},
        409: {"description": "expected_revision 已过期（stale_revision，附 current_revision），或锁竞争"},
        422: {"description": "cooldown_seconds 超出 60–86400 范围，或 expected_revision 非正整数"},
    },
)
def release_registered_name(
    namespace: str, name_id: int, payload: ReleaseRequest, conn=Depends(get_db)
) -> dict:
    """删除已注册名称并设置释放冷却：单个 BEGIN IMMEDIATE 事务内完成。

    事务内按序校验：记录存在且命名空间匹配（否则 404）-> revision 与
    expected_revision 一致（否则 409 stale_revision，附 current_revision）。
    通过后在同一事务内删除 display_names 行并写入一条带 expires_at 的
    name_cooldowns 记录（冷却时长 cooldown_seconds，范围 60–86400 秒）。
    冷却到期前，该名称的规范名与同形名与已注册名称、有效预占同等参与
    单条 / 批量注册、预占、改名、check、命名空间合并及脚本策略更新的
    全部判定，冲突明细以 source="released" 标识；到期记录在后续写操作中
    惰性清理，之后名称可被重新注册。任何失败都整体回滚，不存在“已删除但
    无冷却”的部分更新。
    """
    try:
        return service.release_name(
            conn,
            namespace,
            name_id,
            payload.expected_revision,
            payload.cooldown_seconds,
        )
    except service.NameNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "name_not_found",
                "message": "已注册名称不存在，或不属于该命名空间",
            },
        )
    except service.StaleRevisionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "stale_revision",
                "message": "名称记录已被其他请求修改，请重新读取最新版本后重试",
                "current_revision": exc.current_revision,
            },
        )
    except service.NameConflictError:
        # 锁竞争兜底：事务已整体回滚（名称仍在、冷却未写），重读版本后重试
        row = conn.execute(
            "SELECT revision FROM display_names WHERE id = ? AND namespace = ?",
            (name_id, namespace),
        ).fetchone()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "release_conflict",
                "message": "删除时发生并发竞争，事务已整体回滚（无部分更新），请重试",
                "current_revision": row["revision"] if row else None,
            },
        )


@app.post(
    "/namespaces/{namespace}/names/swap",
    response_model=SwapNamesResponse,
    responses={
        404: {"description": "任一记录不存在，或不属于该命名空间"},
        409: {"description": "任一方 expected_revision 已过期（stale_revision，附双方当前 revision），或锁竞争"},
        422: {"description": "records 不是恰好两条，或两条记录的 id 相同"},
    },
)
def swap_registered_names(
    namespace: str, payload: SwapNamesRequest, conn=Depends(get_db)
) -> dict:
    """原子交换：在单个 BEGIN IMMEDIATE 事务内校验双方归属与版本后成对互换名称。

    事务内按序校验：两条记录都存在且属于该命名空间（任一不满足 -> 404，
    附全部缺失 id）-> 双方 revision 都等于各自 expected_revision（任一
    过期 -> 409 stale_revision，明细同时给出双方当前 revision，客户端
    重读后可整体重试）。通过后在同一事务内借助未提交的随机临时名腾出
    唯一约束槽位，三步成对互换 original_name 与 normalized_name，双方
    revision 各 + 1；id / created_at 保持不变。临时名只存在于未提交事务
    中（写锁 + 快照隔离使任何并发请求都观察不到），任何失败都整体回滚，
    不存在部分更新。
    """
    entries = [(item.id, item.expected_revision) for item in payload.records]
    try:
        records = service.swap_names(conn, namespace, entries)
    except service.SwapRecordNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "name_not_found",
                "message": "待交换的名称记录不存在，或不属于该命名空间",
                "missing_ids": exc.missing_ids,
            },
        )
    except service.SwapStaleRevisionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "stale_revision",
                "message": (
                    "名称记录已被其他请求修改（双方中至少一方版本过期），"
                    "请重新读取双方最新版本后重试"
                ),
                "records": exc.records,
            },
        )
    except service.NameConflictError:
        # 锁竞争 / 临时名随机碰撞（概率可忽略）：事务已整体回滚，重读双方当前版本
        records_detail = []
        for rid, expected in entries:
            row = conn.execute(
                "SELECT revision FROM display_names WHERE id = ? AND namespace = ?",
                (rid, namespace),
            ).fetchone()
            records_detail.append(
                {
                    "id": rid,
                    "expected_revision": expected,
                    "current_revision": row["revision"] if row else None,
                }
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "swap_conflict",
                "message": "交换时发生并发竞争，事务已整体回滚（无部分更新），请重试",
                "records": records_detail,
            },
        )

    items = [
        {
            "index": index,
            "id": record["id"],
            "expected_revision": expected,
            **record,
        }
        for index, ((_, expected), record) in enumerate(zip(entries, records, strict=True))
    ]
    return {"namespace": namespace, "items": items}


@app.post(
    "/namespaces/{namespace}/names/allocate",
    status_code=status.HTTP_201_CREATED,
    response_model=AllocationResponse,
    responses={
        409: {"description": "不存在互不冲突的可行组合（allocation_impossible，附无可选项与逐候选阻断原因），不写入任何记录"},
        413: {"description": "求解访问的搜索状态超过 10000（allocation_search_too_large），不写入任何记录"},
        422: {"description": "items 数量不在 1–20、client_id 重复或为空、某项 candidates 数量不在 1–10"},
    },
)
def allocate_names(namespace: str, payload: AllocationRequest, conn=Depends(get_db)) -> dict:
    """候选集原子注册：单个 BEGIN IMMEDIATE 事务内为全部申请项统一求解。

    接收 1–20 个申请项，每项含请求内唯一的 ``client_id`` 与按优先级排列的
    1–10 个候选名。事务内持写锁惰性清理过期预占与释放冷却、重新加载命名
    空间生效脚本策略，随后对每个候选按与单条注册完全一致的规则做静态
    判定（混脚本 / 规范名相同 / confusables 同形；对手覆盖已注册名称、
    有效预占与未过期释放冷却），再对不同申请项的候选施加同样的两两冲突
    约束，回溯寻找全部申请项的完整分配。候选按序号（优先级）升序试探，
    故返回的解是各项候选序号向量的**字典序最小解**，找到后在同一事务
    一次性注册。

    - 任一申请项无可行组合 -> ``409 allocation_impossible``，列出无可选项
      及其逐候选阻断原因（静态原因 + 与请求中其他项候选的项间冲突），
      事务回滚、不写入部分结果；
    - 搜索访问状态超过 10000 -> ``413 allocation_search_too_large``，
      事务回滚、不写库；
    - 全部成功 -> ``201``，items 按请求顺序回显，附实际中选的
      ``client_id`` / ``candidate_index`` 与落库记录。
    """
    items = [
        {"client_id": item.client_id, "candidates": list(item.candidates)}
        for item in payload.items
    ]
    try:
        return service.allocate_names(conn, namespace, items)
    except service.AllocationImpossibleError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "allocation_impossible",
                "message": (
                    "不存在让全部申请项互不冲突的候选组合；以下申请项的每个候选"
                    "都被静态约束（混脚本 / 规范名 / 同形）或与请求中其他项候选的"
                    "冲突阻断，事务已回滚，未写入任何记录"
                ),
                "searched_states": exc.searched_states,
                "items": exc.items,
            },
        )
    except service.AllocationSearchTooLargeError as exc:
        raise HTTPException(
            status_code=413,  # 413 Payload Too Large（新旧 starlette 常量名不同，直接用字面值）
            detail={
                "error": "allocation_search_too_large",
                "message": (
                    f"为全部申请项寻找互不冲突组合的搜索已访问 "
                    f"{exc.searched_states} 个状态，超过上限 {exc.limit}；"
                    "事务已回滚，未写入任何记录，请调整候选后重试"
                ),
                "searched_states": exc.searched_states,
                "limit": exc.limit,
            },
        )
    except service.NameConflictError:
        # 锁竞争 / 唯一约束兜底：事务已整体回滚（无部分写入），客户端可重试
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "allocation_conflict",
                "message": "候选集注册时发生并发竞争，事务已整体回滚（无部分写入），请重试",
            },
        )


@app.post(
    "/namespaces/{namespace}/names/batch",
    status_code=status.HTTP_201_CREATED,
    response_model=BatchRegisterResponse,
    responses={
        409: {"description": "并发竞争使事务判定失效：已回滚，返回重新分析后的冲突明细"},
        422: {"description": "批次中存在混脚本或库内/预占/批内冲突：汇总明细，未写入任何记录"},
    },
)
def register_names_batch(
    namespace: str, payload: BatchNameRequest, conn=Depends(get_db)
) -> dict:
    # 整批与库内已有名称及有效预占联合分析：冲突 + 批内两两冲突
    analyses = service.analyze_batch(conn, namespace, payload.names)

    failures = [
        item
        for item in analyses
        if item["mixed_script_violation"] or item["conflicts"] or item["batch_conflicts"]
    ]
    if failures:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "batch_validation_failed",
                "message": (
                    f"批次中 {len(failures)}/{len(analyses)} 个名称存在混脚本或冲突问题，"
                    "未写入任何记录"
                ),
                "items": failures,
            },
        )

    try:
        records = service.insert_batch(conn, namespace, payload.names)
    except service.BatchMixedScriptError as exc:
        # 并发策略改严：批次只读分析通过、写事务内复查命中新策略 -> 409
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "mixed_script_violation",
                "message": "并发的策略修改使批次判定失效，事务已回滚；以下为事务内复查命中混脚本策略的项",
                "items": exc.items,
            },
        )
    except service.NameConflictError:
        # 并发竞争：事务已回滚，重新分析后返回 409 冲突明细
        refreshed = service.analyze_batch(conn, namespace, payload.names)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "name_conflict",
                "message": "并发写入使批次判定失效，事务已回滚；以下为重新分析后的冲突明细",
                "items": [
                    item
                    for item in refreshed
                    if item["conflicts"] or item["batch_conflicts"]
                    or item["mixed_script_violation"]
                ],
            },
        )

    return {
        "namespace": namespace,
        "items": [
            {"index": index, **record} for index, record in enumerate(records)
        ],
    }


@app.post(
    "/namespaces/{namespace}/reservations",
    status_code=status.HTTP_201_CREATED,
    response_model=ReservationRecord,
    responses={
        409: {"description": "规范名相同或与已有名称/有效预占/释放冷却同形（confusable）"},
        422: {"description": "混脚本违规，或 ttl_seconds 超出 30–600 范围"},
    },
)
def reserve_name(
    namespace: str, payload: ReservationRequest, conn=Depends(get_db)
) -> dict:
    """预占名称：复用规范化 / 混脚本 / 同形判定，返回不可猜测 token。

    未过期、未消费的预占与已注册名称一样阻止后续单条注册、批量注册及
    新预占；判定同时覆盖未过期释放冷却。预占记录在确认（claim）后才
    真正写入 display_names。
    """
    analysis = service.analyze_name(conn, namespace, payload.name)

    if analysis["mixed_script_violation"]:
        raise _mixed_script_error(analysis)

    if analysis["conflicts"]:
        raise _conflict_error(
            analysis["normalized_name"], analysis["conflicts"], CONFLICT_MESSAGE
        )

    try:
        return service.create_reservation(
            conn, namespace, payload.name, payload.ttl_seconds
        )
    except service.MixedScriptNameError as exc:
        # 并发策略改严：只读分析通过、写事务内复查命中新策略
        raise _mixed_script_error(exc.analysis)
    except service.NameConflictError as exc:
        # 并发：取写锁后复查发现冲突（含其他请求刚刚创建的有效预占）
        conflicts = exc.conflicts or service.find_conflicts(
            conn, namespace, analysis["normalized_name"]
        )
        raise _conflict_error(
            analysis["normalized_name"], conflicts, CONFLICT_MESSAGE
        )


@app.post(
    "/namespaces/{namespace}/reservations/{token}/claim",
    status_code=status.HTTP_201_CREATED,
    response_model=ClaimedNameRecord,
    responses={
        404: {"description": "token 不存在或不属于该命名空间"},
        409: {"description": "预占已被消费（重复/并发确认），或当前与已有名称/其他有效预占/释放冷却冲突"},
        410: {"description": "预占已过期（记录被惰性清理）"},
    },
)
def claim_reservation(namespace: str, token: str, conn=Depends(get_db)) -> dict:
    """在单个 BEGIN IMMEDIATE 事务中校验并确认预占：

    命名空间匹配 -> 有效期（过期 410）-> 消费状态（重复确认 409）
    -> 当前冲突（409，不写入）；全部通过才写 display_names 并标记已消费。
    """
    try:
        return service.claim_reservation(conn, namespace, token)
    except service.ReservationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "reservation_not_found",
                "message": "预占不存在，或不属于该命名空间",
            },
        )
    except service.ReservationExpiredError:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "error": "reservation_expired",
                "message": "预占已过期且未确认，已失效",
            },
        )
    except service.ReservationConsumedError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "reservation_already_claimed",
                "message": "该预占已确认注册，不能重复消费",
            },
        )
    except service.NameConflictError as exc:
        # 事务内复查发现冲突：未写入任何记录。回滚后重新查询保证返回最新明细
        reservation = conn.execute(
            "SELECT normalized_name FROM name_reservations WHERE token = ?",
            (token,),
        ).fetchone()
        normalized_name = reservation["normalized_name"] if reservation else ""
        conflicts = exc.conflicts or service.find_conflicts(
            conn, namespace, normalized_name
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "name_conflict",
                "message": "确认时名称与已注册名称、其他有效预占或释放冷却冲突，未写入",
                "normalized_name": normalized_name,
                "conflicts": conflicts or [],
            },
        )


@app.post(
    "/namespaces/{source}/merge",
    response_model=MergeResult,
    responses={
        409: {
            "description": "源与目标存在规范名/同形冲突（含有效预占与释放冷却），或并发写入使合并判定失效；均未修改数据"
        },
        422: {"description": "目标命名空间空白或与源命名空间相同"},
    },
)
def merge_namespace(source: str, payload: MergeRequest, conn=Depends(get_db)) -> dict:
    """合并命名空间：把 source 的全部已注册名称、有效预占与释放冷却记录迁入 target。

    在单个 BEGIN IMMEDIATE 写锁事务中，先对两侧的已注册名称、有效预占与
    未过期冷却做跨空间规范名 / 同形复查（过期记录先惰性清理）：任一冲突
    -> 409 并返回双方记录，不修改任何数据；无冲突则在同一事务内迁移
    （冷却与名称、预占一起 UPDATE 到目标空间）并返回迁移数量。并发写入
    导致复查或唯一约束失败时整体回滚，重新检测后返回 409。
    """
    target = payload.target_namespace
    if target == source:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "invalid_merge_target",
                "message": "目标命名空间与源相同，无需合并",
            },
        )

    try:
        return service.merge_namespaces(conn, source, target)
    except service.MergeConflictError as exc:
        if exc.conflicts or exc.script_violations:
            # 事务内复查发现冲突或脚本违规：直接返回明细，未修改任何数据
            conflicts = exc.conflicts
            script_violations = exc.script_violations
            if conflicts and script_violations:
                message = (
                    "源与目标命名空间存在规范名/同形冲突（含有效预占与释放冷却），"
                    "且源/目标数据存在违反目标命名空间混脚本策略的记录，"
                    "未修改任何数据"
                )
            elif conflicts:
                message = "源与目标命名空间存在规范名/同形冲突（含有效预占与释放冷却），未修改任何数据"
            else:
                message = (
                    "源/目标命名空间存在违反目标命名空间混脚本策略的已注册名称、"
                    "有效预占或释放冷却记录，未修改任何数据"
                )
        else:
            # 并发写入导致唯一约束/锁失败：已整体回滚，重新检测后返回明细
            conflicts = service.find_merge_conflicts(conn, source, target)
            script_violations = service.find_merge_script_violations(conn, source, target)
            message = "并发写入使合并判定失效，事务已回滚；以下为重新检测后的冲突明细"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "merge_conflict",
                "message": message,
                "source_namespace": source,
                "target_namespace": target,
                "conflicts": conflicts,
                "script_violations": script_violations,
            },
        )


@app.post(
    "/namespaces/{namespace}/check",
    response_model=AnalysisResult,
)
def check_name(namespace: str, payload: NameRequest, conn=Depends(get_db)) -> dict:
    """只分析不落库：返回规范名、脚本分析与潜在冲突（含未过期预占与释放冷却）。"""
    return service.analyze_name(conn, namespace, payload.name)


@app.get(
    "/namespaces/{namespace}/names",
    response_model=list[NameRecord],
)
def list_names(namespace: str, conn=Depends(get_db)) -> list[dict]:
    return service.list_names(conn, namespace)


@app.get(
    "/namespaces/{namespace}/cooldowns",
    response_model=list[CooldownRecord],
)
def list_cooldowns(namespace: str, conn=Depends(get_db)) -> list[dict]:
    """列出命名空间内仍有效的释放冷却记录（过期记录不出现，只读不清理）。"""
    return service.list_cooldowns(conn, namespace)


@app.get(
    "/namespaces/{namespace}/script-policy",
    response_model=ScriptPolicy,
    responses={
        404: {"description": "该命名空间尚未配置混脚本策略（未配置时沿用内建默认策略）"},
    },
)
def get_script_policy(namespace: str, conn=Depends(get_db)) -> dict:
    """读取命名空间混脚本策略。

    未配置策略的命名空间沿用内建默认策略（Latin/Cyrillic/Greek 任意两种
    混用即违规），但本接口对未配置空间返回 404，以便客户端区分“显式
    配置”与“默认行为”，并据此以 expected_revision=0 发起首次 PUT。
    """
    try:
        return service.get_script_policy_or_404(conn, namespace)
    except service.ScriptPolicyNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "script_policy_not_found",
                "message": (
                    "该命名空间尚未配置混脚本策略，当前沿用内建默认策略"
                    "（Latin/Cyrillic/Greek 任意两种混用即违规）；"
                    "首次配置请以 expected_revision=0 调用 PUT"
                ),
            },
        )


@app.put(
    "/namespaces/{namespace}/script-policy",
    response_model=ScriptPolicy,
    responses={
        409: {
            "description":
                "expected_revision 已过期（stale_revision，附 current_revision，"
                "未配置时为 0），或新策略会使现存已注册名称/有效预占/释放冷却"
                "违规（script_policy_conflict，附逐条违规明细），策略不更新"
        },
        422: {"description": "blocked_script_pairs 含未知脚本名 / 同名对 / 项长度不为 2 / 超过上限"},
    },
)
def put_script_policy(
    namespace: str, payload: ScriptPolicyRequest, conn=Depends(get_db)
) -> dict:
    """配置命名空间混脚本策略：单个 BEGIN IMMEDIATE 事务内校验版本与影响面。

    请求体中的 blocked_script_pairs 无序且去重（服务端按字典序规范化）。
    事务内先校验 expected_revision：命名空间未配置时当前版本按 0 计，
    首次配置必须传 0；之后传上次 GET/PUT 返回的 revision，不一致 ->
    409 stale_revision（附 current_revision）。版本通过后，用新策略扫描
    该空间全部已注册名称、有效预占与未过期释放冷却（过期记录先惰性清理）：
    任一现存名称命中新策略中的阻断脚本对 -> 409 script_policy_conflict，
    逐条给出记录（kind 区分 registered/reservation/released）、命中的
    脚本对与码点明细，策略不更新。全部通过才写入：首次配置 revision=1，
    之后每次 PUT 成功 revision 递增。配置为空列表表示该空间不再拦截任何
    混脚本组合。
    """
    pairs = [tuple(pair) for pair in payload.blocked_script_pairs]
    try:
        return service.put_script_policy(
            conn, namespace, pairs, payload.expected_revision
        )
    except service.StalePolicyRevisionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "stale_revision",
                "message": (
                    "策略版本已过期（命名空间未配置时当前版本为 0），"
                    "请重新读取最新版本后重试"
                ),
                "current_revision": exc.current_revision,
            },
        )
    except service.ScriptPolicyConflictError as exc:
        if not exc.violations:
            # 锁竞争兜底：回滚后重读版本，提示客户端重试
            row = conn.execute(
                "SELECT revision FROM namespace_script_policies WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "script_policy_conflict",
                    "message": "更新策略时发生并发竞争，事务已回滚，请重新读取最新版本后重试",
                    "current_revision": row["revision"] if row else 0,
                    "violations": [],
                },
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "script_policy_conflict",
                "message": (
                    "新策略会使命名空间内现存的已注册名称、有效预占或释放冷却"
                    "记录违规，策略未更新；请先改名 / 删除名称 / 等待预占或冷却"
                    "到期后重试"
                ),
                "violations": exc.violations,
            },
        )
