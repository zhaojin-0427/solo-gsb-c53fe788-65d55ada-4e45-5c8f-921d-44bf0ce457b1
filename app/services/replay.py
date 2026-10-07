"""Replay a counter-example against both documents.

Three witness shapes are supported:

* **schema-level** witnesses (schema changes inside a parameter, body or
  response media type): the stored report carries the facet-level finding
  and a JSON instance; replay rebuilds the two schema graphs and validates
  the instance against the *root* media-type/parameter schema of each
  side, then — when a facet JSON Pointer is supplied — navigates to the
  facet and validates the value there. Both root and facet checks return
  the three-state pass / fail / pending verdict; a pending side is never
  a verified one-sided pass (``exactly_one_side`` requires both sides
  definite).
* **structural** witnesses (``PARAMETER_ADDED_REQUIRED`` /
  ``PARAMETER_MADE_REQUIRED`` / ``PARAMETER_REMOVED`` on parameters or
  request bodies, ``STATUS_REMOVED``, ``MEDIA_REMOVED``): one side has no
  media schema at all.  Replay is delegated to
  :mod:`app.services.structuralreplay`, which re-derives declaration,
  required-ness and (where present) the carried value/payload validity
  per the finding's semantics.
* **endpoint-level** witnesses (:class:`ENDPOINT_REMOVED`): there is no
  candidate schema at all.  Replay re-checks the *existence* of the
  endpoint on both documents and re-validates the minimal JSON request
  (method, path with concrete path parameters, required query/header/
  cookie parameters and, when applicable, a minimal body) against the
  baseline request contract.
"""
from __future__ import annotations

from typing import Any, Optional

from . import pointer as ptr
from .endpointrequest import BuildFailure, build_request, check_request, \
    find_operation
from .instance import check_tri, format_pending_hits, reenter_for, \
    TRI_PASS, TRI_FAIL
from .schemagraph import SchemaGraph
from .structuralreplay import replay_structural


class ReplayError(ValueError):
    pass


def _build_graph(pkg) -> SchemaGraph:
    return SchemaGraph(pkg.files, pkg.entry)


def root_schema_node(graph: SchemaGraph, side_info: dict):
    """Rebuild the root media-type schema node for a stored witness."""
    file = side_info.get("root_file") or graph.entry
    pointer = side_info.get("root_pointer")
    if not pointer:
        raise ReplayError("重放信息缺少根 schema 指针")
    return graph.build_at(file, pointer)


def validate_root(node, instance: Any, graph: SchemaGraph) -> dict:
    """Three-state root validation.

    ``valid`` stays boolean for old consumers; ``state`` carries the
    pass / fail / pending verdict and ``valid`` is ``null`` when the value
    might still fail (unsupported keywords such as ``not`` / ``pattern``,
    recursive or unresolvable schema sites). Pending is never reported as
    a verified pass — ``one_side_only`` requires BOTH sides definite.
    """
    result = check_tri(node, instance, reenter=reenter_for(graph))
    return _tri_side(result, graph.entry)


def _tri_side(result, entry: str) -> dict:
    pending = format_pending_hits(result.hits, entry)
    return {
        "state": result.state,
        "valid": True if result.state == TRI_PASS else
        False if result.state == TRI_FAIL else None,
        "errors": result.errors,
        "pending": pending,
    }


def navigate(instance: Any, relpath: str) -> tuple[Any, Optional[str]]:
    """Return (value_at_relpath, error)."""
    if not relpath:
        return instance, None
    cur = instance
    for token in ptr.parse(relpath):
        if isinstance(cur, dict):
            if token not in cur:
                return None, f"反例中不存在路径 {relpath}"
            cur = cur[token]
        elif isinstance(cur, list):
            try:
                cur = cur[int(token)]
            except (ValueError, IndexError):
                return None, f"反例中不存在路径 {relpath}"
        else:
            return None, f"无法沿 {relpath} 导航到标量"
    return cur, None


def facet_value(schema_root: dict, facet_pointer: str) -> Any:
    """Fetch the raw schema fragment at the facet pointer (for display)."""
    try:
        return ptr.resolve(schema_root, facet_pointer)
    except (KeyError, IndexError, ValueError):
        return None


