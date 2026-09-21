# Unicode 显示名防仿冒 API

命名空间（namespace）内的显示名注册服务，基于 **FastAPI + SQLite + confusables + unicodedataplus**，在注册时拦截三类仿冒：

| 判定 | 规则 | 结果 |
| --- | --- | --- |
| 规范名查重 | NFKC + casefold 后与同命名空间已有规范名比较 | 相同 → `409` |
| 同形字符 | 与已有名称**逐条**做 Unicode confusables 判定 | 可混淆 → `409` |
| 混脚本 | 按**所属命名空间策略** `blocked_script_pairs` 判定：名称同时出现某一阻断脚本对中的两个脚本即违规；未配置策略时默认 Latin、Cyrillic、Greek 任意两种混用即违规 | `422`，并列出脚本、命中的脚本对及码点 |

原名始终原样落库，规范名（`normalized_name`）一并保存；`(namespace, normalized_name)` 在数据库层有唯一约束兜底并发写入。每条记录带单调递增的 `revision`（初始为 1），作为乐观锁版本号。

还支持**命名空间级混脚本策略**：`PUT /namespaces/{namespace}/script-policy` 接收**无序且去重**的 `blocked_script_pairs`（每对为两个不同的合法 Unicode 脚本名，空列表表示不拦截任何组合）与 `expected_revision`，在**单个 `BEGIN IMMEDIATE` 事务**中校验版本（未配置时当前版本按 `0` 计，首次配置必须传 `0`，成功后 `revision=1`，之后每次成功 PUT 递增），并用新策略扫描该空间**全部已注册名称、有效预占与未过期释放冷却**（先惰性清理过期记录）；若新策略会使现存名称违规，返回 `409 script_policy_conflict` 并逐条给出违规记录（`kind` 区分 `registered` / `reservation` / `released`，后两者附 `expires_at`）、命中的脚本对与码点明细，**策略不更新**；全部通过才写入。`GET /namespaces/{namespace}/script-policy` 读取已配置策略（未配置返回 `404`，但写入与 `check` 仍按内建默认策略判定）。注册、批量注册、预占、改名与 `check` 均按**所属命名空间**的生效策略判定，写事务内会重新加载策略复查，使并发的策略修改与名称写入互不破坏；合并时以**目标命名空间**的策略检查源、目标两侧数据。


此外支持**原子改名**：`PATCH /namespaces/{namespace}/names/{id}` 凭上次读取到的 `expected_revision` 修改已注册名称。改名在单个 `BEGIN IMMEDIATE` 写锁事务中完成版本校验与冲突复查（混脚本 / 规范名 / 同形，复查时排除当前记录自身，但仍包含全部有效预占与未过期释放冷却）；成功后保留 `id` / `created_at`、更新原名与规范名并把 `revision + 1`，版本过期返回 `409 stale_revision`。

还支持**原子交换**：`POST /namespaces/{namespace}/names/swap` 在单个 `BEGIN IMMEDIATE` 写锁事务中校验同命名空间内**两条不同**记录的归属与双方 `expected_revision`，通过后把双方的 `original_name` 与 `normalized_name` **成对互换**、双方 `revision` 各 + 1，`id` / `created_at` 不变。任一记录不存在（或不属于该命名空间）返回 `404`；任一方版本过期返回 `409 stale_revision` 并同时给出双方当前 revision；任何失败都整体回滚，不存在部分更新。

还支持**预占后确认注册**：先 `POST .../reservations` 拿到不可猜测的 token 与过期时间，在 TTL（30–600 秒）内调用 `.../reservations/{token}/claim` 确认。**未过期且未消费的预占与已注册名称完全等价地参与冲突判定**（单条注册、批量注册、新预占、改名、`check` 均会命中），确认成功后才真正写入 `display_names`；过期记录在后续写入时惰性清理。

还支持**名称释放冷却**：`DELETE /namespaces/{namespace}/names/{id}` 接收 `expected_revision` 与 `cooldown_seconds`（60–86400 秒），在**单个 `BEGIN IMMEDIATE` 事务**内校验归属与版本，通过后删除已注册名称并写入一条带 `expires_at` 的冷却记录（`200` 返回该记录）。**未过期冷却记录与已注册名称、有效预占完全等价地参与全部判定**：单条 / 批量注册、预占、确认（claim）、改名、`check` 的规范名 / 同形查重，命名空间合并的跨空间复查与整体迁移，以及脚本策略 PUT 的混脚本影响面扫描；冲突明细以 `source="released"`（合并 / 策略明细中为 `kind="released"`）标识并附 `expires_at`。记录不存在 / 不属于该命名空间 -> `404`，版本过期 -> `409 stale_revision`。过期冷却不参与判定，在后续写事务开始时惰性物理删除；`GET /namespaces/{namespace}/cooldowns` 列出指定命名空间的有效冷却记录（只读，不清理）。

还支持**命名空间合并**：`POST /namespaces/{source}/merge` 把源命名空间的全部已注册名称、有效预占与释放冷却记录整体迁入目标命名空间。合并前在写锁事务中对**两侧**的已注册名称、有效预占与未过期冷却做跨空间规范名 / 同形复查（过期记录先惰性清理），并以**目标命名空间的混脚本策略**扫描源、目标两侧数据；冲突或脚本违规任一存在即返回 `409`（分别附 `conflicts` 双方记录与 `script_violations` 逐条明细）且不修改任何数据；无冲突且两侧数据均合规才在同一事务内完成迁移并返回迁移数量（含 `merged_cooldowns`）。

还支持**跨空间同形风险簇**：`POST /name-risk-clusters` 接收 2–20 个互不相同的命名空间与 `max_names`（1–500），在**同一个 SQLite 读事务**中固定这些空间的**已注册名称、有效预占与未过期释放冷却**快照（只读、不清理过期记录），随后**只对不同命名空间之间**的节点按规范名相同（`normalized_match`）或 confusables 同形（`confusable`）建边，再以并查集求连通分量、返回至少含 2 个节点的风险簇——因此 A-B、B-C 即使 A-C 并不直接冲突也会归入同一簇。节点以 `namespace` / `kind`（`registered` / `reservation` / `released`）/ `id` 稳定标识，预占与冷却节点附 `expires_at`；边列出排序后的两端节点标识与 `reasons`；簇、节点、边全部确定性排序（`cluster_id` 从 1 开始）。快照节点总量超过 `max_names` 时返回 `413 risk_snapshot_too_large`（附 `snapshot_count` / `max_names`），**不截断快照、不做分簇计算**。

还支持**单命名空间快照 Merkle 包含证明**：`POST /namespaces/{namespace}/snapshot-proof` 接收 1–20 个由 `kind`（`registered` / `reservation` / `released`）与 `id` 表示的节点及 `max_nodes`（1–500），在**单个 SQLite 读事务**中固定该空间的**生效脚本策略、已注册名称、有效预占与未过期释放冷却**。叶集合为 1 个恒存在的 `script_policy` 叶（排序后下标恒为 0；命名空间未配置策略时固定内建默认策略、`revision=0`）加全部业务叶，按 **（类型序, id）** 排序（`script_policy < registered < reservation < released`），业务字段编码为**规范 JSON**（键按字典序、无多余空白、非 ASCII 原样 UTF-8），以 SHA-256 构建 Merkle 树：叶 `H(0x00‖json)`、父 `H(0x01‖left‖right)`、每层奇数个节点时**复制末节点**与自身配对。返回 `root`、`leaf_count`（业务节点数 + 1 个策略叶）、`snapshot_count`、生效策略回显及每个请求节点的叶载荷 / 叶下标 / **带左右方向的包含证明**（`direction` 为兄弟方向，验证时 `left` 取 `H(0x01‖sibling‖current)`、`right` 取 `H(0x01‖current‖sibling)`）。**证明只由节点排序后的位置决定：相同快照下请求节点顺序变化不改变根，也不改变各节点自身的证明**，`items` 仅按请求顺序回显。请求节点在快照中不存在（不存在 / 预占或冷却已过期 / 预占已消费 / 属于其他命名空间）-> `404 snapshot_nodes_not_found`（附全部缺失 `kind/id`）；业务快照总量超过 `max_nodes` -> `413 snapshot_too_large`，**不截断、不建树**。

## 快速开始（Docker，单条命令）

```bash
docker compose up --build
```

