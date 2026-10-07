"""Cycle termination, cross-file refs and OAS 3.1 edge cases."""
import json

from app.services.loader import load_package
from app.services.comparator import compare_packages
from app.services.instance import minimal_instance, validate
from app.services.schemagraph import SchemaGraph


def _pkg(doc):
    return load_package("openapi.json", json.dumps(doc).encode())


def _ref_doc():
    return {
        "openapi": "3.1.0", "info": {"title": "t", "version": "1"},
        "paths": {"/x": {"post": {
            "requestBody": {"content": {"application/json": {
                "schema": {"$ref": "#/$defs/A"}}}},
            "responses": {"200": {"description": "ok"}}}}},
        "$defs": {
            "A": {"allOf": [
                {"$ref": "#/$defs/B"},
                {"type": "object", "required": ["a"],
                 "properties": {
                     "a": {"type": "string"},
                     "self": {"$ref": "#/$defs/A"}}}]},
            "B": {"type": "object", "required": ["b"],
                  "properties": {
                      "b": {"type": "string", "nullable": True},
                      "other": {"$ref": "#/$defs/B"}}}}}


def test_self_and_mutual_cycles_terminate():
    pkg = _pkg(_ref_doc())
    g = SchemaGraph(pkg.files, pkg.entry)
    root = g.build_at(
        pkg.entry,
        "/paths/~1x/post/requestBody/content/application~1json/schema")
    assert root.properties["self"].is_cycle
    assert root.properties["other"].is_cycle
    assert g.stats.cycle_terminations >= 2
    inst = minimal_instance(root)
    assert validate(root, inst) == []
    assert set(inst) == {"a", "b"}


def test_allof_intersection_merges_required_and_type():
    pkg = _pkg(_ref_doc())
    g = SchemaGraph(pkg.files, pkg.entry)
    root = g.build_at(
        pkg.entry,
        "/paths/~1x/post/requestBody/content/application~1json/schema")
    assert root.required == {"a", "b"}
    assert "object" in (root.type or [])


def test_nullable_type_array_31():
    doc = {
        "openapi": "3.1.0", "info": {"title": "t", "version": "1"},
        "paths": {"/x": {"get": {"responses": {"200": {
            "description": "ok", "content": {"application/json": {"schema": {
                "type": ["string", "null"]}}}}}}}}}
    pkg = _pkg(doc)
    g = SchemaGraph(pkg.files, pkg.entry)
    node = g.build_at(
        pkg.entry,
        "/paths/~1x/get/responses/200/content/application~1json/schema")
    assert "null" in node.type and "string" in node.type


def test_identical_recursive_spec_has_no_findings():
    pkg = _pkg(_ref_doc())
    rep = compare_packages(pkg, pkg)
    assert not [f for f in rep.findings if f.severity == "breaking"]
    assert not [f for f in rep.findings if f.severity == "undetermined"]


def test_deep_mutual_recursion_does_not_hang():
    doc = {
        "openapi": "3.0.3", "info": {"title": "t", "version": "1"},
        "paths": {"/x": {"post": {
            "requestBody": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/A"}}}},
            "responses": {"200": {"description": "ok", "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/A"}}}}}}}},
        "components": {"schemas": {
            "A": {"type": "object", "required": ["b"], "properties": {
                "b": {"$ref": "#/components/schemas/B"},
                "children": {"type": "array", "items": {
                    "$ref": "#/components/schemas/A"}}}},
            "B": {"allOf": [
                {"$ref": "#/components/schemas/C"},
                {"type": "object", "properties": {
                    "a": {"$ref": "#/components/schemas/A"}}}]},
            "C": {"type": "object", "properties": {
                "c": {"type": "string"},
                "back": {"$ref": "#/components/schemas/B"}}}}}}
    pkg = _pkg(doc)
    rep = compare_packages(pkg, pkg)
    assert rep.stats["cycles_terminated"] >= 4
    assert not [f for f in rep.findings if f.severity == "breaking"]
