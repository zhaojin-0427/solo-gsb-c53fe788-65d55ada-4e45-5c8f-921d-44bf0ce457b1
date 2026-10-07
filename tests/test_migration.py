"""请求迁移评估（Request Migration）引擎与 HTTP API 测试。"""
import io
import json
import os
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def _zip_bytes(folder: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(folder):
            for fn in files:
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, folder))
    return buf.getvalue()


def _ndjson(rows) -> bytes:
    return ("\n".join(
        r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
        for r in rows)).encode()


def _spec(ops):
    return {"openapi": "3.0.3", "info": {"title": "b", "version": "1"},
            "paths": ops}


def _analysis_from_docs(client, baseline: dict, candidate=None):
    candidate = candidate or baseline
    r = client.post(
        "/api/analyses",
        files={
            "baseline": ("b.json", json.dumps(baseline).encode(),
                         "application/json"),
            "candidate": ("c.json", json.dumps(candidate).encode(),
                          "application/json"),
        })
    assert r.status_code == 201, r.text
    return r.json()["analysis_id"]


def _upload(client, aid, rows):
    return client.post(
        f"/api/analyses/{aid}/migration",
        files={"file": ("requests.ndjson", _ndjson(rows),
                        "application/x-ndjson")})


def _get_op(parameters=None, body=None, required=False):
    op = {"responses": {"200": {"description": "d"}}}
    if parameters:
        op["parameters"] = parameters
    if body is not None:
        op["requestBody"] = {
            "required": required,
            "content": {"application/json": {"schema": body}}}
    return op


