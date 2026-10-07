"""调用覆盖（Call Coverage）引擎与 HTTP API 测试。"""
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


@pytest.fixture(scope="module")
def analysis_id(client):
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


def _upload(client, aid, side, rows):
    return client.post(
        f"/api/analyses/{aid}/coverage/{side}",
        files={"file": ("calls.ndjson", _ndjson(rows),
                        "application/x-ndjson")})


# ============================================================== engine cases
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


PET_LIST_OP = {
    "parameters": [
        {"name": "status", "in": "query",
         "schema": {"type": "string", "enum": ["a", "b"]}},
        {"name": "limit", "in": "query", "required": False,
         "schema": {"type": "integer", "minimum": 1, "maximum": 100}},
    ],
    "responses": {
        "200": {"description": "ok", "content": {"application/json": {
            "schema": {"type": "array", "items": {
                "type": "object", "required": ["id"],
                "properties": {"id": {"type": "integer"}}}}}}},
        "400": {"description": "bad"},
        "default": {"description": "err", "content": {"application/json": {
            "schema": {"type": "object"}}}},
    },
}


def _spec(ops):
    return {"openapi": "3.0.3", "info": {"title": "b", "version": "1"},
            "paths": ops}


def simple_spec():
    return _spec({"/p": {"get": {"responses": {"200": {"description": "d"}}}}})


def test_covered_grid_and_gaps(client):
    aid = _analysis_from_docs(client, _spec({"/pets": {"get": PET_LIST_OP}}))
    rows = [
        {"method": "GET", "path": "/pets", "query": {"limit": "10"},
         "request": {},
         "response": {"status": 200, "media_type": "application/json",
                      "body": [{"id": 1}]}},
        {"method": "GET", "path": "/pets", "request": {},
         "response": {"status": 400}},
    ]
    r = _upload(client, aid, "baseline", rows)
    assert r.status_code == 201, r.text
    doc = r.json()
    s = doc["summary"]
    assert s["cells_total"] == 3
    assert s["cells_covered"] == 2
    assert s["cells_gap"] == 1
    op = doc["operations"][0]
    all_keys = {c["key"] for c in op["cells"]} | \
        {c["key"] for c in op["gaps"]}
    assert all_keys == {
        "200|application/json", "400|(no content)",
        "default|application/json"}
    assert op["covered"] == 2 and op["total"] == 3
    gap_keys = {c["key"] for c in op["gaps"]}
    assert gap_keys == {"default|application/json"}


def test_status_resolution_exact_range_default(client):
    spec = _spec({"/p": {"get": {"responses": {
        "200": {"description": "ok", "content": {"application/json": {
            "schema": {"type": "object"}}}},
        "2XX": {"description": "range", "content": {"application/json": {
            "schema": {"type": "object"}}}},
        "default": {"description": "def", "content": {"application/json": {
            "schema": {"type": "object"}}}}}}}})
    aid = _analysis_from_docs(client, spec)
    rows = [
        {"method": "GET", "path": "/p",
         "response": {"status": 200, "media_type": "application/json",
                      "body": {}}},
        {"method": "GET", "path": "/p",
         "response": {"status": 204, "media_type": "application/json",
                      "body": {}}},
        {"method": "GET", "path": "/p",
         "response": {"status": 500, "media_type": "application/json",
                      "body": {}}},
    ]
    doc = _upload(client, aid, "baseline", rows).json()
    by_status = {r["resolved_status"]: r["outcome"]
                 for r in doc["rows"] if r["outcome"] == "covered"}
    assert by_status == {"200": "covered", "2XX": "covered",
                         "default": "covered"}


def test_no_content_cell_and_entity_mismatch(client):
    spec = _spec({"/p": {"get": {"responses": {"204": {"description": "x"}}}}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p", "response": {"status": 204}},
        {"method": "GET", "path": "/p",
         "response": {"status": 204, "media_type": "application/json",
                      "body": {}}},
    ]).json()
    assert doc["rows"][0]["outcome"] == "covered"
    assert doc["rows"][1]["outcome"] == "validation_failed"
    assert any("无 content" in x for x in doc["rows"][1]["reasons"])


def test_exact_path_precedence(client):
    spec = _spec({
        "/pets/search": {"get": {"responses": {"200": {"description": "d"}}}},
        "/pets/{id}": {"get": {"responses": {"200": {"description": "d"}}}},
    })
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/pets/search",
         "response": {"status": 200}},
    ]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "covered"
    assert row["operation"] == "GET /pets/search"


def test_ambiguous_template(client):
    spec = _spec({
        "/a/{x}": {"get": {"responses": {"200": {"description": "d"}}}},
        "/a/{y}": {"get": {"responses": {"200": {"description": "d"}}}},
    })
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/a/v", "response": {"status": 200}},
    ]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "ambiguous"
    assert "同时命中" in row["reasons"][0]
    assert doc["summary"]["calls_ambiguous"] == 1


def test_unmatched_wrong_method_then_no_template(client):
    spec = _spec({"/p": {"get": {"responses": {"200": {"description": "d"}}}}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "POST", "path": "/p", "response": {"status": 200}},
        {"method": "GET", "path": "/q", "response": {"status": 200}},
    ]).json()
    assert doc["rows"][0]["outcome"] == "unmatched"
    assert "方法" in doc["rows"][0]["reasons"][0]
    assert doc["rows"][1]["outcome"] == "unmatched"


def test_parameter_validation_coercion_and_required(client):
    spec = _spec({"/p": {"get": {"parameters": [
        {"name": "n", "in": "query", "required": True,
         "schema": {"type": "integer", "minimum": 1, "maximum": 100}}],
        "responses": {"200": {"description": "d"}}}}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p", "query": {"n": "7"},
         "response": {"status": 200}},                       # coerce ok
        {"method": "GET", "path": "/p",
         "response": {"status": 200}},                       # missing req
        {"method": "GET", "path": "/p", "query": {"n": "x"},
         "response": {"status": 200}},                       # bad value
    ]).json()
    assert [r["outcome"] for r in doc["rows"]] == [
        "covered", "validation_failed", "validation_failed"]


