import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient


def _zip_bytes(folder: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(folder):
            for fn in files:
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, folder))
    return buf.getvalue()


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


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


def test_health(client):
    d = client.get("/api/health").json()
    assert d["status"] == "ok"
    assert "mode" in d["storage"]


def test_analysis_flow(client, analysis_id):
    d = client.get(f"/api/analyses/{analysis_id}").json()
    assert d["summary"]["breaking"] > 0
    assert {f["severity"] for f in d["findings"]} <= {
        "breaking", "undetermined", "compatible"}


def test_findings_carry_json_pointer(client, analysis_id):
    d = client.get(f"/api/analyses/{analysis_id}").json()
    schema_finds = [f for f in d["findings"]
                    if f["location"] in ("requestBody", "response")
                    and f["pointer"]["candidate"]]
    assert schema_finds
    for f in schema_finds[:10]:
        assert f["pointer"]["candidate"].startswith("/")

def test_export_determinism(client, analysis_id):
    a = client.get(f"/api/analyses/{analysis_id}/export")
    b = client.get(f"/api/analyses/{analysis_id}/export")
    assert a.content == b.content
    hdr = a.headers["x-report-fingerprint"]
    import hashlib
    assert hashlib.sha256(a.content).hexdigest() == hdr


def test_export_severity_filter(client, analysis_id):
    r = client.get(
        f"/api/analyses/{analysis_id}/export?severity=breaking")
    d = r.json()
    assert d["findings"]
    assert all(f["severity"] == "breaking" for f in d["findings"])
    assert d["summary"]["undetermined"] == 0
    assert d["filter"]["severities"] == ["breaking"]


def test_export_invalid_filter(client, analysis_id):
    r = client.get(
        f"/api/analyses/{analysis_id}/export?severity=bogus")
    assert r.status_code == 400