# ============================================================== 行格式
def test_request_lines_and_legacy_call_lines(client):
    """请求行（无 response）与旧调用行（含 response）都可解析；
    response 被整体忽略——即使响应在覆盖口径下会失败，迁移只看请求。"""
    spec = _spec({"/p": {"get": _get_op()}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, [
        {"method": "GET", "path": "/p", "request": {}},          # 请求行
        {"method": "GET", "path": "/p", "query": {"a": "1"},
         "request": {},
         "response": {"status": 999, "media_type": "text/csv"}},  # 旧调用行
    ]).json()
    assert doc["summary"]["requests_total"] == 2
    assert [r["classification"] for r in doc["rows"]] == [
        "unaffected", "unaffected"]
    assert doc["rows"][1]["baseline"]["state"] == "pass"


def test_invalid_lines_counted_and_all_invalid_rejected(client):
    spec = _spec({"/p": {"get": _get_op()}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, [
        "not json",
        {"method": "GET", "path": "/p"},
    ]).json()
    assert doc["summary"]["invalid"] == 1
    assert doc["rows"][0]["classification"] == "invalid"
    assert doc["rows"][0]["baseline"] is None
    assert doc["rows"][1]["classification"] == "unaffected"

    r = _upload(client, aid, ["not json", '{"method": 1}'])
    assert r.status_code == 400


# ============================================================== 分类规则
def test_candidate_missing_endpoint_is_affected(client):
    baseline = _spec({"/legacy": {"post": _get_op()}})
    candidate = _spec({"/other": {"get": _get_op()}})
    aid = _analysis_from_docs(client, baseline, candidate)
    doc = _upload(client, aid, [
        {"method": "POST", "path": "/legacy"},
    ]).json()
    row = doc["rows"][0]
    assert row["classification"] == "affected"
    assert row["baseline"]["state"] == "pass"
    assert row["candidate"]["state"] == "fail"
    assert "没有路径模板匹配" in row["candidate"]["reasons"][0]
    # 候选缺端点的受影响请求按基线端点汇总
    ep = doc["endpoints"][0]
    assert ep["operation"] == "POST /legacy"
    assert ep["affected"] == 1 and ep["affected_lines"] == [1]
    assert ep["candidate_operations"] == []


def test_exact_path_without_method_is_definite_fail(client):
    baseline = _spec({"/p": {"post": _get_op()}})
    candidate = _spec({"/p": {"get": _get_op()}})
    aid = _analysis_from_docs(client, baseline, candidate)
    doc = _upload(client, aid, [
        {"method": "POST", "path": "/p"},
    ]).json()
    row = doc["rows"][0]
    assert row["classification"] == "affected"
    assert row["candidate"]["state"] == "fail"
    assert "未声明 POST 方法" in row["candidate"]["reasons"][0]


def test_both_sides_pass_unaffected(client):
    spec = _spec({"/p": {"get": _get_op(parameters=[
        {"name": "n", "in": "query",
         "schema": {"type": "integer", "maximum": 100}}])}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, [
        {"method": "GET", "path": "/p", "query": {"n": "7"}},
    ]).json()
    row = doc["rows"][0]
    assert row["classification"] == "unaffected"
    assert row["baseline"]["state"] == "pass"
    assert row["candidate"]["state"] == "pass"
    assert row["reasons"] == []


def test_baseline_fail_is_not_applicable(client):
    spec = _spec({"/p": {"get": _get_op(parameters=[
        {"name": "status", "in": "query", "required": True,
         "schema": {"type": "string", "enum": ["a", "b"]}}])}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, [
        {"method": "GET", "path": "/p", "query": {"status": "zzz"}},
        {"method": "GET", "path": "/nowhere"},
    ]).json()
    r1, r2 = doc["rows"]
    assert r1["classification"] == "not_applicable"
    assert r1["baseline"]["state"] == "fail"
    assert r1["candidate"]["state"] == "fail"
    assert r1["reasons"] == r1["baseline"]["reasons"]
    assert r2["classification"] == "not_applicable"
    # 未归属基线端点的行不进入端点汇总
    assert all(ep["operation"] != "GET /nowhere" for ep in doc["endpoints"])


def test_param_made_required_and_narrowed_affected(client):
    baseline = _spec({"/p": {"get": _get_op(parameters=[
        {"name": "status", "in": "query",
         "schema": {"type": "string", "enum": ["a", "b"]}},
        {"name": "limit", "in": "query",
         "schema": {"type": "integer", "maximum": 100}}])}})
    candidate = _spec({"/p": {"get": _get_op(parameters=[
        {"name": "status", "in": "query", "required": True,
         "schema": {"type": "string", "enum": ["a"]}},
        {"name": "limit", "in": "query",
         "schema": {"type": "integer", "maximum": 50}}])}})
    aid = _analysis_from_docs(client, baseline, candidate)
    doc = _upload(client, aid, [
        {"method": "GET", "path": "/p"},                          # 缺必填
        {"method": "GET", "path": "/p", "query": {"status": "b"}},  # 枚举收窄
        {"method": "GET", "path": "/p",
         "query": {"status": "a", "limit": "80"}},                # 上界压低
        {"method": "GET", "path": "/p",
         "query": {"status": "a", "limit": "10"}},                # 两侧均过
    ]).json()
    classes = [r["classification"] for r in doc["rows"]]
    assert classes == ["affected", "affected", "affected", "unaffected"]
    assert "缺少必填查询参数" in doc["rows"][0]["reasons"][0]
    assert "枚举" in doc["rows"][1]["reasons"][0]
    assert "maximum" in doc["rows"][2]["reasons"][0]
    s = doc["summary"]
    assert s["affected"] == 3 and s["unaffected"] == 1
    ep = doc["endpoints"][0]
    assert ep["affected_lines"] == [1, 2, 3]


# ============================================================== 模板变量改名
def test_template_variable_rename_rebinds_per_side(client):
    """模板变量改名（{itemId} → {item_id}）时，候选侧按自己的模板重新
    绑定路径取值并校验候选参数约束，不复用基线绑定。"""
    baseline = _spec({"/items/{itemId}": {"get": _get_op(parameters=[
        {"name": "itemId", "in": "path", "required": True,
         "schema": {"type": "integer"}}])}})
    candidate = _spec({"/items/{item_id}": {"get": _get_op(parameters=[
        {"name": "item_id", "in": "path", "required": True,
         "schema": {"type": "integer", "maximum": 5}}])}})
    aid = _analysis_from_docs(client, baseline, candidate)
    doc = _upload(client, aid, [
        {"method": "GET", "path": "/items/7"},
    ]).json()
    row = doc["rows"][0]
    assert row["classification"] == "affected"
    assert row["baseline"]["matched_template"] == "/items/{itemId}"
    assert row["candidate"]["matched_template"] == "/items/{item_id}"
    # 失败原因引用的是候选侧改名后的参数与其约束，而非基线绑定
    assert "item_id" in row["candidate"]["reasons"][0]
    assert "maximum" in row["candidate"]["reasons"][0]


# ============================================================== 歧义与未支持约束
def test_multi_template_ambiguity_is_pending(client):
    baseline = _spec({"/a/{x}": {"get": _get_op(parameters=[
        {"name": "x", "in": "path", "required": True,
         "schema": {"type": "integer"}}])}})
    candidate = _spec({
        "/a/{x}": {"get": _get_op(parameters=[
            {"name": "x", "in": "path", "required": True,
             "schema": {"type": "integer"}}])},
        "/a/{y}": {"get": _get_op(parameters=[
            {"name": "y", "in": "path", "required": True,
             "schema": {"type": "integer"}}])},
    })
    aid = _analysis_from_docs(client, baseline, candidate)
    doc = _upload(client, aid, [
        {"method": "GET", "path": "/a/1"},
    ]).json()
    row = doc["rows"][0]
    assert row["baseline"]["state"] == "pass"
    assert row["candidate"]["state"] == "pending"
    assert "同时命中" in row["candidate"]["reasons"][0]
    assert row["classification"] == "pending"


def test_unsupported_constraint_is_pending(client):
    baseline = _spec({"/p": {"get": _get_op(parameters=[
        {"name": "q", "in": "query",
         "schema": {"type": "string"}}])}})
    candidate = _spec({"/p": {"get": _get_op(parameters=[
        {"name": "q", "in": "query",
         "schema": {"type": "string", "pattern": "^[a-z]+$"}}])}})
    aid = _analysis_from_docs(client, baseline, candidate)
    doc = _upload(client, aid, [
        {"method": "GET", "path": "/p", "query": {"q": "abc"}},
    ]).json()
    row = doc["rows"][0]
    assert row["baseline"]["state"] == "pass"
    assert row["candidate"]["state"] == "pending"
    assert any("pattern" in x for x in row["candidate"]["reasons"])
    assert row["classification"] == "pending"
    # 待核行汇总在端点上但不计受影响
    ep = doc["endpoints"][0]
    assert ep["pending"] == 1 and ep["affected"] == 0


def test_baseline_pending_never_counts_affected(client):
    """基线一侧待定时，即使候选确定失败也只能记待核。"""
    baseline = _spec({"/p": {"post": _get_op(
        body={"oneOf": [{"type": "object"},
                        {"type": "object", "not": {"type": "string"}}]},
        required=True)}})
    candidate = _spec({"/p": {"post": _get_op(
        body={"type": "string"}, required=True)}})
    aid = _analysis_from_docs(client, baseline, candidate)
    doc = _upload(client, aid, [
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/json", "body": {}}},
    ]).json()
    row = doc["rows"][0]
    assert row["baseline"]["state"] == "pending"
    assert row["candidate"]["state"] == "fail"
    assert row["classification"] == "pending"
    assert any(r.startswith("基线：") for r in row["reasons"])


# ============================================================== 请求体
def test_request_body_required_property_added_affected(client):
    baseline = _spec({"/p": {"post": _get_op(
        body={"type": "object", "required": ["name"],
              "properties": {"name": {"type": "string"},
                             "kind": {"type": "string"}}},
        required=True)}})
    candidate = _spec({"/p": {"post": _get_op(
        body={"type": "object", "required": ["name", "kind"],
              "properties": {"name": {"type": "string"},
                             "kind": {"type": "string"}}},
        required=True)}})
    aid = _analysis_from_docs(client, baseline, candidate)
    doc = _upload(client, aid, [
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/json",
                     "body": {"name": "x"}}},
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/json",
                     "body": {"name": "x", "kind": "y"}}},
        {"method": "POST", "path": "/p"},   # 两侧都缺必填请求体 → 不适用
    ]).json()
    classes = [r["classification"] for r in doc["rows"]]
    assert classes == ["affected", "unaffected", "not_applicable"]
    assert "kind" in doc["rows"][0]["reasons"][0]


