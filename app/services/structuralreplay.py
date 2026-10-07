"""Replay **structural** findings.

Schema-level findings (an enum narrowed on a property, a ``maximum`` lowered
…) carry a JSON document plus root media-type schema pointers and are
replayed by :mod:`app.services.replay` with plain schema validation.
:class:`ENDPOINT_REMOVED` findings are replayed as complete minimal HTTP
requests by :mod:`app.services.endpointrequest`.

The findings in between — *structural* changes that do not produce a media
schema at all on one side — used to fall into the schema-level replay and
fail with “重放信息缺少根 schema 指针”:

* ``PARAMETER_ADDED_REQUIRED`` / ``PARAMETER_MADE_REQUIRED`` — a parameter
  that the baseline (optionally) accepts becomes required, or a required
  parameter appears out of nowhere;
* ``PARAMETER_REMOVED`` (parameter or requestBody) — the candidate no
  longer declares the parameter / request body the old client keeps
  sending;
* ``STATUS_REMOVED`` — a documented response status code disappears;
* ``MEDIA_REMOVED`` — a request/response media type is no longer
  accepted/returned.

For each of these the counter-example is a small *structured descriptor*
(an editable JSON object, shown in the same drawer editor) and replay
re-derives every claim from the stored packages:

* whether the baseline **declares/accepts** the parameter, body, status or
  media type, and whether the carried value/payload satisfies its schema;
* whether the candidate **lacks** it or **tightens** it (optional →
  required).

The verdict never trusts the stored report; when the baseline part cannot
be proven the finding stays breaking but the witness is reported as *not
verifiable*.
"""
from __future__ import annotations

from typing import Any, Optional

from . import pointer as ptr
from .endpointrequest import (find_operation, gather_parameters,
                              is_required, select_body_media)
from .instance import validate
from .schemagraph import SchemaGraph

REQUEST = "request"
RESPONSE = "response"


# ============================================================== raw lookup
def _operation(graph: SchemaGraph, path: str, method: str):
    """Return ``(operation, path_item, path_level_parameters)`` or raise."""
    m = (method or "").lower()
    found = find_operation(graph, path, m)
    if found is None:
        return None, None, []
    return found


def _op_pointer(path: str, method: str) -> str:
    return f"/paths{ptr.build([path, (method or '').lower()])}"


def _request_body_pointer(path: str, method: str) -> str:
    return ptr.join(_op_pointer(path, method), "requestBody")


def _response_pointer(path: str, method: str, code: str) -> str:
    return f"/paths{ptr.build([path, (method or '').lower(), 'responses', code])}"


def _deref(graph: SchemaGraph, container: Any) -> Optional[dict]:
    if not isinstance(container, dict):
        return None
    if "$ref" in container:
        res = graph.resolver.resolve(container["$ref"], graph.entry)
        if not res.ok or not isinstance(res.target, dict):
            return None
        return res.target
    return container


def _deref_site(graph: SchemaGraph, container: Any,
                inline_pointer: str) -> Optional[tuple[str, str, dict]]:
    """Return ``(file, pointer, target_dict)`` for an inline or $ref container."""
    if not isinstance(container, dict):
        return None
    if "$ref" in container:
        res = graph.resolver.resolve(container["$ref"], graph.entry)
        if not res.ok or not isinstance(res.target, dict):
            return None
        return res.file, res.pointer, res.target
    return graph.entry, inline_pointer, container


def _find_param(graph: SchemaGraph, path: str, method: str,
                name: Optional[str] = None, location: Optional[str] = None):
    """Return the merged parameter dict for ``name`` (and optionally ``in``).

    Mirrors :func:`app.services.comparator.Comparator._gather_params`:
    path-level parameters first, operation-level parameters override them.
    Returns ``(param, SchemaNode|None)`` or ``(None, None)``.
    """
    op, _item, path_params = _operation(graph, path, method)
    if op is None:
        return None, None
    params = gather_parameters(graph, op, path_params, path,
                               (method or "").lower())
    if name is not None and name in params:
        param, node = params[name]
        if location and param.get("in") != location:
            return None, None
        return param, node
    if location:
        for pname, (param, node) in params.items():
            if param.get("in") == location and (name is None or pname == name):
                return param, node
    return None, None


def _request_body_site(graph: SchemaGraph, path: str, method: str):
    """``(file, pointer, body_dict)`` for the (possibly $ref'd) requestBody."""
    op, _, _ = _operation(graph, path, method)
    if op is None:
        return None
    return _deref_site(graph, op.get("requestBody"),
                       _request_body_pointer(path, method))