该命令完成镜像构建、依赖安装、**版本化数据库迁移**与 API 启动：容器启动时先执行
`python -m app.database migrate`（按递增版本逐个原子提交，多实例并发启动安全；
首次接管老版本数据库自动建立基线，详见下文「数据库迁移」），迁移成功后才启动
uvicorn；迁移校验失败（迁移文件被改写 / 版本缺口）时容器以非零码退出，API 不会
带病启动。SQLite 文件落在命名卷 `names-data` 中。容器启动后即可直接使用全部接口
（含 `PUT/GET /namespaces/{namespace}/script-policy`、`POST /namespaces/{source}/merge`、
`DELETE /namespaces/{namespace}/names/{id}` 释放冷却、`POST /name-risk-clusters`
跨空间同形风险簇与 `POST /namespaces/{namespace}/snapshot-proof` 快照 Merkle 包含证明）。

- **API 地址：http://localhost:8000**
- **交互式文档（Swagger UI）：http://localhost:8000/docs**
- **端口：8000**（可在 `docker-compose.yml` 的 `ports` 中调整）

## 数据库迁移

表结构变更全部以**版本化迁移**管理：每个迁移有递增版本号、名称与自身 SQL 的
SHA-256 校验和，应用历史记录在 `schema_migrations` 表（version / name /
checksum / applied_at）。每次启动对每个待应用迁移单独开启 `BEGIN IMMEDIATE`
事务，持写锁后重新读取已记录版本再执行——两个进程并发启动时同一迁移只执行
一次，且每个迁移要么整体提交要么整体回滚。

```bash
python -m app.database migrate   # 执行全部待应用迁移（幂等）
python -m app.database status    # 只读输出已应用 / 待应用 / 校验异常（JSON）
```

- **首次接管既有数据库**：`schema_migrations` 为空但业务表已存在时，按当前
  表 / 列状态识别各迁移效果（如 `display_names` 是否已有 `revision` 列），
  把已生效的迁移补记为基线，随后只执行缺失的迁移；空库则从头执行全部迁移，
  两条路径最终结构一致。
- **失败即拒启**：已记录迁移对应的迁移文件被改写（校验和不匹配）、记录了
  未知版本或版本历史出现缺口时，`migrate` 以非零码退出并指明具体版本号，
  应用启动失败、不继续服务。`status` 为纯只读（不建表、不建基线、不执行
  迁移），以 JSON 返回 `applied` / `pending` / `issues`（`checksum_mismatch`、
  `unknown_version`、`version_gap`），可随时安全执行。

## 本地开发（单条命令）

```bash
./scripts/setup.sh    # 安装依赖 + 执行数据库迁移（./names.db）
uvicorn app.main:app --reload --port 8000
```

`setup.sh` 内部执行 `pip install -r requirements.txt` 与
`python -m app.database migrate`；如需虚拟环境，先自行激活再运行该脚本即可。
应用启动时（lifespan）也会再执行一次同样的迁移，幂等无负担。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/namespaces/{namespace}/names` | 注册显示名：`201` 成功；`409` 规范名相同或同形冲突（含有效预占）；`422` 混脚本违规 |
| `PATCH` | `/namespaces/{namespace}/names/{id}` | **原子改名**：`{"name": ..., "expected_revision": ...}`，单个写锁事务内校验版本、混脚本与冲突（排除当前记录，含有效预占与释放冷却）→ 成功 `200`（保留 `id`/`created_at`，`revision + 1`）；记录不存在/不属于该命名空间 → `404`；版本过期 → `409 stale_revision`；名称冲突 → `409`（沿用冲突明细）；混脚本 → `422` |
| `DELETE` | `/namespaces/{namespace}/names/{id}` | **释放名称并设置冷却**：`{"expected_revision": ..., "cooldown_seconds": 60–86400}`，单个写锁事务内校验归属与版本后删除名称、写入带 `expires_at` 的冷却记录 → 成功 `200`（返回冷却记录）；记录不存在/不属于该命名空间 → `404`；版本过期 → `409 stale_revision`；`cooldown_seconds` 越界 → `422` |
| `GET` | `/namespaces/{namespace}/cooldowns` | 列出命名空间内仍有效的**释放冷却记录**（`id` / 原名 / 规范名 / `cooldown_seconds` / `expires_at`，按 id 升序）；过期记录不出现（只读不清理），命名空间无记录时返回 `[]` |
| `POST` | `/namespaces/{namespace}/names/batch` | 批量注册（1–50 个）：全部通过才在**单个事务**中落库 → `201`；任一项混脚本或与库内/有效预占/释放冷却/批内名称冲突 → `422` 汇总明细；并发竞争使判定失效 → 回滚重新分析 → `409` |
| `POST` | `/namespaces/{namespace}/names/swap` | **原子交换**：`{"records":[{"id":...,"expected_revision":...},{...}]}`（恰好两条不同记录），单个写锁事务内校验双方归属与版本 → 成功 `200`（成对互换原名/规范名，双方 `revision + 1`，`id`/`created_at` 不变）；任一记录不存在/不属于该命名空间 → `404`（附 `missing_ids`）；任一方版本过期 → `409 stale_revision`（附双方当前 revision） |
| `POST` | `/namespaces/{namespace}/reservations` | **预占**名称：`{"name": ..., "ttl_seconds": 30–600}` → `201` 返回不可猜测 `token` 与 `expires_at`；冲突 → `409`；混脚本 / TTL 越界 → `422` |
| `POST` | `/namespaces/{namespace}/reservations/{token}/claim` | **确认预占**：单个 `BEGIN IMMEDIATE` 事务内校验命名空间（`404`）、有效期（过期 `410`）、消费状态（重复/并发确认 `409`）与当前冲突（`409`，不写入），通过后注册并标记已消费 → `201` |
| `POST` | `/namespaces/{source}/merge` | **合并命名空间**：`{"target_namespace": ...}`，写锁事务内复查源与目标两侧的已注册名称、有效预占与释放冷却，并以**目标空间策略**检查两侧混脚本 → 有冲突或违规 `409`（`conflicts` 附双方记录、`script_violations` 附逐条明细，不改数据）；无冲突且两侧合规则同事务迁移并返回迁移数量 → `200`；目标与源相同 → `422` |
| `PUT` | `/namespaces/{namespace}/script-policy` | **配置混脚本策略**：`{"blocked_script_pairs": [["Latin","Cyrillic"], ...], "expected_revision": ...}`（脚本对无序、自动去重，空列表表示全部放行；未配置时当前版本为 0），单个写锁事务内校验版本并扫描该空间已注册名称、有效预占与释放冷却 → 成功 `200`（首次 `revision=1`，之后递增）；版本过期 → `409 stale_revision`（附 `current_revision`）；新策略会使现存名称违规 → `409 script_policy_conflict`（附逐条违规明细，不更新）；脚本名未知 / 同名对 / 项长度不为 2 / 超过 500 对 → `422` |
| `GET` | `/namespaces/{namespace}/script-policy` | 读取已配置的混脚本策略 → `200`；命名空间尚未配置 → `404`（写入与 `check` 此时仍按内建默认策略判定） |
| `POST` | `/namespaces/{namespace}/check` | **只分析不落库**：返回规范名、脚本分析、码点、生效策略（`blocked_script_pairs`）、命中的脚本对（`violating_script_pairs`）与潜在冲突（含有效预占与释放冷却） |
| `POST` | `/name-risk-clusters` | **跨空间同形风险簇**：`{"namespaces": ["a","b", ...], "max_names": ≤500}`（2–20 个互不相同的命名空间），单个 SQLite 读事务固定各空间已注册名称 / 有效预占 / 未过期冷却快照，只对跨空间节点按规范名相同或同形建边，按连通分量返回至少含 2 个节点的簇（A-B、B-C 即使 A-C 不冲突也同簇）→ `200`；快照总量超过 `max_names` → `413`（不截断计算）；命名空间数量 / 重复 / `max_names` 越界 → `422` |
| `POST` | `/namespaces/{namespace}/snapshot-proof` | **快照 Merkle 包含证明**：`{"nodes": [{"kind": "registered"|"reservation"|"released", "id": ...}, ...], "max_nodes": ≤500}`（1–20 个互不相同的 kind/id），单个 SQLite 读事务固定生效脚本策略 + 已注册名称 + 有效预占 + 未过期冷却，按（类型序, id）排序后以规范 JSON + SHA-256 建树（叶 `0x00`、父 `0x01`、奇数层复制末节点）→ `200` 返回 `root` / `leaf_count` 与各节点带左右方向的包含证明（与请求顺序无关）；节点不在快照中 → `404`；业务快照超过 `max_nodes` → `413`（不截断、不建树）；nodes 数量 / 重复 / `max_nodes` 越界 → `422` |
| `GET` | `/namespaces/{namespace}/names` | 列出命名空间内已注册名称（预占未确认前不出现在此） |
| `GET` | `/health` | 健康检查 |

单条接口请求体为 `{"name": "<显示名>"}`（1–64 字符，非空白）；改名接口为 `{"name": "<新显示名>", "expected_revision": <上次读取的 revision，≥1 的整数>}`；删除（释放）接口为 `{"expected_revision": <≥1 的整数>, "cooldown_seconds": <60–86400 的整数>}`；交换接口为 `{"records": [{"id": <记录 id>, "expected_revision": <≥1 的整数>}, ...]}`（**恰好两条，且 id 不能相同**，响应 `items` 按请求顺序返回）；批量接口为 `{"names": ["<显示名>", ...]}`（1–50 个）；预占接口为 `{"name": "<显示名>", "ttl_seconds": 30–600}`；合并接口为 `{"target_namespace": "<目标命名空间>"}`（1–64 字符，非空白，且不得与源相同）；风险簇接口为 `{"namespaces": ["<命名空间>", ...], "max_names": <1–500 的整数>}`（**恰好 2–20 个且互不相同**，响应与输入顺序无关——`namespaces` 回显为按字典序规范化后的列表）；快照证明接口为 `{"nodes": [{"kind": "registered"|"reservation"|"released", "id": <≥1 的整数>}, ...], "max_nodes": <1–500 的整数>}`（**1–20 个且 `(kind, id)` 互不相同**；`registered` 与 `reservation` 的同 id 是不同节点，不视为重复；`items` 按请求顺序回显，但根与各节点证明与请求顺序无关）。

策略接口请求体为 `{"blocked_script_pairs": [["<脚本A>", "<脚本B>"], ...], "expected_revision": <≥0 的整数>}`：脚本名大小写敏感、取 Unicode `Script` 属性值（如 `Latin` / `Cyrillic` / `Greek` / `Han` / `Arabic`，不接受 `Common` / `Inherited` / `Unknown`）；**每一对内部顺序无关、整体自动去重**（`["Cyrillic","Latin"]` 与 `["Latin","Cyrillic"]` 等价），响应中统一按字典序排列返回；至多 500 对。命名空间**首次**配置必须传 `expected_revision: 0`，之后传上次 GET/PUT 返回的 `revision`。

混脚本错误明细（`422`）与 `check` 响应统一新增：`blocked_script_pairs`（本次判定生效的策略对；未配置时为内建默认三对）与 `violating_script_pairs`（名称实际命中的阻断脚本对，非空即违规）；历史字段 `restricted_scripts_used`（出现的 Latin/Cyrillic/Greek）保持不变。

冲突明细中的 `source` 字段区分冲突来源：`registered`（已注册名称，默认值，老客户端无需改动）、`reservation`（有效预占，附带该预占的 `expires_at`）或 `released`（未过期释放冷却记录，附带冷却到期时刻 `expires_at`）。命名空间合并的跨空间明细与脚本策略扫描明细则用 `kind` 字段区分同一组三类来源。

### 示例

注册成功：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names \
  -H 'Content-Type: application/json' -d '{"name": "paypal"}'
# 201 {"id":1,"namespace":"chat","original_name":"paypal","normalized_name":"paypal",...}
```