def test_path_param_validation(client):
    spec = _spec({"/p/{id}": {"get": {"parameters": [
        {"name": "id", "in": "path", "required": True,
         "schema": {"type": "integer"}}],
        "responses": {"200": {"description": "d"}}}}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p/42", "response": {"status": 200}},
        {"method": "GET", "path": "/p/abc", "response": {"status": 200}},
    ]).json()
    assert doc["rows"][0]["outcome"] == "covered"
    assert doc["rows"][1]["outcome"] == "validation_failed"


def test_request_body_required_and_schema(client):
    body_schema = {"type": "object", "required": ["name"],
                   "properties": {"name": {"type": "string", "minLength": 1}}}
    op = {
        "requestBody": {"required": True, "content": {
            "application/json": {"schema": body_schema}}},
        "responses": {"201": {"description": "c", "content": {
            "application/json": {"schema": {"type": "object"}}}}},
    }
    spec = _spec({"/p": {"post": op}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/json",
                     "body": {"name": "x"}},
         "response": {"status": 201, "media_type": "application/json",
                      "body": {}}},
        {"method": "POST", "path": "/p", "request": {},
         "response": {"status": 201, "media_type": "application/json",
                      "body": {}}},
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/json", "body": {}},
         "response": {"status": 201, "media_type": "application/json",
                      "body": {}}},
    ]).json()
    assert [r["outcome"] for r in doc["rows"]] == [
        "covered", "validation_failed", "validation_failed"]


def test_non_json_media_excluded_and_zero_denominator(client):
    spec = _spec({
        "/xml": {"get": {"responses": {"200": {"description": "d",
            "content": {"application/xml": {"schema": {
                "type": "string"}}}}}}},
        "/json": {"get": {"responses": {"200": {"description": "d",
            "content": {"application/json": {"schema": {
                "type": "object"}}}}}}},
    })
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/xml",
         "response": {"status": 200, "media_type": "application/xml",
                      "body": "<x/>"}},
        {"method": "GET", "path": "/json",
         "response": {"status": 200, "media_type": "application/json",
                      "body": {}}},
    ]).json()
    assert doc["rows"][0]["outcome"] == "excluded_media"
    assert doc["rows"][1]["outcome"] == "covered"
    ops = {o["path"]: o for o in doc["operations"]}
    assert ops["/xml"]["applicable"] is False
    assert ops["/xml"]["note"]
    assert ops["/json"]["applicable"] is True
    # the xml operation's excluded media is reported
    assert ops["/xml"]["excluded_media"][0]["media_type"] == "application/xml"


def test_undetermined_for_unresolvable_schema(client):
    spec = _spec({"/p": {"get": {"parameters": [
        {"name": "t", "in": "query",
         "schema": {"$ref": "https://e.example.com/s#/T"}}],
        "responses": {"200": {"description": "d"}}}}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p", "query": {"t": "v"},
         "response": {"status": 200}},
    ]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "undetermined"
    assert any("无法判定" in x for x in row["reasons"])


def test_invalid_lines_are_listed(client):
    simple = _spec({"/p": {"get": {"responses": {"200": {"description": "d"}}}}})
    aid = _analysis_from_docs(client, simple)
    doc = _upload(client, aid, "baseline", [
        "not-a-json-line",
        json.dumps({"method": "GET", "path": "/p",
                    "response": {"status": 200}}),
        json.dumps({"method": "WAT", "path": "/p",
                    "response": {"status": 200}}),
        json.dumps({"method": "GET", "path": "p",
                    "response": {"status": 200}}),
        json.dumps({"method": "GET", "path": "/p",
                    "response": {"status": 999}}),
    ]).json()
    outcomes = {r["line"]: r["outcome"] for r in doc["rows"]}
    assert outcomes[1] == "invalid"
    assert outcomes[2] == "covered"
    assert outcomes[3] == "invalid"
    assert outcomes[4] == "invalid"
    assert outcomes[5] == "invalid"
    assert doc["summary"]["calls_invalid"] == 4


def test_empty_file_rejected(client):
    aid = _analysis_from_docs(client, simple_spec())
    r = client.post(
        f"/api/analyses/{aid}/coverage/baseline",
        files={"file": ("empty.ndjson", b"", "application/x-ndjson")})
    assert r.status_code == 400


def test_response_status_undeclared_fails(client):
    aid = _analysis_from_docs(client, simple_spec())
    doc = _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p", "response": {"status": 404}},
    ]).json()
    assert doc["rows"][0]["outcome"] == "validation_failed"
    assert any("未在操作响应中声明" in x for x in doc["rows"][0]["reasons"])


# ============================================================== 未支持关键字
def test_unsupported_keyword_in_parameter(client):
    spec = _spec({"/p": {"get": {"parameters": [
        {"name": "q", "in": "query",
         "schema": {"type": "string", "pattern": "^a"}}],
        "responses": {"200": {"description": "d"}}}}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p", "query": {"q": "abc"},
         "response": {"status": 200}},
    ]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "undetermined"
    # 列出关键字与位置
    assert any("pattern" in x and "#/paths/" in x for x in row["reasons"])
    # 不计覆盖
    assert doc["summary"]["cells_covered"] == 0
    assert doc["summary"]["calls_undetermined"] == 1


def test_unsupported_keyword_in_request_body_via_ref(client):
    spec = _spec({"/p": {"post": {
        "requestBody": {"required": True, "content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Payload"}}}},
        "responses": {"200": {"description": "d"}}}}})
    spec["components"] = {"schemas": {"Payload": {
        "type": "object", "required": ["a"],
        "properties": {"a": {"type": "string"},
                       "b": {"type": "string", "pattern": "^x"}}}}}
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        # 触达 b（pattern）→ 无法判定
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/json",
                     "body": {"a": "1", "b": "xray"}},
         "response": {"status": 200}},
        # 可选属性 b 未触达 → 不误阻断
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/json", "body": {"a": "1"}},
         "response": {"status": 200}},
    ]).json()
    rows = doc["rows"]
    assert rows[0]["outcome"] == "undetermined"
    assert any("pattern" in x and "components/schemas/Payload" in x
               for x in rows[0]["reasons"])
    assert rows[1]["outcome"] == "covered"
    assert doc["summary"]["cells_covered"] == 1


