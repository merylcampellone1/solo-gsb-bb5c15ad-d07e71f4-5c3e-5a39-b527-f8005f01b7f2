"""端到端 API 测试：每个用例使用独立的临时 SQLite 数据库。"""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import database, service
from app.analysis import normalize_name
from app.main import app

# 纯西里尔字母 "арр"（а U+0430, р U+0440），与拉丁 "app" 同形
CYRILLIC_APP = "арр"
# 拉丁 "ad" + 西里尔 "м"（U+043C）+ 拉丁 "in" —— 混脚本
MIXED_ADMIN = "adмin"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(database, "DATABASE_PATH", str(db_path))
    database.init_db(str(db_path))
    with TestClient(app) as test_client:
        yield test_client


def register(client, name, namespace="chat"):
    return client.post(f"/namespaces/{namespace}/names", json={"name": name})


def test_register_returns_201_and_stores_original_and_normalized(client):
    resp = register(client, "Ａｌｉｃｅ")  # 全角字母
    assert resp.status_code == 201
    body = resp.json()
    assert body["original_name"] == "Ａｌｉｃｅ"  # 原名落库
    assert body["normalized_name"] == "alice"  # NFKC + casefold
    assert body["namespace"] == "chat"
    assert body["id"] >= 1


def test_normalized_duplicate_returns_409_with_conflict_record(client):
    assert register(client, "Alice").status_code == 201

    resp = register(client, "ALICE")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "name_conflict"
    assert detail["normalized_name"] == "alice"
    assert detail["conflicts"][0]["original_name"] == "Alice"
    assert detail["conflicts"][0]["reason"] == "normalized_match"

    # NFKC 兼容字符（全角）折叠后同样冲突
    resp = register(client, "Ａｌｉｃｅ")
    assert resp.status_code == 409
    assert resp.json()["detail"]["conflicts"][0]["reason"] == "normalized_match"


def test_confusable_name_returns_409(client):
    assert register(client, "app").status_code == 201

    resp = register(client, CYRILLIC_APP)  # 纯西里尔，单脚本不触发 422
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["conflicts"][0]["reason"] == "confusable"
    assert detail["conflicts"][0]["original_name"] == "app"

    # 反向同样成立：先注册纯西里尔，再注册拉丁
    assert register(client, CYRILLIC_APP, namespace="other").status_code == 201
    resp = register(client, "app", namespace="other")
    assert resp.status_code == 409
    assert resp.json()["detail"]["conflicts"][0]["reason"] == "confusable"


def test_mixed_latin_cyrillic_returns_422_with_code_points(client):
    resp = register(client, MIXED_ADMIN)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "mixed_script_violation"
    assert detail["restricted_scripts_used"] == ["Cyrillic", "Latin"]

    by_script = {d["script"]: d for d in detail["script_details"]}
    assert by_script["Cyrillic"]["characters"] == ["м"]
    assert by_script["Cyrillic"]["code_points"] == ["U+043C"]
    assert set(by_script["Latin"]["characters"]) == {"a", "d", "i", "n"}
    assert "U+0061" in by_script["Latin"]["code_points"]

    # 422 不落库
    assert client.get("/namespaces/chat/names").json() == []


def test_mixed_latin_greek_returns_422(client):
    resp = register(client, "Αlpha")  # 希腊大写 Alpha + 拉丁
    assert resp.status_code == 422
    assert resp.json()["detail"]["restricted_scripts_used"] == ["Greek", "Latin"]


def test_single_restricted_script_is_allowed(client):
    assert register(client, "αρρ").status_code == 201  # 纯西里尔
    assert register(client, "ΑΒΓ").status_code == 201  # 纯希腊
    assert register(client, "hello-123_世界").status_code == 201  # Latin+Han 不拦截


def test_check_endpoint_analyzes_without_persisting(client):
    assert register(client, "app").status_code == 201

    resp = client.post("/namespaces/chat/check", json={"name": CYRILLIC_APP})
    assert resp.status_code == 200
    body = resp.json()
    assert body["normalized_name"] == CYRILLIC_APP
    assert body["mixed_script_violation"] is False
    assert body["scripts"] == ["Cyrillic"]
    assert len(body["conflicts"]) == 1
    assert body["conflicts"][0]["reason"] == "confusable"

    # 检查接口不落库：命名空间内仍只有 1 条记录
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["app"]


def test_check_endpoint_reports_mixed_scripts_without_persisting(client):
    resp = client.post("/namespaces/chat/check", json={"name": MIXED_ADMIN})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mixed_script_violation"] is True
    assert body["restricted_scripts_used"] == ["Cyrillic", "Latin"]
    assert client.get("/namespaces/chat/names").json() == []


def test_namespaces_are_isolated(client):
    assert register(client, "root", namespace="a").status_code == 201
    assert register(client, "root", namespace="b").status_code == 201
    assert len(client.get("/namespaces/a/names").json()) == 1
    assert len(client.get("/namespaces/b/names").json()) == 1


def test_blank_name_rejected(client):
    assert register(client, "   ").status_code == 422
    assert register(client, "").status_code == 422


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


# ---------- 批量注册 POST /namespaces/{namespace}/names/batch ----------


def register_batch(client, names, namespace="chat"):
    return client.post(f"/namespaces/{namespace}/names/batch", json={"names": names})


def test_batch_register_returns_201_and_persists_all_in_order(client):
    resp = register_batch(client, ["Alice", "Ｂｏｂ", "世界-123"])
    assert resp.status_code == 201
    body = resp.json()
    assert body["namespace"] == "chat"
    assert [item["index"] for item in body["items"]] == [0, 1, 2]
    assert [item["original_name"] for item in body["items"]] == ["Alice", "Ｂｏｂ", "世界-123"]
    assert [item["normalized_name"] for item in body["items"]] == ["alice", "bob", "世界-123"]
    assert all(item["id"] >= 1 and item["created_at"] for item in body["items"])

    # 全部落库，且可通过列表接口查到
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["Alice", "Ｂｏｂ", "世界-123"]


def test_batch_max_size_50_accepted(client):
    resp = register_batch(client, [f"user{i}" for i in range(50)])
    assert resp.status_code == 201
    assert len(resp.json()["items"]) == 50


def test_batch_size_limits_rejected(client):
    assert register_batch(client, []).status_code == 422  # 至少 1 个
    assert register_batch(client, [f"user{i}" for i in range(51)]).status_code == 422  # 至多 50 个
    assert register_batch(client, ["ok", "   "]).status_code == 422  # 空白名称
    assert client.get("/namespaces/chat/names").json() == []


def test_batch_in_batch_normalized_duplicate_returns_422_and_marks_both(client):
    resp = register_batch(client, ["Alice", "bob", "ALICE"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "batch_validation_failed"

    items = {item["index"]: item for item in detail["items"]}
    assert set(items) == {0, 2}  # bob 无问题，不出现在明细中
    assert items[0]["batch_conflicts"][0]["index"] == 2
    assert items[0]["batch_conflicts"][0]["original_name"] == "ALICE"
    assert items[0]["batch_conflicts"][0]["reason"] == "normalized_match"
    assert items[2]["batch_conflicts"][0]["index"] == 0
    assert items[2]["batch_conflicts"][0]["original_name"] == "Alice"

    # 422 整批不落库
    assert client.get("/namespaces/chat/names").json() == []


def test_batch_in_batch_confusable_returns_422_and_marks_both(client):
    resp = register_batch(client, ["app", CYRILLIC_APP, "ok-name"])
    assert resp.status_code == 422
    items = {item["index"]: item for item in resp.json()["detail"]["items"]}
    assert set(items) == {0, 1}
    assert items[0]["batch_conflicts"][0]["index"] == 1
    assert items[0]["batch_conflicts"][0]["reason"] == "confusable"
    assert items[1]["batch_conflicts"][0]["index"] == 0
    assert client.get("/namespaces/chat/names").json() == []


def test_batch_db_conflict_returns_422_with_conflict_records(client):
    assert register(client, "app").status_code == 201

    resp = register_batch(client, ["hello", CYRILLIC_APP])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert [item["index"] for item in detail["items"]] == [1]
    conflict = detail["items"][0]["conflicts"][0]
    assert conflict["original_name"] == "app"
    assert conflict["reason"] == "confusable"

    # 只有预先注册的 app，批次未写入
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["app"]


def test_batch_mixed_script_returns_422_with_script_details(client):
    resp = register_batch(client, ["clean", MIXED_ADMIN])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert [item["index"] for item in detail["items"]] == [1]
    item = detail["items"][0]
    assert item["mixed_script_violation"] is True
    assert item["restricted_scripts_used"] == ["Cyrillic", "Latin"]
    by_script = {d["script"]: d for d in item["script_details"]}
    assert by_script["Cyrillic"]["code_points"] == ["U+043C"]
    assert client.get("/namespaces/chat/names").json() == []


def test_batch_aggregates_script_issues_and_all_conflicts(client):
    assert register(client, "Alice").status_code == 201

    # 一批同时覆盖：库内冲突、批内冲突、混脚本
    resp = register_batch(client, ["ALICE", "app", CYRILLIC_APP, MIXED_ADMIN])
    assert resp.status_code == 422
    items = {item["index"]: item for item in resp.json()["detail"]["items"]}
    assert set(items) == {0, 1, 2, 3}
    assert items[0]["conflicts"][0]["reason"] == "normalized_match"  # 与库内 Alice
    assert items[1]["batch_conflicts"][0]["index"] == 2  # 批内同形
    assert items[2]["batch_conflicts"][0]["index"] == 1
    assert items[3]["mixed_script_violation"] is True

    # 一条都不落库
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["Alice"]


def test_batch_concurrent_race_returns_409_with_refreshed_conflicts(client, monkeypatch):
    """并发竞争：批次事务开始时另一连接已抢先写入冲突名称。

    insert_batch 事务内复查发现判定失效 -> 回滚 -> 重新分析 -> 409。
    """
    real_insert_batch = service.insert_batch

    def racing_insert_batch(conn, namespace, names):
        # 模拟并发写入：另一个连接在批次事务前抢注 "Alice"
        other = database.get_connection()
        try:
            service.insert_name(other, namespace, "Alice", "alice")
        finally:
            other.close()
        return real_insert_batch(conn, namespace, names)

    monkeypatch.setattr(service, "insert_batch", racing_insert_batch)

    resp = register_batch(client, ["alice", "bob"])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "name_conflict"
    assert [item["index"] for item in detail["items"]] == [0]
    assert detail["items"][0]["conflicts"][0]["original_name"] == "Alice"
    assert detail["items"][0]["conflicts"][0]["reason"] == "normalized_match"

    # 批次整体回滚：库内只有并发写入的 Alice
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["Alice"]


def test_insert_batch_rolls_back_when_conflict_appears_in_transaction(client):
    """服务层兜底：事务内复查发现库内冲突时整批回滚。"""
    conn = database.get_connection()
    try:
        service.insert_name(conn, "chat", "app", "app")
        with pytest.raises(service.NameConflictError):
            service.insert_batch(conn, "chat", ["alice", CYRILLIC_APP])
        # alice 未写入，整批回滚
        assert [r["original_name"] for r in service.list_names(conn, "chat")] == ["app"]
    finally:
        conn.close()


def test_insert_batch_unique_constraint_rolls_back_entire_batch(client):
    """服务层兜底：批内规范名重复触发唯一约束时整批回滚。"""
    conn = database.get_connection()
    try:
        with pytest.raises(service.NameConflictError):
            service.insert_batch(conn, "chat", ["dup", "DUP"])
        assert service.list_names(conn, "chat") == []
    finally:
        conn.close()


def test_batch_concurrent_requests_single_winner_no_5xx(client):
    """并发回归：同一批次并发提交，恰好一个 201，其余 409/422，无 5xx。

    同时覆盖连接跨线程使用（check_same_thread=False）的回归。
    """

    def post(_):
        return client.post(
            "/namespaces/chat/names/batch", json={"names": ["n1", "n2"]}
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(post, range(8)))

    assert not any(code >= 500 for code in codes)
    assert codes.count(201) == 1
    assert set(codes) <= {201, 409, 422}
    # 只有一个批次落库（all-or-nothing）
    assert len(client.get("/namespaces/chat/names").json()) == 2


# ---------- 预占 POST /namespaces/{namespace}/reservations ----------


def reserve(client, name, ttl_seconds=120, namespace="chat"):
    return client.post(
        f"/namespaces/{namespace}/reservations",
        json={"name": name, "ttl_seconds": ttl_seconds},
    )


def claim(client, token, namespace="chat"):
    return client.post(
        f"/namespaces/{namespace}/reservations/{token}/claim"
    )


def test_reserve_returns_201_with_unguessable_token_and_expires_at(client):
    resp = reserve(client, "Alice", ttl_seconds=60)
    assert resp.status_code == 201
    body = resp.json()
    assert body["namespace"] == "chat"
    assert body["original_name"] == "Alice"
    assert body["normalized_name"] == "alice"
    assert body["ttl_seconds"] == 60
    # token 不可猜测：32 字节随机 -> urlsafe base64 至少 40 字符
    assert len(body["token"]) >= 40
    assert body["expires_at"].endswith("Z")
    assert body["id"] >= 1

    # 两个独立预占的 token 必须不同
    other = reserve(client, "Bob")
    assert other.status_code == 201
    assert other.json()["token"] != body["token"]

    # 预占期间名称尚未注册
    assert client.get("/namespaces/chat/names").json() == []


def test_reserve_ttl_bounds_validated(client):
    assert reserve(client, "a", ttl_seconds=30).status_code == 201
    assert reserve(client, "b", ttl_seconds=600).status_code == 201
    assert reserve(client, "c", ttl_seconds=29).status_code == 422
    assert reserve(client, "d", ttl_seconds=601).status_code == 422
    assert reserve(client, "e", ttl_seconds=0).status_code == 422


def test_reserve_mixed_script_returns_422_and_does_not_persist(client):
    resp = reserve(client, MIXED_ADMIN)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "mixed_script_violation"
    assert detail["restricted_scripts_used"] == ["Cyrillic", "Latin"]

    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM name_reservations").fetchone()[0] == 0
    finally:
        conn.close()


def test_active_reservation_blocks_single_register_normalized(client):
    r = reserve(client, "Alice").json()

    # 大小写折叠后相同 -> 409，冲突来源标记为 reservation
    resp = register(client, "ALICE")
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["reason"] == "normalized_match"
    assert conflict["source"] == "reservation"
    assert conflict["original_name"] == "Alice"
    assert conflict["expires_at"] == r["expires_at"]

    # 注册没有落库
    assert client.get("/namespaces/chat/names").json() == []


def test_active_reservation_blocks_single_register_confusable(client):
    reserve(client, "app")

    resp = register(client, CYRILLIC_APP)
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["reason"] == "confusable"
    assert conflict["source"] == "reservation"


def test_active_reservation_blocks_new_reservation(client):
    reserve(client, "app")

    resp = reserve(client, CYRILLIC_APP)
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["source"] == "reservation"
    assert conflict["reason"] == "confusable"


def test_active_reservation_blocks_batch_register(client):
    reserve(client, "app")

    resp = register_batch(client, ["hello", CYRILLIC_APP])
    assert resp.status_code == 422
    items = resp.json()["detail"]["items"]
    assert items[0]["index"] == 1
    assert items[0]["conflicts"][0]["source"] == "reservation"
    assert items[0]["conflicts"][0]["reason"] == "confusable"
    # 整批不落库，预占也未被消费
    assert client.get("/namespaces/chat/names").json() == []


def test_registered_name_blocks_reservation_with_registered_source(client):
    assert register(client, "app").status_code == 201

    resp = reserve(client, CYRILLIC_APP)
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["source"] == "registered"
    assert conflict["reason"] == "confusable"
    assert "expires_at" not in conflict


def test_check_reports_active_reservation_conflict(client):
    reserve(client, "app")

    resp = client.post("/namespaces/chat/check", json={"name": CYRILLIC_APP})
    assert resp.status_code == 200
    conflicts = resp.json()["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["source"] == "reservation"
    assert conflicts[0]["reason"] == "confusable"


def test_reservations_are_namespace_isolated(client):
    reserve(client, "app", namespace="a")
    assert register(client, "app", namespace="b").status_code == 201
    assert reserve(client, CYRILLIC_APP, namespace="b").status_code == 409
    # 命名空间 a 中预占仍然有效
    assert register(client, "app", namespace="a").status_code == 409


# ---------- 确认注册 POST .../reservations/{token}/claim ----------


def test_claim_success_writes_display_name_and_marks_consumed(client):
    r = reserve(client, "Alice", ttl_seconds=60).json()

    resp = claim(client, r["token"])
    assert resp.status_code == 201
    body = resp.json()
    assert body["original_name"] == "Alice"
    assert body["normalized_name"] == "alice"
    assert body["namespace"] == "chat"
    assert body["token"] == r["token"]
    assert body["expires_at"] == r["expires_at"]
    assert body["consumed_at"].endswith("Z")
    assert body["id"] >= 1

    # 名称已真正注册
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["Alice"]


def test_claim_unknown_token_returns_404(client):
    resp = claim(client, "does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "reservation_not_found"


def test_claim_token_from_other_namespace_returns_404(client):
    r = reserve(client, "app", namespace="a").json()
    resp = claim(client, r["token"], namespace="b")
    assert resp.status_code == 404
    # 预占仍可在原命名空间确认
    assert claim(client, r["token"], namespace="a").status_code == 201


def test_claim_twice_only_first_succeeds(client):
    r = reserve(client, "Alice").json()

    assert claim(client, r["token"]).status_code == 201
    second = claim(client, r["token"])
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "reservation_already_claimed"

    # 只注册了一次
    assert len(client.get("/namespaces/chat/names").json()) == 1


def test_claim_concurrent_requests_exactly_one_wins(client):
    r = reserve(client, "Alice").json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: claim(client, r["token"]), range(8)))

    codes = [resp.status_code for resp in responses]
    assert codes.count(201) == 1
    assert set(codes) <= {201, 409}
    assert not any(code >= 500 for code in codes)
    assert len(client.get("/namespaces/chat/names").json()) == 1


def test_claim_expired_returns_410_and_lazily_deletes(client):
    r = reserve(client, "Alice", ttl_seconds=30).json()

    # 直接把 expires_at 改成过去时刻，模拟 TTL 到期
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_reservations SET expires_at = '2000-01-01T00:00:00.000Z'"
        )
        conn.commit()
    finally:
        conn.close()

    resp = claim(client, r["token"])
    assert resp.status_code == 410
    assert resp.json()["detail"]["error"] == "reservation_expired"

    # 惰性清理：过期记录已删除，名称可被重新注册
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM name_reservations").fetchone()[0] == 0
    finally:
        conn.close()
    assert register(client, "Alice").status_code == 201


def test_expired_reservation_does_not_block_and_is_purged_on_next_write(client):
    reserve(client, "app", ttl_seconds=30)
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_reservations SET expires_at = '2000-01-01T00:00:00.000Z'"
        )
        conn.commit()
    finally:
        conn.close()

    # 过期预占不阻止新预占；创建时惰性清理旧记录
    resp = reserve(client, "app")
    assert resp.status_code == 201
    conn = database.get_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM name_reservations WHERE original_name = 'app'"
        ).fetchone()[0]
        assert count == 1  # 旧的已删除，只剩新的
    finally:
        conn.close()


def test_claim_conflict_with_registered_name_returns_409_and_writes_nothing(client):
    r = reserve(client, "app").json()
    # 预占期间另一个名字（不与预占冲突的）被注册后，再注册与预占同规范名的名字
    # —— 单条注册接口会被预占挡住，因此这里直接走服务层制造并发抢注
    conn = database.get_connection()
    try:
        service.insert_name(conn, "chat", "APP", "app")
    finally:
        conn.close()

    resp = claim(client, r["token"])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "name_conflict"
    assert detail["conflicts"][0]["original_name"] == "APP"
    assert detail["conflicts"][0]["source"] == "registered"

    # 冲突不写入，预占未被消费（但名称仍被注册方占着，后续确认依旧失败）
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["APP"]
    assert claim(client, r["token"]).status_code == 409


