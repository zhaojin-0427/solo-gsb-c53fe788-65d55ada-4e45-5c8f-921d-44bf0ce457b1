"""Build and check endpoint-level JSON request counter-examples.

Schema-level findings carry a JSON document validated against one media-type
schema on each side.  An :class:`ENDPOINT_REMOVED` finding has no such schema on
the candidate side at all; its counter-example is instead a complete, minimal
*HTTP request* against the baseline operation:

* ``method`` / ``path`` — the request line, with path-template placeholders
  substituted by concrete values;
* ``query`` / ``header`` / ``cookie`` — required parameters with minimal values
  (optional parameters are omitted, like an old client would);
* ``body`` — a minimal request body when the baseline operation requires one.

The same builder is reused at replay time, so the "baseline accepts" claim is
always re-derived from the stored baseline package rather than trusted from
the report.  When the request cannot be built reliably (unresolvable refs,
unverifiable parameter schemas, ...) the caller keeps the breaking conclusion
but marks the witness as *not verifiable* instead of pretending a replay
succeeded.
"""
from __future__ import annotations

from typing import Any, Optional

from . import pointer as ptr
from .instance import minimal_instance, validate
from .schemagraph import SchemaGraph

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head",
                "options", "trace")

# Media types whose bodies are JSON documents we can synthesise/validate.
JSON_MEDIA_TYPES = ("application/json",)
# Structured form media types: the *shape* is still schema-validatable (the
# wire encoding caveat is recorded, not treated as a hard failure).
STRUCTURED_MEDIA_TYPES = JSON_MEDIA_TYPES + (
    "application/x-www-form-urlencoded", "multipart/form-data")


class BuildFailure(Exception):
    """Raised when a minimal request cannot be constructed reliably."""


# --------------------------------------------------------------- raw lookup
def find_operation(graph: SchemaGraph, path: str, method: str):
    """Return ``(operation, path_item, path_level_parameters)`` or ``None``.

    The path item may exist without the requested operation (e.g. the method
    was removed) — callers distinguish "path missing" from "method missing"
    via the second return value.
    """
    root = graph.files[graph.entry]
    paths = root.get("paths")
    if not isinstance(paths, dict) or path not in paths:
        return None
    item = paths[path]
    if not isinstance(item, dict):
        return None
    op = item.get(method.lower())
    params = item.get("parameters")
    return (op if isinstance(op, dict) else None), item, \
        params if isinstance(params, list) else []


def operation_pointer(path: str, method: str) -> str:
    return f"/paths{ptr.build([path, method.lower()])}"


# ------------------------------------------------------------------ resolve
def _resolve_dict(graph: SchemaGraph, container: Any,
                  inline_pointer: str = "") -> Optional[dict]:
    """Dereference a local $ref container; ``None`` when absent/unresolvable."""
    if not isinstance(container, dict):
        return None
    if "$ref" in container:
        res = graph.resolver.resolve(container["$ref"], graph.entry)
        if not res.ok or not isinstance(res.target, dict):
            return None
        return res.target
    return container


def gather_parameters(graph: SchemaGraph, op: dict, path_params: list,
                      path: str, method: str) -> dict[str, tuple[dict, Any]]:
    """Return ``{name: (param_dict, SchemaNode-or-None)}``.

    Path-level parameters first, operation-level parameters override them;
    ``$ref`` parameters are resolved locally.  A parameter whose schema cannot
    be resolved/build keeps a ``None`` node so the caller can decide whether
    the request is still verifiable.
    """
    out: dict[str, tuple[dict, Any]] = {}

    def put(lst, base_pointer_tmpl):
        if not isinstance(lst, list):
            return
        for i, item in enumerate(lst):
            if not isinstance(item, dict):
                continue
            if "$ref" in item:
                res = graph.resolver.resolve(item["$ref"], graph.entry)
                if not res.ok or not isinstance(res.target, dict):
                    continue
                p = res.target
                schema_loc = (res.file,
                              ptr.join(res.pointer, "schema"))
            else:
                p = item
                schema_loc = (graph.entry,
                              ptr.join(base_pointer_tmpl(i), "schema"))
            name = p.get("name")
            if not isinstance(name, str):
                continue
            node = None
            raw_schema = p.get("schema")
            if isinstance(raw_schema, dict):
                node = graph.build_at(schema_loc[0], schema_loc[1])
            else:
                content = p.get("content")
                if isinstance(content, dict) and content:
                    mt = next(iter(content))
                    media = content[mt]
                    if isinstance(media, dict) and \
                            isinstance(media.get("schema"), dict):
                        node = graph.build_at(
                            graph.entry,
                            ptr.join(base_pointer_tmpl(i), "content", mt,
                                     "schema"))
            out[name] = (p, node)

    put(path_params,
        lambda i: f"/paths{ptr.build([path, 'parameters', i])}")
    put(op.get("parameters") if isinstance(op.get("parameters"), list)
        else [],
        lambda i: f"/paths{ptr.build([path, method.lower(), 'parameters', i])}")
    return out


