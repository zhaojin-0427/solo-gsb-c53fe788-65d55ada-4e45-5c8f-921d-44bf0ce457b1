"""覆盖缺口补测样本（Coverage Gap Samples）测试。

覆盖：精确码/范围/default 抢占选码、模板路径抢占、必填值/模式待定原因、
三态判定复核、空 NDJSON、前置条件报错，以及「样本重新上传确实补上目标格」。
"""
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


def _spec(paths):
    return {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": paths}


def _json_content(schema):
    return {"description": "r",
            "content": {"application/json": {"schema": schema}}}


def _analysis(client, spec):
    payload = json.dumps(spec).encode()
    r = client.post(
        "/api/analyses",
        files={"baseline": ("b.json", payload, "application/json"),
               "candidate": ("c.json", payload, "application/json")})
    assert r.status_code == 201, r.text
    return r.json()["analysis_id"]


def _coverage_with_only_unmatched(client, aid, side="baseline"):
    # 一行不匹配任何端点的调用：所有格都是缺口
    row = json.dumps({"method": "GET", "path": "/__nope__",
                      "response": {"status": 599}})
    r = client.post(f"/api/analyses/{aid}/coverage/{side}",
                    files={"file": ("c.ndjson", row.encode(),
                                    "application/x-ndjson")})
    assert r.status_code == 201, r.text
    return r.json()


def _gap_doc(client, aid, side="baseline"):
    r = client.get(
        f"/api/analyses/{aid}/coverage/{side}/gap-samples")
    assert r.status_code == 200, r.text
    return r.json()


def _cells_by_key(doc):
    return {c["cell_key"]: c for c in doc["cells"]}


# ============================================================ preconditions
def test_missing_coverage_and_package_errors(client, analysis_id):
    # 尚未上传覆盖 → 409（JSON 与 NDJSON 一致）
    aid = _analysis(client, _spec({"/p": {"get": {"responses": {
        "200": {"description": "d"}}}}}))
    r = client.get(f"/api/analyses/{aid}/coverage/baseline/gap-samples")
    assert r.status_code == 409
    assert "覆盖结果" in r.json()["detail"]
    r = client.get(
        f"/api/analyses/{aid}/coverage/baseline/gap-samples.ndjson")
    assert r.status_code == 409

    # 未知分析 / 非法文档侧
    assert client.get(
        "/api/analyses/nope/coverage/baseline/gap-samples").status_code == 404
    assert client.get(
        f"/api/analyses/{aid}/coverage/sidex/gap-samples").status_code == 400


# ============================================================ status select
def test_exact_range_default_status_selection(client):
    spec = _spec({"/p": {"get": {"responses": {
        "200": _json_content({"type": "object"}),
        "2XX": _json_content({"type": "object"}),
        "default": _json_content({"type": "object"})}}}})
    aid = _analysis(client, spec)
    _coverage_with_only_unmatched(client, aid)
    doc = _gap_doc(client, aid)
    cells = _cells_by_key(doc)
    assert cells["200|application/json"]["request_status"] == "200"
    # 200 被精确码抢占 → 范围格必须选 201
    assert cells["2XX|application/json"]["request_status"] == "201"
    assert cells["2XX|application/json"]["resolved_status"] == "2XX"
    # 2XX 抢占 default 空间的 2xx，但 100-199 空闲
    assert cells["default|application/json"]["request_status"] == "100"
    assert cells["default|application/json"]["resolved_status"] == "default"
    assert doc["summary"]["samples_generated"] == 3


def test_default_unavailable_when_all_ranges_declared(client):
    responses = {k: _json_content({"type": "object"})
                 for k in ("1XX", "2XX", "3XX", "4XX", "5XX", "default")}
    aid = _analysis(client, _spec({"/p": {"get": {"responses": responses}}}))
    _coverage_with_only_unmatched(client, aid)
    cells = _cells_by_key(_gap_doc(client, aid))
    cell = cells["default|application/json"]
    assert cell["generated"] is False
    assert "抢占" in cell["reasons"][0]
    # 范围格本身仍可生成
    assert cells["1XX|application/json"]["generated"] is True


def test_range_unavailable_when_exhausted_by_exact_codes(client):
    responses = {str(n): _json_content({"type": "object"})
                 for n in range(200, 300)}
    responses["2XX"] = _json_content({"type": "object"})
    aid = _analysis(client, _spec({"/p": {"get": {"responses": responses}}}))
    _coverage_with_only_unmatched(client, aid)
    cells = _cells_by_key(_gap_doc(client, aid))
    cell = cells["2XX|application/json"]
    assert cell["generated"] is False
    assert "精确码" in cell["reasons"][0]


# ============================================================ path preemption
def test_integer_template_escapes_exact_path(client):
    spec = _spec({
        "/p/x": {"get": {"responses": {"200": {"description": "e"}}}},
        "/p/{id}": {"get": {
            "parameters": [{"name": "id", "in": "path", "required": True,
                            "schema": {"type": "integer"}}],
            "responses": {"200": _json_content({"type": "object"})}}}})
    aid = _analysis(client, spec)
    _coverage_with_only_unmatched(client, aid)
    doc = _gap_doc(client, aid)
    cells = _cells_by_key(doc)
    # 模板整数 id 可以选 0/1 等绕开精确路径 /p/x
    tcell = cells["200|application/json"]
    assert tcell["generated"] is True
    sample = next(s for s in doc["samples"]
                  if s["cell_key"] == "200|application/json"
                  and s["operation"] == "GET /p/{id}")
    assert sample["call"]["path"] != "/p/x"


def test_string_template_preempted_by_exact_path(client):
    # 无类型约束时生成器仍可绕开（/p/x 以外的值）；这里验证不会选 /p/x
    spec = _spec({
        "/p/x": {"get": {"responses": {"200": {"description": "e"}}}},
        "/p/{id}": {"get": {
            "parameters": [{"name": "id", "in": "path", "required": True,
                            "schema": {"type": "string",
                                       "enum": ["x", "y"]}}],
            "responses": {"200": _json_content({"type": "object"})}}}})
    aid = _analysis(client, spec)
    _coverage_with_only_unmatched(client, aid)
    doc = _gap_doc(client, aid)
    sample = next(s for s in doc["samples"]
                  if s["operation"] == "GET /p/{id}")
    assert sample["call"]["path"] == "/p/y"


def test_enum_only_exact_value_preempted(client):
    # 枚举只有一个值且该值恰好是精确端点路径 → 无法生成，不允许归错格
    spec = _spec({
        "/p/x": {"get": {"responses": {"200": {"description": "e"}}}},
        "/p/{id}": {"get": {
            "parameters": [{"name": "id", "in": "path", "required": True,
                            "schema": {"type": "string", "enum": ["x"]}}],
            "responses": {"200": _json_content({"type": "object"})}}}})
    aid = _analysis(client, spec)
    _coverage_with_only_unmatched(client, aid)
    cells = _cells_by_key(_gap_doc(client, aid))
    cell = cells["200|application/json"]
    assert cell["generated"] is False
    assert cell["reasons"]


# ============================================================ undecidable
def test_undecidable_response_body_pattern(client):
    spec = _spec({"/p": {"get": {"responses": {"200": _json_content({
        "type": "object", "required": ["s"],
        "properties": {"s": {"type": "string", "pattern": "^a+"}}})}}}})
    aid = _analysis(client, spec)
    _coverage_with_only_unmatched(client, aid)
    cells = _cells_by_key(_gap_doc(client, aid))
    cell = cells["200|application/json"]
    assert cell["generated"] is False
    assert "无法可靠构造" in cell["reasons"][0]
    assert "pattern" in cell["reasons"][0]


def test_recursive_required_response_body(client):
    schema = {"type": "object", "required": ["child"],
              "properties": {"child": {"$ref": "#/components/schemas/N"}}}
    spec = {**_spec({"/p": {"get": {"responses": {"200":
                  _json_content(schema)}}}}),
            "components": {"schemas": {"N": {
                "type": "object", "required": ["child"],
                "properties": {"child": {
                    "$ref": "#/components/schemas/N"}}}}}}
    aid = _analysis(client, spec)
    _coverage_with_only_unmatched(client, aid)
    cells = _cells_by_key(_gap_doc(client, aid))
    assert cells["200|application/json"]["generated"] is False


def test_required_parameter_unconstructable(client):
    # 必填 query 参数带未支持 pattern
    spec = _spec({"/p": {"get": {
        "parameters": [{"name": "q", "in": "query", "required": True,
                        "schema": {"type": "string", "pattern": "^x"}}],
        "responses": {"200": {"description": "d"}}}}})
    aid = _analysis(client, spec)
    _coverage_with_only_unmatched(client, aid)
    cells = _cells_by_key(_gap_doc(client, aid))
    cell = cells["200|(no content)"]
    assert cell["generated"] is False
    assert "q" in cell["reasons"][0]


def test_required_body_only_octet_stream(client):
    spec = _spec({"/p": {"post": {
        "requestBody": {"required": True, "content": {
            "application/octet-stream": {}}},
        "responses": {"200": {"description": "d"}}}}})
    aid = _analysis(client, spec)
    _coverage_with_only_unmatched(client, aid)
    cells = _cells_by_key(_gap_doc(client, aid))
    assert cells["200|(no content)"]["generated"] is False
    assert "媒体类型" in cells["200|(no content)"]["reasons"][0]


# ============================================================ positive flows
def test_required_params_body_and_no_content_cell(client):
    spec = _spec({"/p": {"post": {
        "parameters": [{"name": "kind", "in": "query", "required": True,
                        "schema": {"type": "string", "enum": ["a", "b"]}}],
        "requestBody": {"required": True, "content": {
            "application/json": {"schema": {
                "type": "object", "required": ["n"],
                "properties": {"n": {"type": "integer", "minimum": 5}}}}}},
        "responses": {
            "201": _json_content({"type": "object"}),
            "400": {"description": "bad"}}}}})
    aid = _analysis(client, spec)
    _coverage_with_only_unmatched(client, aid)
    doc = _gap_doc(client, aid)
    cells = _cells_by_key(doc)
    assert cells["201|application/json"]["generated"] is True
    assert cells["400|(no content)"]["generated"] is True
    body_sample = next(s for s in doc["samples"]
                       if s["cell_key"] == "201|application/json")
    call = body_sample["call"]
    assert call["query"] == {"kind": "a"}
    assert call["request"]["body"] == {"n": 5}
    assert call["response"] == {"status": 201,
                                "media_type": "application/json",
                                "body": {}}
    noent = next(s for s in doc["samples"]
                 if s["cell_key"] == "400|(no content)")
    assert "body" not in noent["call"]["response"]


def test_no_content_gap_empty_ndjson(client):
    spec = _spec({"/p": {"get": {"responses": {
        "200": _json_content({"type": "object"})}}}})
    aid = _analysis(client, spec)
    row = json.dumps({"method": "GET", "path": "/p",
                      "response": {"status": 200,
                                   "media_type": "application/json",
                                   "body": {}}})
    client.post(f"/api/analyses/{aid}/coverage/baseline",
                files={"file": ("c.ndjson", row.encode())})
    doc = _gap_doc(client, aid)
    assert doc["summary"]["gaps_total"] == 0
    r = client.get(
        f"/api/analyses/{aid}/coverage/baseline/gap-samples.ndjson")
    assert r.status_code == 200
    assert r.content == b""
    assert r.headers["x-gap-samples-generated"] == "0"


# ============================================================ NDJSON roundtrip
def test_ndjson_download_roundtrip_covers_gap(client):
    spec = _spec({"/pets": {"get": {"responses": {
        "200": _json_content({"type": "array", "items": {
            "type": "object", "required": ["id"],
            "properties": {"id": {"type": "integer"}}}}),
        "400": {"description": "bad"}}}}})
    aid = _analysis(client, spec)
    # 覆盖 200，留 400 缺口
    row = json.dumps({"method": "GET", "path": "/pets",
                      "response": {"status": 200,
                                   "media_type": "application/json",
                                   "body": [{"id": 1}]}})
    client.post(f"/api/analyses/{aid}/coverage/baseline",
                files={"file": ("c.ndjson", row.encode())})
    r = client.get(
        f"/api/analyses/{aid}/coverage/baseline/gap-samples.ndjson")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert r.content.endswith(b"\n")
    lines = [json.loads(l) for l in r.content.decode().splitlines() if l]
    assert len(lines) == 1
    assert lines[0]["response"]["status"] == 400
    assert "body" not in lines[0]["response"]

    # 合并原调用重新上传：缺口归零，且样本结局为 covered
    combined = row.encode() + b"\n" + r.content
    doc = client.post(
        f"/api/analyses/{aid}/coverage/baseline",
        files={"file": ("both.ndjson", combined)}).json()
    assert doc["summary"]["cells_gap"] == 0
    assert doc["summary"]["cells_covered"] == 2
    last = doc["rows"][-1]
    assert last["outcome"] == "covered"
    assert last["cell"] == "400|(no content)"


def test_example_package_gap_reasons(client, analysis_id):
    """示例包：POST /pets 400 可补；legacy/decide 200 因 oneOf+not 不可补。"""
    raw = open("examples/calls-baseline.ndjson", "rb").read()
    r = client.post(
        f"/api/analyses/{analysis_id}/coverage/baseline",
        files={"file": ("calls.ndjson", raw)})
    assert r.status_code == 201
    doc = _gap_doc(client, analysis_id)
    assert doc["summary"]["gaps_total"] == 2
    by = {(c["operation"], c["cell_key"]): c for c in doc["cells"]}
    ok = by[("POST /pets", "400|(no content)")]
    assert ok["generated"] is True and ok["request_status"] == "400"
    na = by[("POST /legacy/decide", "200|application/json")]
    assert na["generated"] is False
    assert na["reasons"]


def test_gap_samples_do_not_mutate_saved_results(client, analysis_id):
    raw = open("examples/calls-baseline.ndjson", "rb").read()
    client.post(f"/api/analyses/{analysis_id}/coverage/baseline",
                files={"file": ("calls.ndjson", raw)})
    before = client.get(
        f"/api/analyses/{analysis_id}/coverage/baseline").json()
    client.get(
        f"/api/analyses/{analysis_id}/coverage/baseline/gap-samples")
    client.get(
        f"/api/analyses/{analysis_id}/coverage/baseline/gap-samples.ndjson")
    after = client.get(
        f"/api/analyses/{analysis_id}/coverage/baseline").json()
    assert after["summary"] == before["summary"]
    assert after["rows"] == before["rows"]