def test_claim_conflict_with_other_reservation_returns_409(client):
    # 两个互相同形的预占不能同时存在：先预占 app，再用同形名会被 409。
    # 因此冲突场景用“注册名 vs 预占”之外的手段：同规范名预占经直接插入制造，
    # 模拟并发下两笔预占都通过了各自的事务前分析。
    first = reserve(client, "app").json()
    conn = database.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO name_reservations
                (namespace, original_name, normalized_name, token, expires_at)
            VALUES ('chat', 'APP', 'app', ?, '2999-01-01T00:00:00.000Z')
            """,
            ("second-conflicting-token",),
        )
        conn.commit()
    finally:
        conn.close()

    resp = claim(client, first["token"])
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["source"] == "reservation"
    assert conflict["original_name"] == "APP"
    assert client.get("/namespaces/chat/names").json() == []


def test_claim_after_success_name_blocks_others(client):
    r = reserve(client, "app").json()
    assert claim(client, r["token"]).status_code == 201

    # 确认后与普通注册完全等价：同形名 409
    assert register(client, CYRILLIC_APP).status_code == 409
    assert reserve(client, CYRILLIC_APP).status_code == 409
    # 已消费预占不再出现在冲突明细中
    resp = client.post("/namespaces/chat/check", json={"name": CYRILLIC_APP})
    assert resp.json()["conflicts"][0]["source"] == "registered"


def test_service_claim_rolls_back_when_conflict_detected_in_transaction(client):
    """服务层：claim 事务内复查发现冲突时抛 NameConflictError 且不写入。"""
    r = reserve(client, "app").json()

    # 直接制造与预占同规范名的已注册记录（绕过 API 的预占拦截）
    conn = database.get_connection()
    try:
        service.insert_name(conn, "chat", "APP", "app")
    finally:
        conn.close()

    claim_conn = database.get_connection()
    try:
        with pytest.raises(service.NameConflictError) as exc_info:
            service.claim_reservation(claim_conn, "chat", r["token"])
        conflicts = exc_info.value.conflicts
        assert conflicts and conflicts[0]["source"] == "registered"

        # 事务已回滚：预占仍是未消费状态，可重复进入确认（依旧冲突）
        row = claim_conn.execute(
            "SELECT consumed_at FROM name_reservations WHERE token = ?", (r["token"],)
        ).fetchone()
        assert row["consumed_at"] is None
    finally:
        claim_conn.close()


def test_reserve_concurrent_race_returns_409_with_refreshed_conflicts(client, monkeypatch):
    """并发竞争：只读分析通过后、写事务复查前另一连接抢注同名 -> 409，不落预占。"""
    real_create_reservation = service.create_reservation

    def racing_create_reservation(conn, namespace, name, ttl_seconds):
        # 模拟并发写入：另一个连接在预占写事务前抢注 "Alice"
        other = database.get_connection()
        try:
            service.insert_name(other, namespace, "Alice", "alice")
        finally:
            other.close()
        return real_create_reservation(conn, namespace, name, ttl_seconds)

    monkeypatch.setattr(service, "create_reservation", racing_create_reservation)

    resp = reserve(client, "Alice")
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["original_name"] == "Alice"
    assert conflict["source"] == "registered"

    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM name_reservations").fetchone()[0] == 0
    finally:
        conn.close()


# ---------- 合并命名空间 POST /namespaces/{source}/merge ----------


def merge(client, source="a", target="b"):
    return client.post(f"/namespaces/{source}/merge", json={"target_namespace": target})


def test_merge_success_migrates_names_and_reservations(client):
    assert register(client, "alice", namespace="a").status_code == 201
    assert register(client, "Ｂｏｂ", namespace="a").status_code == 201
    active = reserve(client, "carol", namespace="a").json()
    consumed = reserve(client, "dave", namespace="a").json()
    assert claim(client, consumed["token"], namespace="a").status_code == 201
    # 目标命名空间已有的记录不受影响
    assert register(client, "erin", namespace="b").status_code == 201

    resp = merge(client, "a", "b")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_namespace"] == "a"
    assert body["target_namespace"] == "b"
    assert body["merged_names"] == 3  # alice、Ｂｏｂ、dave（claim 后注册）
    assert body["merged_reservations"] == 2  # 有效预占 carol + 已消费历史记录 dave

    # 源命名空间已清空，目标命名空间包含全部名称（id 与原记录保持不变）
    assert client.get("/namespaces/a/names").json() == []
    names = {n["original_name"]: n for n in client.get("/namespaces/b/names").json()}
    assert set(names) == {"alice", "Ｂｏｂ", "dave", "erin"}
    assert names["Ｂｏｂ"]["normalized_name"] == "bob"

    # 迁移后的有效预占在目标命名空间继续占位，并可在目标命名空间确认
    resp = register(client, "CAROL", namespace="b")
    assert resp.status_code == 409
    assert resp.json()["detail"]["conflicts"][0]["source"] == "reservation"
    assert claim(client, active["token"], namespace="a").status_code == 404
    assert claim(client, active["token"], namespace="b").status_code == 201

    # 源命名空间已空：同名可以重新注册
    assert register(client, "alice", namespace="a").status_code == 201


def test_merge_empty_source_returns_zero_counts(client):
    assert register(client, "erin", namespace="b").status_code == 201

    resp = merge(client, "no-such-namespace", "b")
    assert resp.status_code == 200
    body = resp.json()
    assert body["merged_names"] == 0
    assert body["merged_reservations"] == 0
    # 目标命名空间不受影响
    assert len(client.get("/namespaces/b/names").json()) == 1


def test_merge_normalized_conflict_returns_409_with_both_records(client):
    assert register(client, "Alice", namespace="a").status_code == 201
    assert register(client, "ALICE", namespace="b").status_code == 201

    resp = merge(client, "a", "b")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "merge_conflict"
    assert detail["source_namespace"] == "a"
    assert detail["target_namespace"] == "b"
    pair = detail["conflicts"][0]
    assert pair["reason"] == "normalized_match"
    assert pair["source_record"]["original_name"] == "Alice"
    assert pair["source_record"]["kind"] == "registered"
    assert pair["target_record"]["original_name"] == "ALICE"
    assert pair["target_record"]["kind"] == "registered"

    # 409 不修改任何数据
    assert [n["original_name"] for n in client.get("/namespaces/a/names").json()] == ["Alice"]
    assert [n["original_name"] for n in client.get("/namespaces/b/names").json()] == ["ALICE"]


def test_merge_confusable_conflict_returns_409(client):
    assert register(client, "app", namespace="a").status_code == 201
    assert register(client, CYRILLIC_APP, namespace="b").status_code == 201

    resp = merge(client, "a", "b")
    assert resp.status_code == 409
    pair = resp.json()["detail"]["conflicts"][0]
    assert pair["reason"] == "confusable"
    assert pair["source_record"]["normalized_name"] == "app"
    assert pair["target_record"]["normalized_name"] == CYRILLIC_APP
    assert len(client.get("/namespaces/a/names").json()) == 1
    assert len(client.get("/namespaces/b/names").json()) == 1


def test_merge_conflict_with_active_reservation_on_either_side(client):
    # 源侧有效预占 vs 目标侧已注册名称
    r = reserve(client, "app", namespace="a").json()
    assert register(client, CYRILLIC_APP, namespace="b").status_code == 201

    resp = merge(client, "a", "b")
    assert resp.status_code == 409
    pair = resp.json()["detail"]["conflicts"][0]
    assert pair["reason"] == "confusable"
    assert pair["source_record"]["kind"] == "reservation"
    assert pair["source_record"]["expires_at"] == r["expires_at"]
    assert pair["target_record"]["kind"] == "registered"

    # 反向同样成立：目标侧有效预占 vs 源侧已注册名称
    assert register(client, "app", namespace="c").status_code == 201
    r2 = reserve(client, CYRILLIC_APP, namespace="d").json()
    resp = merge(client, "c", "d")
    assert resp.status_code == 409
    pair = resp.json()["detail"]["conflicts"][0]
    assert pair["source_record"]["kind"] == "registered"
    assert pair["target_record"]["kind"] == "reservation"
    assert pair["target_record"]["expires_at"] == r2["expires_at"]

    # 两侧数据均未变化
    assert client.get("/namespaces/c/names").json()[0]["original_name"] == "app"


def test_merge_expired_reservation_does_not_block_and_is_purged(client):
    reserve(client, "app", ttl_seconds=30, namespace="a")
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_reservations SET expires_at = '2000-01-01T00:00:00.000Z'"
        )
        conn.commit()
    finally:
        conn.close()
    # 过期预占不参与冲突判定：目标侧的同形名不阻止合并
    assert register(client, CYRILLIC_APP, namespace="b").status_code == 201

    resp = merge(client, "a", "b")
    assert resp.status_code == 200
    assert resp.json()["merged_reservations"] == 0  # 过期记录被惰性清理，不迁移

    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM name_reservations").fetchone()[0] == 0
    finally:
        conn.close()


def test_merge_same_namespace_rejected(client):
    resp = merge(client, "a", "a")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_merge_target"


def test_merge_blank_or_missing_target_rejected(client):
    assert client.post(
        "/namespaces/a/merge", json={"target_namespace": "   "}
    ).status_code == 422
    assert client.post("/namespaces/a/merge", json={}).status_code == 422


def test_merge_concurrent_race_returns_409_with_refreshed_conflicts(client, monkeypatch):
    """并发竞争：合并事务开始前另一连接在目标命名空间抢注同规范名 -> 409，不迁移。"""
    assert register(client, "alice", namespace="a").status_code == 201
    real_merge_namespaces = service.merge_namespaces

    def racing_merge_namespaces(conn, source_namespace, target_namespace):
        # 模拟并发写入：另一个连接在合并事务前向目标命名空间抢注 "ALICE"
        other = database.get_connection()
        try:
            service.insert_name(other, target_namespace, "ALICE", "alice")
        finally:
            other.close()
        return real_merge_namespaces(conn, source_namespace, target_namespace)

    monkeypatch.setattr(service, "merge_namespaces", racing_merge_namespaces)

    resp = merge(client, "a", "b")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "merge_conflict"
    pair = detail["conflicts"][0]
    assert pair["reason"] == "normalized_match"
    assert pair["source_record"]["original_name"] == "alice"
    assert pair["target_record"]["original_name"] == "ALICE"

    # 整体回滚：源侧记录未迁移，目标侧只有并发写入的 ALICE
    assert [n["original_name"] for n in client.get("/namespaces/a/names").json()] == ["alice"]
    assert [n["original_name"] for n in client.get("/namespaces/b/names").json()] == ["ALICE"]


def test_merge_unique_constraint_failure_rolls_back_everything(client, monkeypatch):
    """服务层兜底：唯一约束冲突时整体回滚，重新检测后返回 409。"""
    assert register(client, "alice", namespace="a").status_code == 201
    assert register(client, "ALICE", namespace="b").status_code == 201
    # 只绕过事务内的第一次复查，让 UPDATE 触发 (namespace, normalized_name)
    # 唯一约束；回滚后的重新检测仍走真实逻辑
    real_find_merge_conflicts = service.find_merge_conflicts
    calls = {"count": 0}

    def skip_first_find_merge_conflicts(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return []
        return real_find_merge_conflicts(*args, **kwargs)

    monkeypatch.setattr(service, "find_merge_conflicts", skip_first_find_merge_conflicts)

    resp = merge(client, "a", "b")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "merge_conflict"
    # 回滚后重新检测到的冲突明细仍带双方记录
    assert detail["conflicts"][0]["source_record"]["original_name"] == "alice"
    assert detail["conflicts"][0]["target_record"]["original_name"] == "ALICE"

    # 两侧数据均未变化
    assert [n["original_name"] for n in client.get("/namespaces/a/names").json()] == ["alice"]
    assert [n["original_name"] for n in client.get("/namespaces/b/names").json()] == ["ALICE"]


def test_service_merge_rolls_back_when_conflict_detected_in_transaction(client):
    """服务层：merge 事务内复查发现冲突时抛 MergeConflictError 且不迁移。"""
    conn = database.get_connection()
    try:
        service.insert_name(conn, "a", "app", "app")
        service.insert_name(conn, "b", CYRILLIC_APP, CYRILLIC_APP)
        with pytest.raises(service.MergeConflictError) as exc_info:
            service.merge_namespaces(conn, "a", "b")
        conflicts = exc_info.value.conflicts
        assert conflicts[0]["reason"] == "confusable"
        assert conflicts[0]["source_record"]["kind"] == "registered"

        # 事务已回滚：两侧记录保持原样
        assert [r["original_name"] for r in service.list_names(conn, "a")] == ["app"]
        assert [r["original_name"] for r in service.list_names(conn, "b")] == [CYRILLIC_APP]
    finally:
        conn.close()


def test_merge_concurrent_requests_no_5xx_and_all_names_merged_once(client):
    """并发回归：同一源命名空间并发合并，名称恰好迁移一次，无 5xx。"""
    assert register_batch(client, ["n1", "n2"], namespace="a").status_code == 201

    def post(_):
        return client.post("/namespaces/a/merge", json={"target_namespace": "b"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(post, range(8)))

    codes = [resp.status_code for resp in responses]
    assert not any(code >= 500 for code in codes)
    assert set(codes) <= {200, 409}
    # 名称恰好迁移一次：各次合并报告的迁移数量之和等于源侧原有数量
    assert sum(resp.json()["merged_names"] for resp in responses if resp.status_code == 200) == 2
    assert client.get("/namespaces/a/names").json() == []
    assert len(client.get("/namespaces/b/names").json()) == 2


def test_openapi_documents_merge_endpoint(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/namespaces/{source}/merge" in paths
    operation = paths["/namespaces/{source}/merge"]["post"]
    assert "409" in operation["responses"]
    assert "422" in operation["responses"]


# ---------- 原子改名 PATCH /namespaces/{namespace}/names/{id} ----------


def rename(client, name_id, name, expected_revision, namespace="chat"):
    return client.patch(
        f"/namespaces/{namespace}/names/{name_id}",
        json={"name": name, "expected_revision": expected_revision},
    )


def test_rename_success_updates_names_and_bumps_revision_keeps_id_and_created_at(client):
    created = register(client, "Alice").json()
    name_id = created["id"]
    assert created["revision"] == 1

    resp = rename(client, name_id, "Ｂｏｂ", expected_revision=1)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == name_id  # id 保留
    assert body["namespace"] == "chat"
    assert body["original_name"] == "Ｂｏｂ"  # 原名原样更新
    assert body["normalized_name"] == "bob"  # 规范名重新计算
    assert body["revision"] == 2  # revision 递增
    assert body["created_at"] == created["created_at"]  # created_at 保留

    # 再次改名：revision 继续递增，规范名查重按新名称生效
    resp = rename(client, name_id, "Charlie", expected_revision=2)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == name_id
    assert body["original_name"] == "Charlie"
    assert body["normalized_name"] == "charlie"
    assert body["revision"] == 3
    assert body["created_at"] == created["created_at"]

    # 列表接口反映改名结果（仍只有一条记录），旧规范名释放
    names = client.get("/namespaces/chat/names").json()
    assert len(names) == 1
    assert names[0]["original_name"] == "Charlie"
    assert names[0]["revision"] == 3
    freed = register(client, "ALICE")  # 旧规范名 alice 可被重新注册
    assert freed.status_code == 201
    assert freed.json()["id"] != name_id


def test_rename_to_same_name_succeeds_and_bumps_revision(client):
    created = register(client, "Alice").json()

    # 改成与当前完全相同的名称：排除自身后无冲突，允许并递增 revision
    resp = rename(client, created["id"], "Alice", expected_revision=1)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 2
    assert resp.json()["normalized_name"] == "alice"


def test_rename_stale_revision_returns_409_with_current_revision(client):
    created = register(client, "Alice").json()
    name_id = created["id"]
    assert rename(client, name_id, "Alicia", expected_revision=1).status_code == 200

    # 持旧版本再次改名 -> 409 stale_revision，附当前 revision
    resp = rename(client, name_id, "Bob", expected_revision=1)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "stale_revision"
    assert detail["current_revision"] == 2

    # 改名未生效
    assert client.get("/namespaces/chat/names").json()[0]["original_name"] == "Alicia"

    # 用最新版本可成功
    resp = rename(client, name_id, "Bob", expected_revision=2)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 3


def test_rename_unknown_id_returns_404(client):
    resp = rename(client, 999, "whatever", expected_revision=1)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "name_not_found"


def test_rename_id_from_other_namespace_returns_404(client):
    created = register(client, "Alice", namespace="a").json()

    # id 属于命名空间 a，在 b 下改名 -> 404（只能改本空间记录）
    resp = rename(client, created["id"], "Bob", expected_revision=1, namespace="b")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "name_not_found"

    # 原空间记录未被修改
    assert register(client, "alice", namespace="a").status_code == 409
    names = client.get("/namespaces/a/names").json()
    assert names[0]["original_name"] == "Alice"
    assert names[0]["revision"] == 1


def test_rename_normalized_conflict_returns_409_with_existing_record(client):
    first = register(client, "Alice").json()
    other = register(client, "Bob").json()

    # 把 Bob 改成与 Alice 规范名相同 -> 409，冲突明细指向 Alice（排除自身）
    resp = rename(client, other["id"], "ALICE", expected_revision=1)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "name_conflict"
    assert detail["normalized_name"] == "alice"
    assert len(detail["conflicts"]) == 1
    conflict = detail["conflicts"][0]
    assert conflict["id"] == first["id"]
    assert conflict["original_name"] == "Alice"
    assert conflict["reason"] == "normalized_match"
    assert conflict["source"] == "registered"

    # 改名未生效，revision 不递增
    unchanged = client.get("/namespaces/chat/names").json()
    bob = next(n for n in unchanged if n["id"] == other["id"])
    assert bob["original_name"] == "Bob"
    assert bob["revision"] == 1


def test_rename_confusable_conflict_returns_409(client):
    registered = register(client, "app").json()
    target = register(client, "hello").json()

    # 把 hello 改成纯西里尔 арр：与 app 同形但不混脚本 -> 409 confusable
    resp = rename(client, target["id"], CYRILLIC_APP, expected_revision=1)
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["id"] == registered["id"]
    assert conflict["reason"] == "confusable"
    assert client.get("/namespaces/chat/names").json()[1]["original_name"] == "hello"


def test_rename_does_not_conflict_with_itself(client):
    created = register(client, "Alice").json()

    # 仅大小写不同的新规范名与自身相等：排除自身后无冲突
    resp = rename(client, created["id"], "ALICE", expected_revision=1)
    assert resp.status_code == 200
    body = resp.json()
    assert body["original_name"] == "ALICE"
    assert body["normalized_name"] == "alice"
    assert body["revision"] == 2


def test_rename_active_reservation_blocks_even_when_registered_name_freed(client):
    created = register(client, "Alice").json()
    # 一条有效预占占用 carol
    reservation = reserve(client, "carol", ttl_seconds=120).json()

    # 改名到预占名：预占不排除 -> 409，冲突来源为 reservation
    resp = rename(client, created["id"], "CAROL", expected_revision=1)
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["source"] == "reservation"
    assert conflict["reason"] == "normalized_match"
    assert conflict["id"] == reservation["id"]
    assert conflict["expires_at"] == reservation["expires_at"]
    assert client.get("/namespaces/chat/names").json()[0]["revision"] == 1


def test_rename_mixed_script_returns_422_with_script_details(client):
    created = register(client, "Alice").json()

    resp = rename(client, created["id"], MIXED_ADMIN, expected_revision=1)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "mixed_script_violation"
    assert detail["restricted_scripts_used"] == ["Cyrillic", "Latin"]
    by_script = {d["script"]: d for d in detail["script_details"]}
    assert by_script["Cyrillic"]["code_points"] == ["U+043C"]

    # 422 不修改记录，revision 不递增
    record = client.get("/namespaces/chat/names").json()[0]
    assert record["original_name"] == "Alice"
    assert record["revision"] == 1


def test_rename_blank_name_or_non_positive_revision_rejected(client):
    created = register(client, "Alice").json()
    assert rename(client, created["id"], "   ", expected_revision=1).status_code == 422
    assert rename(client, created["id"], "Bob", expected_revision=0).status_code == 422
    # 缺少 expected_revision / name 同样 422
    resp = client.patch(
        f"/namespaces/chat/names/{created['id']}", json={"name": "Bob"}
    )
    assert resp.status_code == 422


def test_rename_non_integer_id_returns_422(client):
    resp = client.patch(
        "/namespaces/chat/names/not-an-int",
        json={"name": "Bob", "expected_revision": 1},
    )
    assert resp.status_code == 422


def test_rename_concurrent_requests_exactly_one_wins(client):
    """并发改名：版本校验串行化，恰好一次成功，其余 409 stale_revision，无 5xx。"""
    created = register(client, "Alice").json()
    name_id = created["id"]

    def post(seq):
        suffix = chr(ord("a") + seq)
        return rename(client, name_id, f"name-{suffix}", expected_revision=1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(post, range(8)))

    codes = [resp.status_code for resp in responses]
    assert codes.count(200) == 1
    assert set(codes) <= {200, 409}
    assert not any(code >= 500 for code in codes)

    record = client.get("/namespaces/chat/names").json()[0]
    assert record["revision"] == 2  # 只有一次改名生效
    # 失败方拿到的 current_revision 都是 2，可用最新版本重试成功
    stale = [resp for resp in responses if resp.status_code == 409]
    assert all(resp.json()["detail"]["error"] == "stale_revision" for resp in stale)
    assert all(resp.json()["detail"]["current_revision"] == 2 for resp in stale)


def test_rename_concurrent_race_returns_409_with_current_revision(client, monkeypatch):
    """并发竞争：改名事务开始前另一连接已抢先改名 -> 409 stale_revision，不修改。"""
    created = register(client, "Alice").json()
    name_id = created["id"]
    real_rename_name = service.rename_name

    def racing_rename_name(conn, namespace, target_id, name, expected_revision):
        # 模拟并发改名：另一个连接在本事务前把 revision 提升到 2
        other = database.get_connection()
        try:
            other.execute(
                """
                UPDATE display_names
                   SET original_name = 'Racer', normalized_name = 'racer',
                       revision = revision + 1
                 WHERE id = ?
                """,
                (target_id,),
            )
            other.commit()
        finally:
            other.close()
        return real_rename_name(conn, namespace, target_id, name, expected_revision)

    monkeypatch.setattr(service, "rename_name", racing_rename_name)

    resp = rename(client, name_id, "Bob", expected_revision=1)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "stale_revision"
    assert detail["current_revision"] == 2

    # 抢先改名生效，本次请求未覆盖
    record = client.get("/namespaces/chat/names").json()[0]
    assert record["original_name"] == "Racer"
    assert record["revision"] == 2


def test_service_rename_is_atomic_in_single_immediate_transaction(client):
    """服务层：各失败分支都在同一事务内回滚，成功分支保留 id/created_at。"""
    conn = database.get_connection()
    try:
        record = service.register_name(conn, "chat", "Alice", "alice")
        name_id = record["id"]
        created_at = record["created_at"]

        # 预置另一条名称制造冲突
        service.insert_name(conn, "chat", "bob", "bob")

        # 版本过期
        with pytest.raises(service.StaleRevisionError):
            service.rename_name(conn, "chat", name_id, "x", expected_revision=99)
        # 混脚本
        with pytest.raises(service.MixedScriptNameError):
            service.rename_name(conn, "chat", name_id, MIXED_ADMIN, expected_revision=1)
        # 冲突（排除自身后与 bob 同规范名）
        with pytest.raises(service.NameConflictError) as exc_info:
            service.rename_name(conn, "chat", name_id, "BOB", expected_revision=1)
        assert exc_info.value.conflicts[0]["reason"] == "normalized_match"
        # 不存在 / 跨命名空间
        with pytest.raises(service.NameNotFoundError):
            service.rename_name(conn, "chat", 999, "x", expected_revision=1)
        with pytest.raises(service.NameNotFoundError):
            service.rename_name(conn, "other", name_id, "x", expected_revision=1)

        # 全部失败后记录原样
        rows = service.list_names(conn, "chat")
        assert [r["original_name"] for r in rows] == ["Alice", "bob"]
        assert rows[0]["revision"] == 1

        # 成功路径
        updated = service.rename_name(
            conn, "chat", name_id, "Alicia", expected_revision=1
        )
        assert updated["id"] == name_id
        assert updated["created_at"] == created_at
        assert updated["original_name"] == "Alicia"
        assert updated["normalized_name"] == "alicia"
        assert updated["revision"] == 2
    finally:
        conn.close()


def test_init_db_backfills_revision_on_legacy_database(tmp_path):
    """启动幂等迁移：老版本 display_names 无 revision 列时自动补齐为 1。"""
    db_path = tmp_path / "legacy.db"
    import sqlite3

    # 直接以老结构建库并写入两条记录
    with sqlite3.connect(db_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE display_names (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace       TEXT NOT NULL,
                original_name   TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                created_at      TEXT NOT NULL DEFAULT '',
                UNIQUE (namespace, normalized_name)
            )
            """
        )
        legacy.execute(
            "INSERT INTO display_names (namespace, original_name, normalized_name) "
            "VALUES ('chat', 'Alice', 'alice')"
        )
        legacy.execute(
            "INSERT INTO display_names (namespace, original_name, normalized_name) "
            "VALUES ('chat', 'Bob', 'bob')"
        )
        legacy.commit()

    # 启动迁移（幂等：连续执行两次）
    database.init_db(str(db_path))
    database.init_db(str(db_path))

    with sqlite3.connect(db_path) as migrated:
        cols = {row[1] for row in migrated.execute("PRAGMA table_info(display_names)")}
        assert "revision" in cols
        rows = migrated.execute(
            "SELECT id, revision, original_name FROM display_names ORDER BY id"
        ).fetchall()
        assert rows == [(1, 1, "Alice"), (2, 1, "Bob")]


