import io
import os
import zipfile

import pytest

from app.services.loader import PackageError, load_package
from app.services.comparator import compare_packages
from app.services.instance import minimal_instance, validate
from app.services.schemagraph import SchemaGraph


def _zip(folder: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(folder):
            for fn in files:
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, folder))
    return buf.getvalue()


@pytest.fixture(scope="module")
def example_packages():
    base = load_package("b.zip", _zip("examples/baseline"))
    cand = load_package("c.zip", _zip("examples/candidate"))
    return base, cand


@pytest.fixture(scope="module")
def report(example_packages):
    return compare_packages(*example_packages)


def _rules(report):
    return [(f.rule, f.severity, f.path, f.method, f.location,
             f.status_code, f.facet) for f in report.findings]


def test_loader_detects_entry_and_cross_files(example_packages):
    base, cand = example_packages
    assert base.entry == "openapi.yaml"
    assert set(base.files) == {
        "openapi.yaml", "models/pet.yaml",
        "models/customer.yaml", "models/common.yaml"}


def test_loader_rejects_non_openapi():
    with pytest.raises(PackageError):
        load_package("x.yaml", b"foo: bar\n")


def test_cycles_terminate(report, example_packages):
    # Pet.parent -> Pet and Customer.friends -> Customer must not hang
    assert report.stats["cycles_terminated"] >= 4


def test_endpoint_removed_is_breaking(report):
    assert any(r[0] == "ENDPOINT_REMOVED" and r[2] == "/legacy/echo"
               and r[1] == "breaking" for r in _rules(report))


def test_endpoint_added_is_compatible(report):
    assert any(r[0] == "ENDPOINT_ADDED" and r[2] == "/pets/search"
               and r[1] == "compatible" for r in _rules(report))


def test_required_param_narrowing(report):
    hits = [f for f in report.findings
            if f.rule == "PARAMETER_MADE_REQUIRED"
            and f.path == "/pets" and f.location == "parameter"]
    assert hits and hits[0].severity == "breaking"


def test_status_code_removed(report):
    assert any(f.rule == "STATUS_REMOVED" and f.status_code == "400"
               and f.path == "/pets" and f.severity == "breaking"
               for f in report.findings)


def test_request_required_property_added(report):
    hits = [f for f in report.findings
            if f.rule == "REQUIRED_ADDED" and f.location == "requestBody"
            and f.facet == "required:kind"]
    assert hits and hits[0].severity == "breaking"


def test_response_enum_widened_is_breaking(report):
    hits = [f for f in report.findings
            if f.rule == "ENUM_WIDENED" and f.location == "response"]
    assert hits
    # status enum gains "adopted"; tier gains "platinum"
    msgs = " ".join(f.message for f in hits)
    assert "adopted" in msgs or "platinum" in msgs


def test_response_numeric_bound_widened(report):
    assert any(f.rule == "NUMBER_BOUNDS_WIDENED"
               and f.path == "/customers/{id}"
               and f.severity == "breaking" for f in report.findings)


def test_unsupported_keyword_is_undetermined(report):
    hits = [f for f in report.findings if f.rule == "UNSUPPORTED_KEYWORD"]
    assert any(f.facet == "pattern" for f in hits)
    assert all(f.severity == "undetermined" for f in hits)


def test_format_change_is_undetermined(report):
    hits = [f for f in report.findings if f.rule == "FORMAT_CHANGED"]
    assert hits and all(f.severity == "undetermined" for f in hits)


def test_oneof_branch_added_response_breaking(report):
    hits = [f for f in report.findings if f.rule == "ONEOF_BRANCH_ADDED"]
    assert hits
    assert all(f.location == "response" and f.severity == "breaking"
               for f in hits)


def test_all_schema_breaking_witnesses_one_sided(report):
    checked = 0
    pending = 0
    for f in report.findings:
        if f.severity != "breaking" or not f.witness:
            continue
        sc = f.witness.get("self_check")
        if not sc or "baseline_valid" not in sc:
            continue
        if "baseline_state" in sc:
            # 三态自检：待定反例不得被标注为已验证单侧通过
            if sc["baseline_state"] == "pending" or \
                    sc["candidate_state"] == "pending":
                pending += 1
                assert sc["one_side_only"] is False, (f.rule, f.path, sc)
                assert sc.get("verifiable") is False
                continue
        checked += 1
        assert sc["one_side_only"] is True, (
            f.rule, f.path, f.facet, sc)
    assert checked >= 20
    assert pending >= 1