def test_non_json_request_media_is_pending(client):
    spec = _spec({"/p": {"post": _get_op(
        body={"type": "object"}, required=True)}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, [
        {"method": "POST", "path": "/p",
         "request": {"media_type": "text/csv", "body": "a,b"}},
    ]).json()
    row = doc["rows"][0]
    assert row["baseline"]["state"] == "pending"
    assert row["candidate"]["state"] == "pending"
    assert "非 JSON" in row["baseline"]["reasons"][0]
    assert row["classification"] == "pending"


def test_undeclared_request_media_is_definite_fail(client):
    spec = _spec({"/p": {"post": _get_op(
        body={"type": "object"}, required=True)}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, [
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/vnd.x+json",
                     "body": {}}},
    ]).json()
    row = doc["rows"][0]
    assert row["classification"] == "not_applicable"
    assert "未在 requestBody.content 中声明" in row["baseline"]["reasons"][0]


# ============================================================== 持久化与 API
@pytest.fixture(scope="module")
def example_analysis_id(client):
    r = client.post(
        "/api/analyses",
        files={
            "baseline": ("b.zip", _zip_bytes("examples/baseline"),
                         "application/zip"),
            "candidate": ("c.zip", _zip_bytes("examples/candidate"),
                          "application/zip"),
        })
    assert r.status_code == 201, r.text
    return r.json()["analysis_id"]