def test_replay_one_sided(client, analysis_id):
    d = client.get(f"/api/analyses/{analysis_id}").json()
    target = next(
        f for f in d["findings"]
        if f["severity"] == "breaking" and f.get("witness")
        and f["witness"].get("self_check", {}).get("one_side_only"))
    r = client.post(
        f"/api/analyses/{analysis_id}/findings/{target['id']}/replay",
        json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["exactly_one_side"] is True
    rb, rc = (body["root_validation"]["baseline"]["valid"],
              body["root_validation"]["candidate"]["valid"])
    assert rb != rc


def test_replay_edited_instance(client, analysis_id):
    d = client.get(f"/api/analyses/{analysis_id}").json()
    target = next(f for f in d["findings"] if f["severity"] == "breaking"
                  and f.get("witness") and "instance" in f["witness"])
    # edit: send an empty object, re-validate
    r = client.post(
        f"/api/analyses/{analysis_id}/findings/{target['id']}/replay",
        json={"instance": {}})
    assert r.status_code == 200
    assert "root_validation" in r.json()


def test_pending_oneof_witness_chain_via_api(client, analysis_id):
    """模式级反例误验证修复的完整链路：finding 自检为 pending，
    重放 API 同样不得宣称基线有效、恰好单侧通过，并给出 not 位置。"""
    d = client.get(f"/api/analyses/{analysis_id}").json()
    target = next(f for f in d["findings"]
                  if f["endpoint"]["path"] == "/legacy/decide"
                  and f["rule"] == "ONEOF_BRANCH_REMOVED")
    sc = target["witness"]["self_check"]
    assert sc["baseline_state"] == "pending"
    assert sc["candidate_state"] == "fail"
    assert sc["baseline_valid"] is None
    assert sc["one_side_only"] is False
    assert sc["verifiable"] is False
    r = client.post(
        f"/api/analyses/{analysis_id}/findings/{target['id']}/replay",
        json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "schema"
    rb = body["root_validation"]["baseline"]
    rc = body["root_validation"]["candidate"]
    assert rb["state"] == "pending" and rb["valid"] is None
    assert rc["state"] == "fail"
    assert body["exactly_one_side"] is False
    assert body["verifiable"] is False
    joined = " ".join(body["pending_reasons"])
    assert "not" in joined and "oneOf/1/not" in joined


def test_404_unknown_analysis(client):
    assert client.get("/api/analyses/nope").status_code == 404


def test_history_lists_analysis(client, analysis_id):
    items = client.get("/api/analyses").json()["items"]
    assert any(x["id"] == analysis_id for x in items)


def test_endpoint_removed_replay(client, analysis_id):
    import json as _json
    d = client.get(f"/api/analyses/{analysis_id}").json()
    target = next(f for f in d["findings"]
                  if f["rule"] == "ENDPOINT_REMOVED")
    # the stored witness is a concrete JSON request, not null
    assert target["witness"]["kind"] == "endpoint"
    assert target["witness"]["instance"]["method"] == "POST"
    assert target["witness"]["verifiable"] is True
    r = client.post(
        f"/api/analyses/{analysis_id}/findings/{target['id']}/replay",
        json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "endpoint"
    assert body["endpoint_check"]["baseline"] is True
    assert body["endpoint_check"]["candidate"] is False
    assert body["baseline_check"]["valid"] is True
    assert body["verifiable"] is True
    assert body["exactly_one_side"] is True


def test_endpoint_removed_replay_editable(client, analysis_id):
    d = client.get(f"/api/analyses/{analysis_id}").json()
    target = next(f for f in d["findings"]
                  if f["rule"] == "ENDPOINT_REMOVED")
    edited = dict(target["witness"]["instance"])
    edited["header"] = {"X": "1"}  # extra headers are tolerated
    r = client.post(
        f"/api/analyses/{analysis_id}/findings/{target['id']}/replay",
        json={"instance": edited})
    assert r.status_code == 200
    assert r.json()["kind"] == "endpoint"


def _analysis_from_docs(client, baseline: dict, candidate: dict):
    import json as _json
    r = client.post(
        "/api/analyses",
        files={
            "baseline": ("b.json", _json.dumps(baseline).encode(),
                         "application/json"),
            "candidate": ("c.json", _json.dumps(candidate).encode(),
                          "application/json"),
        })
    assert r.status_code == 201, r.text
    return r.json()["analysis_id"]


def test_endpoint_removed_unverifiable_via_api(client):
    baseline = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/x": {"post": {
            "parameters": [{"name": "token", "in": "query", "required": True,
                            "schema": {"$ref": "https://e.example.com/s#/T"}}],
            "responses": {"200": {"description": "ok"}}}}}}
    candidate = {"openapi": "3.0.3", "info": {"title": "c", "version": "2"},
                 "paths": {}}
    aid = _analysis_from_docs(client, baseline, candidate)
    d = client.get(f"/api/analyses/{aid}").json()
    target = next(f for f in d["findings"]
                  if f["rule"] == "ENDPOINT_REMOVED")
    assert target["severity"] == "breaking"
    assert target["witness"]["verifiable"] is False
    assert target["witness"].get("instance") is None
    r = client.post(
        f"/api/analyses/{aid}/findings/{target['id']}/replay", json={})
    body = r.json()
    assert body["kind"] == "endpoint"
    assert body["verifiable"] is False
    assert body["exactly_one_side"] is False
    assert body["endpoint_check"]["baseline"] is True
    assert body["endpoint_check"]["candidate"] is False
    assert body["unverifiable_reason"]


# ------------------------------------------------------- structural replay
BODY_SCHEMA = {"type": "object",
               "required": ["a"],
               "properties": {"a": {"type": "string"}}}


def _find_finding(d, rule, **where):
    for f in d["findings"]:
        if f["rule"] != rule:
            continue
        if all(f.get(k) == v for k, v in where.items()):
            return f
    raise AssertionError(f"no {rule} finding matching {where}")