规范名相同（大小写 / 全半角等兼容字符折叠后重复）→ `409`：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names \
  -H 'Content-Type: application/json' -d '{"name": "PAYPAL"}'
# 409 {"detail":{"error":"name_conflict","normalized_name":"paypal",
#      "conflicts":[{"id":1,"original_name":"paypal","reason":"normalized_match",...}]}}
```

同形字符（纯西里尔 `арр` 仿冒拉丁 `app`）→ `409`：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names \
  -H 'Content-Type: application/json' -d '{"name": "арр"}'
# 409 {"detail":{"error":"name_conflict",
#      "conflicts":[{"original_name":"app","reason":"confusable",...}]}}
```

混脚本（拉丁 `ad` + 西里尔 `м` + 拉丁 `in`）→ `422`，列出脚本及码点：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names \
  -H 'Content-Type: application/json' -d '{"name": "adмin"}'
# 422 {"detail":{"error":"mixed_script_violation",
#      "blocked_script_pairs":[["Cyrillic","Greek"],["Cyrillic","Latin"],["Greek","Latin"]],
#      "violating_script_pairs":[["Cyrillic","Latin"]],
#      "restricted_scripts_used":["Cyrillic","Latin"],
#      "script_details":[{"script":"Cyrillic","characters":["м"],"code_points":["U+043C"]},
#                        {"script":"Latin","characters":["a","d","i","n"],
#                         "code_points":["U+0061","U+0064","U+0069","U+006E"]}]}}
```

原子改名：带上次读取到的 `revision` 作为 `expected_revision`，成功后保留
`id` / `created_at`，更新原名与规范名，`revision` 递增：

```bash
curl -X PATCH http://localhost:8000/namespaces/chat/names/1 \
  -H 'Content-Type: application/json' \
  -d '{"name": "payfriend", "expected_revision": 1}'
# 200 {"id":1,"namespace":"chat","original_name":"payfriend",
#      "normalized_name":"payfriend","revision":2,"created_at":"..."}
```

改名的错误语义（全部在单个 `BEGIN IMMEDIATE` 事务内判定，失败不改数据）：

```bash
# expected_revision 已过期（记录已被其他请求改名）-> 409，附当前版本供重读后重试
# 409 {"detail":{"error":"stale_revision",
#      "message":"名称记录已被其他请求修改，请重新读取最新版本后重试",
#      "current_revision":2}}

# 新名称与同空间其他已注册名称（当前记录除外）或有效预占规范名相同/同形
# -> 409，沿用注册接口的冲突明细（source/reason/conflicts/expires_at）
# 409 {"detail":{"error":"name_conflict","normalized_name":"paypal",
#      "conflicts":[{"original_name":"paypal","reason":"normalized_match",...}]}}

# 新名称混脚本 -> 422（结构与注册接口的 mixed_script_violation 相同）
# 记录 id 不存在，或 id 属于其他命名空间 -> 404
# 404 {"detail":{"error":"name_not_found","message":"已注册名称不存在，或不属于该命名空间"}}
```

删除（释放）名称并设置冷却：删除与冷却写入在同一个 `BEGIN IMMEDIATE`
事务中完成，成功返回带 `expires_at` 的冷却记录，原名称立即从已注册列表消失：

```bash
curl -X DELETE http://localhost:8000/namespaces/chat/names/1 \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 2, "cooldown_seconds": 3600}'
# 200 {"id":1,"namespace":"chat","original_name":"payfriend",
#      "normalized_name":"payfriend","cooldown_seconds":3600,
#      "expires_at":"2026-09-20T13:00:00.000Z","created_at":"..."}
```

冷却到期前，规范名相同或同形的名称在**所有**入口都会冲突（注意新的
`source` 字段；冷却同样阻止预占、确认、改名、批量注册与跨空间合并）：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names \
  -H 'Content-Type: application/json' -d '{"name": "PAYFRIEND"}'
# 409 {"detail":{"error":"name_conflict","normalized_name":"payfriend",
#      "conflicts":[{"id":1,"original_name":"payfriend","reason":"normalized_match",
#                    "source":"released",
#                    "expires_at":"2026-09-20T13:00:00.000Z"}]}}
```

查看命名空间内仍有效的冷却记录（过期记录不出现；只读接口不做物理清理）：

```bash
curl http://localhost:8000/namespaces/chat/cooldowns
# 200 [{"id":1,"namespace":"chat","original_name":"payfriend",
#       "normalized_name":"payfriend","cooldown_seconds":3600,
#       "expires_at":"2026-09-20T13:00:00.000Z","created_at":"..."}]
```

释放接口的错误语义（全部在单个 `BEGIN IMMEDIATE` 事务内判定，失败不产生
“名称已删除但无冷却”的部分更新）：

```bash
# 记录 id 不存在，或属于其他命名空间 -> 404
# 404 {"detail":{"error":"name_not_found","message":"已注册名称不存在，或不属于该命名空间"}}

# expected_revision 已过期 -> 409，附当前版本供重读后重试
# 409 {"detail":{"error":"stale_revision",
#      "message":"名称记录已被其他请求修改，请重新读取最新版本后重试",
#      "current_revision":3}}

# cooldown_seconds 超出 60–86400 范围，或 expected_revision < 1 -> 422
```

冷却到期后记录在**下一次写操作**（注册 / 批量 / 预占 / 确认 / 改名 / 交换 /
释放 / 合并 / 策略 PUT）开始时被惰性物理删除，此后名称可被重新注册；只读的
`check` 与冷却列表接口会过滤过期记录但不删除它们。