def replay(pkg_baseline, pkg_candidate, witness: dict,
           instance_override: Any = None, rule: Optional[str] = None) -> dict:
    kind = witness.get("kind")
    if kind == "endpoint":
        return replay_endpoint(pkg_baseline, pkg_candidate, witness,
                               instance_override)
    if _is_structural(kind, witness, rule):
        # Structural findings (parameter made required / added required /
        # removed, requestBody changes, status or media type removed) have
        # no media schema on one side; check declaration/acceptance per the
        # finding's semantics instead of root-schema validation.
        gb = _build_graph(pkg_baseline)
        gc = _build_graph(pkg_candidate)
        result = replay_structural(gb, gc, rule or "", witness,
                                   instance_override)
        result["graph_stats"] = {
            "baseline": vars(gb.stats),
            "candidate": vars(gc.stats),
        }
        return result
    return replay_schema(pkg_baseline, pkg_candidate, witness,
                         instance_override)


# Rules whose counter-example is a structural descriptor (parameter/body/
# response/media presence and required-ness) rather than a JSON document
# validated against two media-type schemas.
_STRUCTURAL_RULES = {
    "PARAMETER_ADDED_REQUIRED", "PARAMETER_MADE_REQUIRED",
    "PARAMETER_REMOVED", "STATUS_REMOVED", "MEDIA_REMOVED",
}
_STRUCTURAL_KINDS = {"parameter", "body", "response", "media"}


def _is_structural(kind: Optional[str], witness: dict,
                   rule: Optional[str]) -> bool:
    """Structural finding, as opposed to a schema-level one.

    Schema findings *inside* a parameter schema (enum narrowed, bounds
    tightened …) also use ``kind == "parameter"`` but carry root media/
    parameter schema pointers and must stay on the schema-validation path.
    Structural findings carry no root pointers and belong to the structural
    rule set.
    """
    if kind not in _STRUCTURAL_KINDS:
        return False
    effective_rule = rule or witness.get("rule")
    has_root = bool((witness.get("baseline") or {}).get("root_pointer"))
    if effective_rule in _STRUCTURAL_RULES:
        return True
    # older stored reports may lack the rule on the witness: structural
    # witnesses never carried root schema pointers
    return not has_root


def replay_schema(pkg_baseline, pkg_candidate, witness: dict,
                  instance_override: Any = None) -> dict:
    """Replay a schema-level witness by validating against both root schemas.

    Each side gets a three-state verdict (确定通过 / 确定失败 / 待定):
    an instance whose validity still depends on an unsupported keyword
    (e.g. a ``oneOf`` branch with ``not``), a recursive schema site or an
    unresolvable node is **待定** on that side, never a verified one-sided
    pass. ``exactly_one_side`` is therefore true only when both sides are
    definite and the verdicts differ; ``verifiable`` mirrors it.
    """
    instance = instance_override if instance_override is not None \
        else witness.get("instance")
    direction = witness.get("direction")
    binfo = witness.get("baseline") or {}
    cinfo = witness.get("candidate") or {}

    gb = _build_graph(pkg_baseline)
    gc = _build_graph(pkg_candidate)
    try:
        bn = root_schema_node(gb, binfo)
        cn = root_schema_node(gc, cinfo)
    except ReplayError as exc:
        return {"ok": False, "error": str(exc)}

    root_b = validate_root(bn, instance, gb)
    root_c = validate_root(cn, instance, gc)

    rel = witness.get("replay_path") or ""
    facet_value_check = None
    sub_value, nav_error = navigate(instance, rel)
    if nav_error is None and rel:
        # validate the located sub-value against the facet nodes by walking
        # the graph along the same relative path
        fb_node = _walk_node(bn, rel)
        fc_node = _walk_node(cn, rel)
        facet_value_check = {
            "path": rel,
            "value": sub_value,
            "baseline": (_tri_side(check_tri(
                fb_node, sub_value, reenter=reenter_for(gb)), gb.entry)
                if fb_node else None),
            "candidate": (_tri_side(check_tri(
                fc_node, sub_value, reenter=reenter_for(gc)), gc.entry)
                if fc_node else None),
        }

    exact = root_b["state"] == TRI_PASS and root_c["state"] == TRI_FAIL or \
        root_b["state"] == TRI_FAIL and root_c["state"] == TRI_PASS
    pending_reasons = (root_b.get("pending") or []) + \
        (root_c.get("pending") or [])
    return {
        "ok": True,
        "kind": "schema",
        "direction": direction,
        "instance": instance,
        "root_validation": {"baseline": root_b, "candidate": root_c},
        "facet_validation": facet_value_check,
        "exactly_one_side": exact,
        "verifiable": exact,
        "pending_reasons": pending_reasons,
        "graph_stats": {
            "baseline": vars(gb.stats),
            "candidate": vars(gc.stats),
        },
    }