def test_unsupported_keyword_in_response_items(client):
    spec = _spec({"/p": {"get": {"responses": {"200": {"description": "d",
        "content": {"application/json": {"schema": {
            "type": "array",
            "items": {"type": "string", "pattern": "^a"}}}}}}}}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p",
         "response": {"status": 200, "media_type": "application/json",
                      "body": ["abc"]}},
    ]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "undetermined"
    assert any("pattern" in x and "/items/" in x for x in row["reasons"])
    assert doc["summary"]["cells_covered"] == 0


def test_unsupported_keyword_in_oneof_branch(client):
    schema = {"oneOf": [
        {"type": "object", "required": ["kind"],
         "properties": {"kind": {"const": "a"},
                        "v": {"type": "string", "pattern": "^a"}}},
        {"type": "object", "required": ["kind"],
         "properties": {"kind": {"const": "b"},
                        "v": {"type": "integer"}}}]}
    spec = _spec({"/p": {"post": {
        "requestBody": {"content": {"application/json": {"schema": schema}}},
        "responses": {"200": {"description": "d"}}}}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        # 仅命中含 pattern 的分支 → 无法判定
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/json",
                     "body": {"kind": "a", "v": "abc"}},
         "response": {"status": 200}},
        # 命中干净分支 → 覆盖
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/json",
                     "body": {"kind": "b", "v": 5}},
         "response": {"status": 200}},
    ]).json()
    assert doc["rows"][0]["outcome"] == "undetermined"
    assert any("pattern" in x and "oneOf/0" in x
               for x in doc["rows"][0]["reasons"])
    assert doc["rows"][1]["outcome"] == "covered"


# ============================================================== 组合分支三态裁决
def _branch_spec(kind):
    """一个分支确定通过、另一个含 pattern 仍可能通过的组合 schema。"""
    schema = {kind: [
        {"type": "object", "required": ["kind"],
         "properties": {"kind": {"const": "a"}, "v": {"type": "string"}}},
        {"type": "object", "required": ["kind"],
         "properties": {"kind": {"const": "a"},
                        "v": {"type": "string", "pattern": "^a"}}},
    ]}
    return _spec({"/p": {"post": {
        "requestBody": {"content": {"application/json": {"schema": schema}}},
        "responses": {"200": {"description": "d"}}}}})


def _branch_call():
    return {"method": "POST", "path": "/p",
            "request": {"media_type": "application/json",
                        "body": {"kind": "a", "v": "abc"}},
            "response": {"status": 200}}