原子交换：在同一个 `BEGIN IMMEDIATE` 写锁事务中校验双方归属与版本，把两条记录的
原名 / 规范名**成对互换**，双方 `revision` 各 + 1，`id` / `created_at` 保持不变，
响应 `items` 按请求顺序返回：

```bash
# 假设记录 1 为 "alice"（revision 1），记录 2 为 "bob"（revision 1）
curl -X POST http://localhost:8000/namespaces/chat/names/swap \
  -H 'Content-Type: application/json' \
  -d '{"records": [{"id": 1, "expected_revision": 1},
                   {"id": 2, "expected_revision": 1}]}'
# 200 {"namespace":"chat","items":[
#   {"index":0,"id":1,"expected_revision":1,"namespace":"chat",
#    "original_name":"bob","normalized_name":"bob","revision":2,"created_at":"..."},
#   {"index":1,"id":2,"expected_revision":1,"namespace":"chat",
#    "original_name":"alice","normalized_name":"alice","revision":2,"created_at":"..."}]}
```

交换的错误语义（全部在单个 `BEGIN IMMEDIATE` 事务内判定，失败不产生部分更新）：

```bash
# 任一记录不存在，或属于其他命名空间 -> 404，missing_ids 列出全部缺失记录
# 404 {"detail":{"error":"name_not_found",
#      "message":"待交换的名称记录不存在，或不属于该命名空间","missing_ids":[999]}}

# 任一方 expected_revision 已过期 -> 409，records 同时给出双方当前 revision，
# 客户端重读后可用最新版本整体重试
# 409 {"detail":{"error":"stale_revision",
#      "message":"名称记录已被其他请求修改（双方中至少一方版本过期），请重新读取双方最新版本后重试",
#      "records":[{"id":1,"expected_revision":1,"current_revision":2},
#                 {"id":2,"expected_revision":1,"current_revision":1}]}}

# records 不是恰好两条，或两条记录 id 相同 -> 422
```

> 实现说明：`(namespace, normalized_name)` 是 SQLite **即时**唯一约束，交换的两个
> 规范名都被对方占着，无法直接互改。事务内会先生成一个随机临时名（仅存在于未提交
> 事务中）腾出槽位，再三步条件 `UPDATE` 成对互换。`BEGIN IMMEDIATE` 的库级写锁 +
> SQLite 快照隔离保证：并发请求在提交前只能读到交换前快照，临时名不会被观察到，
> 交换也不会与并发注册 / 改名相互破坏。

只分析不落库：

```bash
curl -X POST http://localhost:8000/namespaces/chat/check \
  -H 'Content-Type: application/json' -d '{"name": "арр"}'
# 200 {"normalized_name":"арр","mixed_script_violation":false,
#      "scripts":["Cyrillic"],"conflicts":[{"reason":"confusable",...}], ...}
```

批量注册：全部通过 → `201`，`items` 按原请求顺序返回并带 `index`：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names/batch \
  -H 'Content-Type: application/json' \
  -d '{"names": ["alice", "bob", "世界-123"]}'
# 201 {"namespace":"chat","items":[
#   {"index":0,"id":1,"original_name":"alice","normalized_name":"alice",...},
#   {"index":1,"id":2,"original_name":"bob","normalized_name":"bob",...},
#   {"index":2,"id":3,"original_name":"世界-123","normalized_name":"世界-123",...}]}
```

批量注册：任一项有问题 → `422` 汇总所有问题项（库内冲突 `conflicts`、
批内冲突 `batch_conflicts` 按原请求索引互相标注、混脚本明细），**一条都不落库**：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names/batch \
  -H 'Content-Type: application/json' \
  -d '{"names": ["app", "арр", "adмin"]}'
# 422 {"detail":{"error":"batch_validation_failed",
#      "message":"批次中 3/3 个名称存在混脚本或冲突问题，未写入任何记录",
#      "items":[
#        {"index":0,"original_name":"app",...,
#         "batch_conflicts":[{"index":1,"original_name":"арр","reason":"confusable",...}]},
#        {"index":1,"original_name":"арр",...,
#         "batch_conflicts":[{"index":0,"original_name":"app","reason":"confusable",...}]},
#        {"index":2,"original_name":"adмin","mixed_script_violation":true,
#         "restricted_scripts_used":["Cyrillic","Latin"],...}]}}
```

批量注册：并发竞争（另一请求在批次事务提交前抢先写入冲突名称）→ 整批回滚、
重新分析后返回 `409`：

```bash
# 409 {"detail":{"error":"name_conflict",
#      "message":"并发写入使批次判定失效，事务已回滚；以下为重新分析后的冲突明细",
#      "items":[{"index":0,"original_name":"alice",...,
#                "conflicts":[{"original_name":"Alice","reason":"normalized_match",...}]}]}}
```

预占名称：返回不可猜测的 `token`（`secrets.token_urlsafe(32)`，43 字符）与 UTC
`expires_at`；预占期间名称**尚未**出现在已注册列表中，但已开始占位：

```bash
curl -X POST http://localhost:8000/namespaces/chat/reservations \
  -H 'Content-Type: application/json' \
  -d '{"name": "paypal", "ttl_seconds": 120}'
# 201 {"id":1,"namespace":"chat","original_name":"paypal","normalized_name":"paypal",
#      "token":"k8m0...（43 字符随机串）","ttl_seconds":120,
#      "expires_at":"2026-09-19T12:02:00.000Z","created_at":"..."}
```

预占期内，规范名相同或同形的名称在**所有**入口都会冲突（注意 `source` 字段）：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names \
  -H 'Content-Type: application/json' -d '{"name": "PAYPAL"}'
# 409 {"detail":{"error":"name_conflict","normalized_name":"paypal",
#      "conflicts":[{"id":1,"original_name":"paypal","reason":"normalized_match",
#                    "source":"reservation","expires_at":"2026-09-19T12:02:00.000Z"}]}}

curl -X POST http://localhost:8000/namespaces/chat/reservations \
  -H 'Content-Type: application/json' -d '{"name": "раypal", "ttl_seconds": 60}'
# 409 同形判定同样命中（source: "reservation", reason: "confusable"）
```

TTL 越界（<30 或 >600）或混脚本 → `422`：

```bash
curl -X POST http://localhost:8000/namespaces/chat/reservations \
  -H 'Content-Type: application/json' -d '{"name": "paypal", "ttl_seconds": 601}'
# 422 {"detail":[{"type":"less_than_equal",...,"ctx":{"le":600},...}]}
```

确认预占：在单个 `BEGIN IMMEDIATE` 事务中完成全部校验与写入，成功返回已注册
名称记录及消费信息：

```bash
TOKEN='k8m0...'   # 上一步返回的 token
curl -X POST "http://localhost:8000/namespaces/chat/reservations/${TOKEN}/claim"
# 201 {"id":2,"namespace":"chat","original_name":"paypal","normalized_name":"paypal",
#      "created_at":"...","token":"k8m0...","expires_at":"2026-09-19T12:02:00.000Z",
#      "consumed_at":"2026-09-19T12:00:05.123Z"}
```

确认接口的错误语义：

```bash
# token 未知或不属于 URL 中的命名空间 -> 404
# 404 {"detail":{"error":"reservation_not_found","message":"预占不存在，或不属于该命名空间"}}

# 超过 expires_at 才确认 -> 410，过期记录被惰性删除，名称随后可被重新注册/预占
# 410 {"detail":{"error":"reservation_expired","message":"预占已过期且未确认，已失效"}}

# 同一 token 重复或并发确认：只有一次 201，其余 -> 409
# 409 {"detail":{"error":"reservation_already_claimed","message":"该预占已确认注册，不能重复消费"}}

# 确认时名称已与其他已注册名称/有效预占冲突（如预占期内被同规范名抢注）-> 409，不写入
# 409 {"detail":{"error":"name_conflict",
#      "message":"确认时名称与已注册名称或其他有效预占冲突，未写入","conflicts":[...]}}
```

合并命名空间：源空间 `a` 的 2 个已注册名称与 1 条有效预占整体迁入 `b`，
返回迁移数量；迁入的记录（含预占 token、过期时间）原样保留，源空间清空：

```bash
curl -X POST http://localhost:8000/namespaces/a/merge \
  -H 'Content-Type: application/json' -d '{"target_namespace": "b"}'