# ========================================================= endpoint replay
def replay_endpoint(pkg_baseline, pkg_candidate, witness: dict,
                    request_override: Any = None) -> dict:
    """Replay a minimal HTTP request for a removed endpoint.

    The breaking story is structural — baseline defines the operation, the
    candidate does not — and independent of any JSON schema; additionally,
    the request is re-validated against the baseline contract (path/query/
    header/cookie parameters and required body).  If the baseline request
    cannot be reconstructed or validated the verdict stays breaking but the
    counter-example is reported *unverifiable*, never as a one-sided pass.
    """
    gb = _build_graph(pkg_baseline)
    gc = _build_graph(pkg_candidate)

    method = (witness.get("method") or "").lower()
    template = witness.get("request_template") or witness.get("path") or ""
    instance = request_override if request_override is not None \
        else witness.get("instance")

    b_found = find_operation(gb, template, method)
    c_found = find_operation(gc, template, method)
    baseline_exists = b_found is not None and b_found[0] is not None
    candidate_exists = c_found is not None and c_found[0] is not None

    # candidate may keep the path item but drop the method: say precisely so
    if c_found is not None and not candidate_exists:
        candidate_detail = f"候选仍保留路径 {template}，但已无 {method.upper()} 操作"
    elif c_found is None:
        candidate_detail = f"候选文档中不存在路径 {template}"
    else:
        candidate_detail = f"候选仍存在 {method.upper()} {template}"
    if b_found is not None and not baseline_exists:
        baseline_detail = f"基线保留路径 {template}，但无 {method.upper()} 操作"
    else:
        baseline_detail = f"基线定义了 {method.upper()} {template}"

    existence = {
        "baseline": baseline_exists,
        "candidate": candidate_exists,
        "baseline_detail": baseline_detail,
        "candidate_detail": candidate_detail,
    }

    result: dict[str, Any] = {
        "ok": True,
        "kind": "endpoint",
        "direction": witness.get("direction") or "request",
        "method": method.upper(),
        "path": template,
        "instance": instance,
        "endpoint_check": existence,
        "graph_stats": {
            "baseline": vars(gb.stats),
            "candidate": vars(gc.stats),
        },
    }

    if not baseline_exists:
        # report inconsistency: the witness claimed an operation the stored
        # baseline no longer contains — do not pretend the request is valid
        result.update({
            "baseline_check": {"valid": False,
                               "errors": ["基线文档中找不到该端点，"
                                          "反例无法按基线契约核对"]},
            "verifiable": False,
            "exactly_one_side": False,
            "unverifiable_reason": "基线端点缺失，无法核对请求约束",
        })
        return result

    if not isinstance(instance, dict):
        # No usable request (e.g. an old report generated before endpoint
        # witnesses existed, or the user cleared the editor): regenerate a
        # fresh minimal request from the stored baseline package.
        try:
            built = build_request(gb, template, method)
            instance = built["request"]
            result["instance"] = instance
            regenerated = True
            regen_error = None
        except BuildFailure as exc:
            regenerated = False
            regen_error = str(exc)
        if not regenerated:
            result.update({
                "baseline_check": {
                    "valid": False,
                    "errors": [f"无法从基线重建最小请求: {regen_error}"]},
                "verifiable": False,
                "exactly_one_side": False,
                "unverifiable_reason": regen_error,
            })
            return result

    check = check_request(gb, template, method, instance)
    baseline_valid = bool(check.get("valid"))
    result["baseline_check"] = {
        "valid": baseline_valid,
        "errors": check.get("errors", []),
        "caveats": check.get("caveats", []),
        "media_type": check.get("media_type"),
    }

    # Existence asymmetry is the breaking fact; baseline-valid means the
    # counter-example really is a request the old server accepted.
    verifiable = baseline_valid and not candidate_exists
    result["verifiable"] = verifiable
    result["exactly_one_side"] = verifiable
    if not verifiable and not baseline_valid:
        result["unverifiable_reason"] = (
            "反例不满足基线请求约束（可在编辑器中修正后重放）；"
            "端点删除的破坏性结论仍然成立")
    elif not verifiable:
        result["unverifiable_reason"] = "候选仍存在该端点，与删除结论不符"
    return result


def _walk_node(node, relpath: str):
    """Navigate a built SchemaNode along a property/array relative path."""
    cur = node
    for token in ptr.parse(relpath):
        if cur is None:
            return None
        if isinstance(cur.properties, dict) and token in cur.properties:
            cur = cur.properties[token]
        elif token == "0" and cur.items is not None:
            cur = cur.items
        elif isinstance(cur.additional, SchemaNode):
            cur = cur.additional
        else:
            return None
    return cur


from .schemagraph import SchemaNode  # noqa: E402  (used by isinstance above)