def test_openapi_documents_rename_endpoint(client):
    operation = client.get("/openapi.json").json()["paths"][
        "/namespaces/{namespace}/names/{name_id}"
    ]["patch"]
    assert "200" in operation["responses"]
    assert "404" in operation["responses"]
    assert "409" in operation["responses"]
    assert "422" in operation["responses"]


# ---------- 原子交换 POST /namespaces/{namespace}/names/swap ----------


def swap(client, first, second, namespace="chat"):
    return client.post(
        f"/namespaces/{namespace}/names/swap",
        json={"records": [first, second]},
    )


def test_swap_success_swaps_name_pairs_bumps_both_revisions_keeps_ids_and_created_at(client):
    alice = register(client, "Alice").json()
    bob = register(client, "Ｂｏｂ").json()

    resp = swap(
        client,
        {"id": alice["id"], "expected_revision": 1},
        {"id": bob["id"], "expected_revision": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["namespace"] == "chat"
    assert [item["index"] for item in body["items"]] == [0, 1]

    first, second = body["items"]
    # 按请求顺序返回：id 不变、original_name 与 normalized_name 成对互换
    assert first["id"] == alice["id"]
    assert first["original_name"] == "Ｂｏｂ"
    assert first["normalized_name"] == "bob"
    assert first["revision"] == 2
    assert first["created_at"] == alice["created_at"]  # id 不变：沿用记录自身 created_at
    assert second["id"] == bob["id"]
    assert second["original_name"] == "Alice"
    assert second["normalized_name"] == "alice"
    assert second["revision"] == 2
    assert second["created_at"] == bob["created_at"]

    # 列表接口反映交换结果：仍是两条记录，id 与创建时间不变
    names = {n["id"]: n for n in client.get("/namespaces/chat/names").json()}
    assert names[alice["id"]]["original_name"] == "Ｂｏｂ"
    assert names[alice["id"]]["normalized_name"] == "bob"
    assert names[bob["id"]]["original_name"] == "Alice"
    assert names[bob["id"]]["normalized_name"] == "alice"

    # 再交换回来：双方 revision 继续各 + 1
    resp = swap(
        client,
        {"id": alice["id"], "expected_revision": 2},
        {"id": bob["id"], "expected_revision": 2},
    )
    assert resp.status_code == 200
    names = {n["id"]: n for n in client.get("/namespaces/chat/names").json()}
    assert names[alice["id"]]["original_name"] == "Alice"
    assert names[bob["id"]]["normalized_name"] == "bob"
    assert names[alice["id"]]["revision"] == 3
    assert names[bob["id"]]["revision"] == 3


def test_swap_request_order_is_respected_in_response_items(client):
    first = register(client, "Alice").json()
    second = register(client, "Bob").json()

    # 反向提交：items 顺序与 records 顺序一致
    resp = swap(
        client,
        {"id": second["id"], "expected_revision": 1},
        {"id": first["id"], "expected_revision": 1},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["id"] for item in items] == [second["id"], first["id"]]
    assert items[0]["original_name"] == "Alice"
    assert items[1]["original_name"] == "Bob"


def test_swap_stale_revision_returns_409_with_both_current_revisions(client):
    alice = register(client, "Alice").json()
    bob = register(client, "Bob").json()
    # 先把 alice 改一次名，revision 升到 2
    assert rename(client, alice["id"], "Alicia", expected_revision=1).status_code == 200

    # 持 alice 旧版本交换 -> 409，明细同时携带双方当前 revision
    resp = swap(
        client,
        {"id": alice["id"], "expected_revision": 1},
        {"id": bob["id"], "expected_revision": 1},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "stale_revision"
    records = {r["id"]: r for r in detail["records"]}
    assert records[alice["id"]]["expected_revision"] == 1
    assert records[alice["id"]]["current_revision"] == 2
    assert records[bob["id"]]["expected_revision"] == 1
    assert records[bob["id"]]["current_revision"] == 1

    # 交换未生效：双方名称与版本原样
    names = {n["id"]: n for n in client.get("/namespaces/chat/names").json()}
    assert names[alice["id"]]["original_name"] == "Alicia"
    assert names[bob["id"]]["original_name"] == "Bob"
    assert names[bob["id"]]["revision"] == 1

    # 双方都用最新版本重试 -> 成功
    resp = swap(
        client,
        {"id": alice["id"], "expected_revision": 2},
        {"id": bob["id"], "expected_revision": 1},
    )
    assert resp.status_code == 200


def test_swap_unknown_id_returns_404_with_missing_ids(client):
    alice = register(client, "Alice").json()

    resp = swap(
        client,
        {"id": alice["id"], "expected_revision": 1},
        {"id": 999, "expected_revision": 1},
    )
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["error"] == "name_not_found"
    assert detail["missing_ids"] == [999]

    # 两条都不存在
    resp = swap(
        client,
        {"id": 123, "expected_revision": 1},
        {"id": 456, "expected_revision": 1},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["missing_ids"] == [123, 456]


def test_swap_id_from_other_namespace_returns_404_and_changes_nothing(client):
    alice = register(client, "Alice", namespace="a").json()
    bob = register(client, "Bob", namespace="b").json()

    # 跨命名空间记录：在 a 下交换 a 的记录与 b 的记录 -> 404
    resp = swap(
        client,
        {"id": alice["id"], "expected_revision": 1},
        {"id": bob["id"], "expected_revision": 1},
        namespace="a",
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["missing_ids"] == [bob["id"]]

    # 双方数据均未变化
    assert client.get("/namespaces/a/names").json()[0]["original_name"] == "Alice"
    assert client.get("/namespaces/b/names").json()[0]["original_name"] == "Bob"
    assert client.get("/namespaces/a/names").json()[0]["revision"] == 1


def test_swap_same_id_rejected(client):
    alice = register(client, "Alice").json()

    resp = swap(
        client,
        {"id": alice["id"], "expected_revision": 1},
        {"id": alice["id"], "expected_revision": 1},
    )
    assert resp.status_code == 422


def test_swap_body_shape_validated(client):
    # records 必须恰好两条
    assert client.post(
        "/namespaces/chat/names/swap",
        json={"records": [{"id": 1, "expected_revision": 1}]},
    ).status_code == 422
    assert client.post(
        "/namespaces/chat/names/swap",
        json={
            "records": [
                {"id": i, "expected_revision": 1} for i in (1, 2, 3)
            ]
        },
    ).status_code == 422
    # 缺字段 / revision 非正整数
    assert client.post(
        "/namespaces/chat/names/swap", json={"records": [{"id": 1}, {"id": 2}]}
    ).status_code == 422
    assert client.post(
        "/namespaces/chat/names/swap",
        json={"records": [{"id": 1, "expected_revision": 0}, {"id": 2, "expected_revision": 1}]},
    ).status_code == 422
    assert client.post("/namespaces/chat/names/swap", json={}).status_code == 422


def test_swap_does_not_leave_or_expose_temp_names_and_keeps_namespace_consistent(client):
    """交换后库中不存在临时名，同空间名称集合保持一致，可正常继续查重。"""
    alice = register(client, "Alice").json()
    bob = register(client, "Bob").json()

    assert swap(
        client,
        {"id": alice["id"], "expected_revision": 1},
        {"id": bob["id"], "expected_revision": 1},
    ).status_code == 200

    conn = database.get_connection()
    try:
        # 无任何临时名残留
        leaked = conn.execute(
            "SELECT COUNT(*) FROM display_names WHERE normalized_name LIKE '__swap__/%'"
        ).fetchone()[0]
        assert leaked == 0
        # 占用的规范名集合不变：仍是 alice / bob 各一条
        rows = conn.execute(
            "SELECT normalized_name, COUNT(*) AS c FROM display_names "
            "WHERE namespace = 'chat' GROUP BY normalized_name"
        ).fetchall()
        assert sorted((r["normalized_name"], r["c"]) for r in rows) == [
            ("alice", 1),
            ("bob", 1),
        ]
    finally:
        conn.close()

    # 交换后查重语义不变：旧规范名仍在（只是换了记录），新同名依旧 409
    assert register(client, "ALICE").status_code == 409
    assert register(client, "BOB").status_code == 409


def test_swap_is_independent_of_reservations(client):
    """交换只在已注册名称之间进行；有效预占不受影响，也不参与交换判定。"""
    alice = register(client, "Alice").json()
    bob = register(client, "Bob").json()
    reservation = reserve(client, "carol").json()

    assert swap(
        client,
        {"id": alice["id"], "expected_revision": 1},
        {"id": bob["id"], "expected_revision": 1},
    ).status_code == 200

    # 预占仍然有效占位
    resp = register(client, "CAROL")
    assert resp.status_code == 409
    assert resp.json()["detail"]["conflicts"][0]["id"] == reservation["id"]


def test_swap_concurrent_requests_exactly_one_wins(client):
    """并发交换同一对记录：写锁串行化，恰好一次成功，其余 409 stale，无 5xx。"""
    alice = register(client, "Alice").json()
    bob = register(client, "Bob").json()

    def post(_):
        return swap(
            client,
            {"id": alice["id"], "expected_revision": 1},
            {"id": bob["id"], "expected_revision": 1},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(post, range(8)))

    codes = [resp.status_code for resp in responses]
    assert codes.count(200) == 1
    assert set(codes) <= {200, 409}
    assert not any(code >= 500 for code in codes)

    names = {n["id"]: n for n in client.get("/namespaces/chat/names").json()}
    # 双方各只被交换一次：revision 都是 2，名称恰好互换
    assert names[alice["id"]]["revision"] == 2
    assert names[bob["id"]]["revision"] == 2
    assert names[alice["id"]]["normalized_name"] == "bob"
    assert names[bob["id"]]["normalized_name"] == "alice"
    # 失败方拿到的 409 明细都含双方当前 revision（2 / 2）
    stale = [resp for resp in responses if resp.status_code == 409]
    for resp in stale:
        detail = resp.json()["detail"]
        assert detail["error"] == "stale_revision"
        current = {r["id"]: r["current_revision"] for r in detail["records"]}
        assert current == {alice["id"]: 2, bob["id"]: 2}


def test_swap_concurrent_with_rename_exactly_one_wins(client):
    """并发交换与改名竞争同一记录：写锁串行化，无 5xx、无部分更新、无临时名残留。"""
    alice = register(client, "Alice").json()
    bob = register(client, "Bob").json()

    def do_swap(_):
        return swap(
            client,
            {"id": alice["id"], "expected_revision": 1},
            {"id": bob["id"], "expected_revision": 1},
        )

    def do_rename(_):
        return rename(client, alice["id"], "Alicia", expected_revision=1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        swap_responses = list(pool.map(do_swap, range(4)))
        rename_responses = list(pool.map(do_rename, range(4)))

    all_responses = swap_responses + rename_responses
    codes = [resp.status_code for resp in all_responses]
    assert set(codes) <= {200, 409}
    assert not any(code >= 500 for code in codes)

    names = {n["id"]: n for n in client.get("/namespaces/chat/names").json()}
    # alice 只被修改一次（交换或改名二选一胜出），revision 恰为 2
    assert names[alice["id"]]["revision"] == 2
    # 两条记录的规范名仍然互不相同且唯一（无临时名、无重复）
    normalized = [n["normalized_name"] for n in names.values()]
    assert len(normalized) == len(set(normalized))


def test_swap_concurrent_race_returns_409_with_both_revisions(client, monkeypatch):
    """并发竞争：交换事务开始前另一连接已抢先改名 -> 409 stale，双方版本齐全。"""
    alice = register(client, "Alice").json()
    bob = register(client, "Bob").json()
    real_swap_names = service.swap_names

    def racing_swap_names(conn, namespace, entries):
        # 模拟并发写入：另一个连接在交换事务前把 alice 的 revision 提升到 2
        other = database.get_connection()
        try:
            other.execute(
                """
                UPDATE display_names
                   SET original_name = 'Racer', normalized_name = 'racer',
                       revision = revision + 1
                 WHERE id = ?
                """,
                (alice["id"],),
            )
            other.commit()
        finally:
            other.close()
        return real_swap_names(conn, namespace, entries)

    monkeypatch.setattr(service, "swap_names", racing_swap_names)

    resp = swap(
        client,
        {"id": alice["id"], "expected_revision": 1},
        {"id": bob["id"], "expected_revision": 1},
    )
    assert resp.status_code == 409
    current = {r["id"]: r["current_revision"] for r in resp.json()["detail"]["records"]}
    assert current == {alice["id"]: 2, bob["id"]: 1}

    # 抢先改名生效，交换未发生且 bob 未被修改
    names = {n["id"]: n for n in client.get("/namespaces/chat/names").json()}
    assert names[alice["id"]]["original_name"] == "Racer"
    assert names[bob["id"]]["original_name"] == "Bob"
    assert names[bob["id"]]["revision"] == 1


def test_service_swap_is_atomic_in_single_immediate_transaction(client):
    """服务层：各失败分支都整体回滚，成功分支成对互换且保留 id/created_at。"""
    conn = database.get_connection()
    try:
        alice = service.register_name(conn, "chat", "Alice", "alice")
        bob = service.register_name(conn, "chat", "Ｂｏｂ", "bob")

        # 任一记录不存在 / 跨命名空间 -> SwapRecordNotFoundError，数据不变
        with pytest.raises(service.SwapRecordNotFoundError) as exc_info:
            service.swap_names(
                conn, "chat", [(alice["id"], 1), (999, 1)]
            )
        assert exc_info.value.missing_ids == [999]
        with pytest.raises(service.SwapRecordNotFoundError):
            service.swap_names(conn, "other", [(alice["id"], 1), (bob["id"], 1)])

        # 任一方版本过期 -> SwapStaleRevisionError，明细含双方当前版本
        with pytest.raises(service.SwapStaleRevisionError) as exc_info:
            service.swap_names(conn, "chat", [(alice["id"], 99), (bob["id"], 1)])
        current = {r["id"]: r["current_revision"] for r in exc_info.value.records}
        assert current == {alice["id"]: 1, bob["id"]: 1}

        # 全部失败后记录原样：无临时名、revision 不变
        rows = service.list_names(conn, "chat")
        assert [(r["original_name"], r["revision"]) for r in rows] == [
            ("Alice", 1),
            ("Ｂｏｂ", 1),
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM display_names WHERE normalized_name LIKE '__swap__/%'"
        ).fetchone()[0] == 0

        # 成功路径：成对互换、revision 各 + 1、id/created_at 不变
        updated = service.swap_names(
            conn, "chat", [(alice["id"], 1), (bob["id"], 1)]
        )
        by_id = {r["id"]: r for r in updated}
        assert by_id[alice["id"]]["original_name"] == "Ｂｏｂ"
        assert by_id[alice["id"]]["normalized_name"] == "bob"
        assert by_id[alice["id"]]["revision"] == 2
        assert by_id[alice["id"]]["created_at"] == alice["created_at"]
        assert by_id[bob["id"]]["original_name"] == "Alice"
        assert by_id[bob["id"]]["normalized_name"] == "alice"
        assert by_id[bob["id"]]["revision"] == 2
        assert by_id[bob["id"]]["created_at"] == bob["created_at"]
        # 全程无临时名残留
        assert conn.execute(
            "SELECT COUNT(*) FROM display_names WHERE normalized_name LIKE '__swap__/%'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_openapi_documents_swap_endpoint(client):
    operation = client.get("/openapi.json").json()["paths"][
        "/namespaces/{namespace}/names/swap"
    ]["post"]
    assert "200" in operation["responses"]
    assert "404" in operation["responses"]
    assert "409" in operation["responses"]
    assert "422" in operation["responses"]


# ---------- 命名空间混脚本策略 GET/PUT /namespaces/{namespace}/script-policy ----------


DEFAULT_PAIRS = [["Cyrillic", "Greek"], ["Cyrillic", "Latin"], ["Greek", "Latin"]]


def get_policy(client, namespace="chat"):
    return client.get(f"/namespaces/{namespace}/script-policy")


def put_policy(client, pairs, expected_revision, namespace="chat"):
    return client.put(
        f"/namespaces/{namespace}/script-policy",
        json={
            "blocked_script_pairs": pairs,
            "expected_revision": expected_revision,
        },
    )


def test_get_policy_unconfigured_returns_404_with_default_hint(client):
    resp = get_policy(client)
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["error"] == "script_policy_not_found"


def test_put_policy_initial_creates_revision_1(client):
    resp = put_policy(client, [["Cyrillic", "Latin"]], expected_revision=0)
    assert resp.status_code == 200
    body = resp.json()
    assert body["namespace"] == "chat"
    assert body["blocked_script_pairs"] == [["Cyrillic", "Latin"]]
    assert body["revision"] == 1
    assert body["created_at"]
    assert body["updated_at"]

    # 立即可读回
    resp = get_policy(client)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 1
    assert resp.json()["blocked_script_pairs"] == [["Cyrillic", "Latin"]]


def test_put_policy_pairs_are_order_insensitive_and_deduplicated(client):
    # 反向输入与重复对在服务端规范化：按字典序排列、去重
    resp = put_policy(
        client,
        [
            ["Cyrillic", "Latin"],
            ["Latin", "Cyrillic"],   # 与第一对等价（顺序相反）
            ["Latin", "Greek"],      # 规范化为 ["Greek", "Latin"]
        ],
        expected_revision=0,
    )
    assert resp.status_code == 200
    assert resp.json()["blocked_script_pairs"] == [
        ["Cyrillic", "Latin"],
        ["Greek", "Latin"],
    ]


def test_put_policy_empty_pairs_means_allow_all(client):
    resp = put_policy(client, [], expected_revision=0)
    assert resp.status_code == 200
    assert resp.json()["blocked_script_pairs"] == []
    assert resp.json()["revision"] == 1

    # 空策略与“未配置”不同：GET 仍 200，且默认拦截的 Latin+Cyrillic 现在放行
    assert get_policy(client).status_code == 200
    assert register(client, MIXED_ADMIN).status_code == 201


def test_put_policy_increments_revision_on_each_success(client):
    assert put_policy(client, [], 0).json()["revision"] == 1
    assert put_policy(client, [["Cyrillic", "Latin"]], 1).json()["revision"] == 2
    assert put_policy(client, [["Greek", "Latin"]], 2).json()["revision"] == 3
    assert get_policy(client).json()["revision"] == 3


def test_put_policy_wrong_revision_returns_409_with_current_revision(client):
    # 未配置时传非 0 -> 409，current_revision=0，策略未创建
    resp = put_policy(client, [], expected_revision=3)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "stale_revision"
    assert detail["current_revision"] == 0
    assert get_policy(client).status_code == 404

    # 已配置后持旧版本 -> 409，附当前 revision，策略不更新
    assert put_policy(client, [], expected_revision=0).status_code == 200
    resp = put_policy(client, [["Cyrillic", "Latin"]], expected_revision=1)
    assert resp.status_code == 200
    resp = put_policy(client, [["Greek", "Latin"]], expected_revision=1)
    assert resp.status_code == 409
    assert resp.json()["detail"]["current_revision"] == 2
    # 更新未生效：仍是 revision 2 时写入的策略
    current = get_policy(client).json()
    assert current["revision"] == 2
    assert current["blocked_script_pairs"] == [["Cyrillic", "Latin"]]


def test_put_policy_violating_registered_name_returns_409_with_details(client):
    created = register(client, "世界-x").json()  # Han + Latin，默认策略下放行

    # 新策略阻断 Han+Latin：现存名称违规 -> 409，逐条给出明细
    resp = put_policy(client, [["Han", "Latin"]], expected_revision=0)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "script_policy_conflict"
    (violation,) = detail["violations"]
    assert violation["id"] == created["id"]
    assert violation["kind"] == "registered"
    assert violation["original_name"] == "世界-x"
    assert violation["normalized_name"] == "世界-x"
    assert violation["violating_script_pairs"] == [["Han", "Latin"]]
    scripts = {d["script"] for d in violation["script_details"]}
    assert scripts == {"Han", "Latin"}

    # 409 不更新策略：GET 仍 404（未配置），默认行为不变
    assert get_policy(client).status_code == 404
    # 另一个 Han+Latin 名称依旧可以注册（策略未生效）
    assert register(client, "世界-y").status_code == 201


def test_put_policy_violating_active_reservation_blocks_update(client):
    # 默认策略下放行 Han+Latin：预占一个，再尝试收紧策略 -> 409，命中有效预占
    reservation = reserve(client, "世界-r", ttl_seconds=120).json()

    resp = put_policy(client, [["Han", "Latin"]], expected_revision=0)
    assert resp.status_code == 409
    (violation,) = resp.json()["detail"]["violations"]
    assert violation["kind"] == "reservation"
    assert violation["id"] == reservation["id"]
    assert violation["original_name"] == "世界-r"
    assert violation["expires_at"] == reservation["expires_at"]
    assert violation["violating_script_pairs"] == [["Han", "Latin"]]
    # 策略未创建
    assert get_policy(client).status_code == 404


def test_put_policy_purges_expired_reservations_before_scan(client):
    reserve(client, "世界-r", ttl_seconds=30)
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_reservations SET expires_at = '2000-01-01T00:00:00.000Z'"
        )
        conn.commit()
    finally:
        conn.close()

    # 过期预占在写事务内先惰性清理，不再阻止策略收紧
    resp = put_policy(client, [["Han", "Latin"]], expected_revision=0)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 1
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM name_reservations").fetchone()[0] == 0
    finally:
        conn.close()


def test_put_policy_scan_sorts_registered_before_reservations_by_id(client):
    register(client, "世界-a")
    register(client, "世界-b")
    reserve(client, "世界-c")
    resp = put_policy(client, [["Han", "Latin"]], expected_revision=0)
    assert resp.status_code == 409
    violations = resp.json()["detail"]["violations"]
    assert [v["kind"] for v in violations] == [
        "registered",
        "registered",
        "reservation",
    ]


def test_put_policy_after_renaming_violating_name_succeeds(client):
    # 改名使现存数据合规后，同一策略可以写入
    created = register(client, "世界-x").json()
    assert put_policy(client, [["Han", "Latin"]], 0).status_code == 409

    # 改成纯 Han 名称（去掉 Latin 字符与默认脚本字符），revision 升到 2
    assert rename(client, created["id"], "世界", expected_revision=1).status_code == 200
    resp = put_policy(client, [["Han", "Latin"]], expected_revision=0)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 1


def test_put_policy_invalid_pairs_returns_422_and_persists_nothing(client):
    # 未知脚本名
    assert put_policy(client, [["Latin", "Klingon"]], 0).status_code == 422
    # 大小写必须与 Unicode Script 属性值一致
    assert put_policy(client, [["latin", "cyrillic"]], 0).status_code == 422
    # Common / Inherited / Unknown 不是实际文字脚本
    assert put_policy(client, [["Latin", "Common"]], 0).status_code == 422
    assert put_policy(client, [["Latin", "Inherited"]], 0).status_code == 422
    # 同名对无意义
    assert put_policy(client, [["Latin", "Latin"]], 0).status_code == 422
    # 项长度不是 2
    assert put_policy(client, [["Latin"]], 0).status_code == 422
    assert put_policy(client, [["Latin", "Greek", "Han"]], 0).status_code == 422
    # 超过对数上限
    assert put_policy(
        client, [["Latin", f"Han{i}"] for i in range(501)], 0
    ).status_code == 422
    # expected_revision 为负数
    assert client.put(
        "/namespaces/chat/script-policy",
        json={"blocked_script_pairs": [], "expected_revision": -1},
    ).status_code == 422
    # 缺少字段
    assert client.put(
        "/namespaces/chat/script-policy", json={"blocked_script_pairs": []}
    ).status_code == 422
    assert get_policy(client).status_code == 404


def test_put_policy_is_namespace_scoped(client):
    assert register(client, "世界-x", namespace="a").status_code == 201
    # b 空间无数据：可以直接配置严格策略
    assert put_policy(client, [["Han", "Latin"]], 0, namespace="b").status_code == 200
    # a 空间未受影响：策略不存在
    assert get_policy(client, namespace="a").status_code == 404
    # a 空间仍可注册 Han+Latin，b 空间被策略拦截
    assert register(client, "世界-y", namespace="a").status_code == 201
    assert register(client, "世界-z", namespace="b").status_code == 422


def test_put_policy_concurrent_initial_creates_exactly_one_winner(client):
    def put(_):
        return put_policy(client, [], expected_revision=0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(put, range(8)))

    codes = [r.status_code for r in responses]
    assert codes.count(200) == 1
    assert set(codes) <= {200, 409}
    assert not any(code >= 500 for code in codes)
    body = get_policy(client).json()
    assert body["revision"] == 1


def test_put_policy_concurrent_updates_exactly_one_wins(client):
    assert put_policy(client, [], 0).status_code == 200

    def put(_):
        return put_policy(client, [["Cyrillic", "Latin"]], expected_revision=1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(put, range(8)))

    codes = [r.status_code for r in responses]
    assert codes.count(200) == 1
    assert all(
        r.json()["detail"]["current_revision"] == 2
        for r in responses
        if r.status_code == 409
    )
    assert get_policy(client).json()["revision"] == 2


# ---------- 策略对注册 / 批量 / 预占 / 改名 / check 的判定 ----------


def test_check_reports_effective_policy_fields(client):
    # 未配置：返回内建默认策略
    body = client.post("/namespaces/chat/check", json={"name": MIXED_ADMIN}).json()
    assert body["blocked_script_pairs"] == DEFAULT_PAIRS
    assert body["violating_script_pairs"] == [["Cyrillic", "Latin"]]
    assert body["mixed_script_violation"] is True

    # 配置自定义策略后按新策略判定：只阻断 Latin+Greek 时 Latin+Cyrillic 放行
    assert put_policy(client, [["Greek", "Latin"]], 0).status_code == 200
    body = client.post("/namespaces/chat/check", json={"name": MIXED_ADMIN}).json()
    assert body["blocked_script_pairs"] == [["Greek", "Latin"]]
    assert body["violating_script_pairs"] == []
    assert body["mixed_script_violation"] is False


def test_register_uses_namespace_policy(client):
    # 只阻断 Han+Latin：Latin+Cyrillic 混写放行，Han+Latin 拦截
    assert put_policy(client, [["Han", "Latin"]], 0).status_code == 200
    assert register(client, MIXED_ADMIN).status_code == 201

    resp = register(client, "世界x")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "mixed_script_violation"
    assert detail["violating_script_pairs"] == [["Han", "Latin"]]
    assert detail["blocked_script_pairs"] == [["Han", "Latin"]]
    assert set(detail["restricted_scripts_used"]) == {"Latin"}  # 历史字段仍返回


def test_reserve_uses_namespace_policy(client):
    assert put_policy(client, [["Han", "Latin"]], 0).status_code == 200
    assert reserve(client, MIXED_ADMIN).status_code == 201
    resp = reserve(client, "世界x")
    assert resp.status_code == 422
    assert resp.json()["detail"]["violating_script_pairs"] == [["Han", "Latin"]]


def test_batch_uses_namespace_policy(client):
    assert put_policy(client, [["Han", "Latin"]], 0).status_code == 200
    resp = register_batch(client, ["ok", MIXED_ADMIN, "世界x"])
    # MIXED_ADMIN（Latin+Cyrillic）按自定义策略放行；只有 Han+Latin 项违规
    assert resp.status_code == 422
    items = {item["index"]: item for item in resp.json()["detail"]["items"]}
    assert set(items) == {2}
    assert items[2]["violating_script_pairs"] == [["Han", "Latin"]]
    assert client.get("/namespaces/chat/names").json() == []


def test_rename_uses_namespace_policy(client):
    created = register(client, "alice").json()
    assert put_policy(client, [["Han", "Latin"]], 0).status_code == 200

    resp = rename(client, created["id"], "世界x", expected_revision=1)
    assert resp.status_code == 422
    assert resp.json()["detail"]["violating_script_pairs"] == [["Han", "Latin"]]

    # 改成合规名成功
    assert rename(client, created["id"], "bob", expected_revision=1).status_code == 200


def test_claim_of_reservation_created_under_old_strict_policy_still_enforced(client):
    """有效预占在其创建时已通过当时的策略；之后策略放宽，确认照常成功。"""
    # 默认策略下 Latin+Cyrillic 预占会被拒；先放宽，预占成功，再确认
    assert put_policy(client, [], 0).status_code == 200
    token = reserve(client, MIXED_ADMIN).json()["token"]
    assert claim(client, token).status_code == 201
    body = client.get("/namespaces/chat/names").json()[0]
    assert body["original_name"] == MIXED_ADMIN


def test_policy_tightening_concurrent_with_register_returns_422_and_persists_nothing(
    client, monkeypatch
):
    """并发策略收紧与注册竞争：只读分析通过、写事务复查前策略被收紧 -> 422，不落库。"""
    real_register_name = service.register_name

    def racing_register_name(conn, namespace, name, normalized_name):
        # 未配置默认策略放行 Han+Latin；另一连接在注册写事务复查前收紧策略
        other = database.get_connection()
        try:
            other.execute(
                "INSERT INTO namespace_script_policies "
                "(namespace, blocked_script_pairs, revision) VALUES (?, ?, 1)",
                ("chat", '[["Han","Latin"]]'),
            )
            other.commit()
        finally:
            other.close()
        return real_register_name(conn, namespace, name, normalized_name)

    monkeypatch.setattr(service, "register_name", racing_register_name)
    resp = register(client, "世界x")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "mixed_script_violation"
    assert resp.json()["detail"]["violating_script_pairs"] == [["Han", "Latin"]]
    assert client.get("/namespaces/chat/names").json() == []


def test_policy_tightening_concurrent_with_batch_returns_409_with_items(
    client, monkeypatch
):
    """批次只读分析通过、写事务复查前策略被并发收紧 -> 整批回滚 -> 409。"""
    real_insert_batch = service.insert_batch

    def racing_insert_batch(conn, namespace, names):
        other = database.get_connection()
        try:
            other.execute(
                "INSERT INTO namespace_script_policies "
                "(namespace, blocked_script_pairs, revision) VALUES (?, ?, 1)",
                ("chat", '[["Han","Latin"]]'),
            )
            other.commit()
        finally:
            other.close()
        return real_insert_batch(conn, namespace, names)

    monkeypatch.setattr(service, "insert_batch", racing_insert_batch)
    resp = register_batch(client, ["ok", "世界x"])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "mixed_script_violation"
    assert [item["index"] for item in detail["items"]] == [1]
    assert detail["items"][0]["violating_script_pairs"] == [["Han", "Latin"]]
    assert client.get("/namespaces/chat/names").json() == []


def test_policy_tightening_concurrent_with_reservation_returns_422(
    client, monkeypatch
):
    real_create_reservation = service.create_reservation

    def racing_create_reservation(conn, namespace, name, ttl_seconds):
        other = database.get_connection()
        try:
            other.execute(
                "INSERT INTO namespace_script_policies "
                "(namespace, blocked_script_pairs, revision) VALUES (?, ?, 1)",
                ("chat", '[["Han","Latin"]]'),
            )
            other.commit()
        finally:
            other.close()
        return real_create_reservation(conn, namespace, name, ttl_seconds)

    monkeypatch.setattr(service, "create_reservation", racing_create_reservation)
    resp = reserve(client, "世界x")
    assert resp.status_code == 422
    assert resp.json()["detail"]["violating_script_pairs"] == [["Han", "Latin"]]
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM name_reservations").fetchone()[0] == 0
    finally:
        conn.close()


# ---------- 合并时按目标策略检查源、目标两侧 ----------


def test_merge_blocked_when_source_names_violate_target_policy(client):
    # 源空间 a 放宽策略（允许 Latin+Cyrillic）并存入混写名称
    assert put_policy(client, [], 0, namespace="a").status_code == 200
    assert register(client, MIXED_ADMIN, namespace="a").status_code == 201
    # 目标空间 b 保持默认策略（阻断 Latin+Cyrillic）
    assert register(client, "zzz", namespace="b").status_code == 201

    resp = merge(client, "a", "b")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "merge_conflict"
    assert detail["conflicts"] == []
    (violation,) = detail["script_violations"]
    assert violation["side"] == "source"
    assert violation["record"]["kind"] == "registered"
    assert violation["record"]["original_name"] == MIXED_ADMIN
    assert violation["violating_script_pairs"] == [["Cyrillic", "Latin"]]

    # 两侧数据均未变化
    assert client.get("/namespaces/a/names").json()[0]["original_name"] == MIXED_ADMIN
    assert client.get("/namespaces/b/names").json()[0]["original_name"] == "zzz"


def test_merge_blocked_when_source_reservation_violates_target_policy(client):
    assert put_policy(client, [], 0, namespace="a").status_code == 200
    reservation = reserve(client, MIXED_ADMIN, namespace="a").json()

    resp = merge(client, "a", "b")
    assert resp.status_code == 409
    (violation,) = resp.json()["detail"]["script_violations"]
    assert violation["side"] == "source"
    assert violation["record"]["kind"] == "reservation"
    assert violation["record"]["id"] == reservation["id"]
    assert violation["record"]["expires_at"] == reservation["expires_at"]


def test_merge_into_relaxed_target_succeeds_even_with_mixed_source(client):
    # 目标 a 显式放行全部组合；源 b 的 Latin+Cyrillic 混写名称可迁入
    assert put_policy(client, [], 0, namespace="a").status_code == 200
    # b 也需放行才能注册混写名称
    assert put_policy(client, [], 0, namespace="b").status_code == 200
    assert register(client, MIXED_ADMIN, namespace="b").status_code == 201

    resp = merge(client, "b", "a")
    assert resp.status_code == 200
    assert resp.json()["merged_names"] == 1
    names = client.get("/namespaces/a/names").json()
    assert names[0]["original_name"] == MIXED_ADMIN


def test_merge_reports_both_conflicts_and_script_violations(client):
    # 同规范名冲突 + 源侧混脚本违规同时存在：一次 409 同时返回两类明细
    assert put_policy(client, [], 0, namespace="a").status_code == 200
    assert register(client, MIXED_ADMIN, namespace="a").status_code == 201  # 混写
    assert register(client, "alice", namespace="a").status_code == 201
    assert register(client, "ALICE", namespace="b").status_code == 201  # 规范名冲突

    resp = merge(client, "a", "b")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["conflicts"][0]["reason"] == "normalized_match"
    assert detail["script_violations"][0]["side"] == "source"
    assert detail["script_violations"][0]["record"]["original_name"] == MIXED_ADMIN


def test_merge_scans_target_side_against_target_policy(client):
    """目标侧在不变量下通常合规；目标存在显式空策略时迁入照常成功。"""
    assert register(client, "alice", namespace="a").status_code == 201
    # 目标 b 显式配置默认三对策略（显式配置而非未配置），其自身名称合规
    assert put_policy(
        client,
        [["Cyrillic", "Greek"], ["Cyrillic", "Latin"], ["Greek", "Latin"]],
        0,
        namespace="b",
    ).status_code == 200
    assert register(client, "bob", namespace="b").status_code == 201
    resp = merge(client, "a", "b")
    assert resp.status_code == 200
    # 扫描了两侧但没有任何违规
    assert {n["original_name"] for n in client.get("/namespaces/b/names").json()} == {
        "alice",
        "bob",
    }


def test_merge_concurrent_policy_tightening_returns_409_with_violations(client, monkeypatch):
    """合并事务复查前目标空间策略被并发收紧 -> 整体回滚，重新检测后 409。"""
    assert put_policy(client, [], 0, namespace="a").status_code == 200
    assert register(client, MIXED_ADMIN, namespace="a").status_code == 201
    real_merge_namespaces = service.merge_namespaces

    def racing_merge_namespaces(conn, source_namespace, target_namespace):
        other = database.get_connection()
        try:
            # 目标 b 在合并事务前被配置为阻断 Latin+Cyrillic
            other.execute(
                "INSERT INTO namespace_script_policies "
                "(namespace, blocked_script_pairs, revision) VALUES (?, ?, 1)",
                ("b", '[["Cyrillic","Latin"]]'),
            )
            other.commit()
        finally:
            other.close()
        return real_merge_namespaces(conn, source_namespace, target_namespace)

    monkeypatch.setattr(service, "merge_namespaces", racing_merge_namespaces)

    resp = merge(client, "a", "b")
    assert resp.status_code == 409
    violations = resp.json()["detail"]["script_violations"]
    assert violations and violations[0]["side"] == "source"
    assert violations[0]["record"]["original_name"] == MIXED_ADMIN
    # 整体回滚：源侧记录未迁移
    assert client.get("/namespaces/a/names").json()[0]["original_name"] == MIXED_ADMIN


def test_init_db_creates_policy_table_idempotently(tmp_path):
    """新库自动建策略表，重复初始化不报错。"""
    db_path = tmp_path / "policy.db"
    database.init_db(str(db_path))
    database.init_db(str(db_path))
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "namespace_script_policies" in tables


def test_openapi_documents_script_policy_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    put_op = paths["/namespaces/{namespace}/script-policy"]["put"]
    get_op = paths["/namespaces/{namespace}/script-policy"]["get"]
    assert "200" in put_op["responses"]
    assert "409" in put_op["responses"]
    assert "422" in put_op["responses"]
    assert "200" in get_op["responses"]
    assert "404" in get_op["responses"]


# ---------- 名称释放冷却 DELETE /namespaces/{namespace}/names/{id} ----------


def release(client, name_id, expected_revision, cooldown_seconds=3600, namespace="chat"):
    return client.request(
        "DELETE",
        f"/namespaces/{namespace}/names/{name_id}",
        json={
            "expected_revision": expected_revision,
            "cooldown_seconds": cooldown_seconds,
        },
    )


def list_cooldowns(client, namespace="chat"):
    return client.get(f"/namespaces/{namespace}/cooldowns")


def test_release_deletes_name_and_returns_cooldown_record(client):
    created = register(client, "Alice").json()

    resp = release(client, created["id"], expected_revision=1, cooldown_seconds=3600)
    assert resp.status_code == 200
    body = resp.json()
    assert body["namespace"] == "chat"
    assert body["original_name"] == "Alice"
    assert body["normalized_name"] == "alice"
    assert body["cooldown_seconds"] == 3600
    assert body["expires_at"].endswith("Z")
    assert body["created_at"]
    assert body["id"] >= 1

    # 已注册名称已删除
    assert client.get("/namespaces/chat/names").json() == []
    # 冷却记录可列出
    cooldowns = list_cooldowns(client).json()
    assert len(cooldowns) == 1
    assert cooldowns[0]["id"] == body["id"]
    assert cooldowns[0]["expires_at"] == body["expires_at"]


def test_release_cooldown_bounds_validated(client):
    # 边界值 60 / 86400 都接受（每次删除一个不同的名称）
    low = register(client, "low").json()
    assert release(client, low["id"], 1, cooldown_seconds=60).status_code == 200
    high = register(client, "high").json()
    assert release(client, high["id"], 1, cooldown_seconds=86400).status_code == 200

    # 越界 / 非正 revision -> 422，名称不删除
    bad = register(client, "bad").json()
    assert release(client, bad["id"], 1, cooldown_seconds=59).status_code == 422
    assert release(client, bad["id"], 1, cooldown_seconds=86401).status_code == 422
    assert release(client, bad["id"], 1, cooldown_seconds=0).status_code == 422
    assert release(client, bad["id"], 0, cooldown_seconds=60).status_code == 422
    # 缺少字段 -> 422
    assert client.request(
        "DELETE", f"/namespaces/chat/names/{bad['id']}", json={"expected_revision": 1}
    ).status_code == 422
    # 名称未被删除、revision 不变
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["bad"]
    assert names[0]["revision"] == 1


def test_release_unknown_or_other_namespace_id_returns_404(client):
    resp = release(client, 999, expected_revision=1)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "name_not_found"

    created = register(client, "Alice", namespace="a").json()
    resp = release(client, created["id"], expected_revision=1, namespace="b")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "name_not_found"
    # 原空间记录仍在
    assert register(client, "alice", namespace="a").status_code == 409


def test_release_stale_revision_returns_409_with_current_revision(client):
    created = register(client, "Alice").json()
    assert rename(client, created["id"], "Alicia", expected_revision=1).status_code == 200

    resp = release(client, created["id"], expected_revision=1)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "stale_revision"
    assert detail["current_revision"] == 2

    # 未删除：用最新版本可以释放
    assert release(client, created["id"], expected_revision=2).status_code == 200
    assert client.get("/namespaces/chat/names").json() == []


def test_release_then_patch_or_second_delete_of_same_id_returns_404(client):
    created = register(client, "Alice").json()
    assert release(client, created["id"], 1).status_code == 200

    assert rename(client, created["id"], "Bob", expected_revision=1).status_code == 404
    assert release(client, created["id"], expected_revision=1).status_code == 404


def test_active_cooldown_blocks_reregister_same_normalized_name(client):
    created = register(client, "Alice").json()
    cooldown = release(client, created["id"], 1, cooldown_seconds=3600).json()

    resp = register(client, "ALICE")
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["reason"] == "normalized_match"
    assert conflict["source"] == "released"
    assert conflict["original_name"] == "Alice"
    assert conflict["expires_at"] == cooldown["expires_at"]
    # 注册未落库
    assert client.get("/namespaces/chat/names").json() == []


def test_active_cooldown_blocks_reregister_confusable(client):
    created = register(client, "app").json()
    release(client, created["id"], 1)

    resp = register(client, CYRILLIC_APP)
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["reason"] == "confusable"
    assert conflict["source"] == "released"


def test_active_cooldown_blocks_new_reservation(client):
    created = register(client, "app").json()
    release(client, created["id"], 1)

    resp = reserve(client, CYRILLIC_APP)
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["source"] == "released"
    assert conflict["reason"] == "confusable"


def test_active_cooldown_blocks_batch_register(client):
    created = register(client, "app").json()
    release(client, created["id"], 1)

    resp = register_batch(client, ["hello", CYRILLIC_APP])
    assert resp.status_code == 422
    items = resp.json()["detail"]["items"]
    assert items[0]["index"] == 1
    assert items[0]["conflicts"][0]["source"] == "released"
    assert items[0]["conflicts"][0]["reason"] == "confusable"
    # 整批不落库
    assert client.get("/namespaces/chat/names").json() == []


def test_active_cooldown_blocks_rename(client):
    created = register(client, "app").json()
    other = register(client, "hello").json()
    cooldown = release(client, created["id"], 1).json()

    resp = rename(client, other["id"], "APP", expected_revision=1)
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["source"] == "released"
    assert conflict["reason"] == "normalized_match"
    assert conflict["expires_at"] == cooldown["expires_at"]
    # 改名未生效
    assert client.get("/namespaces/chat/names").json()[0]["original_name"] == "hello"


def test_active_cooldown_blocks_claim(client):
    """释放冷却期间，预占确认同样命中冷却（直接插入预占构造场景）。"""
    created = register(client, "app").json()
    release(client, created["id"], 1)

    # 注册接口会被冷却挡住，直接写一条有效预占，再确认 -> 409 source=released
    conn = database.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO name_reservations
                (namespace, original_name, normalized_name, token, expires_at)
            VALUES ('chat', 'app', 'app', 'cooldown-claim-token',
                    '2999-01-01T00:00:00.000Z')
            """,
        )
        conn.commit()
    finally:
        conn.close()

    resp = claim(client, "cooldown-claim-token")
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["source"] == "released"
    assert conflict["reason"] == "normalized_match"
    # 名称未注册，预占未消费
    assert client.get("/namespaces/chat/names").json() == []


def test_check_reports_active_cooldown_conflict(client):
    created = register(client, "app").json()
    release(client, created["id"], 1)

    resp = client.post("/namespaces/chat/check", json={"name": CYRILLIC_APP})
    assert resp.status_code == 200
    conflicts = resp.json()["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["source"] == "released"
    assert conflicts[0]["reason"] == "confusable"
    # check 不落库、不清理
    assert len(list_cooldowns(client).json()) == 1


def test_cooldowns_are_namespace_isolated(client):
    created = register(client, "app", namespace="a").json()
    release(client, created["id"], 1, namespace="a")

    # 其他命名空间不受冷却影响
    assert register(client, "app", namespace="b").status_code == 201
    assert register(client, CYRILLIC_APP, namespace="b").status_code == 409  # 与本空间注册名冲突
    assert list_cooldowns(client, namespace="b").json() == []
    cooldowns = list_cooldowns(client, namespace="a").json()
    assert [c["original_name"] for c in cooldowns] == ["app"]


def test_list_cooldowns_excludes_expired_and_orders_by_id(client):
    first = register(client, "alice").json()
    second = register(client, "bob").json()
    third = register(client, "carol").json()
    release(client, first["id"], 1, cooldown_seconds=60)
    release(client, second["id"], 1, cooldown_seconds=120)
    release(client, third["id"], 1, cooldown_seconds=180)

    # 把第二条置为过期（只读列表不清理，仅过滤）
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_cooldowns SET expires_at = '2000-01-01T00:00:00.000Z' "
            "WHERE original_name = 'bob'"
        )
        conn.commit()
    finally:
        conn.close()

    cooldowns = list_cooldowns(client).json()
    assert [c["original_name"] for c in cooldowns] == ["alice", "carol"]
    assert all(c["expires_at"] > "2000-01-01" for c in cooldowns)


def test_expired_cooldown_is_purged_on_next_write_and_name_is_free_again(client):
    created = register(client, "app").json()
    release(client, created["id"], 1, cooldown_seconds=60)
    assert register(client, "app").status_code == 409  # 冷却中

    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_cooldowns SET expires_at = '2000-01-01T00:00:00.000Z'"
        )
        conn.commit()
    finally:
        conn.close()

    # 过期冷却不阻止注册；注册写事务惰性清理旧记录
    resp = register(client, "app")
    assert resp.status_code == 201
    assert list_cooldowns(client).json() == []
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM name_cooldowns").fetchone()[0] == 0
    finally:
        conn.close()


def test_expired_cooldown_does_not_block_check_without_write(client):
    created = register(client, "app").json()
    release(client, created["id"], 1, cooldown_seconds=60)
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_cooldowns SET expires_at = '2000-01-01T00:00:00.000Z'"
        )
        conn.commit()
    finally:
        conn.close()

    # 只读路径：过期冷却既不出现于 check，也不出现于冷却列表（但记录物理上仍在）
    resp = client.post("/namespaces/chat/check", json={"name": "app"})
    assert resp.json()["conflicts"] == []
    assert list_cooldowns(client).json() == []
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM name_cooldowns").fetchone()[0] == 1
    finally:
        conn.close()


def test_release_concurrent_requests_exactly_one_wins_no_5xx(client):
    """并发删除同一记录：写锁串行化，恰好一次 200，其余 404/409，无 5xx。"""
    created = register(client, "Alice").json()

    def post(_):
        return release(client, created["id"], expected_revision=1).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(post, range(8)))

    assert codes.count(200) == 1
    assert set(codes) <= {200, 404, 409}
    assert not any(code >= 500 for code in codes)
    # 名称只删除一次，冷却记录恰好一条
    assert client.get("/namespaces/chat/names").json() == []
    assert len(list_cooldowns(client).json()) == 1


def test_service_release_is_atomic_in_single_immediate_transaction(client):
    """服务层：失败分支不删除名称也不写冷却；成功分支同事务完成。"""
    conn = database.get_connection()
    try:
        record = service.register_name(conn, "chat", "Alice", "alice")

        # 版本过期：记录原样
        with pytest.raises(service.StaleRevisionError) as exc_info:
            service.release_name(conn, "chat", record["id"], 99, 3600)
        assert exc_info.value.current_revision == 1
        # 未知 id / 跨命名空间
        with pytest.raises(service.NameNotFoundError):
            service.release_name(conn, "chat", 999, 1, 3600)
        with pytest.raises(service.NameNotFoundError):
            service.release_name(conn, "other", record["id"], 1, 3600)

        rows = service.list_names(conn, "chat")
        assert [r["original_name"] for r in rows] == ["Alice"]
        assert service.list_cooldowns(conn, "chat") == []

        # 成功路径：名称删除与冷却写入同事务
        cooldown = service.release_name(conn, "chat", record["id"], 1, 3600)
        assert cooldown["original_name"] == "Alice"
        assert cooldown["normalized_name"] == "alice"
        assert cooldown["cooldown_seconds"] == 3600
        assert service.list_names(conn, "chat") == []
        assert [c["original_name"] for c in service.list_cooldowns(conn, "chat")] == [
            "Alice"
        ]
        # 冷却立即参与冲突判定（服务层需传入规范名，与接口层一致）
        conflicts = service.find_conflicts(
            conn, "chat", normalize_name("ALICE")
        )
        assert conflicts[0]["source"] == "released"
        assert conflicts[0]["reason"] == "normalized_match"
    finally:
        conn.close()


# ---------- 冷却记录参与命名空间合并 ----------


def test_merge_migrates_active_cooldowns_and_they_keep_blocking_in_target(client):
    created = register(client, "app", namespace="a").json()
    cooldown = release(client, created["id"], 1, cooldown_seconds=3600, namespace="a").json()
    assert register(client, "erin", namespace="b").status_code == 201

    resp = merge(client, "a", "b")
    assert resp.status_code == 200
    assert resp.json()["merged_cooldowns"] == 1

    # 源空间冷却清空，目标空间可列出且继续占位
    assert list_cooldowns(client, namespace="a").json() == []
    moved = list_cooldowns(client, namespace="b").json()
    assert len(moved) == 1
    assert moved[0]["id"] == cooldown["id"]  # id 原样保留
    assert moved[0]["expires_at"] == cooldown["expires_at"]

    resp = register(client, CYRILLIC_APP, namespace="b")
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["source"] == "released"
    assert conflict["reason"] == "confusable"

    # 源空间已无任何占位，同名可重新注册
    assert register(client, "app", namespace="a").status_code == 201


def test_merge_conflict_with_active_cooldown_returns_409_with_released_kind(client):
    created = register(client, "app", namespace="a").json()
    cooldown = release(client, created["id"], 1, namespace="a").json()
    assert register(client, CYRILLIC_APP, namespace="b").status_code == 201

    resp = merge(client, "a", "b")
    assert resp.status_code == 409
    pair = resp.json()["detail"]["conflicts"][0]
    assert pair["reason"] == "confusable"
    assert pair["source_record"]["kind"] == "released"
    assert pair["source_record"]["original_name"] == "app"
    assert pair["source_record"]["expires_at"] == cooldown["expires_at"]
    assert pair["target_record"]["kind"] == "registered"

    # 两侧数据均未变化：冷却仍在源空间
    assert [c["namespace"] for c in list_cooldowns(client, namespace="a").json()] == ["a"]


def test_merge_blocked_when_source_cooldown_violates_target_policy(client):
    # 源空间放宽策略，注册混脚本名称后释放（冷却保留混脚本原名）
    assert put_policy(client, [], 0, namespace="a").status_code == 200
    created = register(client, MIXED_ADMIN, namespace="a").json()
    cooldown = release(client, created["id"], 1, namespace="a").json()

    resp = merge(client, "a", "b")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["conflicts"] == []
    (violation,) = detail["script_violations"]
    assert violation["side"] == "source"
    assert violation["record"]["kind"] == "released"
    assert violation["record"]["id"] == cooldown["id"]
    assert violation["record"]["original_name"] == MIXED_ADMIN
    assert violation["record"]["expires_at"] == cooldown["expires_at"]
    assert violation["violating_script_pairs"] == [["Cyrillic", "Latin"]]

    # 冷却未迁移
    assert len(list_cooldowns(client, namespace="a").json()) == 1
    assert list_cooldowns(client, namespace="b").json() == []


def test_merge_purges_expired_cooldowns_and_does_not_migrate_them(client):
    created = register(client, "app", namespace="a").json()
    release(client, created["id"], 1, cooldown_seconds=60, namespace="a")
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_cooldowns SET expires_at = '2000-01-01T00:00:00.000Z'"
        )
        conn.commit()
    finally:
        conn.close()

    # 过期冷却不占位：目标侧同形名不阻止合并；冷却被惰性清理，不迁移
    assert register(client, CYRILLIC_APP, namespace="b").status_code == 201
    resp = merge(client, "a", "b")
    assert resp.status_code == 200
    assert resp.json()["merged_cooldowns"] == 0
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM name_cooldowns").fetchone()[0] == 0
    finally:
        conn.close()


# ---------- 冷却记录参与脚本策略更新 ----------


def test_put_policy_violating_active_cooldown_blocks_update(client):
    # 默认策略下放行 Han+Latin：注册后释放，冷却仍保留该原名
    assert put_policy(client, [], 0).status_code == 200
    created = register(client, "世界-x").json()
    cooldown = release(client, created["id"], 1, cooldown_seconds=3600).json()

    resp = put_policy(client, [["Han", "Latin"]], expected_revision=1)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "script_policy_conflict"
    (violation,) = detail["violations"]
    assert violation["kind"] == "released"
    assert violation["id"] == cooldown["id"]
    assert violation["original_name"] == "世界-x"
    assert violation["expires_at"] == cooldown["expires_at"]
    assert violation["violating_script_pairs"] == [["Han", "Latin"]]

    # 策略未更新：仍是空策略
    assert get_policy(client).json()["blocked_script_pairs"] == []


def test_put_policy_purges_expired_cooldowns_before_scan(client):
    assert put_policy(client, [], 0).status_code == 200
    created = register(client, "世界-x").json()
    release(client, created["id"], 1, cooldown_seconds=60)
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_cooldowns SET expires_at = '2000-01-01T00:00:00.000Z'"
        )
        conn.commit()
    finally:
        conn.close()

    # 过期冷却先被惰性清理，不再阻止策略收紧
    resp = put_policy(client, [["Han", "Latin"]], expected_revision=1)
    assert resp.status_code == 200
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM name_cooldowns").fetchone()[0] == 0
    finally:
        conn.close()


# ---------- 迁移幂等性与 OpenAPI ----------


def test_init_db_creates_cooldown_table_idempotently(tmp_path):
    """新库自动建冷却表，重复初始化不报错。"""
    db_path = tmp_path / "cooldown.db"
    database.init_db(str(db_path))
    database.init_db(str(db_path))
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "name_cooldowns" in tables
        columns = {row[1] for row in conn.execute("PRAGMA table_info(name_cooldowns)")}
        assert columns == {
            "id",
            "namespace",
            "original_name",
            "normalized_name",
            "cooldown_seconds",
            "expires_at",
            "created_at",
        }


def test_init_db_adds_cooldown_table_to_legacy_database(tmp_path):
    """老版本数据库（无冷却表）启动后自动获得 name_cooldowns，既有数据无损。"""
    db_path = tmp_path / "legacy-cooldown.db"
    import sqlite3

    with sqlite3.connect(db_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE display_names (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace       TEXT NOT NULL,
                original_name   TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                revision        INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT NOT NULL DEFAULT '',
                UNIQUE (namespace, normalized_name)
            )
            """
        )
        legacy.execute(
            "INSERT INTO display_names (namespace, original_name, normalized_name) "
            "VALUES ('chat', 'Alice', 'alice')"
        )
        legacy.commit()

    database.init_db(str(db_path))

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "name_cooldowns" in tables
        # 既有数据无损
        row = conn.execute(
            "SELECT revision, original_name FROM display_names WHERE id = 1"
        ).fetchone()
        assert row == (1, "Alice")


def test_openapi_documents_release_and_cooldown_listing_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    delete_op = paths["/namespaces/{namespace}/names/{name_id}"]["delete"]
    assert "200" in delete_op["responses"]
    assert "404" in delete_op["responses"]
    assert "409" in delete_op["responses"]
    assert "422" in delete_op["responses"]
    list_op = paths["/namespaces/{namespace}/cooldowns"]["get"]
    assert "200" in list_op["responses"]


# ---------- 跨空间同形风险簇 POST /name-risk-clusters ----------


# 传递链三元组（规范化后仍是单字符）：
# "A" -> "a"，与 Cherokee Ꭺ(U+13AA) 同形；Ꭺ 与小大写 ᴀ(U+1D00) 同形；
# 但 "a" 与 ᴀ 不同形 —— 用于验证 A-B、B-C 即使 A-C 不冲突也归入同簇
CHEROKEE_GO = "Ꭺ"
SMALL_CAPITAL_A = "ᴀ"


def risk_clusters(client, namespaces, max_names=500):
    return client.post(
        "/name-risk-clusters",
        json={"namespaces": namespaces, "max_names": max_names},
    )


def test_risk_clusters_confusable_pair_forms_single_cluster(client):
    chat_id = register(client, "app", namespace="chat").json()["id"]
    forum_id = register(client, CYRILLIC_APP, namespace="forum").json()["id"]

    resp = risk_clusters(client, ["forum", "chat"])  # 故意乱序，验证确定性
    assert resp.status_code == 200
    body = resp.json()
    # namespaces 按字典序规范化返回
    assert body["namespaces"] == ["chat", "forum"]
    assert body["max_names"] == 500
    assert body["snapshot_count"] == 2
    assert len(body["clusters"]) == 1
    cluster = body["clusters"][0]
    assert cluster["cluster_id"] == 1
    assert [n["namespace"] for n in cluster["nodes"]] == ["chat", "forum"]
    # registered 节点不含 expires_at
    assert all("expires_at" not in n for n in cluster["nodes"])
    chat_node = cluster["nodes"][0]
    assert chat_node == {
        "namespace": "chat",
        "kind": "registered",
        "id": chat_id,
        "original_name": "app",
        "normalized_name": "app",
    }
    # 边端点是稳定标识 namespace/kind/id，小者为 source
    edge = cluster["edges"][0]
    assert edge["source"] == {"namespace": "chat", "kind": "registered", "id": chat_id}
    assert edge["target"] == {"namespace": "forum", "kind": "registered", "id": forum_id}
    assert edge["reasons"] == ["confusable"]


def test_risk_clusters_normalized_match_edge_reason(client):
    assert register(client, "Alice", namespace="a").status_code == 201
    assert register(client, "ALICE", namespace="b").status_code == 201

    body = risk_clusters(client, ["a", "b"]).json()
    assert len(body["clusters"]) == 1
    edge = body["clusters"][0]["edges"][0]
    assert edge["reasons"] == ["normalized_match"]


def test_risk_clusters_transitive_chain_merges_even_without_direct_edge(client):
    # a/A --confusable--> b/Ꭺ --confusable--> c/ᴀ，而 a 与 c 不同形：
    # 连通分量仍把三者归入同一簇，边恰好只有两条
    assert register(client, "A", namespace="a").status_code == 201
    assert register(client, CHEROKEE_GO, namespace="b").status_code == 201
    assert register(client, SMALL_CAPITAL_A, namespace="c").status_code == 201

    body = risk_clusters(client, ["c", "b", "a"]).json()
    assert body["snapshot_count"] == 3
    assert len(body["clusters"]) == 1
    cluster = body["clusters"][0]
    assert [n["namespace"] for n in cluster["nodes"]] == ["a", "b", "c"]
    assert len(cluster["edges"]) == 2
    pairs = sorted((e["source"]["namespace"], e["target"]["namespace"]) for e in cluster["edges"])
    assert pairs == [("a", "b"), ("b", "c")]
    assert all(e["reasons"] == ["confusable"] for e in cluster["edges"])


def test_risk_clusters_same_namespace_names_never_get_edges(client):
    # 同一命名空间内（直接插库绕过写入拦截）的同形记录不建边，
    # 只有跨空间的边能把节点带入簇
    conn = database.get_connection()
    try:
        service.insert_name(conn, "a", "app", "app")
        service.insert_name(conn, "a", CYRILLIC_APP, CYRILLIC_APP)
        service.insert_name(conn, "b", "арр-bridge", "арр-bridge")
    finally:
        conn.close()
    # 只查 a：两条同空间记录即使同形也不形成簇
    body = risk_clusters(client, ["a", "empty"]).json()
    assert body["snapshot_count"] == 2
    assert body["clusters"] == []

    # 引入与之跨空间同形的第三空间（app 与 арр-bridge 不同形，单独造一个
    # 与 арр 同形的第三空间名称）：app-арр 是跨空间边，同空间的 арр 被带入簇
    assert register(client, CYRILLIC_APP, namespace="c").status_code == 201
    body = risk_clusters(client, ["a", "c"]).json()
    assert len(body["clusters"]) == 1
    namespaces = sorted(n["namespace"] for n in body["clusters"][0]["nodes"])
    assert namespaces == ["a", "a", "c"]
    for edge in body["clusters"][0]["edges"]:
        assert edge["source"]["namespace"] != edge["target"]["namespace"]


def test_risk_clusters_unrelated_names_yield_empty_clusters(client):
    assert register(client, "alice", namespace="a").status_code == 201
    assert register(client, "bob", namespace="b").status_code == 201
    body = risk_clusters(client, ["a", "b"]).json()
    assert body["snapshot_count"] == 2
    assert body["clusters"] == []


def test_risk_clusters_include_active_reservations_and_cooldowns_with_expires_at(client):
    reservation = reserve(client, CYRILLIC_APP, ttl_seconds=300, namespace="d").json()
    created = register(client, "APP", namespace="e").json()
    cooldown = release(
        client, created["id"], expected_revision=1, cooldown_seconds=3600, namespace="e"
    ).json()

    body = risk_clusters(client, ["d", "e"]).json()
    assert body["snapshot_count"] == 2  # 1 条有效预占 + 1 条未过期冷却
    cluster = body["clusters"][0]
    by_kind = {n["kind"]: n for n in cluster["nodes"]}
    assert set(by_kind) == {"reservation", "released"}
    assert by_kind["reservation"]["id"] == reservation["id"]
    assert by_kind["reservation"]["expires_at"] == reservation["expires_at"]
    assert by_kind["released"]["id"] == cooldown["id"]
    assert by_kind["released"]["expires_at"] == cooldown["expires_at"]
    edge = cluster["edges"][0]
    # kind 排序 registered(0) < reservation(1) < released(2)：reservation 为 source
    assert edge["source"]["kind"] == "reservation"
    assert edge["target"]["kind"] == "released"
    assert edge["reasons"] == ["confusable"]


def test_risk_clusters_exclude_expired_and_consumed_records(client):
    reserve(client, "app", ttl_seconds=30, namespace="a")
    consumed = reserve(client, "gone", ttl_seconds=300, namespace="a").json()
    assert claim(client, consumed["token"], namespace="a").status_code == 201
    created = register(client, "cool", namespace="a").json()
    release(client, created["id"], expected_revision=1, cooldown_seconds=60, namespace="a")
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_reservations SET expires_at = '2000-01-01T00:00:00.000Z' "
            "WHERE original_name = 'app'"
        )
        conn.execute(
            "UPDATE name_cooldowns SET expires_at = '2000-01-01T00:00:00.000Z'"
        )
        conn.commit()
    finally:
        conn.close()

    # 快照只含已消费后注册的 gone：过期预占 / 过期冷却 / 已消费预占都不计入
    body = risk_clusters(client, ["a", "b"]).json()
    assert body["snapshot_count"] == 1
    assert body["clusters"] == []
    # 只读分析不清理：过期记录物理上仍在
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM name_cooldowns").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM name_reservations").fetchone()[0] == 2
    finally:
        conn.close()


def test_risk_clusters_413_when_snapshot_exceeds_max_names_and_does_not_truncate(client):
    assert register(client, "alice", namespace="a").status_code == 201
    assert register(client, "bob", namespace="b").status_code == 201  # 与 alice 无关
    assert register(client, "app", namespace="c").status_code == 201

    # max_names=2：快照 3 个节点 -> 413，即便真正冲突的只有 app 一侧也不截断
    resp = risk_clusters(client, ["a", "b", "c"], max_names=2)
    assert resp.status_code == 413
    detail = resp.json()["detail"]
    assert detail["error"] == "risk_snapshot_too_large"
    assert detail["snapshot_count"] == 3
    assert detail["max_names"] == 2

    # 边界：恰好等于 max_names 时正常计算
    resp = risk_clusters(client, ["a", "b", "c"], max_names=3)
    assert resp.status_code == 200
    assert resp.json()["snapshot_count"] == 3

    # 413 只过滤时间维度（过期 / 已消费），不按 max_names 截断：预置一条过期记录，
    # 它不计入快照，总量降回上限内时计算正常
    created = register(client, "dave", namespace="d").json()
    release(client, created["id"], expected_revision=1, cooldown_seconds=60, namespace="d")
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_cooldowns SET expires_at = '2000-01-01T00:00:00.000Z'"
        )
        conn.commit()
    finally:
        conn.close()
    resp = risk_clusters(client, ["a", "b", "c", "d"], max_names=3)
    assert resp.status_code == 200
    assert resp.json()["snapshot_count"] == 3


def test_risk_clusters_ordering_is_deterministic(client):
    # 多簇、多节点、多种 kind：验证簇 / 节点 / 边排序与 cluster_id 稳定
    app_a = register(client, "app", namespace="a").json()["id"]
    app_b = register(client, CYRILLIC_APP, namespace="b").json()["id"]
    alice_a = register(client, "Alice", namespace="a").json()["id"]
    alice_c = register(client, "ALICE", namespace="c").json()["id"]

    first = risk_clusters(client, ["c", "b", "a"]).json()
    second = risk_clusters(client, ["a", "b", "c"]).json()
    assert first == second  # 与请求顺序无关

    # 簇按簇内最小节点键排序：a/app 的 id 小于 a/Alice，故 app 簇为第 1 簇
    assert len(first["clusters"]) == 2
    assert first["clusters"][0]["cluster_id"] == 1
    assert first["clusters"][1]["cluster_id"] == 2
    first_members = {(n["namespace"], n["id"]) for n in first["clusters"][0]["nodes"]}
    assert first_members == {("a", app_a), ("b", app_b)}
    second_members = {(n["namespace"], n["id"]) for n in first["clusters"][1]["nodes"]}
    assert second_members == {("a", alice_a), ("c", alice_c)}


def test_risk_clusters_unknown_namespaces_and_empty_snapshot(client):
    # 不存在的命名空间：快照为 0，簇为空
    body = risk_clusters(client, ["no-such", "ghost"]).json()
    assert body["namespaces"] == ["ghost", "no-such"]
    assert body["snapshot_count"] == 0
    assert body["clusters"] == []


def test_risk_clusters_request_validation(client):
    # 少于 2 个 / 多于 20 个命名空间 -> 422
    assert risk_clusters(client, ["a"]).status_code == 422
    assert risk_clusters(client, [f"ns{i}" for i in range(21)]).status_code == 422
    # 重复命名空间 -> 422
    assert risk_clusters(client, ["a", "a"]).status_code == 422
    # 空白命名空间 -> 422
    assert risk_clusters(client, ["a", "  "]).status_code == 422
    # max_names 越界（1–500）-> 422
    assert risk_clusters(client, ["a", "b"], max_names=0).status_code == 422
    assert risk_clusters(client, ["a", "b"], max_names=501).status_code == 422
    # 缺字段 -> 422
    assert client.post("/name-risk-clusters", json={"namespaces": ["a", "b"]}).status_code == 422
    assert client.post(
        "/name-risk-clusters", json={"max_names": 10}
    ).status_code == 422


def test_risk_clusters_fix_snapshot_in_single_read_transaction_wal(client, monkeypatch):
    """WAL 下读事务快照固定：事务开始后另一连接的提交不影响本次分簇。

    已注册名称表先读；在预占表读取前，另一连接提交一个会与既有名称形成
    跨空间同形簇的预占。单一 BEGIN 读事务保证该提交对本事务不可见：
    结果快照不含新预占，也不产生对应的新簇。
    """
    assert register(client, "app", namespace="a").status_code == 201
    conn = database.get_connection()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    finally:
        conn.close()

    real_reservation_rows = service._reservation_snapshot_rows
    injected = {"done": False}

    def racing_reservation_rows(read_conn, namespaces, now):
        # 读事务已开始且已读完 display_names：另一连接提交新的有效预占
        # （仅在第一次调用时注入，第二次请求应直接读到该预占）
        if not injected["done"]:
            injected["done"] = True
            other = database.get_connection()
            try:
                other.execute(
                    """
                    INSERT INTO name_reservations
                        (namespace, original_name, normalized_name, token, expires_at)
                    VALUES ('b', ?, ?, 'racing-token', '2999-01-01T00:00:00.000Z')
                    """,
                    (CYRILLIC_APP, CYRILLIC_APP),
                )
                other.commit()
            finally:
                other.close()
        return real_reservation_rows(read_conn, namespaces, now)

    monkeypatch.setattr(service, "_reservation_snapshot_rows", racing_reservation_rows)

    body = risk_clusters(client, ["a", "b"]).json()
    # 新预占在本事务快照之后提交：不计入快照、不形成簇
    assert body["snapshot_count"] == 1
    assert body["clusters"] == []

    # 不带注入再次调用：新预占可见，正常形成跨空间簇
    resp = risk_clusters(client, ["a", "b"])
    assert resp.status_code == 200
    assert resp.json()["snapshot_count"] == 2
    assert len(resp.json()["clusters"]) == 1


def test_risk_clusters_service_raises_on_oversized_snapshot_without_truncation(client):
    """服务层：快照超限抛 RiskSnapshotTooLargeError，携带真实总数；不做任何计算。"""
    conn = database.get_connection()
    try:
        service.insert_name(conn, "a", "app", "app")
        service.insert_name(conn, "b", CYRILLIC_APP, CYRILLIC_APP)
        with pytest.raises(service.RiskSnapshotTooLargeError) as exc_info:
            service.find_name_risk_clusters(conn, ["a", "b"], max_names=1)
        assert exc_info.value.snapshot_count == 2
        assert exc_info.value.limit == 1

        # 只读事务已结束（rollback）：连接可正常复用，数据原样
        assert [r["original_name"] for r in service.list_names(conn, "a")] == ["app"]
        assert [r["original_name"] for r in service.list_names(conn, "b")] == [CYRILLIC_APP]
    finally:
        conn.close()


def test_openapi_documents_name_risk_clusters_endpoint(client):
    operation = client.get("/openapi.json").json()["paths"]["/name-risk-clusters"]["post"]
    assert "200" in operation["responses"]
    assert "413" in operation["responses"]
    assert "422" in operation["responses"]


# ---------- 快照 Merkle 包含证明 POST /namespaces/{namespace}/snapshot-proof ----------


def snapshot_proof(client, nodes, max_nodes=500, namespace="chat"):
    return client.post(
        f"/namespaces/{namespace}/snapshot-proof",
        json={"nodes": nodes, "max_nodes": max_nodes},
    )


def canonical_leaf_bytes(leaf):
    """与服务端独立的规范 JSON 编码：键排序、紧凑分隔、UTF-8 原样非 ASCII。"""
    return json.dumps(
        leaf, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def replay_proof(leaf_bytes, proof):
    """仅凭叶载荷字节与证明步骤重算根（独立于 app.merkle 的实现）。"""
    current = hashlib.sha256(b"\x00" + leaf_bytes).digest()
    for step in proof:
        sibling = bytes.fromhex(step["hash"])
        if step["direction"] == "left":
            current = hashlib.sha256(b"\x01" + sibling + current).digest()
        else:
            current = hashlib.sha256(b"\x01" + current + sibling).digest()
    return current.hex()


def proof_by_key(body):
    return {(item["kind"], item["id"]): item for item in body["items"]}


def policy_leaf_bytes(body):
    """用响应回显的策略重建策略叶的规范 JSON（叶内带固定 id=0）。"""
    policy = dict(body["policy"])
    policy["id"] = 0
    policy["namespace"] = body["namespace"]
    return canonical_leaf_bytes(policy)


def test_snapshot_proof_registered_node_verifies_against_root(client):
    created = register(client, "alice").json()
    body = snapshot_proof(client, [{"kind": "registered", "id": created["id"]}]).json()

    assert body["namespace"] == "chat"
    assert body["max_nodes"] == 500
    assert body["snapshot_count"] == 1
    # 1 个业务节点 + 恒存在的策略叶
    assert body["leaf_count"] == 2
    item = body["items"][0]
    assert item["leaf_index"] == 1  # 策略叶占下标 0
    leaf = item["leaf"]
    assert leaf["kind"] == "registered"
    assert leaf["id"] == created["id"]
    assert leaf["original_name"] == "alice"
    assert leaf["normalized_name"] == "alice"
    assert leaf["revision"] == 1
    assert leaf["expires_at"] is None
    assert leaf["cooldown_seconds"] is None

    # 叶哈希与包含证明可独立重放出根
    leaf_without_hash = {key: value for key, value in leaf.items() if key != "leaf_hash"}
    expected_leaf_hash = hashlib.sha256(
        b"\x00" + canonical_leaf_bytes(leaf_without_hash)
    ).hexdigest()
    assert leaf["leaf_hash"] == expected_leaf_hash
    assert replay_proof(canonical_leaf_bytes(leaf_without_hash), item["proof"]) == body["root"]

    # 策略叶是本叶唯一的兄弟：证明一步，方向 left
    assert len(item["proof"]) == 1
    assert item["proof"][0]["direction"] == "left"
    policy_hash = hashlib.sha256(b"\x00" + policy_leaf_bytes(body)).hexdigest()
    assert item["proof"][0]["hash"] == policy_hash


def test_snapshot_proof_policy_leaf_is_committed_to_root(client):
    register(client, "alice")
    body = snapshot_proof(client, [{"kind": "registered", "id": 1}]).json()
    item = body["items"][0]

    # 用策略叶哈希作为叶、以业务叶为右兄弟直接组合出根（策略叶下标 0，
    # 业务叶在下标 1：root = H(0x01 || H(0x00||policy) || H(0x00||registered))）
    policy_hash = hashlib.sha256(b"\x00" + policy_leaf_bytes(body)).digest()
    leaf_bytes = canonical_leaf_bytes(
        {key: value for key, value in item["leaf"].items() if key != "leaf_hash"}
    )
    registered_hash = hashlib.sha256(b"\x00" + leaf_bytes).digest()
    root = hashlib.sha256(b"\x01" + policy_hash + registered_hash).hexdigest()
    assert root == body["root"]


def test_snapshot_proof_policy_is_pinned_even_when_unconfigured(client):
    register(client, "bob")
    body = snapshot_proof(client, [{"kind": "registered", "id": 1}]).json()

    policy = body["policy"]
    assert policy["kind"] == "script_policy"
    assert policy["configured"] is False
    assert policy["revision"] == 0
    from app.analysis import DEFAULT_BLOCKED_PAIRS

    assert policy["blocked_script_pairs"] == [list(pair) for pair in DEFAULT_BLOCKED_PAIRS]


def test_snapshot_proof_configured_policy_is_pinned_in_snapshot(client):
    put_policy(client, [["Latin", "Cyrillic"]], expected_revision=0)
    register(client, "bob")
    body = snapshot_proof(client, [{"kind": "registered", "id": 1}]).json()

    policy = body["policy"]
    assert policy["configured"] is True
    assert policy["revision"] == 1
    assert policy["blocked_script_pairs"] == [["Cyrillic", "Latin"]]

    # 策略叶承诺进根：把策略再 PUT 一次（内容相同，revision 递增）后根应改变
    put_policy(client, [["Latin", "Cyrillic"]], expected_revision=1)
    body_after = snapshot_proof(client, [{"kind": "registered", "id": 1}]).json()
    assert body_after["policy"]["revision"] == 2
    assert body_after["root"] != body["root"]
    # 业务叶本身未变
    assert (
        body_after["items"][0]["leaf"]["leaf_hash"]
        == body["items"][0]["leaf"]["leaf_hash"]
    )


def test_snapshot_proof_includes_reservations_and_cooldowns_with_expires_at(client):
    reservation = reserve(client, "alice-res", ttl_seconds=300, namespace="ns").json()
    created = register(client, "cool-name", namespace="ns").json()
    cooldown = release(
        client, created["id"], expected_revision=1, cooldown_seconds=7200, namespace="ns"
    ).json()

    body = snapshot_proof(
        client,
        [
            {"kind": "reservation", "id": reservation["id"]},
            {"kind": "released", "id": cooldown["id"]},
        ],
        namespace="ns",
    ).json()
    assert body["snapshot_count"] == 2
    assert body["leaf_count"] == 3  # 策略 + 预占 + 冷却

    by_key = proof_by_key(body)
    res_leaf = by_key[("reservation", reservation["id"])]["leaf"]
    assert res_leaf["expires_at"] == reservation["expires_at"]
    assert res_leaf["revision"] is None
    assert res_leaf["cooldown_seconds"] is None
    rel_leaf = by_key[("released", cooldown["id"])]["leaf"]
    assert rel_leaf["expires_at"] == cooldown["expires_at"]
    assert rel_leaf["cooldown_seconds"] == 7200
    assert rel_leaf["revision"] is None

    # 类型序 registered(1) < reservation(2) < released(3)：预占在下标 1、冷却在下标 2
    assert by_key[("reservation", reservation["id"])]["leaf_index"] == 1
    assert by_key[("released", cooldown["id"])]["leaf_index"] == 2

    # 每个证明都能独立重放到同一根
    for item in body["items"]:
        leaf = {key: value for key, value in item["leaf"].items() if key != "leaf_hash"}
        assert replay_proof(canonical_leaf_bytes(leaf), item["proof"]) == body["root"]


def test_snapshot_proof_request_order_does_not_change_root_or_proofs(client):
    a = register(client, "alice").json()["id"]
    b = register(client, "bob").json()["id"]
    c = register(client, "carol").json()["id"]

    forward = snapshot_proof(
        client,
        [
            {"kind": "registered", "id": a},
            {"kind": "registered", "id": b},
            {"kind": "registered", "id": c},
        ],
    ).json()
    reverse = snapshot_proof(
        client,
        [
            {"kind": "registered", "id": c},
            {"kind": "registered", "id": a},
            {"kind": "registered", "id": b},
        ],
    ).json()

    assert reverse["root"] == forward["root"]
    assert reverse["leaf_count"] == forward["leaf_count"]

    fwd_by_key = proof_by_key(forward)
    rev_by_key = proof_by_key(reverse)
    for key in fwd_by_key:
        # 同一节点：叶、下标与证明步骤完全相同（与请求位置无关）
        assert rev_by_key[key]["leaf"] == fwd_by_key[key]["leaf"]
        assert rev_by_key[key]["leaf_index"] == fwd_by_key[key]["leaf_index"]
        assert rev_by_key[key]["proof"] == fwd_by_key[key]["proof"]

    # items 仍按请求顺序回显
    assert [item["id"] for item in reverse["items"]] == [c, a, b]


def test_snapshot_proof_root_is_independent_of_requested_subset(client):
    ids = [register(client, f"name-{i}").json()["id"] for i in range(5)]

    full = snapshot_proof(
        client, [{"kind": "registered", "id": i} for i in ids]
    ).json()
    partial = snapshot_proof(client, [{"kind": "registered", "id": ids[2]}]).json()
    # 根承诺的是整个快照，而不是被请求节点的子集
    assert partial["root"] == full["root"]
    assert partial["leaf_count"] == full["leaf_count"] == 6


def test_snapshot_proof_odd_level_duplicates_last_node(client):
    # 4 个业务节点 + 策略叶 = 5 叶（奇数），首层复制末节点；末节点的证明
    # 兄弟应是它自身（复制），方向 right
    ids = [register(client, f"n{i}").json()["id"] for i in range(4)]
    body = snapshot_proof(
        client, [{"kind": "registered", "id": i} for i in ids]
    ).json()
    assert body["leaf_count"] == 5
    last = proof_by_key(body)[("registered", ids[3])]
    first_step = last["proof"][0]
    assert first_step["direction"] == "right"
    assert first_step["hash"] == last["leaf"]["leaf_hash"]

    # 全部证明均可重放到根
    for item in body["items"]:
        leaf = {key: value for key, value in item["leaf"].items() if key != "leaf_hash"}
        assert replay_proof(canonical_leaf_bytes(leaf), item["proof"]) == body["root"]


def test_snapshot_proof_404_when_any_requested_node_missing(client):
    register(client, "alice")
    resp = snapshot_proof(
        client,
        [
            {"kind": "registered", "id": 1},
            {"kind": "registered", "id": 999},
            {"kind": "reservation", "id": 42},
        ],
    )
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["error"] == "snapshot_nodes_not_found"
    assert detail["missing"] == [
        {"kind": "registered", "id": 999},
        {"kind": "reservation", "id": 42},
    ]


def test_snapshot_proof_expired_or_consumed_nodes_return_404(client):
    active = reserve(client, "active-one", ttl_seconds=300).json()
    expired = reserve(client, "expired-one", ttl_seconds=30).json()
    consumed = reserve(client, "consumed-one", ttl_seconds=300).json()
    assert claim(client, consumed["token"]).status_code == 201
    created = register(client, "cooled").json()
    released = release(
        client, created["id"], expected_revision=1, cooldown_seconds=60
    ).json()

    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_reservations SET expires_at = '2000-01-01T00:00:00.000Z' "
            "WHERE id = ?",
            (expired["id"],),
        )
        conn.execute(
            "UPDATE name_cooldowns SET expires_at = '2000-01-01T00:00:00.000Z'"
        )
        conn.commit()
    finally:
        conn.close()

    resp = snapshot_proof(
        client,
        [
            {"kind": "reservation", "id": active["id"]},
            {"kind": "reservation", "id": expired["id"]},
            {"kind": "reservation", "id": consumed["id"]},
            {"kind": "released", "id": released["id"]},
        ],
    )
    assert resp.status_code == 404
    missing = resp.json()["detail"]["missing"]
    assert missing == [
        {"kind": "reservation", "id": expired["id"]},
        {"kind": "reservation", "id": consumed["id"]},
        {"kind": "released", "id": released["id"]},
    ]

    # 只读路径不清理：过期记录物理上仍在
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM name_cooldowns").fetchone()[0] == 1
    finally:
        conn.close()


def test_snapshot_proof_wrong_namespace_node_returns_404(client):
    nid = register(client, "alice", namespace="a").json()["id"]
    resp = snapshot_proof(client, [{"kind": "registered", "id": nid}], namespace="b")
    assert resp.status_code == 404
    assert resp.json()["detail"]["missing"] == [{"kind": "registered", "id": nid}]


def test_snapshot_proof_413_when_snapshot_exceeds_max_nodes_and_does_not_truncate(client):
    for i in range(3):
        register(client, f"name-{i}")

    resp = snapshot_proof(client, [{"kind": "registered", "id": 1}], max_nodes=2)
    assert resp.status_code == 413
    detail = resp.json()["detail"]
    assert detail["error"] == "snapshot_too_large"
    assert detail["snapshot_count"] == 3
    assert detail["max_nodes"] == 2

    # 边界：恰好等于上限时成功，leaf_count 仍含策略叶（上限只管业务节点）
    ok = snapshot_proof(client, [{"kind": "registered", "id": 1}], max_nodes=3)
    assert ok.status_code == 200
    assert ok.json()["snapshot_count"] == 3
    assert ok.json()["leaf_count"] == 4

    # 即使请求的节点本身存在，超限也优先 413，且不建树
    resp = snapshot_proof(client, [{"kind": "registered", "id": 999}], max_nodes=2)
    assert resp.status_code == 413


def test_snapshot_proof_unchanged_snapshot_yields_stable_root(client):
    register(client, "alice")
    first = snapshot_proof(client, [{"kind": "registered", "id": 1}]).json()
    second = snapshot_proof(client, [{"kind": "registered", "id": 1}]).json()
    assert second["root"] == first["root"]


def test_snapshot_proof_request_validation(client):
    # nodes 为空 / 超过 20 个 -> 422
    assert snapshot_proof(client, []).status_code == 422
    assert snapshot_proof(
        client, [{"kind": "registered", "id": i} for i in range(21)]
    ).status_code == 422
    # 重复 (kind, id) -> 422（注意 registered/reservation 的同 id 不算重复）
    assert snapshot_proof(
        client,
        [
            {"kind": "registered", "id": 1},
            {"kind": "registered", "id": 1},
        ],
    ).status_code == 422
    # 不同 kind 的同 id 允许
    assert snapshot_proof(
        client,
        [
            {"kind": "registered", "id": 1},
            {"kind": "reservation", "id": 1},
        ],
    ).status_code in (200, 404)
    # 未知 kind / id<1 -> 422
    assert snapshot_proof(client, [{"kind": "policy", "id": 0}]).status_code == 422
    assert snapshot_proof(client, [{"kind": "registered", "id": 0}]).status_code == 422
    # max_nodes 越界（1–500）-> 422
    assert snapshot_proof(
        client, [{"kind": "registered", "id": 1}], max_nodes=0
    ).status_code == 422
    assert snapshot_proof(
        client, [{"kind": "registered", "id": 1}], max_nodes=501
    ).status_code == 422
    # 缺字段 -> 422
    assert client.post(
        "/namespaces/chat/snapshot-proof", json={"max_nodes": 10}
    ).status_code == 422
    assert client.post(
        "/namespaces/chat/snapshot-proof", json={"nodes": [{"kind": "registered", "id": 1}]}
    ).status_code == 422


def test_snapshot_proof_canonical_json_is_order_insensitive_and_unicode_safe(client):
    # 非 ASCII 原名原样 UTF-8 编码，且叶哈希与字段插入顺序无关
    created = register(client, "世界").json()
    body = snapshot_proof(client, [{"kind": "registered", "id": created["id"]}]).json()
    leaf = body["items"][0]["leaf"]
    leaf_without_hash = {key: value for key, value in leaf.items() if key != "leaf_hash"}
    # 打乱键顺序后重新编码，哈希必须相同
    shuffled = dict(reversed(list(leaf_without_hash.items())))
    assert canonical_leaf_bytes(shuffled) == canonical_leaf_bytes(leaf_without_hash)
    assert (
        hashlib.sha256(b"\x00" + canonical_leaf_bytes(shuffled)).hexdigest()
        == leaf["leaf_hash"]
    )
    # 编码字节中原样包含中文（而非 \\uXXXX 转义）
    assert "世界".encode("utf-8") in canonical_leaf_bytes(leaf_without_hash)


def test_snapshot_proof_fix_snapshot_in_single_read_transaction_wal(client, monkeypatch):
    """WAL 下读事务快照固定：事务开始后另一连接的提交不进入本次树。

    已注册名称先读；在预占表读取前，另一连接提交一个新预占。单一 BEGIN
    读事务保证该提交对本事务不可见：snapshot_count 不含新预占、根也不承诺它。
    """
    register(client, "app")
    conn = database.get_connection()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    finally:
        conn.close()

    real_reservation_rows = service._proof_reservation_rows
    injected = {"done": False}

    def racing_reservation_rows(read_conn, namespace, now):
        if not injected["done"]:
            injected["done"] = True
            other = database.get_connection()
            try:
                other.execute(
                    """
                    INSERT INTO name_reservations
                        (namespace, original_name, normalized_name, token, expires_at)
                    VALUES ('chat', 'later-res', 'later-res', 'racing-token',
                            '2999-01-01T00:00:00.000Z')
                    """,
                )
                other.commit()
            finally:
                other.close()
        return real_reservation_rows(read_conn, namespace, now)

    monkeypatch.setattr(service, "_proof_reservation_rows", racing_reservation_rows)

    body = snapshot_proof(client, [{"kind": "registered", "id": 1}]).json()
    # 新预占在本事务快照之后提交：不计入快照
    assert body["snapshot_count"] == 1
    assert body["leaf_count"] == 2

    # 不带注入再次调用：新预占可见并被根承诺
    resp = snapshot_proof(client, [{"kind": "registered", "id": 1}])
    assert resp.status_code == 200
    assert resp.json()["snapshot_count"] == 2
    assert resp.json()["leaf_count"] == 3


def test_snapshot_proof_service_raises_on_oversized_snapshot_without_truncation(client):
    """服务层：快照超限抛 SnapshotTooLargeError，携带真实总数；不建树。"""
    conn = database.get_connection()
    try:
        service.insert_name(conn, "a", "one", "one")
        service.insert_name(conn, "a", "two", "two")
        with pytest.raises(service.SnapshotTooLargeError) as exc_info:
            service.build_snapshot_proof(
                conn, "a", [{"kind": "registered", "id": 1}], max_nodes=1
            )
        assert exc_info.value.snapshot_count == 2
        assert exc_info.value.limit == 1

        # 只读事务已结束（rollback）：连接可正常复用，数据原样
        assert [r["original_name"] for r in service.list_names(conn, "a")] == ["one", "two"]
    finally:
        conn.close()


def test_openapi_documents_snapshot_proof_endpoint(client):
    operation = client.get("/openapi.json").json()["paths"][
        "/namespaces/{namespace}/snapshot-proof"
    ]["post"]
    assert "200" in operation["responses"]
    assert "404" in operation["responses"]
    assert "413" in operation["responses"]
    assert "422" in operation["responses"]


# ---------- 候选集原子注册 POST /namespaces/{namespace}/names/allocate ----------


def allocate(client, items, namespace="chat"):
    return client.post(
        f"/namespaces/{namespace}/names/allocate", json={"items": items}
    )


def _alloc_item(client_id, *candidates):
    return {"client_id": client_id, "candidates": list(candidates)}


def test_allocate_success_registers_first_choices_for_all_items(client):
    resp = allocate(
        client,
        [_alloc_item("a", "alice", "alice-2"), _alloc_item("b", "bob", "bob-2")],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["namespace"] == "chat"
    assert [item["client_id"] for item in body["items"]] == ["a", "b"]
    assert [item["candidate_index"] for item in body["items"]] == [0, 0]
    assert [item["original_name"] for item in body["items"]] == ["alice", "bob"]
    # items 按请求顺序回显并带 index；落库记录 revision 初始为 1
    assert [item["index"] for item in body["items"]] == [0, 1]
    assert all(item["revision"] == 1 for item in body["items"])
    # 全部一次性落库
    names = client.get("/namespaces/chat/names").json()
    assert {n["original_name"] for n in names} == {"alice", "bob"}


def test_allocate_falls_back_to_lower_priority_candidate_when_first_blocked(client):
    assert register(client, "alice").status_code == 201

    # 首个候选与已注册名称规范名相同 -> 回退到第二个候选
    resp = allocate(client, [_alloc_item("a", "ALICE", "zoe")])
    assert resp.status_code == 201, resp.text
    item = resp.json()["items"][0]
    assert item["candidate_index"] == 1
    assert item["original_name"] == "zoe"
    assert [n["original_name"] for n in client.get("/namespaces/chat/names").json()] == [
        "alice",
        "zoe",
    ]


def test_allocate_confusable_existing_name_blocks_candidate(client):
    assert register(client, "app").status_code == 201
    # 纯西里尔 арр 与拉丁 app 同形：第 0 候选被同形冲突挡住，回退第 1 候选
    resp = allocate(client, [_alloc_item("a", CYRILLIC_APP, "zulu")])
    assert resp.status_code == 201, resp.text
    assert resp.json()["items"][0]["candidate_index"] == 1


def test_allocate_picks_lexicographically_smallest_candidate_index_vector(client):
    # 两项候选集合相同：第 0 候选互相冲突，字典序最小解为 (0, 1) 而非 (1, 0)
    resp = allocate(
        client,
        [
            _alloc_item("a", "mike", "nina"),
            _alloc_item("b", "mike", "nina"),
        ],
    )
    assert resp.status_code == 201, resp.text
    assert [item["candidate_index"] for item in resp.json()["items"]] == [0, 1]
    assert [item["original_name"] for item in resp.json()["items"]] == ["mike", "nina"]

    # 三候选两申请项：优先满足靠前的申请项
    resp = allocate(
        client,
        [
            _alloc_item("p", "x1", "x2", "x3"),
            _alloc_item("q", "x1", "x2", "x3"),
        ],
        namespace="ns2",
    )
    assert resp.status_code == 201
    assert [item["candidate_index"] for item in resp.json()["items"]] == [0, 1]


def test_allocate_request_conflict_normalized_match_across_items(client):
    # 两项都只有同一个规范名（大小写不同）且库中无占用 -> 无可行组合
    resp = allocate(client, [_alloc_item("a", "oscar"), _alloc_item("b", "OSCAR")])
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "allocation_impossible"
    assert len(detail["items"]) == 2
    blocks = detail["items"][0]["candidates"][0]["blocks"]
    assert blocks[0]["kind"] == "request_conflict"
    assert blocks[0]["item_index"] == 1
    assert blocks[0]["client_id"] == "b"
    assert blocks[0]["candidate_index"] == 0
    assert blocks[0]["reason"] == "normalized_match"
    # 未写入任何记录
    assert client.get("/namespaces/chat/names").json() == []


def test_allocate_request_conflict_confusable_across_items(client):
    resp = allocate(client, [_alloc_item("a", "app"), _alloc_item("b", CYRILLIC_APP)])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["items"][0]["candidates"][0]["blocks"][0]["kind"] == "request_conflict"
    assert detail["items"][0]["candidates"][0]["blocks"][0]["reason"] == "confusable"


def test_allocate_409_lists_every_item_with_per_candidate_existing_conflicts(client):
    assert register(client, "alice").status_code == 201

    # 两项的唯一候选都与已注册的 alice 规范名相同
    resp = allocate(
        client, [_alloc_item("a1", "ALICE"), _alloc_item("a2", "Alice")]
    )
    assert resp.status_code == 409
    items = resp.json()["detail"]["items"]
    assert [item["index"] for item in items] == [0, 1]
    assert [item["client_id"] for item in items] == ["a1", "a2"]
    for item in items:
        blocks = item["candidates"][0]["blocks"]
        assert blocks[0]["kind"] == "existing_conflict"
        assert blocks[0]["source"] == "registered"
        assert blocks[0]["reason"] == "normalized_match"
        assert blocks[0]["original_name"] == "alice"
    # 无部分写入（仍只有最初的 alice）
    assert [n["original_name"] for n in client.get("/namespaces/chat/names").json()] == [
        "alice"
    ]


def test_allocate_409_group_infeasible_reports_request_conflicts_for_every_item(client):
    # 3 个申请项竞争 2 个候选名：无全局解；每个候选都给出与其他申请项候选的冲突
    resp = allocate(
        client,
        [
            _alloc_item("a", "aa", "bb"),
            _alloc_item("b", "aa", "bb"),
            _alloc_item("c", "aa", "bb"),
        ],
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert len(detail["items"]) == 3
    first_blocks = detail["items"][0]["candidates"][0]["blocks"]
    assert {b["kind"] for b in first_blocks} == {"request_conflict"}
    assert {b["item_index"] for b in first_blocks} == {1, 2}
    # 第二项的冲突对手是 0 和 2
    assert {
        b["item_index"]
        for b in detail["items"][1]["candidates"][0]["blocks"]
    } == {0, 2}


def test_allocate_mixed_script_candidate_skipped_and_registered_when_alternative_exists(
    client,
):
    resp = allocate(client, [_alloc_item("a", MIXED_ADMIN, "henry")])
    assert resp.status_code == 201
    assert resp.json()["items"][0]["candidate_index"] == 1
    assert [n["original_name"] for n in client.get("/namespaces/chat/names").json()] == [
        "henry"
    ]


def test_allocate_mixed_script_only_candidate_returns_409_with_script_detail(client):
    resp = allocate(client, [_alloc_item("a", MIXED_ADMIN)])
    assert resp.status_code == 409
    block = resp.json()["detail"]["items"][0]["candidates"][0]["blocks"][0]
    assert block["kind"] == "mixed_script"
    assert block["violating_script_pairs"] == [["Cyrillic", "Latin"]]
    assert "script_details" in block
    assert client.get("/namespaces/chat/names").json() == []


def test_allocate_respects_namespace_script_policy(client):
    assert (
        client.put(
            "/namespaces/ns/script-policy",
            json={"blocked_script_pairs": [["Han", "Latin"]], "expected_revision": 0},
        ).status_code
        == 200
    )
    # "世界x"（Han+Latin）在该空间违规 -> 回退纯拉丁候选
    resp = allocate(client, [_alloc_item("a", "世界x", "plain")], namespace="ns")
    assert resp.status_code == 201
    assert resp.json()["items"][0]["candidate_index"] == 1
    block_resp = allocate(client, [_alloc_item("b", "世界x")], namespace="ns")
    assert block_resp.status_code == 409
    assert (
        block_resp.json()["detail"]["items"][0]["candidates"][0]["blocks"][0]["kind"]
        == "mixed_script"
    )


def test_allocate_active_reservation_blocks_candidate(client):
    assert (
        client.post(
            "/namespaces/chat/reservations",
            json={"name": "reserved-x", "ttl_seconds": 120},
        ).status_code
        == 201
    )
    resp = allocate(client, [_alloc_item("a", "RESERVED-X", "alt-y")])
    assert resp.status_code == 201
    assert resp.json()["items"][0]["candidate_index"] == 1

    impossible = allocate(client, [_alloc_item("b", "RESERVED-X")])
    assert impossible.status_code == 409
    block = impossible.json()["detail"]["items"][0]["candidates"][0]["blocks"][0]
    assert block["kind"] == "existing_conflict"
    assert block["source"] == "reservation"
    assert "expires_at" in block


def test_allocate_active_cooldown_blocks_candidate(client):
    record = register(client, "coolname").json()
    deleted = client.request(
        "DELETE",
        f"/namespaces/chat/names/{record['id']}",
        json={"expected_revision": 1, "cooldown_seconds": 3600},
    )
    assert deleted.status_code == 200

    resp = allocate(client, [_alloc_item("a", "COOLNAME", "other-z")])
    assert resp.status_code == 201
    assert resp.json()["items"][0]["candidate_index"] == 1

    impossible = allocate(client, [_alloc_item("b", "COOLNAME")])
    assert impossible.status_code == 409
    block = impossible.json()["detail"]["items"][0]["candidates"][0]["blocks"][0]
    assert block["kind"] == "existing_conflict"
    assert block["source"] == "released"
    assert "expires_at" in block


def test_allocate_purges_expired_reservations_and_cooldowns_in_write_transaction(client):
    client.post(
        "/namespaces/ns/reservations", json={"name": "tempres", "ttl_seconds": 30}
    )
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_reservations SET expires_at = '2000-01-01T00:00:00.000Z'"
        )
        conn.commit()
    finally:
        conn.close()

    resp = allocate(client, [_alloc_item("a", "TEMPRES")], namespace="ns")
    assert resp.status_code == 201
    conn = database.get_connection()
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM name_reservations").fetchone()[0] == 0
        )
    finally:
        conn.close()


def test_allocate_413_when_search_exceeds_10000_states_and_writes_nothing(client):
    # 11 个申请项竞争 10 个互不冲突的候选名（任意两两规范化均不同），无解；
    # 回溯搜索状态数很快超过 10000
    items = [
        _alloc_item(f"c{i}", *[f"name{j}" for j in range(10)]) for i in range(11)
    ]
    resp = allocate(client, items)
    assert resp.status_code == 413, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "allocation_search_too_large"
    assert detail["states_explored"] > 10000
    assert detail["limit"] == 10000
    # 放弃搜索、不写任何记录
    assert client.get("/namespaces/chat/names").json() == []


def test_allocate_413_takes_precedence_and_keeps_existing_data(client):
    assert register(client, "keeper").status_code == 201
    items = [
        _alloc_item(f"c{i}", *[f"name{j}" for j in range(10)]) for i in range(11)
    ]
    resp = allocate(client, items)
    assert resp.status_code == 413
    names = [n["original_name"] for n in client.get("/namespaces/chat/names").json()]
    assert names == ["keeper"]


def test_allocate_solution_below_limit_still_succeeds_at_boundary(client):
    # 10 个申请项竞争 10 个候选名：恰好可行（分配 0..9），状态数远低于上限
    items = [
        _alloc_item(f"c{i}", *[f"slot{j}" for j in range(10)]) for i in range(10)
    ]
    resp = allocate(client, items)
    assert resp.status_code == 201, resp.text
    assert [item["candidate_index"] for item in resp.json()["items"]] == list(range(10))


def test_allocate_states_explored_counts_partial_assignments(client):
    resp = allocate(
        client, [_alloc_item("a", "u1"), _alloc_item("b", "u2"), _alloc_item("c", "u3")]
    )
    # 每个申请项扩展一次选择即一个搜索状态：3 项一次走通 = 3
    assert resp.json()["states_explored"] == 3


def test_allocate_request_validation(client):
    # 空 items -> 422
    assert allocate(client, []).status_code == 422
    # 21 个申请项 -> 422
    too_many = [_alloc_item(f"c{i}", "x") for i in range(21)]
    assert allocate(client, too_many).status_code == 422
    # client_id 重复 -> 422
    assert allocate(
        client, [_alloc_item("dup", "a"), _alloc_item("dup", "b")]
    ).status_code == 422
    # 候选为空 / 11 个候选 -> 422
    assert allocate(client, [{"client_id": "a", "candidates": []}]).status_code == 422
    assert allocate(
        client, [{"client_id": "a", "candidates": [f"c{i}" for i in range(11)]}]
    ).status_code == 422
    # client_id 空白 / 候选名空白 / 超长 -> 422
    assert allocate(client, [_alloc_item("   ", "x")]).status_code == 422
    assert allocate(client, [_alloc_item("a", "   ")]).status_code == 422
    # 缺字段 -> 422
    assert client.post(
        "/namespaces/chat/names/allocate", json={"items": [{"client_id": "a"}]}
    ).status_code == 422
    assert client.post(
        "/namespaces/chat/names/allocate",
        json={"items": [{"candidates": ["x"]}]},
    ).status_code == 422


def test_allocate_single_item_behaves_like_register(client):
    resp = allocate(client, [_alloc_item("solo", "soloname")])
    assert resp.status_code == 201
    item = resp.json()["items"][0]
    assert item["revision"] == 1
    assert item["normalized_name"] == "soloname"
    assert item["client_id"] == "solo"


def test_allocate_namespace_isolation(client):
    assert register(client, "shared", namespace="a").status_code == 201
    # 其他命名空间不受影响：同名可在 b 空间分配
    resp = allocate(client, [_alloc_item("a", "shared")], namespace="b")
    assert resp.status_code == 201


def test_allocate_duplicate_candidates_within_item_allowed(client):
    resp = allocate(client, [_alloc_item("dup", "samename", "samename", "othername")])
    assert resp.status_code == 201
    assert resp.json()["items"][0]["candidate_index"] == 0


def test_allocate_409_writes_no_partial_results(client):
    # 第 1 项可注册，第 2 项无候选：整体 409，第 1 项也不得落库
    resp = allocate(
        client,
        [
            _alloc_item("ok", "fine-name"),
            _alloc_item("blocked", MIXED_ADMIN),
        ],
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    # 两项都被列出；可注册项的唯一候选与混脚本项不冲突，但全局无解
    assert {item["client_id"] for item in detail["items"]} == {"ok", "blocked"}
    assert client.get("/namespaces/chat/names").json() == []


def test_allocate_failed_request_writes_nothing_and_successful_one_purges(client):
    # 预置一条过期冷却：失败的 allocate 整体回滚（含惰性清理 DELETE），
    # 不写入任何新名称；随后成功的 allocate 提交时才真正物理清理过期记录。
    record = register(client, "gone", namespace="ns").json()
    client.request(
        "DELETE",
        f"/namespaces/ns/names/{record['id']}",
        json={"expected_revision": 1, "cooldown_seconds": 3600},
    )
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_cooldowns SET expires_at = '2000-01-01T00:00:00.000Z' "
            "WHERE namespace = 'ns'"
        )
        conn.commit()
    finally:
        conn.close()

    resp = allocate(client, [_alloc_item("a", MIXED_ADMIN)], namespace="ns")
    assert resp.status_code == 409
    conn = database.get_connection()
    try:
        # 失败整体回滚：无新名称；惰性清理随事务一起回滚（与批量 / 预占等
        # 写事务失败时行为一致），过期冷却行仍在（但不影响判定）
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM display_names WHERE namespace = 'ns'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM name_cooldowns WHERE namespace = 'ns'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()

    # 成功的 allocate 提交时惰性清理过期冷却
    ok = allocate(client, [_alloc_item("b", "fresh-name")], namespace="ns")
    assert ok.status_code == 201
    conn = database.get_connection()
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM name_cooldowns WHERE namespace = 'ns'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_service_allocate_runs_in_single_immediate_transaction(client, monkeypatch):
    """服务层：allocate_names 全程使用单个 BEGIN IMMEDIATE 事务。

    成功与失败路径都不得在事务外写入；409 / 413 时连接回到无活动事务状态。
    sqlite3.Connection 为内置不可变类型、实例属性也只读，故用代理连接包装
    单个真实连接并统计 BEGIN IMMEDIATE 次数（service 只通过传入的 conn 访问）。
    """

    class CountingConnection:
        def __init__(self, inner):
            self._inner = inner
            self.begin_count = 0

        def execute(self, sql, *args, **kwargs):
            if sql == "BEGIN IMMEDIATE":
                self.begin_count += 1
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    real_conn = database.get_connection()
    conn = CountingConnection(real_conn)
    try:
        # 成功路径：恰好一个 BEGIN IMMEDIATE
        result = service.allocate_names(
            conn, "chat", [{"client_id": "a", "candidates": ["one", "two"]}]
        )
        assert result["items"][0]["candidate_index"] == 0
        assert conn.begin_count == 1
        assert real_conn.in_transaction is False

        # 无解路径：同样一个 BEGIN IMMEDIATE，结束已回滚、无残留事务
        with pytest.raises(service.AllocationImpossibleError):
            service.allocate_names(
                conn,
                "chat",
                [
                    {"client_id": "x", "candidates": ["one"]},
                    {"client_id": "y", "candidates": ["ONE"]},
                ],
            )
        assert conn.begin_count == 2
        assert real_conn.in_transaction is False

        # 搜索超限路径：再一个 BEGIN IMMEDIATE，回滚且不写入
        oversized = [
            {"client_id": f"c{i}", "candidates": [f"n{j}" for j in range(10)]}
            for i in range(11)
        ]
        with pytest.raises(service.AllocationSearchTooLargeError):
            service.allocate_names(conn, "chat", oversized)
        assert conn.begin_count == 3
        assert real_conn.in_transaction is False

        # 只有成功路径的 "one" 落库
        rows = service.list_names(real_conn, "chat")
        assert [r["original_name"] for r in rows] == ["one"]
    finally:
        real_conn.close()


def test_allocate_concurrent_requests_serialize_without_5xx(client):
    """两个并发 allocate 在写锁下串行：至多一次抢得冲突候选，整体无 5xx。"""
    from concurrent.futures import ThreadPoolExecutor

    def call(seq):
        return allocate(
            client,
            [
                _alloc_item(f"c{seq}-0", "contested", f"alt-{seq}-0"),
                _alloc_item(f"c{seq}-1", f"other-{seq}", f"alt-{seq}-1"),
            ],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(call, range(2)))
    assert all(resp.status_code in (201, 409) for resp in responses), responses
    # "contested" 至多被注册一次
    contested = [
        n
        for n in client.get("/namespaces/chat/names").json()
        if n["normalized_name"] == "contested"
    ]
    assert len(contested) == 1
    # 两个请求都成功时必须注册了 4 条互不冲突的名称
    assert all(resp.status_code == 201 for resp in responses)
    assert len(client.get("/namespaces/chat/names").json()) == 4


def test_openapi_documents_allocate_endpoint(client):
    operation = client.get("/openapi.json").json()["paths"][
        "/namespaces/{namespace}/names/allocate"
    ]["post"]
    assert "201" in operation["responses"]
    assert "409" in operation["responses"]
    assert "413" in operation["responses"]
    assert "422" in operation["responses"]


# ---------- 版本化迁移（schema_migrations） ----------


def _legacy_db_without_revision(db_path):
    """构造最老形态的数据库：display_names 无 revision 列，无其他业务表。

    created_at 默认值与索引均与迁移 v1 的历史 DDL 一致，保证接管基线后
    与空库从头迁移的最终结构完全对齐。
    """
    import sqlite3

    with sqlite3.connect(db_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE display_names (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace       TEXT NOT NULL,
                original_name   TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE (namespace, normalized_name)
            )
            """
        )
        legacy.execute(
            "CREATE INDEX idx_display_names_namespace "
            "ON display_names (namespace)"
        )
        legacy.execute(
            "INSERT INTO display_names (namespace, original_name, normalized_name) "
            "VALUES ('chat', 'Alice', 'alice')"
        )
        legacy.commit()


def _table_structure(db_path):
    """读取全部表结构（列名/类型/默认值）与索引名，用于跨库结构比对。"""
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        tables = {}
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ):
            tables[table] = [
                (row[1], row[2], row[3], row[4])
                for row in conn.execute(f"PRAGMA table_info({table})")
            ]
        indexes = sorted(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        )
    return tables, indexes


def test_migrate_fresh_database_records_all_versions(tmp_path):
    """空库从头迁移：schema_migrations 记录全部版本（名称/校验和/applied_at）。"""
    db_path = tmp_path / "fresh.db"
    applied = database.migrate(str(db_path))
    assert applied == [m.version for m in database.MIGRATIONS]

    # 幂等：再次迁移不再执行任何版本
    assert database.migrate(str(db_path)) == []

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT version, name, checksum, applied_at "
            "FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert [row[0] for row in rows] == [m.version for m in database.MIGRATIONS]
    for row, migration in zip(rows, database.MIGRATIONS, strict=True):
        assert row[1] == migration.name
        assert row[2] == migration.checksum  # SHA-256 校验和随记录落库
        assert row[3]  # applied_at 非空


def test_migrate_legacy_takeover_establishes_baseline(tmp_path):
    """接管既有库：按表/列状态建立基线，只执行缺失迁移，最终结构与空库一致。"""
    legacy_path = tmp_path / "legacy.db"
    _legacy_db_without_revision(legacy_path)

    applied = database.migrate(str(legacy_path))
    # display_names 已存在（v1 基线），其余迁移按序补齐
    assert applied == [2, 3, 4, 5]
    assert 1 not in applied

    import sqlite3

    with sqlite3.connect(legacy_path) as conn:
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == [1, 2, 3, 4, 5]
        # 既有数据无损，revision 回填为 1
        assert conn.execute(
            "SELECT id, revision FROM display_names"
        ).fetchall() == [(1, 1)]

    # 与空库从头迁移的最终结构完全一致
    fresh_path = tmp_path / "fresh.db"
    database.migrate(str(fresh_path))
    assert _table_structure(legacy_path) == _table_structure(fresh_path)


def test_migrate_fails_when_recorded_migration_file_rewritten(tmp_path, monkeypatch):
    """已记录迁移文件被改写（校验和不匹配）：启动失败并指明具体版本。"""
    db_path = tmp_path / "tampered.db"
    database.migrate(str(db_path))

    original = database.MIGRATIONS
    hacked = tuple(
        database.Migration(m.version, m.name, m.statements + ("SELECT 1",))
        if m.version == 3
        else m
        for m in original
    )
    monkeypatch.setattr(database, "MIGRATIONS", hacked)
    monkeypatch.setattr(
        database, "_MIGRATIONS_BY_VERSION", {m.version: m for m in hacked}
    )

    with pytest.raises(database.MigrationError) as exc_info:
        database.init_db(str(db_path))
    assert "3" in str(exc_info.value)
    assert "add_display_names_revision" in str(exc_info.value)


def test_migrate_fails_on_version_gap(tmp_path):
    """版本历史出现缺口（中间版本缺失但更高版本已记录）：启动失败并指明版本。"""
    db_path = tmp_path / "gap.db"
    database.migrate(str(db_path))

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = 2")
        conn.commit()

    with pytest.raises(database.MigrationError) as exc_info:
        database.init_db(str(db_path))
    assert "2" in str(exc_info.value)
    assert "缺口" in str(exc_info.value)


def test_migrate_fails_on_unknown_recorded_version(tmp_path):
    """记录了迁移文件中不存在的版本：启动失败并指明具体版本。"""
    db_path = tmp_path / "unknown.db"
    database.migrate(str(db_path))

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum) "
            "VALUES (99, 'future_migration', 'deadbeef')"
        )
        conn.commit()

    with pytest.raises(database.MigrationError) as exc_info:
        database.init_db(str(db_path))
    assert "99" in str(exc_info.value)


def test_migration_status_is_read_only_and_reports_issues(tmp_path):
    """status 只读：不建表不建基线；返回已应用 / 待应用 / 校验异常。"""
    db_path = tmp_path / "status.db"
    _legacy_db_without_revision(db_path)

    # 既有库未迁移：全部待应用，且不创建 schema_migrations（只读）
    status = database.migration_status(str(db_path))
    assert status["applied"] == []
    assert [item["version"] for item in status["pending"]] == [1, 2, 3, 4, 5]
    assert status["issues"] == []

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "schema_migrations" not in tables

    # 迁移后：全部已应用、无待应用、无异常
    database.migrate(str(db_path))
    status = database.migration_status(str(db_path))
    assert [item["version"] for item in status["applied"]] == [1, 2, 3, 4, 5]
    assert status["pending"] == []
    assert status["issues"] == []

    # 篡改校验和：status 报告异常但不修改数据
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 4"
        )
        conn.commit()
    status = database.migration_status(str(db_path))
    assert status["issues"] == [
        {
            "version": 4,
            "type": "checksum_mismatch",
            "message": status["issues"][0]["message"],
        }
    ]
    assert "4" in status["issues"][0]["message"]


def test_migration_status_on_missing_database_reports_all_pending(tmp_path):
    """数据库文件不存在时 status 不创建文件，全部迁移视为待应用。"""
    db_path = tmp_path / "missing.db"
    status = database.migration_status(str(db_path))
    assert status["applied"] == []
    assert [item["version"] for item in status["pending"]] == [1, 2, 3, 4, 5]
    assert status["issues"] == []
    assert not db_path.exists()


def test_concurrent_migrate_applies_each_version_once(tmp_path):
    """多线程并发启动迁移：同一迁移只执行一次，最终版本各记录一条。"""
    db_path = tmp_path / "race.db"
    _legacy_db_without_revision(db_path)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: database.migrate(str(db_path)), range(4))
        )

    # 每个版本在全部进程中至多被执行一次
    applied = [version for result in results for version in result]
    assert sorted(applied) == [2, 3, 4, 5]

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT version, COUNT(*) FROM schema_migrations GROUP BY version"
        ).fetchall()
        assert rows == [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1)]
        assert conn.execute(
            "SELECT revision FROM display_names WHERE id = 1"
        ).fetchone() == (1,)


def test_database_cli_migrate_and_status(tmp_path, monkeypatch, capsys):
    """命令行：migrate 幂等执行迁移，status 只读输出 JSON；校验失败退出码为 1。"""
    db_path = tmp_path / "cli.db"
    monkeypatch.setattr(database, "DATABASE_PATH", str(db_path))

    assert database.main(["migrate"]) == 0
    assert "applied versions: [1, 2, 3, 4, 5]" in capsys.readouterr().out
    # 幂等：再次执行无待应用迁移
    assert database.main(["migrate"]) == 0
    assert "up to date" in capsys.readouterr().out

    assert database.main(["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert [item["version"] for item in status["applied"]] == [1, 2, 3, 4, 5]
    assert status["pending"] == []
    assert status["issues"] == []

    # 篡改已记录迁移：migrate 以退出码 1 失败并指明版本
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 2"
        )
        conn.commit()
    assert database.main(["migrate"]) == 1
    assert "2" in capsys.readouterr().err