# 200 {"source_namespace":"a","target_namespace":"b",
#      "merged_names":2,"merged_reservations":1,"merged_cooldowns":0}
```

合并前冲突：源空间 `a` 的 `app` 与目标空间 `b` 的纯西里尔 `арр` 同形 → `409`，
`conflicts` 逐项给出双方记录（`kind` 区分已注册名称 / 有效预占），两侧数据均不变：

```bash
curl -X POST http://localhost:8000/namespaces/a/merge \
  -H 'Content-Type: application/json' -d '{"target_namespace": "b"}'
# 409 {"detail":{"error":"merge_conflict",
#      "message":"源与目标命名空间存在规范名/同形冲突（含有效预占），未修改任何数据",
#      "source_namespace":"a","target_namespace":"b",
#      "conflicts":[{"reason":"confusable",
#        "source_record":{"id":1,"original_name":"app","normalized_name":"app","kind":"registered"},
#        "target_record":{"id":7,"original_name":"арр","normalized_name":"арр","kind":"registered"}}]}}
```

目标命名空间与源相同（或目标为空白）→ `422`；并发写入使合并判定失效时整体回滚，
重新检测后同样返回 `409` 及最新冲突明细。

命名空间混脚本策略：未配置的空间沿用内建默认策略（Latin/Cyrillic/Greek 任意两种
混用即违规）。首次配置（`expected_revision=0`），脚本对无序、重复自动去重：

```bash
curl -X PUT http://localhost:8000/namespaces/chat/script-policy \
  -H 'Content-Type: application/json' \
  -d '{"blocked_script_pairs": [["Greek","Latin"], ["Latin","Cyrillic"]],
       "expected_revision": 0}'
# 200 {"namespace":"chat",
#      "blocked_script_pairs":[["Cyrillic","Latin"],["Greek","Latin"]],
#      "revision":1,"created_at":"...","updated_at":"..."}
```

之后所有写入入口都按该空间的新策略判定（注意命中的脚本对随响应返回）：

```bash
# Latin+Cyrillic 仍被阻断
curl -X POST http://localhost:8000/namespaces/chat/names \
  -H 'Content-Type: application/json' -d '{"name": "adмin"}'
# 422 {"detail":{"error":"mixed_script_violation",
#      "blocked_script_pairs":[["Cyrillic","Latin"],["Greek","Latin"]],
#      "violating_script_pairs":[["Cyrillic","Latin"]],
#      "restricted_scripts_used":["Cyrillic","Latin"], ...}}

# 但 Latin+Han 不再被拦截（默认策略会拦截 Latin+Cyrillic/Greek，Han 不在三脚本内；
# 这里换成只阻断 Han+Latin 的策略即可拦截，见下）
```

改成空策略（显式放行全部脚本组合），revision 递增为 2：

```bash
curl -X PUT http://localhost:8000/namespaces/chat/script-policy \
  -H 'Content-Type: application/json' -d '{"blocked_script_pairs": [], "expected_revision": 1}'
# 200 {"blocked_script_pairs":[],"revision":2,...}
# 此后 Latin+Cyrillic 混写也可注册 / 预占 / 改名
```

策略收紧会被现存数据阻止：假设空间里已有 `世界-x`（Han+Latin），PUT 一个阻断
Han+Latin 的新策略 → `409 script_policy_conflict`，逐条给出违规模明细且策略不更新：

```bash
curl -X PUT http://localhost:8000/namespaces/chat/script-policy \
  -H 'Content-Type: application/json' \
  -d '{"blocked_script_pairs": [["Han","Latin"]], "expected_revision": 2}'
# 409 {"detail":{"error":"script_policy_conflict",
#      "message":"新策略会使命名空间内现存的已注册名称或有效预占违规，策略未更新；...",
#      "violations":[{"id":7,"kind":"registered","original_name":"世界-x",
#                     "normalized_name":"世界-x",
#                     "violating_script_pairs":[["Han","Latin"]],
#                     "script_details":[{"script":"Han",...},{"script":"Latin",...}]}]}}
```

有效预占同样参与扫描（`kind:"reservation"` 并附 `expires_at`）；过期预占在扫描前
被惰性清理。先改名 / 删除来源（或等待预占过期）后重试即可成功。

版本与校验错误：

```bash
# 命名空间尚未配置时 GET -> 404（但写入与 check 仍按默认策略判定）
# 404 {"detail":{"error":"script_policy_not_found",
#      "message":"该命名空间尚未配置混脚本策略，当前沿用内建默认策略...；首次配置请以 expected_revision=0 调用 PUT"}}

# expected_revision 过期（未配置时当前版本为 0）-> 409，附 current_revision
# 409 {"detail":{"error":"stale_revision","current_revision":2}}

# 未知脚本名 / Common 等非文字脚本 / 同名对 / 项长度不为 2 -> 422
curl -X PUT http://localhost:8000/namespaces/chat/script-policy \
  -H 'Content-Type: application/json' \
  -d '{"blocked_script_pairs": [["Latin","Klingon"]], "expected_revision": 2}'
# 422
```

合并时以**目标**空间策略检查源、目标两侧：源空间在宽松策略下注册的 Latin+Cyrillic
混写名称，迁入保持默认严格策略的目标空间时会被 `409 merge_conflict` 拦下，
`script_violations` 逐项给出 `side`（`source` / `target`）、`record`（结构与跨空间
冲突中的双方记录一致，`kind` 区分已注册名称 / 有效预占）、命中的脚本对与码点：

```bash
curl -X POST http://localhost:8000/namespaces/a/merge \
  -H 'Content-Type: application/json' -d '{"target_namespace": "b"}'
# 409 {"detail":{"error":"merge_conflict","conflicts":[],
#      "script_violations":[{"side":"source",
#        "record":{"id":3,"kind":"registered","original_name":"adмin","normalized_name":"adмin"},
#        "violating_script_pairs":[["Cyrillic","Latin"]], ...}]}}
```

跨空间同形风险簇：在单个读事务快照内联合多个命名空间，只对**跨空间**的
规范名相同 / 同形节点对建边，再按连通分量返回风险簇。下例中 `chat/app`
与 `forum/арр` 同形成簇，`marketplace/bob` 无跨空间冲突被丢弃：

```bash
curl -X POST http://localhost:8000/name-risk-clusters \
  -H 'Content-Type: application/json' \
  -d '{"namespaces": ["chat", "forum", "marketplace"], "max_names": 500}'
# 200 {"namespaces":["chat","forum","marketplace"],"max_names":500,"snapshot_count":3,
#      "clusters":[{"cluster_id":1,
#        "nodes":[
#          {"namespace":"chat","kind":"registered","id":1,
#           "original_name":"app","normalized_name":"app"},
#          {"namespace":"forum","kind":"registered","id":2,
#           "original_name":"арр","normalized_name":"арр"}],
#        "edges":[
#          {"source":{"namespace":"chat","kind":"registered","id":1},
#           "target":{"namespace":"forum","kind":"registered","id":2},
#           "reasons":["confusable"]}]}]}
```

连通分量语义：A-B、B-C 建边而 A-C 并不直接冲突时，三者仍归入**同一个簇**
（边列表中只有 A-B、B-C 两条）。节点 `kind` 为 `reservation` / `released`
时附 `expires_at`，`registered` 节点不含该字段；同一命名空间内部永不建边
（空间内写入时已保证互不冲突），但同空间节点可被跨空间边“带入”簇中。
命名空间不存在或全部为空时 `snapshot_count` 为 0、`clusters` 为 `[]`。

快照超限时直接 `413`，不做截断计算：

```bash
curl -X POST http://localhost:8000/name-risk-clusters \
  -H 'Content-Type: application/json' -d '{"namespaces": ["a", "b"], "max_names": 10}'
# 413 {"detail":{"error":"risk_snapshot_too_large",
#      "message":"这些命名空间的...快照总量为 12，超过请求给定的 max_names=10；未截断快照，也未进行分簇计算...",
#      "snapshot_count":12,"max_names":10}}
```

快照 Merkle 包含证明：假设 `chat` 空间已有已注册名称 `alice`(id=1)、`bob`(id=2)，
对 1 个 registered 节点与 1 个 reservation 节点（故意乱序）取证明。返回的 `root`
承诺整个快照（含策略叶与**未请求**的节点），各 `items[].proof` 只由节点排序后
的位置决定——相同快照下改变请求顺序，根与各自证明不变：

```bash
curl -X POST http://localhost:8000/namespaces/chat/snapshot-proof \
  -H 'Content-Type: application/json' \
  -d '{"nodes": [{"kind": "reservation", "id": 1},
                 {"kind": "registered", "id": 1}], "max_nodes": 500}'