def test_pending_oneof_witness_keeps_breaking_not_verified(report):
    """基线 oneOf 一过一待定（另一支含未支持 not）、候选仅 string：
    文档比对生成的 {} 反例不得自检为「基线通过、候选失败」。"""
    hits = [f for f in report.findings
            if f.path == "/legacy/decide" and f.rule == "ONEOF_BRANCH_REMOVED"]
    assert hits and hits[0].severity == "breaking"
    f = hits[0]
    sc = f.witness["self_check"]
    assert f.witness["instance"] == {}
    assert sc["baseline_state"] == "pending"
    assert sc["candidate_state"] == "fail"
    # 兼容字段：待定用 null（非 true），不得宣称单侧通过
    assert sc["baseline_valid"] is None
    assert sc["candidate_valid"] is False
    assert sc["one_side_only"] is False
    assert sc["verifiable"] is False
    reasons = sc["pending_reasons"]
    assert any("not" in r and "oneOf/1/not" in r for r in reasons), reasons


def _oneof_not_packages():
    base = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/x": {"post": {
            "requestBody": {"content": {"application/json": {"schema": {
                "oneOf": [
                    {"type": "object"},
                    {"type": "object", "not": {"type": "string"}}]}}}},
            "responses": {"200": {"description": "ok"}}}}}}
    cand = {
        "openapi": "3.0.3", "info": {"title": "c", "version": "1"},
        "paths": {"/x": {"post": {
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "string"}}}},
            "responses": {"200": {"description": "ok"}}}}}}
    import json as _json
    return (load_package("b.json", _json.dumps(base).encode()),
            load_package("c.json", _json.dumps(cand).encode()))


def test_oneof_pass_pending_replay_is_not_one_sided():
    """重放同样三态：一过一待定不得宣称基线有效、恰好单侧通过。"""
    from app.services.replay import replay
    pb, pc = _oneof_not_packages()
    rep = compare_packages(pb, pc)
    f = [f for f in rep.findings if f.rule == "ONEOF_BRANCH_REMOVED"][0]
    r = replay(pb, pc, f.witness)
    assert r["kind"] == "schema"
    rb, rc = r["root_validation"]["baseline"], r["root_validation"]["candidate"]
    assert rb["state"] == "pending" and rb["valid"] is None
    assert rc["state"] == "fail" and rc["valid"] is False
    assert r["exactly_one_side"] is False
    assert r["verifiable"] is False
    assert any("not" in x for x in r["pending_reasons"])
    # 编辑成 string 后：基线 oneOf 两支都不接受 object 约束 → 基线失败、
    # 候选通过 → 恰好单侧（确定通过/确定失败）
    r2 = replay(pb, pc, f.witness, instance_override="hello")
    assert r2["root_validation"]["candidate"]["state"] == "pass"
    assert r2["root_validation"]["baseline"]["state"] == "fail"
    assert r2["exactly_one_side"] is True


def test_failed_branch_not_hit_discarded_when_other_passes():
    """已知失败应压过同支未决命中：分支缺必填（确定失败）里的 not 丢弃，
    oneOf 另一支确定通过 → 确定通过。"""
    from app.services.instance import assess, TRI_PASS, TRI_FAIL
    from app.services.instance import reenter_for
    base = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/x": {"post": {
            "requestBody": {"content": {"application/json": {"schema": {
                "oneOf": [
                    {"type": "object"},
                    {"type": "object", "required": ["a"],
                     "not": {"type": "string"}}]}}}},
            "responses": {"200": {"description": "ok"}}}}}}
    import json as _json
    pkg = load_package("b.json", _json.dumps(base).encode())
    g = SchemaGraph(pkg.files, pkg.entry)
    node = g.build_at(
        "b.json",
        "/paths/~1x/post/requestBody/content/application~1json/schema")
    r = assess(node, {}, reenter=reenter_for(g))
    assert r.state == TRI_PASS and not r.hits


def test_anyof_one_pass_one_pending_with_settled_facets_passes():
    """同层其他约束确定时，anyOf 一过一待定仍可确定通过。"""
    from app.services.instance import assess, TRI_PASS, reenter_for
    base = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/x": {"post": {
            "requestBody": {"content": {"application/json": {"schema": {
                "anyOf": [
                    {"type": "object"},
                    {"type": "object", "not": {"type": "string"}}]}}}},
            "responses": {"200": {"description": "ok"}}}}}}
    import json as _json
    pkg = load_package("b.json", _json.dumps(base).encode())
    g = SchemaGraph(pkg.files, pkg.entry)
    node = g.build_at(
        "b.json",
        "/paths/~1x/post/requestBody/content/application~1json/schema")
    r = assess(node, {}, reenter=reenter_for(g))
    assert r.state == TRI_PASS