def test_example_requests_summary(client, example_analysis_id):
    with open("examples/requests-baseline.ndjson", "rb") as fh:
        raw = fh.read()
    r = client.post(f"/api/analyses/{example_analysis_id}/migration",
                    files={"file": ("requests-baseline.ndjson", raw,
                                    "application/x-ndjson")})
    assert r.status_code == 201, r.text
    doc = r.json()
    s = doc["summary"]
    assert s["requests_total"] == 14
    assert s["affected"] == 5
    assert s["unaffected"] == 4
    assert s["not_applicable"] == 3
    assert s["pending"] == 2
    assert s["invalid"] == 0
    assert s["endpoints_affected"] == 3
    # 端点汇总：受影响端点含 GET /pets、POST /pets、POST /legacy/echo
    affected_ops = {e["operation"] for e in doc["endpoints"]
                    if e["affected"] > 0}
    assert affected_ops == {"GET /pets", "POST /pets", "POST /legacy/echo"}
    # 旧调用行（含 response）被忽略响应后正常评估
    legacy = next(r for r in doc["rows"] if r["path"] == "/legacy/echo")
    assert legacy["classification"] == "affected"
    assert legacy["candidate"]["state"] == "fail"


def test_migration_persistence_export_delete(client, example_analysis_id):
    aid = example_analysis_id
    with open("examples/requests-baseline.ndjson", "rb") as fh:
        raw = fh.read()
    r = client.post(f"/api/analyses/{aid}/migration",
                    files={"file": ("requests.ndjson", raw,
                                    "application/x-ndjson")})
    assert r.status_code == 201, r.text

    got = client.get(f"/api/analyses/{aid}/migration")
    assert got.status_code == 200
    assert got.json()["summary"] == r.json()["summary"]

    # 确定性导出：字节一致 + SHA-256 指纹头
    e1 = client.get(f"/api/analyses/{aid}/migration/export")
    e2 = client.get(f"/api/analyses/{aid}/migration/export")
    assert e1.status_code == 200
    assert e1.content == e2.content
    import hashlib
    assert hashlib.sha256(e1.content).hexdigest() == \
        e1.headers["x-migration-fingerprint"]

    # 分析总导出内嵌 request_migration，原 call_coverage 语义不变
    full = client.get(f"/api/analyses/{aid}/export").json()
    assert full["request_migration"]["summary"] == r.json()["summary"]

    # 覆盖结果不受迁移上传影响
    cov = client.post(
        f"/api/analyses/{aid}/coverage/baseline",
        files={"file": ("calls.ndjson",
                        open("examples/calls-baseline.ndjson", "rb").read(),
                        "application/x-ndjson")})
    assert cov.status_code == 201, cov.text
    full2 = client.get(f"/api/analyses/{aid}/export").json()
    assert "call_coverage" in full2 and "request_migration" in full2

    d = client.delete(f"/api/analyses/{aid}/migration")
    assert d.status_code == 200
    assert client.get(f"/api/analyses/{aid}/migration").status_code == 404
    # 覆盖不受迁移删除影响
    assert client.get(
        f"/api/analyses/{aid}/coverage/baseline").status_code == 200


def test_migration_reupload_replaces(client):
    spec = _spec({"/p": {"get": _get_op()}})
    aid = _analysis_from_docs(client, spec)
    _upload(client, aid, [{"method": "GET", "path": "/p"}])
    _upload(client, aid, [{"method": "GET", "path": "/p"},
                          {"method": "GET", "path": "/p"}])
    doc = client.get(f"/api/analyses/{aid}/migration").json()
    assert doc["summary"]["requests_total"] == 2


def test_migration_deleted_with_analysis(client):
    spec = _spec({"/p": {"get": _get_op()}})
    aid = _analysis_from_docs(client, spec)
    _upload(client, aid, [{"method": "GET", "path": "/p"}])
    assert client.get(f"/api/analyses/{aid}/migration").status_code == 200
    assert client.delete(f"/api/analyses/{aid}").status_code == 200
    assert client.get(f"/api/analyses/{aid}/migration").status_code == 404


def test_migration_unknown_analysis_404(client):
    assert client.get("/api/analyses/nope/migration").status_code == 404
    assert client.delete("/api/analyses/nope/migration").status_code == 404
    r = client.post("/api/analyses/nope/migration",
                    files={"file": ("r.ndjson", b"{}\n",
                                    "application/x-ndjson")})
    assert r.status_code == 404
    # 未上传时查询/删除 404
    spec = _spec({"/p": {"get": _get_op()}})
    aid = _analysis_from_docs(client, spec)
    assert client.get(f"/api/analyses/{aid}/migration").status_code == 404
    assert client.delete(f"/api/analyses/{aid}/migration").status_code == 404
    assert client.get(
        f"/api/analyses/{aid}/migration/export").status_code == 404