def _responses(graph: SchemaGraph, path: str, method: str) -> dict:
    op, _, _ = _operation(graph, path, method)
    if op is None:
        return {}
    raw = op.get("responses")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for code, resp in raw.items():
        d = _deref(graph, resp)
        if isinstance(d, dict):
            out[str(code)] = d
    return out


def _response_site(graph: SchemaGraph, path: str, method: str,
                   code: str):
    """``(file, pointer, response_dict)`` for a (possibly $ref'd) response."""
    op, _, _ = _operation(graph, path, method)
    if op is None:
        return None
    raw = op.get("responses")
    if not isinstance(raw, dict) or code not in raw:
        return None
    return _deref_site(
        graph, raw[code], _response_pointer(path, method, code))


def _media_schema_node(graph: SchemaGraph, site: Optional[tuple[str, str]],
                       media_type: str):
    """Build the SchemaNode of a media type's schema, or ``None``."""
    if site is None:
        return None
    file, container_pointer, _container = site
    try:
        container = ptr.resolve(graph.files[file], container_pointer)
    except (KeyError, IndexError, ValueError):
        return None
    content = container.get("content") if isinstance(container, dict) else None
    media = content.get(media_type) if isinstance(content, dict) else None
    if not isinstance(media, dict) or not isinstance(media.get("schema"),
                                                     dict):
        return None
    schema_pointer = ptr.join(container_pointer, "content", media_type,
                              "schema")
    return graph.build_at(file, schema_pointer)


# =================================================================== checks
def _check(ok: bool, detail: str, **extra) -> dict:
    d = {"ok": bool(ok), "detail": detail}
    d.update(extra)
    return d


def _errors(node, value, label: str) -> list[str]:
    if node is None:
        return [f"{label}的 schema 无法解析，不能核对其取值"]
    if node.invalid or node.is_cycle:
        return [f"{label}的 schema 不可判定（{node.invalid or '循环引用'}）"]
    return validate(node, value)


# --------------------------------------------------------------- parameters
def check_parameter(rule: str, gb: SchemaGraph, gc: SchemaGraph,
                    witness: dict, instance: Any) -> dict:
    path = witness.get("path")
    method = (witness.get("method") or "").lower()
    name = witness.get("parameter")

    b_param, b_node = _find_param(gb, path, method, name,
                                  witness.get("param_in"))
    c_param, c_node = _find_param(gc, path, method, name,
                                  witness.get("param_in"))

    value = instance.get("value") if isinstance(instance, dict) else None
    b_errors = _errors(b_node, value,
                       f"基线参数 {name!r}") if b_param is not None else []
    c_errors = _errors(c_node, value,
                       f"候选参数 {name!r}") if c_param is not None else []
    b_valid = b_param is not None and not b_errors
    c_valid = c_param is not None and not c_errors
    b_required = b_param is not None and is_required(b_param)
    c_required = c_param is not None and is_required(c_param)

    if rule in ("PARAMETER_ADDED_REQUIRED", "PARAMETER_MADE_REQUIRED"):
        # the old client omits the parameter: baseline must accept its
        # absence (undeclared or declared optional) while the candidate
        # rejects it (declared required).
        if rule == "PARAMETER_ADDED_REQUIRED":
            b_ok = b_param is None
            b_detail = (f"基线未声明参数 {name!r}，旧客户端省略它即可成功"
                        if b_param is None else
                        f"基线已声明参数 {name!r}，与新增结论不符")
        else:
            b_ok = b_param is not None and not b_required
            b_detail = (
                f"基线声明了参数 {name!r} 且为可选，省略它的旧请求可被接受"
                if b_param is not None and not b_required else
                f"基线未声明可选参数 {name!r}（required={b_required}）")
        baseline_check = _check(
            b_ok, b_detail, declared=b_param is not None,
            required=b_required)
        candidate_check = _check(
            c_param is not None and c_required,
            (f"候选将参数 {name!r} 收紧为必填，省略它的旧请求将被拒绝"
             if c_param is not None and c_required else
             f"候选未把参数 {name!r} 声明为必填（required={c_required}）"),
            declared=c_param is not None, required=c_required)
        carried = None
        verifiable = baseline_check["ok"] and candidate_check["ok"]
        reason = None if verifiable else "未能同时证明基线不要求该参数、候选必填"
    else:  # PARAMETER_REMOVED
        # the old client keeps sending the parameter: the baseline must
        # declare (and accept) it, the candidate must no longer know it.
        baseline_check = _check(
            b_valid,
            (f"基线声明了参数 {name!r} 且接受携带该参数的请求"
             if b_valid else
             f"基线参数 {name!r} 无法核对或拒绝该取值"),
            declared=b_param is not None, required=b_required,
            errors=b_errors)
        candidate_check = _check(
            c_param is None,
            (f"候选不再声明参数 {name!r}，旧客户端仍会发送"
             if c_param is None else
             f"候选仍声明参数 {name!r}，与删除结论不符"),
            declared=c_param is not None)
        carried = {
            "label": f"携带参数 {name!r} 的旧请求",
            "baseline": {"valid": b_valid, "errors": b_errors},
            "candidate": ({"applicable": False,
                          "detail": "候选已无该参数契约，不做取值校验"}
                          if c_param is None else
                          {"valid": c_valid, "errors": c_errors}),
        }
        verifiable = baseline_check["ok"] and candidate_check["ok"]
        reason = None if verifiable else \
            "未能同时证明基线接受该参数、候选已删除该参数"

    return _structural_result(
        rule, witness, instance, REQUEST,
        [("baseline", baseline_check), ("candidate", candidate_check)],
        verifiable, reason, carried=carried)


