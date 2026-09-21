"""候选集原子注册的约束满足模型与字典序最小解搜索。

每个申请项（item）带一个唯一 client_id 与按优先级排列的 1–10 个候选名。
搜索前由服务层（service.allocate_names）在单个 ``BEGIN IMMEDIATE`` 事务内
完成惰性清理、脚本策略复查与逐项逐候选的静态判定，产出本模块使用的
``CandidateSpec``：

- ``valid`` 为 True 时候选通过**静态约束**（命名空间生效脚本策略、规范名
  查重、confusables 同形判定；对手为已注册名称、有效预占与未过期释放冷却）；
- ``valid`` 为 False 时 ``reasons`` 给出逐候选阻断原因
  （``mixed_script`` 附脚本明细；``conflicts`` 附既有占用冲突明细）。

搜索还需满足**项间约束**：同一请求中不同申请项选中的候选，其规范名相等
或 confusables 同形即冲突（与既有名称之间的冲突规则完全一致）。两项之间
至多需要一次选择，故冲突矩阵按 (项, 候选) 二元组预计算，用整数位掩码
（bitset）表示“选中该位置后，其他项哪些位置不可选”。

求解（``solve``）：
- 自第 0 个申请项起按候选序号升序做深度优先回溯，候选按优先级（序号）
  升序试探，因此找到的**第一个完整分配**就是各项候选序号向量的字典序
  最小解；
- 每进入一个搜索节点（一次候选试探）计入一次搜索状态，超过
  ``MAX_SEARCH_STATES``（10000）立即抛 ``AllocationSearchTooLargeError``，
  不做任何写库（由调用方保证搜索全部在写锁事务内、异常时回滚）；
- 使用前向检查剪枝：分配后若某未决定项的全部候选都与已分配位置冲突，
  立即回溯。剪枝只剔除不可行分支，不改变候选试探顺序，故不影响字典序
  最小性。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 申请项 / 候选数量边界（与 schemas 中的 Pydantic 校验保持一致）
MIN_ITEMS = 1
MAX_ITEMS = 20
MIN_CANDIDATES_PER_ITEM = 1
MAX_CANDIDATES_PER_ITEM = 10

# 搜索状态上限：一次分配请求允许访问的搜索节点数；超过即由接口层返回 413
MAX_SEARCH_STATES = 10_000


@dataclass(frozen=True)
class CandidateSpec:
    """单个候选名的静态判定结果（由服务层在写事务内产出）。

    valid=True 表示通过脚本策略、规范名与同形三类静态约束；
    valid=False 时 reasons 按固定结构给出阻断原因：
      - {"reason": "mixed_script", ...脚本分析明细}
      - {"reason": "normalized_match"|"confusable", ...冲突记录}
    """

    item_index: int
    candidate_index: int
    client_id: str
    original_name: str
    normalized_name: str
    valid: bool
    reasons: tuple[dict, ...] = ()


@dataclass
class AllocationModel:
    """一次候选集原子注册请求的约束模型。

    candidates 按申请项顺序分组（每组 1–10 个候选，序号即组内下标）。
    valid_positions 为各项通过静态约束的候选位置位掩码；
    conflict_masks[(i, j)] 为选中 (i, j) 后其他位置的禁用位掩码（仅覆盖
    其他项——同一项的两个位置不可能同时被选）。
    """

    items: list[dict]
    candidates: list[list[CandidateSpec]]
    valid_positions: list[int]
    conflict_masks: dict[tuple[int, int], int] = field(default_factory=dict)
    max_states: int = MAX_SEARCH_STATES
    # solve() 实际访问的搜索节点数（含首个越界节点），供接口层回显
    visited_states: int = 0

    def solve(self) -> list[int] | None:
        """求字典序最小的完整分配（每项选中的候选序号）；无解返回 None。

        访问搜索节点数超过 self.max_states 时抛
        AllocationSearchTooLargeError（携带已访问状态数，未做任何写库）。
        """
        assignment: list[int] = []
        blocked = 0  # 已分配位置禁用的全部位掩码（全局，位置 j 位于 item*width+j）
        width = MAX_CANDIDATES_PER_ITEM

        def dfs(item_index: int) -> bool:
            nonlocal blocked
            if item_index == len(self.items):
                return True
            candidates = self.candidates[item_index]
            # blocked 为全局位掩码（位置 j 位于 item*width+j），比较前下平移
            item_blocked = (blocked >> (item_index * width)) & (
                (1 << width) - 1
            )
            allowed = self.valid_positions[item_index] & ~item_blocked
            for candidate_index in range(len(candidates)):
                bit = 1 << candidate_index
                if not (allowed & bit):
                    continue
                self.visited_states += 1
                if self.visited_states > self.max_states:
                    raise AllocationSearchTooLargeError(
                        self.visited_states, self.max_states
                    )
                added = self.conflict_masks[(item_index, candidate_index)]
                blocked |= added
                assignment.append(candidate_index)
                # 前向检查：后续每个未决定项至少须剩一个可用候选，
                # 否则该试探不可能扩展为完整解，立即回溯
                if (
                    self._all_future_items_have_candidate(item_index + 1, blocked)
                    and dfs(item_index + 1)
                ):
                    return True
                assignment.pop()
                blocked &= ~added
            return False

        return list(assignment) if dfs(0) else None

    def _all_future_items_have_candidate(
        self, start_item: int, blocked: int
    ) -> bool:
        """前向检查：自 start_item 起每项至少有一个未被禁用的静态有效候选。"""
        width = MAX_CANDIDATES_PER_ITEM
        for item_index in range(start_item, len(self.items)):
            offset = item_index * width
            item_blocked = (blocked >> offset) & ((1 << width) - 1)
            if not (self.valid_positions[item_index] & ~item_blocked):
                return False
        return True


class AllocationSearchTooLargeError(Exception):
    """搜索访问状态数超过上限（HTTP 413），不得写库。

    searched_states 为中止时已访问的搜索节点数（首个越界节点也计入），
    limit 为请求允许的搜索状态上限（10000）。
    """

    def __init__(self, searched_states: int, limit: int):
        super().__init__(
            f"allocation search states {searched_states} exceed limit {limit}"
        )
        self.searched_states = searched_states
        self.limit = limit