def _replay(client, aid, f, payload=None):
    r = client.post(
        f"/api/analyses/{aid}/findings/{f['id']}/replay",
        json={} if payload is None else payload)
    assert r.status_code == 200, r.text
    return r.json()


def test_structural_parameter_made_required_replay(client):
    baseline = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/p": {"get": {"parameters": [
            {"name": "q", "in": "query", "required": False,
             "schema": {"type": "string"}}],
            "responses": {"200": {"description": "ok"}}}}}}
    candidate = {
        "openapi": "3.0.3", "info": {"title": "c", "version": "2"},
        "paths": {"/p": {"get": {"parameters": [
            {"name": "q", "in": "query", "required": True,
             "schema": {"type": "string"}}],
            "responses": {"200": {"description": "ok"}}}}}}
    aid = _analysis_from_docs(client, baseline, candidate)
    d = client.get(f"/api/analyses/{aid}").json()
    f = _find_finding(d, "PARAMETER_MADE_REQUIRED", location="parameter")
    w = f["witness"]
    assert w["kind"] == "parameter"
    assert isinstance(w["instance"], dict)
    assert w["instance"]["parameter"] == "q"
    body = _replay(client, aid, f)
    assert body["kind"] == "structural"
    assert body["ok"] is True
    assert body["structural_check"]["baseline"]["ok"] is True
    assert body["structural_check"]["baseline"]["required"] is False
    assert body["structural_check"]["candidate"]["ok"] is True
    assert body["structural_check"]["candidate"]["required"] is True
    assert body["verifiable"] is True
    assert body["exactly_one_side"] is True
    assert "root_validation" not in body  # never fell back to schema replay


def test_structural_parameter_removed_replay(client):
    baseline = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/p": {"get": {"parameters": [
            {"name": "t", "in": "header",
             "schema": {"type": "integer", "minimum": 1}}],
            "responses": {"200": {"description": "ok"}}}}}}
    candidate = {
        "openapi": "3.0.3", "info": {"title": "c", "version": "2"},
        "paths": {"/p": {"get": {
            "responses": {"200": {"description": "ok"}}}}}}
    aid = _analysis_from_docs(client, baseline, candidate)
    d = client.get(f"/api/analyses/{aid}").json()
    f = _find_finding(d, "PARAMETER_REMOVED", location="parameter")
    body = _replay(client, aid, f)
    assert body["structural_check"]["baseline"]["declared"] is True
    assert body["structural_check"]["candidate"]["declared"] is False
    assert body["verifiable"] is True
    # an edited value violating the baseline schema makes it unverifiable
    bad = dict(f["witness"]["instance"])
    bad["value"] = -5
    body2 = _replay(client, aid, f, {"instance": bad})
    assert body2["verifiable"] is False
    assert body2["exactly_one_side"] is False
    assert body2["structural_check"]["baseline"]["ok"] is False


def test_structural_request_body_removed_replay(client):
    baseline = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/p": {"post": {"requestBody": {
            "required": True,
            "content": {"application/json": {"schema": BODY_SCHEMA}}},
            "responses": {"200": {"description": "ok"}}}}}}
    candidate = {
        "openapi": "3.0.3", "info": {"title": "c", "version": "2"},
        "paths": {"/p": {"post": {
            "responses": {"200": {"description": "ok"}}}}}}
    aid = _analysis_from_docs(client, baseline, candidate)
    d = client.get(f"/api/analyses/{aid}").json()
    f = _find_finding(d, "PARAMETER_REMOVED", location="requestBody")
    w = f["witness"]
    assert w["kind"] == "body"
    assert w["instance"]["media_type"] == "application/json"
    assert w["instance"]["body"] == {"a": ""}
    body = _replay(client, aid, f)
    assert body["structural_check"]["baseline"]["ok"] is True
    assert body["structural_check"]["candidate"]["declared"] is False
    assert body["carried_value"]["baseline"]["valid"] is True
    assert body["carried_value"]["candidate"]["applicable"] is False
    assert body["verifiable"] is True