def is_required(param: dict) -> bool:
    if param.get("in") == "path":
        return True
    return bool(param.get("required"))


def minimal_param_value(node) -> tuple[Any, bool]:
    """Return ``(value, ok)`` for a parameter schema node."""
    if node is None:
        return None, False
    if node.invalid or node.is_cycle:
        return None, False
    try:
        return minimal_instance(node), True
    except Exception:
        return None, False


# ---------------------------------------------------------------- request body
def select_body_media(body: dict) -> Optional[str]:
    """Pick a content media type for which a body can be synthesised."""
    content = body.get("content")
    if not isinstance(content, dict) or not content:
        return None
    for mt in STRUCTURED_MEDIA_TYPES:
        if mt in content:
            return mt
    # a vendor JSON type (application/vnd.xxx+json) is still JSON
    for mt in content:
        if isinstance(mt, str) and mt.endswith("+json"):
            return mt
    return None


def build_request(graph: SchemaGraph, path: str, method: str) -> dict:
    """Construct a minimal JSON request for a baseline operation.

    Returns the request descriptor plus bookkeeping.  Raises
    :class:`BuildFailure` when a *required* part cannot be produced reliably.
    """
    found = find_operation(graph, path, method)
    if found is None or found[0] is None:
        raise BuildFailure(f"基线中不存在端点 {method.upper()} {path}")
    op, _item, path_params = found

    caveats: list[str] = []
    params = gather_parameters(graph, op, path_params, path, method)

    by_location: dict[str, dict] = {"path": {}, "query": {},
                                    "header": {}, "cookie": {}}
    required_missing: list[str] = []
    for name in sorted(params):
        param, node = params[name]
        loc = param.get("in")
        if loc not in by_location:
            continue
        value, ok = minimal_param_value(node)
        if not is_required(param):
            # optional params are omitted — but note schemas we cannot read
            if not ok:
                caveats.append(f"可选参数 {name!r} 的模式不可靠，已省略")
            continue
        if not ok:
            required_missing.append(name)
            continue
        by_location[loc][name] = value

    if required_missing:
        raise BuildFailure(
            "无法为必填参数构造可信值: " + ", ".join(sorted(required_missing)))

    # substitute concrete values into the path template
    concrete_path = substitute_path(path, by_location["path"])
    if concrete_path is None:
        raise BuildFailure(f"路径模板 {path} 的占位符与 path 参数不一致")

    request: dict[str, Any] = {
        "method": method.upper(),
        "path": concrete_path,
        "query": by_location["query"],
        "header": by_location["header"],
        "cookie": by_location["cookie"],
    }

    body_info = _resolve_dict(graph, op.get("requestBody"),
                              operation_pointer(path, method) + "/requestBody")
    body_descriptor = None
    if isinstance(body_info, dict) and body_info.get("required"):
        media_type = select_body_media(body_info)
        content = body_info.get("content")
        if media_type is None:
            raise BuildFailure("必填 requestBody 没有可构造的媒体类型")
        media = content.get(media_type) if isinstance(content, dict) else None
        schema = media.get("schema") if isinstance(media, dict) else None
        if not isinstance(schema, dict):
            raise BuildFailure("必填 requestBody 缺少 schema")
        node = graph.build_value(
            schema, graph.entry,
            f"/paths{ptr.build([path, method.lower(), 'requestBody', 'content', media_type, 'schema'])}")
        if node.invalid or node.is_cycle:
            raise BuildFailure(
                f"必填 requestBody 的模式不可判定（{node.invalid or '循环引用'}）")
        try:
            body_value = minimal_instance(node)
        except Exception as exc:
            raise BuildFailure(f"无法构造最小请求体: {exc}") from exc
        errors = validate(node, body_value)
        if errors:
            raise BuildFailure(
                "最小请求体未通过基线 schema 校验: " + "; ".join(errors[:3]))
        request["body"] = body_value
        body_descriptor = {"media_type": media_type, "required": True}
        if media_type not in JSON_MEDIA_TYPES and not (
                media_type.endswith("+json")):
            caveats.append(
                f"请求体媒体类型 {media_type} 以结构化形式校验，"
                "实际线缆编码（表单字段）未重放")
    elif isinstance(body_info, dict):
        caveats.append("requestBody 为可选，最小请求按旧客户端惯例省略")

    return {
        "request": request,
        "body": body_descriptor,
        "caveats": caveats,
    }


def substitute_path(template: str, path_values: dict) -> Optional[str]:
    """Replace ``{name}`` placeholders with concrete string values.

    Every path parameter must have a matching placeholder and vice versa;
    otherwise the request cannot be addressed reliably.
    """
    import re
    placeholders = re.findall(r"\{([^}]+)\}", template)
    if set(placeholders) != set(path_values):
        return None
    out = template
    for name in placeholders:
        out = out.replace("{" + name + "}", _http_text(path_values[name]))
    return out