def test_oneof_pass_plus_pending_branch_is_undetermined(client):
    # oneOf「恰好一个」：一分支确定通过 + 一分支仍可能通过 → 无法判定
    aid = _analysis_from_docs(client, _branch_spec("oneOf"))
    doc = _upload(client, aid, "baseline", [_branch_call()]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "undetermined"
    # 列出待定关键字及位置（第二个分支里的 pattern）
    assert any("pattern" in x and "oneOf/1" in x for x in row["reasons"])
    # 不计覆盖格
    assert doc["summary"]["cells_covered"] == 0
    assert doc["summary"]["calls_undetermined"] == 1
    op = doc["operations"][0]
    assert {c["key"] for c in op["gaps"]} == {"200|(no content)"}
    # 保存后的确定性导出给出同一逐行结论与缺口
    exported = json.loads(client.get(
        f"/api/analyses/{aid}/coverage/baseline/export").content)
    assert exported["rows"] == doc["rows"]
    assert exported["summary"] == doc["summary"]
    assert exported["operations"] == doc["operations"]


def test_anyof_pass_plus_pending_branch_is_covered(client):
    # anyOf「至少一个」：相同条件下一个确定通过即可覆盖
    aid = _analysis_from_docs(client, _branch_spec("anyOf"))
    doc = _upload(client, aid, "baseline", [_branch_call()]).json()
    assert doc["rows"][0]["outcome"] == "covered"
    assert doc["summary"]["cells_covered"] == 1


def test_oneof_two_definite_passes_fail_validation(client):
    # oneOf「恰好一个」：两个分支都确定通过 → 校验失败
    schema = {"oneOf": [
        {"type": "object", "required": ["kind"],
         "properties": {"kind": {"const": "a"}, "v": {"type": "string"}}},
        {"type": "object", "required": ["kind"],
         "properties": {"kind": {"const": "a"},
                        "v": {"type": "string", "minLength": 1}}},
    ]}
    spec = _spec({"/p": {"post": {
        "requestBody": {"content": {"application/json": {"schema": schema}}},
        "responses": {"200": {"description": "d"}}}}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [_branch_call()]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "validation_failed"
    assert any("恰好一个" in x for x in row["reasons"])
    assert doc["summary"]["cells_covered"] == 0


# ============================================================== 分支命中污染
def _nested_branch_spec(kind, branch_b, *, in_response=False):
    """嵌套属性的组合：对象属性 x 的 schema 为 oneOf/anyOf，
    分支 0 确定通过，分支 1 由参数给出。"""
    branch_pass = {"type": "object", "required": ["kind"],
                   "properties": {"kind": {"const": "a"}}}
    prop_schema = {kind: [branch_pass, branch_b]}
    body_schema = {"type": "object", "properties": {"x": prop_schema}}
    op = {"responses": {"200": {"description": "d"}}}
    if in_response:
        op["responses"]["200"]["content"] = {
            "application/json": {"schema": body_schema}}
    else:
        op["requestBody"] = {
            "content": {"application/json": {"schema": body_schema}}}
    return _spec({"/p": {"post": op}})


# 分支 1：缺少必填属性确定失败，同时含未支持的 not
_FAIL_WITH_NOT = {"type": "object", "required": ["other"],
                  "not": {"required": ["kind"]},
                  "properties": {"other": {"type": "string"}}}
# 分支 1：已知约束全过但含 pattern → 待定
_PENDING_PATTERN = {"type": "object", "required": ["kind"],
                    "properties": {"kind": {"const": "a"},
                                   "v": {"type": "string",
                                         "pattern": "^a"}}}


def _nested_call(in_response=False):
    call = {"method": "POST", "path": "/p",
            "response": {"status": 200}}
    if in_response:
        call["response"]["media_type"] = "application/json"
        call["response"]["body"] = {"x": {"kind": "a"}}
    else:
        call["request"] = {"media_type": "application/json",
                           "body": {"x": {"kind": "a"}}}
    return call


def test_failed_branch_not_hit_does_not_pollute_nested_oneof(client):
    # 回归：嵌套属性 oneOf 一支确定通过、另一支因缺少必填属性确定失败
    # （虽含未支持的 not）→ 记 covered 并计格；失败分支的 not 命中不得
    # 经组合与父属性汇总把行污染成无法判定。
    aid = _analysis_from_docs(
        client, _nested_branch_spec("oneOf", _FAIL_WITH_NOT))
    doc = _upload(client, aid, "baseline", [_nested_call()]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "covered"
    assert row["cell"] == "200|(no content)"
    assert doc["summary"]["cells_covered"] == 1
    assert doc["summary"]["calls_undetermined"] == 0
    op = doc["operations"][0]
    assert op["gaps"] == []
    # 保存查询与确定性导出逐行一致
    got = client.get(f"/api/analyses/{aid}/coverage/baseline").json()
    assert got["rows"] == doc["rows"]
    exported = json.loads(client.get(
        f"/api/analyses/{aid}/coverage/baseline/export").content)
    assert exported["rows"] == doc["rows"]
    assert exported["summary"] == doc["summary"]


def test_failed_branch_not_hit_does_not_pollute_nested_anyof(client):
    # 同上，anyOf 版本
    aid = _analysis_from_docs(
        client, _nested_branch_spec("anyOf", _FAIL_WITH_NOT))
    doc = _upload(client, aid, "baseline", [_nested_call()]).json()
    assert doc["rows"][0]["outcome"] == "covered"
    assert doc["summary"]["cells_covered"] == 1
    assert doc["operations"][0]["gaps"] == []


def test_failed_branch_not_hit_does_not_pollute_response_body(client):
    # 响应体嵌套属性的 oneOf 同样不被失败分支的 not 命中污染
    aid = _analysis_from_docs(
        client, _nested_branch_spec("oneOf", _FAIL_WITH_NOT,
                                    in_response=True))
    doc = _upload(client, aid, "baseline",
                  [_nested_call(in_response=True)]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "covered"
    assert row["cell"] == "200|application/json"
    assert doc["summary"]["cells_covered"] == 1


def test_nested_anyof_pass_plus_pending_branch_is_covered(client):
    # 嵌套 anyOf 一过一待定：一个确定通过即可计格——待定分支的命中
    # 已不可能改变「至少一个」的组合结论，不向父属性传播
    aid = _analysis_from_docs(
        client, _nested_branch_spec("anyOf", _PENDING_PATTERN))
    call = _nested_call()
    call["request"]["body"] = {"x": {"kind": "a", "v": "abc"}}
    doc = _upload(client, aid, "baseline", [call]).json()
    assert doc["rows"][0]["outcome"] == "covered"
    assert doc["summary"]["cells_covered"] == 1


def test_nested_oneof_pass_plus_pending_branch_is_undetermined(client):
    # 嵌套 oneOf 一过一待定：是否「恰好一个」无定论 → 无法判定、不计格，
    # 且列出待定分支的未决命中（该命中仍可能改变组合结论）
    aid = _analysis_from_docs(
        client, _nested_branch_spec("oneOf", _PENDING_PATTERN))
    call = _nested_call()
    call["request"]["body"] = {"x": {"kind": "a", "v": "abc"}}
    doc = _upload(client, aid, "baseline", [call]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "undetermined"
    assert any("pattern" in x and "oneOf/1" in x for x in row["reasons"])
    assert doc["summary"]["cells_covered"] == 0
    assert doc["summary"]["calls_undetermined"] == 1
    op = doc["operations"][0]
    assert {c["key"] for c in op["gaps"]} == {"200|(no content)"}


def test_oneof_pending_branch_in_response_body(client):
    # 响应体中的 oneOf 同样按三态裁决
    schema = {"oneOf": [
        {"type": "object", "required": ["id"],
         "properties": {"id": {"type": "integer"}}},
        {"type": "object", "required": ["id"],
         "properties": {"id": {"type": "integer"},
                        "code": {"type": "string", "pattern": "^[A-Z]+$"}}},
    ]}
    spec = _spec({"/p": {"get": {"responses": {"200": {"description": "d",
        "content": {"application/json": {"schema": schema}}}}}}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        # 触达待定分支的 pattern → 无法判定
        {"method": "GET", "path": "/p",
         "response": {"status": 200, "media_type": "application/json",
                      "body": {"id": 1, "code": "ABC"}}},
        # 未触达可选属性 code → 两个分支都确定通过 → 校验失败
        {"method": "GET", "path": "/p",
         "response": {"status": 200, "media_type": "application/json",
                      "body": {"id": 1}}},
    ]).json()
    assert doc["rows"][0]["outcome"] == "undetermined"
    assert any("pattern" in x and "oneOf/1" in x
               for x in doc["rows"][0]["reasons"])
    assert doc["rows"][1]["outcome"] == "validation_failed"
    assert doc["summary"]["cells_covered"] == 0


def test_oneof_pending_branch_in_parameter(client):
    # 查询参数的 oneOf 同样按三态裁决
    spec = _spec({"/p": {"get": {"parameters": [
        {"name": "q", "in": "query",
         "schema": {"oneOf": [
             {"type": "string"},
             {"type": "string", "pattern": "^a"}]}}],
        "responses": {"200": {"description": "d"}}}}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p", "query": {"q": "abc"},
         "response": {"status": 200}},
    ]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "undetermined"
    assert any("pattern" in x and "oneOf/1" in x for x in row["reasons"])
    assert doc["summary"]["cells_covered"] == 0


def test_unsupported_keyword_in_additional_properties(client):
    spec = _spec({"/p": {"post": {
        "requestBody": {"content": {"application/json": {"schema": {
            "type": "object",
            "additionalProperties": {"type": "string",
                                     "pattern": "^a"}}}}},
        "responses": {"200": {"description": "d"}}}}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/json", "body": {"x": "abc"}},
         "response": {"status": 200}},
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/json", "body": {}},
         "response": {"status": 200}},
    ]).json()
    assert doc["rows"][0]["outcome"] == "undetermined"
    assert any("additionalProperties" in x
               for x in doc["rows"][0]["reasons"])
    assert doc["rows"][1]["outcome"] == "covered"


def test_unsupported_keyword_irrelevant_type_still_covered(client):
    # pattern 只约束字符串；整数值的通过与否不依赖它
    spec = _spec({"/p": {"post": {
        "requestBody": {"content": {"application/json": {"schema": {
            "pattern": "^a", "minimum": 1}}}},
        "responses": {"200": {"description": "d"}}}}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/json", "body": 5},
         "response": {"status": 200}},
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/json", "body": "abc"},
         "response": {"status": 200}},
    ]).json()
    assert doc["rows"][0]["outcome"] == "covered"
    assert doc["rows"][1]["outcome"] == "undetermined"


def test_unsupported_keyword_not_keyword_any_type(client):
    spec = _spec({"/p": {"post": {
        "requestBody": {"content": {"application/json": {"schema": {
            "type": "integer", "not": {"const": 5}}}}},
        "responses": {"200": {"description": "d"}}}}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/json", "body": 7},
         "response": {"status": 200}},
    ]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "undetermined"
    assert any("not" in x for x in row["reasons"])
    assert doc["summary"]["cells_covered"] == 0


# ============================================================== 循环引用沿值裁决
def _recursive_spec():
    """自引用的响应体：Node.child / Node.kids 都指回 Node 本身。"""
    return _spec({"/nodes": {"get": {"responses": {"200": {
        "description": "ok",
        "content": {"application/json": {"schema": {
            "$ref": "#/components/schemas/Node"}}}}}}}})


_RECURSIVE_COMPONENTS = {"components": {"schemas": {"Node": {
    "type": "object", "required": ["id"],
    "properties": {
        "id": {"type": "integer"},
        "child": {"$ref": "#/components/schemas/Node"},
        "kids": {"type": "array",
                 "items": {"$ref": "#/components/schemas/Node"}}}}}}}


def _rec_doc():
    spec = _recursive_spec()
    spec.update(_RECURSIVE_COMPONENTS)
    return spec


def _rec_call(body):
    return {"method": "GET", "path": "/nodes", "request": {},
            "response": {"status": 200, "media_type": "application/json",
                         "body": body}}


def test_self_ref_child_undetermined_not_covered(client):
    # 回归：取值的 child 触达自引用节点 → 无法判定（报告模式位置），
    # 不再返回 covered / 空 reasons，也不增加覆盖格。
    aid = _analysis_from_docs(client, _rec_doc())
    doc = _upload(client, aid, "baseline", [
        _rec_call({"id": 1, "child": {"id": 2, "child": {"id": 3}}}),
    ]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "undetermined"
    assert row["cell"] is None
    assert any("循环引用" in x and "components/schemas/Node" in x
               for x in row["reasons"])
    assert doc["summary"]["calls_undetermined"] == 1
    assert doc["summary"]["cells_covered"] == 0
    assert doc["summary"]["calls_covered"] == 0
    op = doc["operations"][0]
    assert {c["key"] for c in op["gaps"]} == {"200|application/json"}


def test_known_constraint_failure_inside_recursion_fails(client):
    # 同一分支的已知约束失败优先：循环内部 id 类型错误 → 校验失败，
    # 即使取值同时触达了循环节点。
    aid = _analysis_from_docs(client, _rec_doc())
    doc = _upload(client, aid, "baseline", [
        _rec_call({"id": 1, "child": {"id": "not-an-int"}}),
        _rec_call({"id": 1, "kids": [{"id": "bad"}]}),
    ]).json()
    assert [r["outcome"] for r in doc["rows"]] == [
        "validation_failed", "validation_failed"]
    assert any("/child/id" in x for x in doc["rows"][0]["reasons"])
    assert any("/kids/0/id" in x for x in doc["rows"][1]["reasons"])
    assert doc["summary"]["cells_covered"] == 0


def test_optional_recursive_property_absent_still_covered(client):
    # 未触达的可选循环属性仍可覆盖
    aid = _analysis_from_docs(client, _rec_doc())
    doc = _upload(client, aid, "baseline", [
        _rec_call({"id": 1}),
        _rec_call({"id": 1, "kids": []}),
    ]).json()
    assert [r["outcome"] for r in doc["rows"]] == ["covered", "covered"]
    assert doc["summary"]["cells_covered"] == 1  # 同一格只记一次


def test_unresolvable_node_along_value_reports_location(client):
    spec = _spec({"/p": {"get": {"responses": {"200": {"description": "d",
        "content": {"application/json": {"schema": {
            "$ref": "#/components/schemas/Outer"}}}}}}}})
    spec["components"] = {"schemas": {"Outer": {
        "type": "object", "properties": {"inner": {
            "$ref": "https://e.example.com/schemas#/X"}}}}}
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p", "request": {},
         "response": {"status": 200, "media_type": "application/json",
                      "body": {"inner": {}}}},
        # 未触达该可选属性 → 覆盖
        {"method": "GET", "path": "/p", "request": {},
         "response": {"status": 200, "media_type": "application/json",
                      "body": {}}},
    ]).json()
    assert doc["rows"][0]["outcome"] == "undetermined"
    assert any("模式不可判定" in x and "properties/inner" in x
               for x in doc["rows"][0]["reasons"])
    assert doc["rows"][1]["outcome"] == "covered"