def test_structural_request_body_made_required_replay(client):
    baseline = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/p": {"post": {"requestBody": {
            "required": False,
            "content": {"application/json": {"schema": BODY_SCHEMA}}},
            "responses": {"200": {"description": "ok"}}}}}}
    candidate = {
        "openapi": "3.0.3", "info": {"title": "c", "version": "2"},
        "paths": {"/p": {"post": {"requestBody": {
            "required": True,
            "content": {"application/json": {"schema": BODY_SCHEMA}}},
            "responses": {"200": {"description": "ok"}}}}}}
    aid = _analysis_from_docs(client, baseline, candidate)
    d = client.get(f"/api/analyses/{aid}").json()
    f = _find_finding(d, "PARAMETER_MADE_REQUIRED", location="requestBody")
    body = _replay(client, aid, f)
    assert body["structural_check"]["baseline"]["required"] is False
    assert body["structural_check"]["candidate"]["required"] is True
    assert body["verifiable"] is True


def test_structural_parameter_added_required_replay(client):
    baseline = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/p": {"get": {
            "responses": {"200": {"description": "ok"}}}}}}
    candidate = {
        "openapi": "3.0.3", "info": {"title": "c", "version": "2"},
        "paths": {"/p": {"get": {"parameters": [
            {"name": "newp", "in": "query", "required": True,
             "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "ok"}}}}}}
    aid = _analysis_from_docs(client, baseline, candidate)
    d = client.get(f"/api/analyses/{aid}").json()
    f = _find_finding(d, "PARAMETER_ADDED_REQUIRED", location="parameter")
    assert f["witness"]["instance"]["parameter"] == "newp"
    body = _replay(client, aid, f)
    assert body["structural_check"]["baseline"]["declared"] is False
    assert body["structural_check"]["candidate"]["required"] is True
    assert body["verifiable"] is True


def test_structural_request_body_added_required_replay(client):
    baseline = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/p": {"post": {
            "responses": {"200": {"description": "ok"}}}}}}
    candidate = {
        "openapi": "3.0.3", "info": {"title": "c", "version": "2"},
        "paths": {"/p": {"post": {"requestBody": {
            "required": True,
            "content": {"application/json": {"schema": BODY_SCHEMA}}},
            "responses": {"200": {"description": "ok"}}}}}}
    aid = _analysis_from_docs(client, baseline, candidate)
    d = client.get(f"/api/analyses/{aid}").json()
    f = _find_finding(d, "PARAMETER_ADDED_REQUIRED", location="requestBody")
    body = _replay(client, aid, f)
    assert body["structural_check"]["baseline"]["declared"] is False
    assert body["structural_check"]["candidate"]["required"] is True
    assert body["verifiable"] is True


def test_structural_status_removed_replay(client):
    baseline = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/p": {"post": {
            "responses": {
                "200": {"description": "ok"},
                "400": {"description": "bad", "content": {
                    "application/json": {"schema": {"type": "object"}}}}}}}}}
    candidate = {
        "openapi": "3.0.3", "info": {"title": "c", "version": "2"},
        "paths": {"/p": {"post": {
            "responses": {"200": {"description": "ok"}}}}}}
    aid = _analysis_from_docs(client, baseline, candidate)
    d = client.get(f"/api/analyses/{aid}").json()
    f = _find_finding(d, "STATUS_REMOVED", status_code="400")
    w = f["witness"]
    assert w["kind"] == "response"
    assert w["instance"]["status_code"] == "400"
    body = _replay(client, aid, f)
    assert body["structural_check"]["baseline"]["declared"] is True
    assert body["structural_check"]["candidate"]["declared"] is False
    assert body["carried_value"]["candidate"]["applicable"] is False
    assert body["verifiable"] is True
    # example package STATUS_REMOVED (400 with no content) also replays