# ------------------------------------------------------------- requestBody
def check_body(rule: str, gb: SchemaGraph, gc: SchemaGraph,
               witness: dict, instance: Any) -> dict:
    path = witness.get("path")
    method = (witness.get("method") or "").lower()
    b_site = _request_body_site(gb, path, method)
    c_site = _request_body_site(gc, path, method)
    b_body = b_site[2] if b_site else None
    c_body = c_site[2] if c_site else None

    body_value = instance.get("body") if isinstance(instance, dict) else None
    media_type = instance.get("media_type") if isinstance(instance, dict) \
        else None

    b_mt = media_type
    if b_body is not None:
        b_mt = media_type if (media_type and isinstance(
            b_body.get("content"), dict) and media_type
            in b_body["content"]) else select_body_media(b_body)
    c_mt = media_type
    if c_body is not None:
        c_mt = media_type if (media_type and isinstance(
            c_body.get("content"), dict) and media_type
            in c_body["content"]) else select_body_media(c_body)

    b_node = _media_schema_node(gb, b_site, b_mt) \
        if b_body is not None and b_mt else None
    c_node = _media_schema_node(gc, c_site, c_mt) \
        if c_body is not None and c_mt else None
    b_errors = _errors(b_node, body_value, "基线请求体") if b_node else (
        [] if b_body is None or b_mt else
        ["基线 requestBody 仅声明了无法结构化重放的媒体类型"
         "（如 application/octet-stream），无法核对 body"])
    c_errors = _errors(c_node, body_value, "候选请求体") if c_node else (
        [] if c_body is None or c_mt else
        ["候选 requestBody 仅声明了无法结构化重放的媒体类型"])

    b_required = b_body is not None and bool(b_body.get("required"))
    c_required = c_body is not None and bool(c_body.get("required"))

    if rule == "PARAMETER_REMOVED":
        # requestBody deleted on the candidate.  The baseline must declare
        # the body; the carried payload is schema-checked only when a
        # structured media type exists (octet-stream stays inconclusive).
        b_payload_ok = b_body is not None and (b_mt is None or not b_errors)
        baseline_check = _check(
            b_payload_ok,
            ("基线声明了 requestBody 并接受该请求体"
             if b_mt and not b_errors else
             "基线声明了 requestBody，但仅提供无法结构化重放的媒体类型，"
             "不能核对 body（破坏性结论保留，反例不可验证）"
             if b_body is not None and not b_mt else
             "基线 requestBody 无法核对或拒绝该请求体"),
            declared=b_body is not None, media_type=b_mt,
            verifiable_payload=b_mt is not None and not b_errors,
            errors=b_errors if b_mt else [])
        candidate_check = _check(
            c_body is None,
            "候选不再声明 requestBody，旧客户端仍会发送 body"
            if c_body is None else
            "候选仍声明 requestBody，与删除结论不符",
            declared=c_body is not None)
        verifiable = (baseline_check["ok"]
                      and baseline_check["verifiable_payload"]
                      and candidate_check["ok"])
        reason = None if verifiable else \
            "未能同时证明基线接受该请求体、候选已删除 requestBody"
    elif rule == "PARAMETER_ADDED_REQUIRED":
        # required requestBody appears out of nowhere
        baseline_check = _check(
            b_body is None,
            "基线未声明 requestBody，旧客户端不发送 body 即可成功"
            if b_body is None else
            "基线仍声明了 requestBody，与新增结论不符",
            declared=b_body is not None)
        candidate_check = _check(
            c_body is not None and c_required,
            "候选新增了必填 requestBody，不发送 body 的旧请求将被拒绝"
            if c_body is not None and c_required else
            f"候选 requestBody 必填={c_required}，未形成收窄",
            declared=c_body is not None, required=c_required)
        verifiable = baseline_check["ok"] and candidate_check["ok"]
        reason = None if verifiable else \
            "未能同时证明基线无 requestBody、候选新增必填 requestBody"
    else:  # PARAMETER_MADE_REQUIRED: optional -> required
        baseline_check = _check(
            b_body is not None and not b_required,
            "基线 requestBody 为可选，不发送 body 的旧请求可被接受"
            if b_body is not None and not b_required else
            f"基线 requestBody 必填={b_required}",
            declared=b_body is not None, required=b_required)
        candidate_check = _check(
            c_body is not None and c_required,
            "候选将 requestBody 收紧为必填，不发送 body 的旧请求将被拒绝"
            if c_body is not None and c_required else
            f"候选 requestBody 必填={c_required}，未形成收窄",
            declared=c_body is not None, required=c_required)
        verifiable = baseline_check["ok"] and candidate_check["ok"]
        reason = None if verifiable else \
            "未能同时证明基线 requestBody 可选、候选必填"

    carried = {
        "label": "携带请求体的旧请求" if rule == "PARAMETER_REMOVED"
        else "请求体最小载荷（可选携带）",
        "baseline": {"valid": not b_errors, "errors": b_errors,
                     "media_type": b_mt},
        "candidate": ({"applicable": False,
                       "detail": "候选已无 requestBody 契约，不做载荷校验"}
                      if c_body is None and rule == "PARAMETER_REMOVED"
                      else {"valid": not c_errors, "errors": c_errors,
                            "media_type": c_mt}),
    }
    return _structural_result(
        rule, witness, instance, REQUEST,
        [("baseline", baseline_check), ("candidate", candidate_check)],
        verifiable, reason, carried=carried)