def test_mutual_recursion_known_fail_priority_and_pending(client):
    spec = _spec({"/x": {"get": {"responses": {"200": {"description": "d",
        "content": {"application/json": {"schema": {
            "$ref": "#/components/schemas/A"}}}}}}}})
    spec["components"] = {"schemas": {
        "A": {"type": "object", "required": ["a"],
              "properties": {"a": {"type": "string"},
                             "b": {"$ref": "#/components/schemas/B"}}},
        "B": {"type": "object", "required": ["b"],
              "properties": {"b": {"type": "string"},
                             "a": {"$ref": "#/components/schemas/A"}}}}}
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        # 合法值触达相互引用 → 待定
        {"method": "GET", "path": "/x", "request": {},
         "response": {"status": 200, "media_type": "application/json",
                      "body": {"a": "1", "b": {"b": "2",
                                               "a": {"a": "3"}}}}},
        # 递归深处已知约束失败 → 失败优先
        {"method": "GET", "path": "/x", "request": {},
         "response": {"status": 200, "media_type": "application/json",
                      "body": {"a": "1", "b": {"b": "2",
                                               "a": {"a": 7}}}}},
    ]).json()
    assert doc["rows"][0]["outcome"] == "undetermined"
    assert any("循环引用" in x for x in doc["rows"][0]["reasons"])
    assert doc["rows"][1]["outcome"] == "validation_failed"
    assert any("/b/a/a" in x for x in doc["rows"][1]["reasons"])