def test_oneof_two_passes_fails():
    from app.services.instance import assess, TRI_FAIL, reenter_for
    base = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/x": {"post": {
            "requestBody": {"content": {"application/json": {"schema": {
                "oneOf": [{"type": "object"}, {"type": "object"}]}}}},
            "responses": {"200": {"description": "ok"}}}}}}
    import json as _json
    pkg = load_package("b.json", _json.dumps(base).encode())
    g = SchemaGraph(pkg.files, pkg.entry)
    node = g.build_at(
        "b.json",
        "/paths/~1x/post/requestBody/content/application~1json/schema")
    assert assess(node, {}, reenter=reenter_for(g)).state == TRI_FAIL


def test_minimal_instance_validates(example_packages):
    base, _ = example_packages
    g = SchemaGraph(base.files, base.entry)
    pet = g.build_at("models/pet.yaml", "/Pet")
    inst = minimal_instance(pet)
    assert validate(pet, inst) == []


def test_external_ref_marked_unresolved():
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref":
                                            "https://example.com/schemas/x.json#/A"
                                    }}}}}}}}}
    pkg = load_package("openapi.json",
                       __import__("json").dumps(doc).encode())
    rep = compare_packages(pkg, pkg)
    assert rep.stats["unresolved_refs"] >= 1 or any(
        f.rule == "UNRESOLVED_REF" for f in rep.findings)


def test_unrelated_documents_are_comparable():
    a = {"openapi": "3.0.3", "info": {"title": "a", "version": "1"},
         "paths": {"/a": {"get": {"responses": {"200": {"description": "ok"}}}}}}
    b = {"openapi": "3.0.3", "info": {"title": "b", "version": "2"},
         "paths": {"/b": {"post": {"responses": {"201": {"description": "ok"}}}}}}
    import json as _json
    pa = load_package("a.json", _json.dumps(a).encode())
    pb = load_package("b.json", _json.dumps(b).encode())
    rep = compare_packages(pa, pb)
    # /a removed (breaking), /b added (compatible) without crashing
    rules = {f.rule for f in rep.findings}
    assert "ENDPOINT_REMOVED" in rules and "ENDPOINT_ADDED" in rules


# ------------------------------------------------- endpoint-level witnesses
def _removed_report(baseline_doc, candidate_paths=None):
    import json as _json
    pb = load_package("b.json", _json.dumps(baseline_doc).encode())
    cand = {"openapi": "3.0.3",
            "info": {"title": "c", "version": "2"},
            "paths": candidate_paths or {}}
    pc = load_package("c.json", _json.dumps(cand).encode())
    return pb, pc, compare_packages(pb, pc)


def _removed_witness(report, path=None, method=None):
    hits = [f for f in report.findings if f.rule == "ENDPOINT_REMOVED"
            and (path is None or f.path == path)
            and (method is None or f.method == method)]
    assert hits, "expected an ENDPOINT_REMOVED finding"
    return hits[0]


def test_endpoint_removed_witness_is_json_request():
    doc = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {
            "/shops/{shopId}/orders/{orderId}": {"post": {
                "parameters": [
                    {"name": "shopId", "in": "path", "required": True,
                     "schema": {"type": "integer", "minimum": 5}},
                    {"name": "orderId", "in": "path", "required": True,
                     "schema": {"type": "string", "minLength": 3}},
                    {"name": "X-Trace", "in": "header", "required": True,
                     "schema": {"type": "string"}},
                    {"name": "dryRun", "in": "query",
                     "schema": {"type": "boolean"}}],
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {
                        "type": "object", "required": ["qty"],
                        "properties": {"qty": {"type": "integer",
                                              "minimum": 1}}}}}},
                "responses": {"201": {"description": "ok"}}}}}}
    _, _, rep = _removed_report(doc)
    f = _removed_witness(rep, "/shops/{shopId}/orders/{orderId}", "POST")
    w = f.witness
    assert w["kind"] == "endpoint"
    assert w["verifiable"] is True
    req = w["instance"]
    assert req["method"] == "POST"
    # path placeholders substituted with concrete, schema-valid values
    assert req["path"] == "/shops/5/orders/xxx"
    assert req["header"] == {"X-Trace": ""}
    # optional query parameter omitted, like an old client would
    assert "dryRun" not in req["query"]
    assert req["body"] == {"qty": 1}
    assert w["request_body"]["media_type"] == "application/json"