def _http_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# ------------------------------------------------------------- self-check
def check_request(graph: SchemaGraph, path: str, method: str,
                  request: dict) -> dict:
    """Re-validate an edited/regenerated request against the baseline contract.

    Returns a structured verdict; never raises on malformed user input — the
    problems become ``errors`` entries so the UI can show them.
    """
    found = find_operation(graph, path, method)
    if found is None:
        return {"errors": [f"基线文档中不存在路径 {path}"]}
    op, _item, path_params = found
    if op is None:
        return {"errors": [
            f"基线路径 {path} 存在，但不支持 {method.upper()} 方法"]}

    errors: list[str] = []
    caveats: list[str] = []
    params = gather_parameters(graph, op, path_params, path, method)

    concrete_path = request.get("path")
    if not isinstance(concrete_path, str) or not concrete_path:
        errors.append("反例缺少具体请求路径 path")
    else:
        names = [n for n, (p, _) in params.items() if p.get("in") == "path"]
        for name in names:
            value = (request.get("path_values") or {}).get(name)
            if value is None:
                # no explicit values: parse from the concrete path template
                value = _extract_path_value(path, concrete_path, name)
            if value is None:
                errors.append(f"缺少必填路径参数 {name!r} 的值")
                continue
            node = params[name][1]
            if node is not None and not node.invalid and not node.is_cycle:
                errs = validate(node, value)
                if errs:
                    errors.append(
                        f"路径参数 {name!r}={value!r} 不满足基线约束: "
                        + "; ".join(errs[:2]))

    for location in ("query", "header", "cookie"):
        provided = request.get(location)
        if provided is None:
            provided = {}
        if not isinstance(provided, dict):
            errors.append(f"{location} 必须是对象")
            provided = {}
        for name, (param, node) in params.items():
            if param.get("in") != location:
                continue
            if name not in provided:
                if is_required(param):
                    errors.append(
                        f"缺少必填{_loc_zh(location)}参数 {name!r}")
                continue
            if node is not None and not node.invalid and not node.is_cycle:
                errs = validate(node, provided[name])
                if errs:
                    errors.append(
                        f"{location} 参数 {name!r} 不满足基线约束: "
                        + "; ".join(errs[:2]))

    body_info = _resolve_dict(graph, op.get("requestBody"),
                              operation_pointer(path, method) + "/requestBody")
    media_type_used = None
    body_required = isinstance(body_info, dict) and body_info.get("required")
    if body_required and "body" not in request:
        mt = select_body_media(body_info)
        errors.append(f"缺少必填请求体（媒体类型 {mt or '?'}）")
    elif "body" in request:
        if not isinstance(body_info, dict):
            errors.append("基线 operation 未声明 requestBody，反例不应携带 body")
        elif not isinstance(request.get("body"), (dict, list)):
            errors.append(
                f"请求体必须是 JSON 对象/数组，当前为 "
                f"{type(request.get('body')).__name__}")
        else:
            mt = select_body_media(body_info)
            content = body_info.get("content")
            media = content.get(mt) if mt and isinstance(content, dict) \
                else None
            schema = media.get("schema") if isinstance(media, dict) else None
            if mt is None or not isinstance(schema, dict):
                errors.append(
                    "基线 requestBody 仅声明了无法结构化重放的媒体类型"
                    "（如 application/octet-stream），无法核对 body")
            else:
                node = graph.build_value(
                    schema, graph.entry,
                    f"/paths{ptr.build([path, method.lower(), 'requestBody', 'content', mt, 'schema'])}")
                if node.invalid or node.is_cycle:
                    errors.append(
                        f"请求体模式不可判定（{node.invalid or '循环引用'}）")
                else:
                    body_errors = validate(node, request["body"])
                    errors.extend(
                        f"请求体{e[1:] if e.startswith('#') else e}"
                        for e in body_errors[:4])
                    media_type_used = mt
                    if mt not in JSON_MEDIA_TYPES and not mt.endswith("+json"):
                        caveats.append(
                            f"媒体类型 {mt} 仅按结构 schema 校验，"
                            "未重放实际表单编码")

    return {
        "errors": errors,
        "caveats": caveats,
        "media_type": media_type_used,
        "valid": not errors,
    }


def _extract_path_value(template: str, concrete: str, name: str):
    """Pull a concrete value for ``{name}`` out of a filled-in path."""
    import re
    names = re.findall(r"\{([^}]+)\}", template)
    if name not in names:
        return None
    parts: list[str] = []
    idx = 0
    last = 0
    for m in re.finditer(r"\{([^}]+)\}", template):
        parts.append(re.escape(template[last:m.start()]))
        parts.append(f"(?P<g{idx}>[^/]+)")
        last = m.end()
        idx += 1
    parts.append(re.escape(template[last:]))
    match = re.compile("^" + "".join(parts) + "$").match(concrete)
    if not match:
        return None
    return _coerce_scalar(match.group(f"g{names.index(name)}"))


def _coerce_scalar(raw: str):
    if raw == "true":
        return True
    if raw == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _loc_zh(location: str) -> str:
    return {"query": "查询", "header": "请求头", "cookie": "Cookie",
            "path": "路径"}.get(location, location)