def test_oneof_pass_plus_cycle_pending_no_cell(client):
    # 一支确定通过、另一支沿值触达循环仍可能通过 → oneOf 不计格
    schema = {"oneOf": [
        {"type": "object", "required": ["kind"],
         "additionalProperties": False,
         "properties": {"kind": {"type": "string"}}},
        {"$ref": "#/components/schemas/Node"}]}
    spec = _spec({"/nodes": {"post": {
        "requestBody": {"content": {"application/json": {"schema": schema}}},
        "responses": {"200": {"description": "d"}}}}})
    spec.update(_RECURSIVE_COMPONENTS)
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        # branch0 因 additionalProperties:false 确定失败；
        # branch1(Node) 已知约束全过但触达 child 循环 → 待定 → 整体待定
        {"method": "POST", "path": "/nodes",
         "request": {"media_type": "application/json",
                     "body": {"kind": "x", "id": 1,
                              "child": {"id": 2}}},
         "response": {"status": 200}},
        # branch0 确定失败、branch1 递归部分已知失败 → 整体校验失败
        {"method": "POST", "path": "/nodes",
         "request": {"media_type": "application/json",
                     "body": {"kind": "x", "id": 1,
                              "child": {"id": "bad"}}},
         "response": {"status": 200}},
    ]).json()
    assert doc["rows"][0]["outcome"] == "undetermined"
    assert any("循环引用" in x and "components/schemas/Node" in x
               for x in doc["rows"][0]["reasons"])
    assert doc["rows"][1]["outcome"] == "validation_failed"
    assert doc["summary"]["cells_covered"] == 0


def test_anyof_pass_plus_cycle_pending_covers(client):
    # anyOf 至少一个：一个分支确定通过即可覆盖，即使另一支触达循环
    schema = {"anyOf": [
        {"type": "object", "required": ["kind"],
         "properties": {"kind": {"type": "string"}}},
        {"$ref": "#/components/schemas/Node"}]}
    spec = _spec({"/nodes": {"post": {
        "requestBody": {"content": {"application/json": {"schema": schema}}},
        "responses": {"200": {"description": "d"}}}}})
    spec.update(_RECURSIVE_COMPONENTS)
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        # 干净分支确定通过（Node 分支虽待定，anyOf 不影响结论）
        {"method": "POST", "path": "/nodes",
         "request": {"media_type": "application/json",
                     "body": {"kind": "x"}},
         "response": {"status": 200}},
        # 无确定通过、仅剩触达循环的待定分支 → 无法判定
        {"method": "POST", "path": "/nodes",
         "request": {"media_type": "application/json",
                     "body": {"id": 1, "child": {"id": 2}}},
         "response": {"status": 200}},
    ]).json()
    assert doc["rows"][0]["outcome"] == "covered"
    assert doc["rows"][1]["outcome"] == "undetermined"
    assert doc["summary"]["cells_covered"] == 1


def test_cycle_verdict_is_deterministic_across_export(client):
    aid = _analysis_from_docs(client, _rec_doc())
    d = _upload(client, aid, "baseline", [
        _rec_call({"id": 1, "child": {"id": 2}}),
    ]).json()
    exported = json.loads(client.get(
        f"/api/analyses/{aid}/coverage/baseline/export").content)
    assert exported["rows"] == d["rows"]
    assert exported["summary"] == d["summary"]
    # 主报告导出内嵌同一份确定性结论
    main = json.loads(client.get(
        f"/api/analyses/{aid}/export").content)
    assert main["call_coverage"]["baseline"]["rows"] == d["rows"]


# ============================================================== 同层属性 × 组合交集裁决
def _same_level_schema(kind, branches=None):
    """同一节点同时含自引用 properties.child 与 oneOf/anyOf。"""
    return {
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "integer"},
            "child": {"$ref": "#/components/schemas/Node"},
        },
        kind: branches if branches is not None else [
            {"type": "object", "required": ["kind"],
             "additionalProperties": False,
             "properties": {"kind": {"const": "n"}, "id": {"type": "integer"}}},
            {"$ref": "#/components/schemas/Node"},
        ],
    }


def _same_level_doc(kind, branches=None):
    spec = _spec({"/nodes": {"post": {
        "requestBody": {"content": {"application/json": {
            "schema": _same_level_schema(kind, branches)}}},
        "responses": {"200": {"description": "d"}}}}})
    spec.update(_RECURSIVE_COMPONENTS)
    return spec