def test_structural_media_removed_replay(client):
    baseline = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/p": {
            "post": {"requestBody": {
                "required": True,
                "content": {"application/xml": {"schema": {"type": "object"}}}},
                "responses": {"200": {"description": "ok", "content": {
                    "text/plain": {"schema": {"type": "string"}}}}}}}}}
    candidate = {
        "openapi": "3.0.3", "info": {"title": "c", "version": "2"},
        "paths": {"/p": {"post": {"requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"type": "object"}}}},
            "responses": {"200": {"description": "ok"}}}}}}
    aid = _analysis_from_docs(client, baseline, candidate)
    d = client.get(f"/api/analyses/{aid}").json()
    f_req = _find_finding(d, "MEDIA_REMOVED", location="requestBody",
                          media_type="application/xml")
    body = _replay(client, aid, f_req)
    assert body["structural_check"]["baseline"]["declared"] is True
    assert body["structural_check"]["candidate"]["declared"] is False
    assert body["carried_value"]["baseline"]["valid"] is True
    assert body["verifiable"] is True

    f_resp = _find_finding(d, "MEDIA_REMOVED", location="response",
                           media_type="text/plain")
    body2 = _replay(client, aid, f_resp)
    assert body2["verifiable"] is True
    assert body2["direction"] == "response"


def test_structural_ref_containers_replay(client):
    """$ref'd requestBody / response must be resolved during replay."""
    import io
    import zipfile

    def zip_bytes(files):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in files.items():
                zf.writestr(name, data)
        return buf.getvalue()

    import json as _json
    baseline = {
        "openapi.yaml": _json.dumps({
            "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
            "paths": {"/p": {"post": {
                "requestBody": {"$ref": "components.yaml#/RequestBody/Item"},
                "responses": {
                    "200": {"description": "ok"},
                    "418": {"$ref": "components.yaml#/Responses/Tea"}}}}}}),
        "components.yaml": _json.dumps({
            "RequestBody": {"Item": {
                "required": True,
                "content": {"application/json": {"schema": {
                    "type": "object", "required": ["id"],
                    "properties": {"id": {"type": "integer"}}}}}}},
            "Responses": {"Tea": {
                "description": "teapot",
                "content": {"application/json": {
                    "schema": {"type": "object"}}}}}}),
    }
    candidate = {
        "openapi.yaml": _json.dumps({
            "openapi": "3.0.3", "info": {"title": "c", "version": "2"},
            "paths": {"/p": {"post": {
                "responses": {"200": {"description": "ok"}}}}}}),
        "components.yaml": _json.dumps({}),
    }
    r = client.post(
        "/api/analyses",
        files={
            "baseline": ("b.zip", zip_bytes(baseline), "application/zip"),
            "candidate": ("c.zip", zip_bytes(candidate), "application/zip"),
        })
    assert r.status_code == 201, r.text
    aid = r.json()["analysis_id"]
    d = client.get(f"/api/analyses/{aid}").json()
    body_finding = _find_finding(d, "PARAMETER_REMOVED",
                                 location="requestBody")
    assert body_finding["witness"]["instance"]["body"] == {"id": 0}
    body = _replay(client, aid, body_finding)
    assert body["verifiable"] is True
    assert body["carried_value"]["baseline"]["valid"] is True

    status_finding = _find_finding(d, "STATUS_REMOVED", status_code="418")
    sbody = _replay(client, aid, status_finding)
    assert sbody["verifiable"] is True
    assert sbody["carried_value"]["baseline"]["valid"] is True


def test_structural_editable_instance(client):
    baseline = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/p": {"get": {"parameters": [
            {"name": "q", "in": "query", "required": False,
             "schema": {"type": "integer", "minimum": 10}}],
            "responses": {"200": {"description": "ok"}}}}}}
    candidate = {
        "openapi": "3.0.3", "info": {"title": "c", "version": "2"},
        "paths": {"/p": {"get": {"parameters": [
            {"name": "q", "in": "query", "required": True,
             "schema": {"type": "integer", "minimum": 10}}],
            "responses": {"200": {"description": "ok"}}}}}}
    aid = _analysis_from_docs(client, baseline, candidate)
    d = client.get(f"/api/analyses/{aid}").json()
    f = _find_finding(d, "PARAMETER_MADE_REQUIRED", location="parameter")
    # edited descriptor with an extra unrelated field is still accepted
    edited = dict(f["witness"]["instance"])
    edited["note"] = "hand-edited"
    body = _replay(client, aid, f, {"instance": edited})
    assert body["verifiable"] is True
    assert body["instance"]["note"] == "hand-edited"