# 200 {"namespace":"chat",
#      "root":"9f2c...（64 位十六进制）","leaf_count":4,"snapshot_count":3,"max_nodes":500,
#      "policy":{"kind":"script_policy","revision":0,"configured":false,
#                "blocked_script_pairs":[["Cyrillic","Greek"],["Cyrillic","Latin"],["Greek","Latin"]]},
#      "items":[
#        {"kind":"reservation","id":1,"leaf_index":2,
#         "leaf":{"kind":"reservation","id":1,"namespace":"chat","original_name":"...",
#                 "normalized_name":"...","revision":null,"cooldown_seconds":null,
#                 "expires_at":"2026-09-21T12:02:00.000Z","created_at":"...",
#                 "leaf_hash":"ab12..."},
#         "proof":[{"direction":"right","hash":"..."}, ...]},
#        {"kind":"registered","id":1,"leaf_index":1,"leaf":{...,"revision":1,
#                 "expires_at":null,"cooldown_seconds":null,"leaf_hash":"cd34..."},
#         "proof":[{"direction":"right","hash":"..."}, ...]}]}
```

叶载荷的字段集合跨类型固定（不适用的字段为 `null`）：

| kind | 业务字段（除公共的 kind/id/namespace/original_name/normalized_name/created_at） |
| --- | --- |
| `script_policy`（策略叶，下标恒为 0，不可请求） | `revision`（未配置为 0）、`configured`、`blocked_script_pairs` |
| `registered` | `revision`；`expires_at` / `cooldown_seconds` 恒为 `null` |
| `reservation` | `expires_at`；`revision` / `cooldown_seconds` 恒为 `null` |
| `released` | `expires_at`、`cooldown_seconds`；`revision` 恒为 `null` |

> 注：预占叶刻意**不含** `token`——token 是只在确认接口使用的能力凭证，不属于
> 需要对外承诺的业务状态；`script_policy` 叶额外含固定 `id=0`（响应回显的
> `policy` 对象不带该字段，验证时补回即可）。

独立验证一个证明只需按下列规则重放（无需依赖本服务）：

```python
import hashlib, json
h = hashlib.sha256(b"\x00" + json.dumps(leaf, ensure_ascii=False,
                                        sort_keys=True, separators=(",", ":")).encode()).digest()
for step in proof:
    sib = bytes.fromhex(step["hash"])
    h = hashlib.sha256(b"\x01" + (sib + h if step["direction"] == "left" else h + sib)).digest()
assert h.hex() == root
```

快照证明的错误语义：

```bash
# 请求节点在快照中不存在（记录不存在 / 预占或冷却已过期 / 预占已消费 / 属于其他命名空间）
# -> 404，missing 按请求顺序列出全部缺失节点
# 404 {"detail":{"error":"snapshot_nodes_not_found",
#      "message":"请求的部分节点在当前快照中不存在（记录不存在，或预占 / 冷却已过期、预占已被消费）",
#      "missing":[{"kind":"registered","id":999},{"kind":"reservation","id":42}]}}

# 业务快照总量（不含策略叶）超过 max_nodes -> 413，不截断、不建树（优先于 404）
# 413 {"detail":{"error":"snapshot_too_large","snapshot_count":501,"max_nodes":500}}