def _same_level_call(body):
    return {"method": "POST", "path": "/nodes",
            "request": {"media_type": "application/json", "body": body},
            "response": {"status": 200}}


def test_same_level_child_pending_anyof_pass_is_undetermined(client):
    # 回归（核心）：取值的 child 已使本层属性校验待定，anyOf 一支对整个
    # 载荷确定通过（组合结论 PASS）也不得直接判 covered / 增加覆盖格——
    # 交集裁决为无法判定。
    branches = [
        {"type": "object"},  # 对整个载荷确定通过
        {"$ref": "#/components/schemas/Node"},  # 随 child 触达循环 → 待定
    ]
    aid = _analysis_from_docs(
        client, _same_level_doc("anyOf", branches))
    doc = _upload(client, aid, "baseline", [
        _same_level_call({"id": 1, "child": {"id": 2, "child": {"id": 3}}}),
    ]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "undetermined"
    assert row["cell"] is None
    assert any("循环引用" in x and "components/schemas/Node" in x
               for x in row["reasons"])
    assert doc["summary"]["calls_undetermined"] == 1
    assert doc["summary"]["cells_covered"] == 0
    assert doc["summary"]["calls_covered"] == 0
    op = doc["operations"][0]
    assert {c["key"] for c in op["gaps"]} == {"200|(no content)"}


def test_same_level_child_pending_oneof_pass_is_undetermined(client):
    # 同层 child 待定 + oneOf 恰好一个分支对载荷确定通过 → 仍无法判定
    branches = [
        {"type": "object"},  # 确定通过
        {"type": "object", "required": ["missing"],
         "properties": {"missing": {"const": "x"}}},  # 确定失败
    ]
    aid = _analysis_from_docs(
        client, _same_level_doc("oneOf", branches))
    doc = _upload(client, aid, "baseline", [
        _same_level_call({"id": 1, "child": {"id": 2, "child": {"id": 3}}}),
    ]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "undetermined"
    assert any("循环引用" in x and "components/schemas/Node" in x
               for x in row["reasons"])
    assert doc["summary"]["cells_covered"] == 0


def test_same_level_known_failure_dominates_combinator_pass(client):
    # 本层已知约束失败（child 内 id 类型错）即使 anyOf 一支对载荷确定通过
    # 也判失败——失败优先于组合结论。
    branches = [
        {"type": "object"},  # 组合层面确定通过
        {"$ref": "#/components/schemas/Node"},
    ]
    aid = _analysis_from_docs(
        client, _same_level_doc("anyOf", branches))
    doc = _upload(client, aid, "baseline", [
        _same_level_call({"id": 1, "child": {"id": "bad"}}),
    ]).json()
    assert doc["rows"][0]["outcome"] == "validation_failed"
    assert any("/child/id" in x for x in doc["rows"][0]["reasons"])
    assert doc["summary"]["cells_covered"] == 0


def test_same_level_combinator_failure_dominates_pending(client):
    # 组合整体确定失败（oneOf 无任何分支通过）时，即使本层 child 待定也失败
    branches = [
        {"type": "object", "required": ["nope"],
         "properties": {"nope": {"const": "x"}}},
        {"type": "object", "required": ["also"],
         "properties": {"also": {"const": "y"}}},
    ]
    aid = _analysis_from_docs(
        client, _same_level_doc("oneOf", branches))
    doc = _upload(client, aid, "baseline", [
        _same_level_call({"id": 1, "child": {"id": 2, "child": {"id": 3}}}),
    ]).json()
    assert doc["rows"][0]["outcome"] == "validation_failed"
    assert any("不满足 oneOf" in x for x in doc["rows"][0]["reasons"])
    assert doc["summary"]["cells_covered"] == 0


def test_same_level_optional_child_absent_does_not_block(client):
    # 未触达的可选 child：anyOf 分支确定通过 → 正常覆盖；
    # oneOf 在两个分支都确定通过时仍按「恰好一个」判失败。
    aid = _analysis_from_docs(client, _same_level_doc("anyOf"))
    doc = _upload(client, aid, "baseline", [
        _same_level_call({"id": 1}),
    ]).json()
    assert doc["rows"][0]["outcome"] == "covered"
    assert doc["summary"]["cells_covered"] == 1

    branches = [
        {"type": "object", "required": ["id"],
         "properties": {"id": {"type": "integer"}}},
        {"$ref": "#/components/schemas/Node"},
    ]
    aid = _analysis_from_docs(
        client, _same_level_doc("oneOf", branches))
    doc = _upload(client, aid, "baseline", [
        _same_level_call({"id": 1}),
    ]).json()
    assert doc["rows"][0]["outcome"] == "validation_failed"
    assert any("恰好一个" in x for x in doc["rows"][0]["reasons"])


def test_same_level_unsupported_keyword_pending_blocks_anyof_pass(client):
    # 同层未支持关键字（pattern，与取值字符串类型相关）待定，
    # 即使 anyOf 一支确定通过也判无法判定并报告模式位置。
    schema = {"type": "string", "pattern": "^x",
              "anyOf": [{"const": "abc"}, {"const": "xyz"}]}
    spec = _spec({"/p": {"post": {
        "requestBody": {"content": {"application/json": {"schema": schema}}},
        "responses": {"200": {"description": "d"}}}}})
    aid = _analysis_from_docs(client, spec)
    doc = _upload(client, aid, "baseline", [
        {"method": "POST", "path": "/p",
         "request": {"media_type": "application/json", "body": "abc"},
         "response": {"status": 200}},
    ]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "undetermined"
    assert any("pattern" in x and "/schema/pattern" in x
               for x in row["reasons"])
    assert doc["summary"]["cells_covered"] == 0
    # 保存查询与确定性导出逐行一致
    got = client.get(
        f"/api/analyses/{aid}/coverage/baseline").json()
    assert got["rows"] == doc["rows"]
    exported = json.loads(client.get(
        f"/api/analyses/{aid}/coverage/baseline/export").content)
    assert exported["rows"] == doc["rows"]
    assert exported["operations"] == doc["operations"]


def test_same_level_pending_on_candidate_side(client):
    # 候选侧同样按交集裁决
    aid = _analysis_from_docs(client, _same_level_doc("anyOf"))
    doc = _upload(client, aid, "candidate", [
        _same_level_call({"id": 1, "child": {"id": 2, "child": {"id": 3}}}),
    ]).json()
    assert doc["analysis_side"] == "candidate"
    assert doc["rows"][0]["outcome"] == "undetermined"
    assert doc["summary"]["cells_covered"] == 0


def test_same_level_pending_verdict_consistent_in_main_export(client):
    # 同层交集待定结论：页面/API/保存查询/两侧确定性导出逐行一致
    aid = _analysis_from_docs(client, _same_level_doc("anyOf"))
    doc = _upload(client, aid, "baseline", [
        _same_level_call({"id": 1, "child": {"id": 2, "child": {"id": 3}}}),
    ]).json()
    assert doc["rows"][0]["outcome"] == "undetermined"
    assert doc["summary"]["cells_covered"] == 0

    # 保存后重新查询：逐行结论、汇总、缺口一致
    got = client.get(
        f"/api/analyses/{aid}/coverage/baseline").json()
    assert got["rows"] == doc["rows"]
    assert got["summary"] == doc["summary"]
    assert got["operations"] == doc["operations"]
    avail = client.get(f"/api/analyses/{aid}/coverage").json()
    sides = {s["side"]: s for s in avail["sides"]}
    assert sides["baseline"]["available"] is True
    assert sides["baseline"]["summary"]["calls_undetermined"] == 1

    # 侧导出
    side_export = json.loads(client.get(
        f"/api/analyses/{aid}/coverage/baseline/export").content)
    assert side_export["rows"] == doc["rows"]
    assert side_export["summary"] == doc["summary"]
    assert side_export["operations"] == doc["operations"]

    # 主报告导出内嵌同一份确定性结论
    main = json.loads(client.get(
        f"/api/analyses/{aid}/export").content)
    embedded = main["call_coverage"]["baseline"]
    assert embedded["rows"] == doc["rows"]
    assert embedded["summary"] == doc["summary"]
    assert embedded["operations"] == doc["operations"]


# ============================================================== HTTP surface
def test_coverage_404_unknown_analysis(client):
    r = _upload(client, "nope", "baseline", [
        json.dumps({"method": "GET", "path": "/p",
                    "response": {"status": 200}})])
    assert r.status_code == 404


def test_coverage_bad_side(client, analysis_id):
    r = client.post(
        f"/api/analyses/{analysis_id}/coverage/sideways",
        files={"file": ("c.ndjson", b"{}", "application/x-ndjson")})
    assert r.status_code == 400


def test_coverage_upload_get_export_and_embed(client, analysis_id):
    with open("examples/calls-baseline.ndjson", "rb") as fh:
        raw = fh.read()
    r = client.post(
        f"/api/analyses/{analysis_id}/coverage/baseline",
        files={"file": ("calls-baseline.ndjson", raw,
                        "application/x-ndjson")})
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["analysis_side"] == "baseline"
    assert doc["summary"]["calls_covered"] >= 4

    # list endpoint reports availability
    lst = client.get(
        f"/api/analyses/{analysis_id}/coverage").json()
    sides = {s["side"]: s for s in lst["sides"]}
    assert sides["baseline"]["available"] is True
    assert sides["candidate"]["available"] is False

    # GET single side
    g = client.get(
        f"/api/analyses/{analysis_id}/coverage/baseline")
    assert g.status_code == 200
    assert g.json()["summary"]["cells_total"] == doc["summary"]["cells_total"]

    # GET missing side -> 404
    assert client.get(
        f"/api/analyses/{analysis_id}/coverage/candidate").status_code == 404

    # deterministic export
    e1 = client.get(
        f"/api/analyses/{analysis_id}/coverage/baseline/export")
    e2 = client.get(
        f"/api/analyses/{analysis_id}/coverage/baseline/export")
    assert e1.content == e2.content
    import hashlib
    assert hashlib.sha256(e1.content).hexdigest() == \
        e1.headers["x-coverage-fingerprint"]
    parsed = json.loads(e1.content)
    assert parsed["rows"] == doc["rows"]

    # coverage embedded in main export and stays deterministic
    m1 = client.get(f"/api/analyses/{analysis_id}/export").content
    m2 = client.get(f"/api/analyses/{analysis_id}/export").content
    assert m1 == m2
    embedded = json.loads(m1)
    assert "call_coverage" in embedded
    assert "baseline" in embedded["call_coverage"]
    assert hashlib.sha256(m1).hexdigest() == \
        client.get(f"/api/analyses/{analysis_id}/export")\
        .headers["x-report-fingerprint"]


def test_coverage_overwrite_and_delete(client, analysis_id):
    good = [{"method": "GET", "path": "/pets",
             "request": {},
             "response": {"status": 200, "media_type": "application/json",
                          "body": []}}]
    assert _upload(client, analysis_id, "candidate", good).status_code == 201
    doc = client.get(
        f"/api/analyses/{analysis_id}/coverage/candidate").json()
    assert doc["summary"]["calls_total"] == 1

    # re-upload replaces
    assert _upload(client, analysis_id, "candidate",
                   good + good).status_code == 201
    doc2 = client.get(
        f"/api/analyses/{analysis_id}/coverage/candidate").json()
    assert doc2["summary"]["calls_total"] == 2

    d = client.delete(
        f"/api/analyses/{analysis_id}/coverage/candidate")
    assert d.status_code == 200
    assert client.get(
        f"/api/analyses/{analysis_id}/coverage/candidate").status_code == 404
    # baseline still present
    assert client.get(
        f"/api/analyses/{analysis_id}/coverage/baseline").status_code == 200


def test_coverage_survives_analysis_reload(client, analysis_id):
    g = client.get(
        f"/api/analyses/{analysis_id}/coverage/baseline")
    assert g.status_code == 200
    cells = g.json()["summary"]["cells_total"]
    assert cells > 0