def test_parameter_schema_finding_still_uses_schema_replay(client):
    """Schema findings inside a parameter schema must not be routed to the
    structural replay (both share witness.kind == "parameter")."""
    baseline = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/p": {"get": {"parameters": [
            {"name": "status", "in": "query",
             "schema": {"type": "string",
                        "enum": ["available", "pending", "sold"]}}],
            "responses": {"200": {"description": "ok"}}}}}}
    candidate = {
        "openapi": "3.0.3", "info": {"title": "c", "version": "2"},
        "paths": {"/p": {"get": {"parameters": [
            {"name": "status", "in": "query",
             "schema": {"type": "string",
                        "enum": ["available", "sold"]}}],
            "responses": {"200": {"description": "ok"}}}}}}
    aid = _analysis_from_docs(client, baseline, candidate)
    d = client.get(f"/api/analyses/{aid}").json()
    f = _find_finding(d, "ENUM_NARROWED", location="parameter")
    assert f["witness"]["kind"] == "parameter"
    body = _replay(client, aid, f)
    assert body["kind"] == "schema"
    assert body["exactly_one_side"] is True
    rb = body["root_validation"]["baseline"]["valid"]
    rc = body["root_validation"]["candidate"]["valid"]
    assert rb is True and rc is False


def test_example_package_structural_replays(client, analysis_id):
    """Regression: structural findings no longer fail on root schema ptr."""
    d = client.get(f"/api/analyses/{analysis_id}").json()
    structural_rules = {"PARAMETER_MADE_REQUIRED", "PARAMETER_REMOVED",
                        "PARAMETER_ADDED_REQUIRED", "STATUS_REMOVED",
                        "MEDIA_REMOVED"}
    targets = [f for f in d["findings"]
               if f["rule"] in structural_rules and f.get("witness")]
    assert targets  # example package contains PARAMETER_MADE_REQUIRED etc.
    for f in targets:
        body = _replay(client, analysis_id, f)
        assert body["ok"] is True, (f["rule"], body)
        assert body["kind"] == "structural"
        assert "error" not in body
        assert body["structural_check"]["baseline"]
        assert body["structural_check"]["candidate"]


# ------------------------------------------------------------- release gate
def test_release_gate_uses_complete_findings_and_exceptions(client,
                                                             analysis_id):
    report = client.get(f"/api/analyses/{analysis_id}").json()
    breaking = next(f for f in report["findings"]
                    if f["severity"] == "breaking")
    undetermined = next(f for f in report["findings"]
                        if f["severity"] == "undetermined")

    gate = client.get(
        f"/api/analyses/{analysis_id}/release-decision").json()
    assert gate["decision"] == "blocked"
    assert gate["evaluation"]["severity_source"] == \
        "report.findings[*].severity"
    assert gate["evaluation"]["endpoint_summary_used"] is False
    assert gate["evaluation"]["filtered_findings_used"] is False
    assert gate["evaluation"]["witness_self_check_used_as_severity"] is False
    assert len(gate["items"]) == len(report["findings"])

    r = client.put(
        f"/api/analyses/{analysis_id}/release-exceptions/{breaking['id']}",
        json={"reason": "已确认兼容窗口内灰度",
              "expires_at": "3000-01-01T00:00:00Z"})
    assert r.status_code == 200, r.text
    assert r.json()["exception"]["report_version"] == \
        report["report_version"]
    gate = r.json()["decision"]
    assert gate["decision"] == "pending_review"
    assert gate["summary"]["waived_breaking"] >= 1
    assert gate["summary"]["remaining_breaking"] < gate["summary"]["breaking"]

    r = client.put(
        f"/api/analyses/{analysis_id}/release-exceptions/"
        f"{undetermined['id']}",
        json={"reason": "人工复核完成", "expires_at": "3000-01-01T00:00:00Z"})
    assert r.status_code == 200, r.text
    assert r.json()["decision"]["decision"] == "releasable"