# -------------------------------------------------------------- status code
def check_status(gb: SchemaGraph, gc: SchemaGraph, witness: dict,
                 instance: Any) -> dict:
    path = witness.get("path")
    method = (witness.get("method") or "").lower()
    code = str(witness.get("status_code")
               or (instance.get("status_code") if isinstance(instance, dict)
                   else ""))
    b_responses = _responses(gb, path, method)
    c_responses = _responses(gc, path, method)
    b_resp = b_responses.get(code)
    c_resp = c_responses.get(code)
    b_site = _response_site(gb, path, method, code)

    media_type = instance.get("media_type") if isinstance(instance, dict) \
        else None
    body_value = instance.get("body") if isinstance(instance, dict) else None

    if b_resp is not None and not media_type:
        content = b_resp.get("content")
        if isinstance(content, dict) and content:
            media_type = select_body_media(b_resp) or next(iter(content))
    b_node = _media_schema_node(gb, b_site, media_type) \
        if b_resp is not None and media_type else None
    b_errors = _errors(b_node, body_value,
                       f"基线 {code} 响应体") if b_node else []
    b_media = None
    if b_resp is not None and media_type:
        b_content = b_resp.get("content")
        b_media = b_content.get(media_type) if isinstance(b_content, dict) \
            else None
    b_has_schema = b_node is not None or (
        isinstance(b_media, dict) and isinstance(b_media.get("schema"), dict))

    if b_resp is None:
        b_detail = f"基线未声明 {code} 响应"
    elif media_type and b_has_schema and b_errors:
        b_detail = (f"基线声明了 {code} 响应，但反例响应体违反其 "
                    f"{media_type} schema")
    elif media_type and b_has_schema:
        b_detail = (f"基线声明了 {method.upper()} {path} 的 {code} 响应，"
                    "且反例响应体满足其 schema")
    elif media_type:
        b_detail = (f"基线声明了 {code} 响应及媒体类型 {media_type}"
                    "（无 schema，载荷不做结构校验）")
    else:
        b_detail = f"基线声明了 {method.upper()} {path} 的 {code} 响应"
    b_ok = b_resp is not None and not (b_has_schema and bool(b_errors))
    baseline_check = _check(
        b_ok, b_detail, declared=b_resp is not None,
        media_type=media_type,
        errors=b_errors if b_has_schema else [])
    candidate_check = _check(
        c_resp is None,
        f"候选不再声明 {code} 响应，为该状态码编写分支的客户端无法匹配"
        if c_resp is None else
        f"候选仍声明 {code} 响应，与删除结论不符",
        declared=c_resp is not None)
    carried = None
    if media_type is not None:
        carried = {
            "label": f"基线 {code} 响应可返回的最小载荷",
            "baseline": {"valid": b_has_schema and not b_errors,
                         "errors": b_errors, "applicable": b_has_schema,
                         "media_type": media_type},
            "candidate": {"applicable": False,
                          "detail": f"候选已无 {code} 响应契约，不做载荷校验"},
        }
    verifiable = baseline_check["ok"] and candidate_check["ok"]
    reason = None if verifiable else \
        f"未能同时证明基线声明 {code} 响应、候选已删除该状态码"
    return _structural_result(
        "STATUS_REMOVED", witness, instance, RESPONSE,
        [("baseline", baseline_check), ("candidate", candidate_check)],
        verifiable, reason, carried=carried)