# nodes 为空 / 超过 20 个 / (kind,id) 重复 / id<1 / kind 非法 / max_nodes 越界 -> 422
```

## 判定规则说明

1. **规范化**：`unicodedataplus.normalize("NFKC", name).casefold()`。NFKC 折叠全半角、合字等兼容字符，casefold 折叠大小写（比 lower 更彻底，如 `ß`→`ss`）。
2. **冲突检测**：候选规范名与同命名空间内每条**已注册名称**、**未过期、未消费预占**及**未过期释放冷却记录**比较——完全相等记为 `normalized_match`；否则用 `confusables.is_confusable` 做基于 Unicode `confusables.txt` 的同形判定，命中记为 `confusable`。冲突记录以 `source`（`registered` / `reservation` / `released`，后两者附 `expires_at`）区分来源。命名空间之间相互隔离。
3. **混脚本检测**：逐字符取 `unicodedataplus.script()`，忽略 `Common`（数字、标点、空白等）与 `Inherited`（组合符）后，按**所属命名空间的生效策略**判定：名称中同时出现 `blocked_script_pairs` 里某一对的两个脚本，即记为该对命中，命中至少一对即违规；`violating_script_pairs` 给出实际命中的脚本对子集。命名空间未配置策略时使用内建默认策略——Latin / Cyrillic / Greek 三对全阻断（即三者中出现两种及以上即违规），故纯西里尔、纯希腊或与 Han 等其他脚本混用在默认策略下不触发本规则（仍受同形判定约束）；配置空策略 `[]` 则显式放行全部脚本组合。脚本对在存储与传输中统一规范化为按字典序排列的无序对，PUT 输入顺序无关、重复对去重。
4. **批量注册**：先把整批与库内已有名称、有效预占及未过期冷却联合分析（混脚本按所属空间策略）——每项复用上述单条规则，再对批内两两做规范名相等 / 同形判定，命中时按原请求索引互相标注在双方的 `batch_conflicts` 中。任一项存在混脚本或（库内 / 预占 / 冷却 / 批内）冲突即整体返回 `422` 并汇总所有问题项，不写入任何记录；全部通过时在**单个 SQLite 事务**（`BEGIN IMMEDIATE`，事务开始时先惰性清理过期预占与冷却）中重新加载空间策略复查混脚本、复查冲突后批量落库，并发竞争（含策略被并发改严）导致判定失效时整批回滚、重新分析并返回 `409` 明细（脚本违规项的 `error` 为 `mixed_script_violation`）。
5. **预占与确认**：预占复用规范化、混脚本（所属空间策略）与同形判定，先做只读分析，再在 `BEGIN IMMEDIATE` 事务中惰性清理过期预占与冷却、重新加载策略复查混脚本与冲突（冲突覆盖有效冷却），通过后写入 `name_reservations`（`token = secrets.token_urlsafe(32)`，不可猜测；`expires_at` 为 UTC ISO 8601）。确认时在**单个 `BEGIN IMMEDIATE` 事务**中按序校验：① token 存在且命名空间匹配（否则 `404`）→ ② `expires_at` 未过（过期则删除该记录并提交，返回 `410`）→ ③ `consumed_at IS NULL`（否则 `409`）→ ④ 惰性清理过期冷却后复查当前冲突（覆盖其他预占与有效冷却，排除自身预占；否则 `409`，不写入）；全部通过后写入 `display_names` 并以条件 `UPDATE ... WHERE consumed_at IS NULL` 回写消费时间，影响行数为 0 即说明并发确认抢先、整事务回滚。因此同一 token 的并发或重复确认至多一次成功。过期记录不在后台定时清理，而是在后续预占 / 注册 / 批量 / 确认 / 改名 / 交换 / 释放 / 合并 / 策略 PUT 等写事务中惰性删除。
6. **命名空间合并**：`POST /namespaces/{source}/merge` 在**单个 `BEGIN IMMEDIATE` 写锁事务**中完成全部工作：先惰性清理两侧过期预占与冷却，再对源与目标的**已注册名称、有效预占及未过期冷却**做跨空间规范名相等 / 同形复查（同一命名空间内部在写入时已保证互不冲突，只查跨空间组合），并**以目标命名空间的混脚本策略扫描源、目标两侧数据**（目标侧在“策略生效后现存数据始终合规”的不变量下实际不会有违规，但仍显式扫描；源侧记录此前只受源空间策略约束，是主要拒绝来源）。任一冲突即回滚并返回 `409`：规范名 / 同形问题放在 `conflicts`（每项带双方记录 `source_record` / `target_record`，`kind` 为 `registered` / `reservation` / `released`，后两者附 `expires_at`），脚本违规放在 `script_violations`（每项带 `side` 为 `source` / `target`、`record` 与 `violating_script_pairs` / `script_details`），两类问题可同时出现，不修改任何数据；无冲突且两侧数据均合规才在同一事务内把源空间的 `display_names` 与 `name_reservations`（含已消费的历史预占）整体 `UPDATE` 到目标空间（有效冷却记录一并迁移，id / expires_at 原样保留），返回 `merged_names` / `merged_reservations` / `merged_cooldowns` 三个迁移数量。并发写入导致复查或 `(namespace, normalized_name)` 唯一约束失败时整体回滚，由接口层重新检测（冲突与脚本违规都重查）后返回 `409`。目标与源相同 → `422`；源空间不存在或为空时合并为无操作，返回三个 `0`。
7. **原子改名（乐观锁）**：每条 `display_names` 记录带单调递增的 `revision`（注册时为 1）。`PATCH /namespaces/{namespace}/names/{id}` 请求体为 `{"name": ..., "expected_revision": ...}`，在**单个 `BEGIN IMMEDIATE` 写锁事务**中按序校验：① `id` 存在且属于 URL 中的命名空间（否则 `404 name_not_found`）→ ② 当前 `revision` 等于 `expected_revision`（否则 `409 stale_revision`，附 `current_revision`）→ ③ 新名称不违反所属空间当前混脚本策略（在事务内重新加载策略，否则 `422`）→ ④ 惰性清理过期预占与冷却后复查新规范名与**其他已注册名称（显式排除当前记录自身，故改成与自身相等的名称是允许的）、全部有效预占及未过期冷却**的规范名相等 / 同形冲突（否则 `409 name_conflict`，明细结构与注册完全一致）。全部通过才在同一事务 `UPDATE original_name / normalized_name` 并以 `revision = revision + 1` 条件更新（`WHERE id = ? AND namespace = ? AND revision = ?`，影响行数为 0 同样按 `stale_revision` 处理）；`id` 与 `created_at` 永不改变，任何失败都整体回滚。改名释放的旧规范名可被其他记录重新注册（除非该规范名仍处于释放冷却中），改名占用的新规范名同样阻止后续注册 / 预占 / 合并。
8. **原子交换（乐观锁）**：`POST /namespaces/{namespace}/names/swap` 请求体为 `{"records": [{"id", "expected_revision"}, {"id", "expected_revision"}]}`（恰好两条且 id 不同，否则 `422`），在**单个 `BEGIN IMMEDIATE` 写锁事务**中按序校验：事务开始先惰性清理过期预占与冷却（交换不改变占用规范名集合，无需冲突复查），① 两条记录都存在且属于 URL 中的命名空间（任一缺失 / 跨空间即 `404 name_not_found`，`missing_ids` 列出全部缺失 id）→ ② 双方当前 `revision` 分别等于各自 `expected_revision`（任一过期即 `409 stale_revision`，`records` **同时携带双方**的 expected / current revision，供客户端整体重读后重试）。通过后在同一事务成对互换双方的 `original_name` / `normalized_name`，双方 `revision` 各 + 1，`id` / `created_at` 不变。由于 `(namespace, normalized_name)` 是 SQLite **即时**唯一约束（两条记录的规范名互为对方占用，单条或逐条 UPDATE 会立刻违约——单条多行 `UPDATE ... CASE` 也会在逐行更新时触发约束），实现上先把其中一条更新为随机临时名（`secrets.token_hex(32)`，并复查碰撞）腾出唯一槽位，再三步带 `AND revision = ?` 谓词的条件 UPDATE 完成互换（仅后两步递增 revision）。临时名只存在于未提交事务中：写锁串行化所有写入，其他连接在提交前通过快照隔离读到的始终是交换前状态，故临时名不会暴露、也不会破坏并发请求；交换不改变命名空间内已占用规范名的集合，因此与有效预占天然不产生新冲突。任一步骤失败（含唯一约束兜底 / 锁竞争）都整体回滚，绝不产生部分更新。
9. **命名空间混脚本策略（script-policy）**：策略存于 `namespace_script_policies` 表（每空间至多一行；`blocked_script_pairs` 以 JSON 保存无序去重的规范脚本对，`revision` 首次配置为 1、每次成功 PUT 递增；未配置空间的逻辑版本视为 0）。`GET /namespaces/{namespace}/script-policy` 返回已配置策略；未配置返回 `404 script_policy_not_found`（此时各写入入口与 `check` 仍按内建默认策略 Latin/Cyrillic/Greek 两两阻断判定）。`PUT` 请求体为 `{"blocked_script_pairs": [[脚本A, 脚本B], ...], "expected_revision": ...}`，Pydantic 在入库前完成校验与规范化：每对必须恰好两个**不同**的合法 Unicode 脚本名（不接受 `Common` / `Inherited` / `Unknown` 与未知脚本名，大小写敏感），至多 500 对；对内部按字典序排列、整体去重，非法即 `422`。服务端在**单个 `BEGIN IMMEDIATE` 写锁事务**中按序：① 先惰性清理该空间过期预占与冷却 → ② 版本校验（未配置时当前版本按 0 计；不一致即 `409 stale_revision` 附 `current_revision`）→ ③ 用新策略扫描全部已注册名称、剩余有效预占与未过期冷却（按原名逐字符分析，命中任一阻断对即记录），存在违规即 `409 script_policy_conflict` 并整体回滚，`violations` 按已注册名称（id 升序）在前、有效预占（id 升序）居中、未过期冷却（id 升序）在后逐条给出 `kind` / `original_name` / `normalized_name`（预占与冷却附 `expires_at`）、`violating_script_pairs` 与 `script_details`；全部通过才首次 `INSERT`（revision=1）或条件 `UPDATE ... AND revision = ?`（revision+1 并刷新 `updated_at`）。该扫描维持核心不变量：**策略生效后，空间内现存名称、有效预占与未过期冷却始终合规**。因此注册 / 批量 / 预占 / 改名各自的写锁事务都会**重新加载所属空间策略**再复查一次混脚本（只读分析阶段用当时策略，可能与并发的 PUT 产生竞争）：复查命中时注册 / 预占 / 改名返回 `422 mixed_script_violation`，批量整批回滚并返回 `409`（携带事务内复查命中的违规项）。预占确认（claim）无需复查混脚本——它只是把已合规的有效预占转成已注册名称（策略 PUT 扫描会先挡住违规预占的产生），但仍复查与冷却记录的规范名 / 同形冲突；交换（swap）不引入新名称，无需混脚本复查。脚本名合法集合取 `unicodedataplus`（Unicode 16.0）实际枚举到的 `Script` 属性值全集。
10. **名称释放冷却（DELETE）**：`DELETE /namespaces/{namespace}/names/{id}` 请求体为 `{"expected_revision": ..., "cooldown_seconds": 60–86400}`（边界由 Pydantic 强制：<60 或 >86400 即 `422`，`expected_revision` 必须 ≥1），在**单个 `BEGIN IMMEDIATE` 写锁事务**中按序校验：① `id` 存在且属于 URL 中的命名空间（否则 `404 name_not_found`）→ ② 当前 `revision` 等于 `expected_revision`（否则 `409 stale_revision` 附 `current_revision`）。通过后在同一事务以条件 `DELETE ... WHERE id = ? AND namespace = ? AND revision = ?` 删除已注册名称（影响行数不为 1 同样按 stale 处理），并随即向 `name_cooldowns` 插入一条记录（`original_name` / `normalized_name` 原样保留，`expires_at = now + cooldown_seconds`，UTC ISO 8601），任一步失败整体回滚——绝不会出现“名称已删除但没有冷却占位”的部分更新。冷却记录自提交起与已注册名称、有效预占**完全等价**：注册 / 批量 / 预占 / 确认 / 改名 / `check` 的 `find_conflicts` 按已注册（id 升序）→ 有效预占（id 升序）→ 未过期冷却（id 升序）的顺序输出，冲突明细 `source="released"` 并附 `expires_at`；合并时冷却参与跨空间复查（`kind="released"`）、参与目标策略脚本扫描，并随名称、预占一起 `UPDATE` 迁移到目标空间（响应新增 `merged_cooldowns`）；策略 PUT 的影响面扫描同样覆盖冷却（违规明细 `kind="released"`）。改名 / 交换等只操作 `display_names` 的事务不涉及冷却记录本身。过期冷却（`expires_at <= now`）不参与任何只读判定，也没有后台任务：每个写事务（注册 / 批量 / 预占 / 确认 / 改名 / 交换 / 释放 / 合并 / 策略 PUT）在持写锁后先调用与预占同构的惰性清理按 id 物理删除过期冷却（合并清理两侧、其余清理所属空间）；只读的 `check` 与 `GET .../cooldowns` 在 Python 侧按 `expires_at` 字符串比较过滤过期记录但不删除。`GET /namespaces/{namespace}/cooldowns` 返回该空间全部未过期冷却记录（`SELECT *` 后 Python 侧过滤，按 id 升序），空间不存在或无记录时返回 `[]`。
11. **跨空间同形风险簇（POST /name-risk-clusters）**：请求体为 `{"namespaces": [2–20 个互不相同的 1–64 字符非空白命名空间], "max_names": 1–500 的整数}`，数量越界、含重复项或 `max_names` 越界均由 Pydantic 拒绝（`422`）。服务端先把输入命名空间集合按字典序规范化，再在**同一个只读事务（`BEGIN` 读事务）**中依次读取这些空间的全部已注册名称、未消费未过期预占与未过期冷却（三条查询共享同一份数据库快照：事务期间其他连接提交的注册 / 预占 / 删除对本连接不可见；只读路径不做惰性清理，过期 / 已消费记录仅在 Python 侧按 `expires_at` / `consumed_at` 过滤，与 `check` 一致）。三类记录合计的**快照节点总量**一旦超过 `max_names` 立即抛错并由接口层返回 `413 risk_snapshot_too_large`（附 `snapshot_count` 与 `max_names`），读事务整体回滚（只读无副作用），**绝不截断快照后继续计算**——上限只过滤时间维度（过期 / 已消费），不限制命名空间数量。未超限才在内存中建模：节点稳定标识为 `(namespace, kind, id)`（`kind ∈ registered / reservation / released`，排序序值 registered < reservation < released；预占与冷却节点附 `expires_at`，已注册节点无该字段），并带 `original_name` / `normalized_name`。建边**只发生在不同命名空间的节点之间**：先按 `normalized_name` 分组、组内跨空间两两连 `normalized_match` 边（同空间写入时已保证规范名唯一）；再对不同的规范名两两调用 `is_confusable`（按无序规范名对去重判定，避免对多节点共享的规范名重复计算），命中且两端空间不同则连 `confusable` 边。随后以并查集（按秩合并 + 路径压缩）求连通分量，只保留至少含 2 个节点的分量——**A-B、B-C 建边而 A-C 不直接冲突时，三者仍归入同一簇**；孤立节点（含仅与同空间记录“疑似相似”但无跨空间边的节点）一律丢弃。输出完全确定性排序：簇内节点按 `(namespace, kind 序, id)` 升序；边端点按节点键小者为 `source`、大者为 `target`，边按 `(source, target, reasons)` 升序（`reasons` 内 `normalized_match` 在 `confusable` 前）；簇按簇内最小节点键升序排列并从 1 顺序赋予 `cluster_id`。响应顶层回显字典序 `namespaces`、`max_names` 与 `snapshot_count`；命名空间全部不存在 / 为空时 `snapshot_count=0`、`clusters=[]`。该接口为纯只读分析，不创建表、不写数据，Docker 一键启动后可直接使用。
12. **快照 Merkle 包含证明（POST /namespaces/{namespace}/snapshot-proof）**：请求体为 `{"nodes": [1–20 个 {"kind": registered|reservation|released, "id": ≥1}], "max_nodes": 1–500 的整数}`，节点数量越界、含重复 `(kind, id)`（不同 kind 的同 id 不算重复，因三类表的 id 空间独立）、id 非正、kind 非法或 `max_nodes` 越界均由 Pydantic 拒绝（`422`）。服务端在**同一个只读事务（`BEGIN` 读事务）**中依次读取该命名空间的生效脚本策略、全部已注册名称、未消费未过期预占与未过期释放冷却（四个读取共享同一份数据库快照：事务期间其他连接提交的策略 PUT / 注册 / 预占 / 释放对本连接不可见；只读路径不做惰性清理，过期 / 已消费记录仅在 Python 侧按 `expires_at` / `consumed_at` 过滤，与风险簇、`check` 一致），读事务以 rollback 结束（只读无副作用）。业务快照节点总量（已注册 + 预占 + 冷却，**不含策略叶**）一旦超过 `max_nodes` 立即返回 `413 snapshot_too_large`（附 `snapshot_count` / `max_nodes`），**不截断快照、不构建 Merkle 树**，且 413 判定优先于节点存在性（404）判定。未超限再校验请求的全部 `(kind, id)` 均在快照中，任一缺失（记录不存在、属于其他命名空间、预占 / 冷却已过期或预占已消费）返回 `404 snapshot_nodes_not_found`，`missing` 按请求顺序列出全部缺失引用。叶集合恒为 **1 个 `script_policy` 叶**（固定 `id=0`，排序后下标恒为 0；命名空间已配置时承诺库中 `revision` 与规范脚本对，未配置时 `configured=false`、`revision=0`、承诺内建默认三对——与各写入 / check 接口实际生效的判定一致，故默认策略与后续 PUT 都体现在根里）加全部业务叶，按 **(类型序, id)** 排序，序值 `script_policy=0 < registered=1 < reservation=2 < released=3`（同类 SQL 已按 id 升序）。每个叶把**业务字段**（不含预占 token 等能力凭证；字段集合跨类型固定、不适用为 `null`；策略叶另含固定 `id=0`）编码为**规范 JSON**——`json.dumps(ensure_ascii=False, sort_keys=True, separators=(",", ":"))` 后 UTF-8 编码（非 ASCII 字符原样保留），叶哈希 `SHA-256(0x00 ‖ canonical_json)`，父哈希 `SHA-256(0x01 ‖ left ‖ right)`；每层节点数为奇数时**复制本层最后一个节点**与自身配对（末节点证明第一层的兄弟即其自身、方向 `right`），单叶树根即叶哈希（本接口因策略叶恒在，实际至少 2 叶）。包含证明自叶向上给出每一层兄弟哈希（十六进制）与方向：兄弟在左 `direction="left"`（`parent=H(0x01‖sibling‖current)`），兄弟在右 `"right"`（`parent=H(0x01‖current‖sibling)`），单叶树为空列表。**证明只由节点排序后的全局下标决定，与请求中的顺序及其他请求节点无关**：相同快照下请求顺序变化或请求子集变化都不改变根，也不改变任一节点自身的 `leaf` / `leaf_index` / `proof`；`items` 仅按请求顺序回显以便客户端对齐。`0x00` / `0x01` 域分隔前缀防止叶哈希与内部节点哈希互通（第二原像防护）。响应附 `root`、`leaf_count`（业务节点数 + 1）、`snapshot_count`（业务节点数，即 413 判定值）、生效策略回显与每项叶载荷（含 `leaf_hash`），客户端可仅凭响应 JSON 独立重放证明核对根。该接口为纯只读分析，不写数据，Docker 一键启动后可直接使用。

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `./names.db`（容器内 `/data/names.db`） | SQLite 数据库文件路径 |

## 项目结构

```
app/
  main.py       # FastAPI 路由与错误响应（200/201/409/422/410/404/413）
  service.py    # 注册/改名/交换/释放冷却/批量/预占/确认/合并/策略/跨空间风险簇/快照包含证明业务逻辑，逐条冲突判定与事务控制
  merkle.py     # 规范 JSON 编码与 SHA-256 Merkle 树（叶 0x00 / 父 0x01 / 奇数层复制末节点）及包含证明
  analysis.py   # NFKC+casefold 规范化、脚本与码点分析、脚本对规范化与校验
  database.py   # SQLite 连接与版本化 schema 迁移（schema_migrations 记录版本/名称/SHA-256/applied_at；python -m app.database migrate|status）
  schemas.py    # Pydantic 请求/响应模型（含策略对无序去重规范化与快照证明模型）