def test_release_exception_rejects_unknown_finding_and_bad_expiry(client,
                                                                  analysis_id):
    r = client.put(
        f"/api/analyses/{analysis_id}/release-exceptions/unknown",
        json={"reason": "x", "expires_at": "3000-01-01T00:00:00Z"})
    assert r.status_code == 404

    report = client.get(f"/api/analyses/{analysis_id}").json()
    fid = next(f["id"] for f in report["findings"]
               if f["severity"] == "breaking")
    r = client.put(
        f"/api/analyses/{analysis_id}/release-exceptions/{fid}",
        json={"reason": "x", "expires_at": "2000-01-01T00:00:00Z"})
    assert r.status_code == 400

    r = client.put(
        f"/api/analyses/{analysis_id}/release-exceptions/{fid}",
        json={"reason": "x", "expires_at": "3000-01-01T00:00:00"})
    assert r.status_code == 400


def test_release_exception_is_bound_to_recomputed_report_version(
        client, monkeypatch):
    import copy
    from app.api import routes
    from app.serialization import findings_version

    r = client.post(
        "/api/analyses",
        files={
            "baseline": ("b.zip", _zip_bytes("examples/baseline"),
                         "application/zip"),
            "candidate": ("c.zip", _zip_bytes("examples/candidate"),
                          "application/zip"),
        })
    assert r.status_code == 201, r.text
    first = r.json()
    aid = first["analysis_id"]
    fid = next(f["id"] for f in first["findings"]
               if f["severity"] == "breaking")
    old_version = first["report_version"]

    r = client.put(f"/api/analyses/{aid}/release-exceptions/{fid}",
                   json={"reason": "old report approval",
                         "expires_at": "3000-01-01T00:00:00Z"})
    assert r.status_code == 200, r.text

    original_analyze = routes.analyze

    def recomputed_analyze(*args, **kwargs):
        result = original_analyze(*args, **kwargs)
        changed = copy.deepcopy(result)
        target = next(f for f in changed["report_doc"]["findings"]
                      if f["id"] == fid)
        target["rule_detail"] = target["rule_detail"] + "（重算后补充依据）"
        changed["report_doc"]["report_version"] = findings_version(
            changed["report_doc"]["findings"])
        return changed

    monkeypatch.setattr(routes, "analyze", recomputed_analyze)
    r = client.post(
        "/api/analyses",
        files={
            "baseline": ("b.zip", _zip_bytes("examples/baseline"),
                         "application/zip"),
            "candidate": ("c.zip", _zip_bytes("examples/candidate"),
                          "application/zip"),
        })
    assert r.status_code == 201, r.text
    assert r.json()["analysis_id"] == aid
    assert r.json()["report_version"] != old_version

    gate = client.get(f"/api/analyses/{aid}/release-decision").json()
    item = next(x for x in gate["items"] if x["finding_id"] == fid)
    assert item["active_exception"] is None
    assert item["stale_exceptions"]
    assert item["stale_exceptions"][0]["state"] == "stale"
    assert "已重算" in item["reason"]
    assert gate["summary"]["exceptions_stale"] >= 1

    monkeypatch.undo()
    r = client.post(
        "/api/analyses",
        files={
            "baseline": ("b.zip", _zip_bytes("examples/baseline"),
                         "application/zip"),
            "candidate": ("c.zip", _zip_bytes("examples/candidate"),
                          "application/zip"),
        })
    assert r.status_code == 201, r.text
    assert r.json()["report_version"] == old_version