# --------------------------------------------------------------- media type
def check_media(gb: SchemaGraph, gc: SchemaGraph, witness: dict,
                instance: Any) -> dict:
    path = witness.get("path")
    method = (witness.get("method") or "").lower()
    media_type = witness.get("media_type") \
        or (instance.get("media_type") if isinstance(instance, dict) else None)
    location = witness.get("location")
    status_code = witness.get("status_code")
    direction = witness.get("direction") or (
        RESPONSE if location == "response" else REQUEST)

    def container(graph: SchemaGraph):
        if location == "response":
            return _response_site(graph, path, method, str(status_code))
        return _request_body_site(graph, path, method)

    b_site = container(gb)
    c_site = container(gc)
    b_container = b_site[2] if b_site else None
    c_container = c_site[2] if c_site else None
    b_content = b_container.get("content") if isinstance(b_container, dict) \
        else None
    c_content = c_container.get("content") if isinstance(c_container, dict) \
        else None
    b_has = isinstance(b_content, dict) and media_type in b_content
    c_has = isinstance(c_content, dict) and media_type in c_content

    body_value = instance.get("body") if isinstance(instance, dict) else None
    b_node = _media_schema_node(gb, b_site, media_type) if b_has else None
    b_errors = _errors(b_node, body_value,
                       f"基线媒体类型 {media_type} 的载荷") if b_node else []

    where = ("响应" if direction == RESPONSE else "请求")
    verb = "返回" if direction == RESPONSE else "接受"
    b_media = b_content.get(media_type) if b_has else None
    b_has_schema = b_node is not None or (
        isinstance(b_media, dict) and isinstance(b_media.get("schema"), dict))
    if b_has and not b_has_schema:
        # declared but no schema to validate payload against: the breaking
        # fact (media removed on candidate) still holds, the payload check
        # is simply not applicable
        b_detail = (f"基线在{where}中声明了媒体类型 {media_type}"
                    "（未提供 schema，载荷不做结构校验）")
        b_ok = True
        payload_valid = True
    elif b_node is None:
        b_detail = (f"基线在{where}中声明了媒体类型 {media_type}，"
                    "但其 schema 无法解析，不能核对载荷")
        b_ok = False
        payload_valid = False
    elif b_errors:
        b_detail = f"基线声明了媒体类型 {media_type}，但该载荷违反其 schema"
        b_ok = False
        payload_valid = False
    else:
        b_detail = (f"基线在{where}中声明了媒体类型 {media_type} 并{verb}该载荷"
                    if b_has else f"基线未声明媒体类型 {media_type}")
        b_ok = b_has
        payload_valid = b_has
    baseline_check = _check(
        b_ok, b_detail, declared=b_has, media_type=media_type,
        errors=b_errors if b_node is not None else [])
    candidate_check = _check(
        not c_has,
        f"候选不再{verb}媒体类型 {media_type}，旧客户端的内容协商失败"
        if not c_has else
        f"候选仍{verb}媒体类型 {media_type}，与删除结论不符",
        declared=c_has)
    carried = {
        "label": f"使用 {media_type} 的旧{where}载荷",
        "baseline": {"valid": payload_valid, "errors": b_errors,
                     "applicable": b_has_schema,
                     "media_type": media_type},
        "candidate": {"applicable": False,
                      "detail": f"候选已无媒体类型 {media_type} 的契约，"
                                "不做载荷校验"},
    }
    verifiable = baseline_check["ok"] and candidate_check["ok"]
    reason = None if verifiable else \
        f"未能同时证明基线{verb} {media_type}、候选已删除该媒体类型"
    return _structural_result(
        "MEDIA_REMOVED", witness, instance, direction,
        [("baseline", baseline_check), ("candidate", candidate_check)],
        verifiable, reason, carried=carried)


