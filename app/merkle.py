"""规范 JSON 编码与 SHA-256 Merkle 树（含包含证明）。

节点快照证明（POST /namespaces/{namespace}/snapshot-proof）使用的哈希约定：

- 每个叶节点先把业务字段编码为**规范 JSON**（键按字典序、无多余空白、
  原样保留非 ASCII 字符，UTF-8 编码），再计算
  ``leaf_hash = SHA-256(0x00 || canonical_json_bytes)``；
- 父节点 ``parent_hash = SHA-256(0x01 || left_hash || right_hash)``；
- 每层节点数为奇数时，复制本层最后一个节点与自身配对；
- 整棵树只有一个叶子时，根即该叶哈希；空树的根定义为 32 个零字节
  （快照证明路径上恒有脚本策略叶，实际不会出现空树）。

包含证明（inclusion proof）以**兄弟哈希 + 方向**给出：从目标叶逐层上行，
``direction`` 表示兄弟节点位于当前节点的左侧还是右侧——
``"left"`` 时 parent = SHA-256(0x01 || sibling || current)，
``"right"`` 时 parent = SHA-256(0x01 || current || sibling)。
"""
from __future__ import annotations

import hashlib
import json

# 叶 / 父节点域分隔前缀，防止第二原像攻击（叶哈希与内部节点哈希不互通）
LEAF_PREFIX = b"\x00"
PARENT_PREFIX = b"\x01"

# 空树根：定义为 32 个零字节（调用方一般不会对空快照求根）
EMPTY_ROOT = b"\x00" * hashlib.sha256().digest_size


def canonical_json_bytes(payload: dict) -> bytes:
    """把业务字段编码为规范 JSON 字节串。

    - sort_keys=True：键按字典序排列，与插入顺序无关；
    - separators 压缩全部多余空白；
    - ensure_ascii=False：非 ASCII 字符（如西里尔 / 汉字）以原样 UTF-8 编码，
      避免 \\uXXXX 转义形式差异。
    """
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def leaf_hash(payload: dict) -> bytes:
    """叶哈希：SHA-256(0x00 || 规范 JSON 字节)。"""
    return hashlib.sha256(LEAF_PREFIX + canonical_json_bytes(payload)).digest()


def parent_hash(left: bytes, right: bytes) -> bytes:
    """父哈希：SHA-256(0x01 || left || right)。"""
    return hashlib.sha256(PARENT_PREFIX + left + right).digest()


def _promote(level: list[bytes]) -> list[bytes]:
    """单层上卷：奇数个节点时复制末节点，再两两配对求父哈希。"""
    if len(level) % 2 == 1:
        level = [*level, level[-1]]
    return [
        parent_hash(level[pos], level[pos + 1])
        for pos in range(0, len(level), 2)
    ]


def merkle_root(hashes: list[bytes]) -> bytes:
    """由叶哈希列表求 Merkle 根；空列表返回 EMPTY_ROOT。"""
    if not hashes:
        return EMPTY_ROOT
    level = list(hashes)
    while len(level) > 1:
        level = _promote(level)
    return level[0]


def inclusion_proof(hashes: list[bytes], index: int) -> list[dict]:
    """求 ``hashes[index]`` 的包含证明：自叶向上的兄弟哈希与方向。

    返回 [{"direction": "left"|"right", "hash": <hex>}, ...]，顺序即
    验证时从叶哈希逐层重放的顺序。单叶树返回空列表（叶哈希即根）。
    """
    if not 0 <= index < len(hashes):
        raise IndexError("leaf index out of range")
    level = list(hashes)
    steps: list[dict] = []
    while len(level) > 1:
        if len(level) % 2 == 1:
            # 奇数层：末节点与自身的复制配对（复制体在右侧）
            level = [*level, level[-1]]
        sibling_index = index ^ 1
        if index & 1:
            # 当前节点是右孩子：兄弟在左
            steps.append({"direction": "left", "hash": level[sibling_index].hex()})
        else:
            # 当前节点是左孩子（含末节点被复制的情形）：兄弟在右
            steps.append({"direction": "right", "hash": level[sibling_index].hex()})
        level = _promote_from_pairs(level)
        index >>= 1
    return steps


def _promote_from_pairs(level: list[bytes]) -> list[bytes]:
    """与 _promote 相同，但输入已保证偶数长度（含奇数层时追加的复制）。"""
    return [
        parent_hash(level[pos], level[pos + 1])
        for pos in range(0, len(level), 2)
    ]