def test_endpoint_removed_witness_optional_body_omitted():
    doc = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/echo": {"post": {
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object"}}}},
            "responses": {"200": {"description": "ok"}}}}}}
    _, _, rep = _removed_report(doc)
    w = _removed_witness(rep, "/echo", "POST").witness
    assert w["verifiable"] is True
    assert "body" not in w["instance"]
    assert w["self_check"]["caveats"]


def test_endpoint_removed_witness_unverifiable_keeps_breaking():
    # a required parameter whose schema is an external ref cannot be built
    doc = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/x": {"post": {
            "parameters": [{"name": "token", "in": "query", "required": True,
                            "schema": {"$ref": "https://e.example.com/s#/T"}}],
            "responses": {"200": {"description": "ok"}}}}}}
    _, _, rep = _removed_report(doc)
    f = _removed_witness(rep, "/x", "POST")
    assert f.severity == "breaking"  # conclusion kept
    assert f.witness["verifiable"] is False
    assert f.witness.get("instance") is None
    assert f.witness["unverifiable_reason"]
    assert f.witness["self_check"]["verifiable"] is False
    assert f.witness["self_check"]["endpoint_exists"] == {
        "baseline": True, "candidate": False}

    # a required body with only a binary media type is likewise unverifiable
    doc2 = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/blob": {"put": {
            "requestBody": {"required": True, "content": {
                "application/octet-stream": {"schema": {
                    "type": "string", "format": "binary"}}}},
            "responses": {"200": {"description": "ok"}}}}}}
    _, _, rep2 = _removed_report(doc2)
    w2 = _removed_witness(rep2, "/blob", "PUT").witness
    assert w2["verifiable"] is False and w2.get("instance") is None


def test_endpoint_replay_checks_existence_and_baseline_contract():
    from app.services.replay import replay
    doc = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {
            "/a/{id}": {"post": {
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "integer", "minimum": 1}}],
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {
                        "type": "object", "required": ["qty"],
                        "properties": {"qty": {"type": "integer",
                                              "minimum": 1}}}}}},
                "responses": {"201": {"description": "ok"}}}},
            # candidate keeps the path item but drops POST -> precise detail
        }}
    pb, pc, rep = _removed_report(doc, candidate_paths={
        "/a/{id}": {"get": {"responses": {"200": {"description": "kept"}}}}})
    w = _removed_witness(rep, "/a/{id}", "POST").witness
    r = replay(pb, pc, w)
    assert r["kind"] == "endpoint"
    assert r["endpoint_check"]["baseline"] is True
    assert r["endpoint_check"]["candidate"] is False
    assert "无 POST" in r["endpoint_check"]["candidate_detail"]
    assert r["baseline_check"]["valid"] is True
    assert r["verifiable"] is True and r["exactly_one_side"] is True

    # an edited request violating baseline constraints must not be faked
    bad = {"method": "POST", "path": "/a/0", "query": {}, "header": {},
           "cookie": {}, "body": {"qty": -2}}
    r2 = replay(pb, pc, w, instance_override=bad)
    assert r2["verifiable"] is False
    assert r2["exactly_one_side"] is False
    joined = " ".join(r2["baseline_check"]["errors"])
    assert "id" in joined and "qty" in joined

    # an unparseable/empty instance regenerates from the stored baseline
    r3 = replay(pb, pc, w, instance_override=None)
    assert r3["verifiable"] is True
    assert r3["instance"]["path"] == "/a/1"


def test_endpoint_replay_unverifiable_does_not_claim_one_side():
    from app.services.replay import replay
    doc = {
        "openapi": "3.0.3", "info": {"title": "b", "version": "1"},
        "paths": {"/x": {"post": {
            "parameters": [{"name": "token", "in": "query", "required": True,
                            "schema": {"$ref": "https://e.example.com/s#/T"}}],
            "responses": {"200": {"description": "ok"}}}}}}
    pb, pc, rep = _removed_report(doc)
    w = _removed_witness(rep, "/x", "POST").witness
    r = replay(pb, pc, w)
    assert r["exactly_one_side"] is False
    assert r["verifiable"] is False
    assert r["endpoint_check"]["baseline"] is True
    assert r["endpoint_check"]["candidate"] is False
    assert r.get("unverifiable_reason")