# ------------------------------------------------------------- result shape
def _structural_result(rule: str, witness: dict, instance: Any,
                       direction: str, checks: list[tuple[str, dict]],
                       verifiable: bool, reason: Optional[str],
                       carried: Optional[dict] = None) -> dict:
    check_map = {name: body for name, body in checks}
    result = {
        "ok": True,
        "kind": "structural",
        "rule": rule,
        "direction": direction,
        "method": witness.get("method"),
        "path": witness.get("path"),
        "location": witness.get("location"),
        "media_type": witness.get("media_type"),
        "status_code": witness.get("status_code"),
        "parameter": witness.get("parameter"),
        "instance": instance,
        "structural_check": check_map,
        "carried_value": carried,
        "verifiable": verifiable,
        "exactly_one_side": verifiable,
        "unverifiable_reason": None if verifiable else reason,
        "note": witness.get("note"),
    }
    return result


# ================================================================ dispatch
STRUCTURAL_KINDS = {"parameter", "body", "response", "media"}


def replay_structural(gb: SchemaGraph, gc: SchemaGraph, rule: str,
                      witness: dict, instance_override: Any = None) -> dict:
    """Replay one structural finding. Always returns a structured verdict."""
    kind = witness.get("kind")
    instance = instance_override if instance_override is not None \
        else witness.get("instance")

    if kind == "parameter":
        instance = _ensure_descriptor(instance, _parameter_template(witness))
        if rule not in ("PARAMETER_ADDED_REQUIRED", "PARAMETER_MADE_REQUIRED",
                        "PARAMETER_REMOVED"):
            rule = "PARAMETER_REMOVED"
        return check_parameter(rule, gb, gc, witness, instance)
    if kind == "body":
        instance = _ensure_descriptor(instance, _body_template(witness))
        if rule not in ("PARAMETER_ADDED_REQUIRED", "PARAMETER_MADE_REQUIRED",
                        "PARAMETER_REMOVED"):
            rule = "PARAMETER_REMOVED"
        return check_body(rule, gb, gc, witness, instance)
    if kind == "response":
        instance = _ensure_descriptor(instance, _response_template(witness))
        return check_status(gb, gc, witness, instance)
    if kind == "media":
        instance = _ensure_descriptor(instance, _media_template(witness))
        return check_media(gb, gc, witness, instance)
    raise ValueError(f"未知的结构化反例类型: {kind!r}")


def _ensure_descriptor(instance: Any, template: dict) -> dict:
    """Coerce legacy scalar witnesses / user edits into a descriptor dict."""
    if isinstance(instance, dict):
        out = dict(template)
        out.update(instance)
        return out
    # legacy parameter witnesses carried the bare scalar value
    if "value" in template:
        out = dict(template)
        out["value"] = instance
        return out
    if "body" in template:
        out = dict(template)
        out["body"] = instance
        return out
    return dict(template)


def _parameter_template(witness: dict) -> dict:
    return {"method": witness.get("method"), "path": witness.get("path"),
            "parameter": witness.get("parameter"),
            "in": witness.get("param_in"), "value": None}


def _body_template(witness: dict) -> dict:
    return {"method": witness.get("method"), "path": witness.get("path"),
            "media_type": witness.get("media_type"), "body": None}


def _response_template(witness: dict) -> dict:
    return {"method": witness.get("method"), "path": witness.get("path"),
            "status_code": witness.get("status_code"),
            "media_type": witness.get("media_type"), "body": None}


def _media_template(witness: dict) -> dict:
    return {"method": witness.get("method"), "path": witness.get("path"),
            "location": witness.get("location"),
            "status_code": witness.get("status_code"),
            "media_type": witness.get("media_type"), "body": None}