tests/test_api.py
Dockerfile  docker-compose.yml  scripts/setup.sh
requirements.txt  requirements-dev.txt
```

### 数据表

- `schema_migrations`：迁移历史（每版本一行：`version` 主键、`name`、`checksum`（迁移 SQL 的 SHA-256）、`applied_at`），是迁移状态的唯一事实来源；启动时据此跳过已应用迁移并校验迁移文件未被改写。
- `display_names`：已注册名称，`(namespace, normalized_name)` 唯一约束兜底并发；`revision` 为乐观锁版本（初始 1，每次原子改名递增）。老版本数据库缺少该列时，由版本化迁移（v3 `ALTER TABLE ADD COLUMN ... DEFAULT 1`）在启动时补齐。
- `name_reservations`：预占记录（`token` 唯一、`expires_at`、`consumed_at`）；仅未消费且未过期的行参与冲突判定与策略影响面扫描，过期行惰性物理删除。
- `namespace_script_policies`：命名空间混脚本策略（每空间至多一行），`blocked_script_pairs` 为 JSON 文本保存的无序去重规范脚本对，`revision` 首次配置为 1、每次成功 PUT 递增；未配置的空间不设行（逻辑版本 0），写入与 `check` 按内建默认策略判定。
- `name_cooldowns`：名称释放冷却记录（DELETE 时与名称删除在同一事务写入），保存 `original_name` / `normalized_name` / `cooldown_seconds` / `expires_at`；仅未过期行参与冲突判定、合并迁移与策略影响面扫描，过期行在后续写事务中惰性物理删除。老版本数据库由版本化迁移（v5 建表）在启动时自动获得该表。
