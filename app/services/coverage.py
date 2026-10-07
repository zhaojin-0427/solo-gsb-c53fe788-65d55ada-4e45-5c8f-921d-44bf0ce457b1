"""调用覆盖（Call Coverage）引擎。

给定一侧 OpenAPI 文档包（基线或候选）与一份 NDJSON 调用记录，逐行判定每个调用
覆盖了哪个 operation 的哪个**响应格**：

* 路径先匹配精确路径，再匹配**唯一**的路径模板；多个模板同时命中记为**未归属**；
* operation 的覆盖格由「响应状态码 × JSON 媒体类型」组成，状态码依次按
  精确值 → ``1XX``～``5XX`` 范围键 → ``default`` 解析；无 ``content`` 的响应
  单列一个无实体格；非 JSON 媒体类型不生成覆盖格（按行列明排除原因）；
* 只有当**请求参数**、**请求体**、**响应体**三侧的可判定约束全部通过时，调用
  才覆盖该格；校验失败、无法判定、未匹配/未归属均逐行给出原因。
* 已知约束全部通过后，引擎还会**沿调用实际取值遍历解析后的 schema 图**
  （本地 ``$ref``、嵌套属性、数组元素、``oneOf``/``anyOf`` 组合分支）：
  取值的 child 触达**自引用 / 相互引用循环**或无法解析的模式节点时，会沿
  实际值重新展开被引模式继续核对（同一分支的已知失败优先判失败），确实
  无法定论的记为**无法判定**并报告模式位置；能否通过仍依赖 ``pattern``
  等未支持关键字时同样列出关键字与位置并记为**无法判定**（不计覆盖）；
  **未触达的可选属性（含可选循环属性）不会阻断覆盖**，与取值类型无关的
  关键字（如整数值上的 ``pattern``）也不会误报。同层 facet 与组合结果按
  **交集**裁决——本层已知失败或组合整体确定失败才判失败；本层属性 /
  未支持关键字等 facet 待定（如 schema 同节点同时含自引用
  ``properties.child`` 与 ``oneOf``/``anyOf``，调用值沿 child 触达循环）
  时，即使组合分支已确定通过也判无法判定、报告模式位置且不计格。
  组合本身按三态裁决（确定通过 / 确定失败 / 待定）：``oneOf`` 要求恰好
  一个——一个分支确定通过而另一分支仍可能通过时记无法判定（不计格），
  两个确定通过记校验失败；``anyOf`` 要求至少一个——没有待定 facet 时
  一个分支确定通过即可覆盖。分支的未决命中只在**仍可能改变组合结论**
  时才向父节点传递：确定失败分支的命中（如分支因缺少必填属性确定失败、
  同时含未支持的 ``not``）不可能翻案，直接丢弃；``anyOf`` 已有确定通过
  时其余分支的待定命中同样不再传播（嵌套属性的组合确定通过即正常计格）；
  只有组合结论本身为待定（``oneOf`` 一过一待定或零通过有待定、``anyOf``
  仅剩待定分支）时，待定分支的命中才随结论上报。

结果是确定性的：operation / cell 按固定顺序排列，每个格只记录最早一行覆盖调用，
相同输入永远产生相同文档。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import pointer as ptr
from .instance import assess, TRI_FAIL, TRI_PENDING
from .schemagraph import SchemaGraph

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head",
                "options", "trace")

SCHEMA_VERSION = "1.0"

# 行结局
COVERED = "covered"
FAILED = "validation_failed"
UNDETERMINED = "undetermined"
UNMATCHED = "unmatched"
AMBIGUOUS = "ambiguous"
EXCLUDED = "excluded_media"
INVALID = "invalid"

OUTCOME_LABELS = {
    COVERED: "已覆盖",
    FAILED: "校验失败",
    UNDETERMINED: "无法判定",
    UNMATCHED: "未匹配",
    AMBIGUOUS: "未归属",
    EXCLUDED: "非 JSON 媒体排除",
    INVALID: "行格式错误",
}

NO_CONTENT_KEY = "(no content)"


class CoverageError(ValueError):
    """上传内容整体无法用于覆盖分析（例如没有任何可解析的行）。"""


# ============================================================ media types
def _norm_mt(mt: Any) -> str:
    if not isinstance(mt, str):
        return ""
    return mt.split(";", 1)[0].strip().lower()


def _is_json_mt(mt: str) -> bool:
    return mt == "application/json" or mt.endswith("+json")


# ============================================================ data models
@dataclass
class _ResponseCell:
    status: str
    kind: str                      # "content" | "no_content"
    media_type: Optional[str]

    @property
    def key(self) -> str:
        target = self.media_type if self.kind == "content" else NO_CONTENT_KEY
        return f"{self.status}|{target}"


@dataclass
class _Operation:
    path: str
    method: str
    operation_id: Optional[str]
    params: dict[str, tuple[dict, Any]]              # name -> (param, node)
    body_site: Optional[tuple[str, str, dict]]       # (file, pointer, body)
    responses: dict[str, tuple[str, str, dict]]      # code -> (file, ptr, resp)
    cells: list[_ResponseCell] = field(default_factory=list)
    excluded_media: list[dict] = field(default_factory=list)


@dataclass
class _Call:
    line: int
    method: str
    path: str
    query: dict
    headers: dict
    cookies: dict
    request: dict
    response: dict


# ============================================================ public API
def build_coverage(pkg, side: str, raw: bytes,
                   source_file: str = "calls.ndjson") -> dict:
    graph = SchemaGraph(pkg.files, pkg.entry)
    operations = _index_operations(graph)
    rows: list[dict] = []
    valid_calls: list[_Call] = []

    for line_no, raw_line in _iter_lines(raw):
        parsed = _parse_line(line_no, raw_line)
        if isinstance(parsed, dict) and "__error__" in parsed:
            rows.append({"line": line_no, "outcome": INVALID,
                         "method": None, "path": None, "status": None,
                         "reasons": [parsed["__error__"]]})
            continue
        valid_calls.append(parsed)

    if not valid_calls and not rows:
        raise CoverageError("NDJSON 为空或没有任何非空行")
    if not valid_calls:
        raise CoverageError(
            "NDJSON 中没有任何可解析的调用行："
            + "；".join(f"第{r['line']}行 {r['reasons'][0]}"
                        for r in rows[:5]))

    # 每行调用 → 结局；按行顺序处理，格只记录最早一次覆盖
    covered_by: dict[str, int] = {}
    for call in valid_calls:
        row = _evaluate(graph, operations, call)
        rows.append(row)
        if row["outcome"] == COVERED:
            covered_by.setdefault(_cell_id_for(row), call.line)

    ops_doc = []
    cells_total = cells_covered = 0
    ops_applicable = ops_without_cells = 0
    for op in operations:
        cells_doc = []
        gaps_doc = []
        cid_prefix = f"{op.method.upper()} {op.path}|"
        for cell in op.cells:
            cells_total += 1
            line = covered_by.get(cid_prefix + cell.key)
            item = {
                "key": cell.key,
                "status": cell.status,
                "kind": cell.kind,
                "media_type": cell.media_type,
                "covered": line is not None,
                "covered_by_line": line,
            }
            (cells_doc if item["covered"] else gaps_doc).append(item)
            if item["covered"]:
                cells_covered += 1
        applicable = bool(op.cells)
        if applicable:
            ops_applicable += 1
        else:
            ops_without_cells += 1
        ops_doc.append({
            "path": op.path,
            "method": op.method.upper(),
            "operation_id": op.operation_id,
            "applicable": applicable,
            "note": None if applicable else "无适用格（该操作没有可覆盖的格）",
            "covered": len(cells_doc),
            "total": len(op.cells),
            "cells": cells_doc,
            "gaps": gaps_doc,
            "excluded_media": op.excluded_media,
        })

    def count(outcome: str) -> int:
        return sum(1 for r in rows if r["outcome"] == outcome)

    calls_total = len(rows)
    summary = {
        "operations_total": len(operations),
        "operations_applicable": ops_applicable,
        "operations_without_cells": ops_without_cells,
        "cells_total": cells_total,
        "cells_covered": cells_covered,
        "cells_gap": cells_total - cells_covered,
        "calls_total": calls_total,
        "calls_covered": count(COVERED),
        "calls_validation_failed": count(FAILED),
        "calls_undetermined": count(UNDETERMINED),
        "calls_unmatched": count(UNMATCHED),
        "calls_ambiguous": count(AMBIGUOUS),
        "calls_excluded_media": count(EXCLUDED),
        "calls_invalid": count(INVALID),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis_side": side,
        "generated_by": "OpenAPI Contract Evolution Workbench",
        "source_file": source_file,
        "summary": summary,
        "operations": ops_doc,
        "rows": rows,
    }


def _cell_id_for(row: dict) -> str:
    return f"{row['operation']}|{row['cell']}"


# ============================================================ NDJSON parsing
def _iter_lines(raw: bytes):
    text = raw.decode("utf-8-sig")
    for i, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            yield i, line


def _parse_line(line_no: int, raw_line: str) -> Any:
    try:
        obj = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        return {"__error__": f"不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）"}
    if not isinstance(obj, dict):
        return {"__error__": "调用行必须是 JSON 对象"}

    def err(msg):
        return {"__error__": msg}

    method = obj.get("method")
    if not isinstance(method, str) or not method.strip():
        return err("缺少 method 或不是字符串")
    method = method.strip().lower()
    if method not in HTTP_METHODS:
        return err(f"不支持的 HTTP 方法 {method!r}")
    path = obj.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        return err("缺少 path 或不是以 / 开头的字符串")

    maps = {}
    for key in ("query", "headers", "cookies"):
        val = obj.get(key)
        if val is None:
            val = {}
        if not isinstance(val, dict):
            return err(f"{key} 必须是对象")
        maps[key] = val

    request = obj.get("request")
    if request is None:
        request = {}
    if not isinstance(request, dict):
        return err("request 必须是对象")
    response = obj.get("response")
    if response is None:
        return err("缺少 response 对象")
    if not isinstance(response, dict):
        return err("response 必须是对象")
    status = response.get("status")
    if isinstance(status, bool) or not isinstance(status, (int, str)):
        return err("response.status 缺失或不是整数/数字字符串")
    status_text = str(status).strip()
    if not re.fullmatch(r"[1-5]\d{2}", status_text):
        return err(f"response.status={status!r} 不是合法 HTTP 状态码")

    return _Call(line=line_no, method=method, path=path,
                 query=maps["query"], headers=maps["headers"],
                 cookies=maps["cookies"], request=request,
                 response={**response, "status": status_text})


# ============================================================ operation index
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
    if not isinstance(container, dict):
        return None
    if "$ref" in container:
        res = graph.resolver.resolve(container["$ref"], graph.entry)
        if not res.ok or not isinstance(res.target, dict):
            return None
        return res.file, res.pointer, res.target
    return graph.entry, inline_pointer, dict(container)


def _gather_params(graph: SchemaGraph, op: dict, path_params: list,
                   path: str, method: str) -> dict[str, tuple[dict, Any]]:
    """镜像 endpointrequest.gather_parameters：name -> (param, SchemaNode)。"""
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
                schema_loc = (res.file, ptr.join(res.pointer, "schema"))
            else:
                p = item
                schema_loc = (graph.entry,
                              ptr.join(base_pointer_tmpl(i), "schema"))
            name = p.get("name")
            if not isinstance(name, str):
                continue
            node = None
            if isinstance(p.get("schema"), dict):
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
        lambda i: f"/paths{ptr.build([path, method, 'parameters', i])}")
    return out


def _status_sort_key(code: str):
    if code.isdigit():
        return (0, int(code))
    order = {"1XX": 1, "2XX": 2, "3XX": 3, "4XX": 4, "5XX": 5}
    rank = order.get(code.upper())
    if rank is not None:
        return (1, rank)
    return (2, 0, code)


def _index_operations(graph: SchemaGraph) -> list[_Operation]:
    root = graph.files[graph.entry]
    paths = root.get("paths")
    if not isinstance(paths, dict):
        return []
    operations: list[_Operation] = []
    for path in sorted(paths):
        item = paths[path]
        if not isinstance(item, dict):
            continue
        path_params = item.get("parameters")
        path_params = path_params if isinstance(path_params, list) else []
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params = _gather_params(graph, op, path_params, path, method)

            bp = f"/paths{ptr.build([path, method, 'requestBody'])}"
            body_site = _deref_site(graph, op.get("requestBody"), bp)

            responses: dict[str, tuple[str, str, dict]] = {}
            raw_responses = op.get("responses")
            if isinstance(raw_responses, dict):
                for code in raw_responses:
                    site = _deref_site(
                        graph, raw_responses[code],
                        f"/paths{ptr.build([path, method, 'responses', str(code)])}")
                    if site is not None:
                        responses[str(code)] = site

            cells: list[_ResponseCell] = []
            excluded: list[dict] = []
            for code in sorted(responses, key=_status_sort_key):
                resp_obj = responses[code][2]
                content = resp_obj.get("content")
                content = content if isinstance(content, dict) else {}
                if not content:
                    cells.append(_ResponseCell(code, "no_content", None))
                    continue
                json_mts = sorted(mt for mt in {_norm_mt(k) for k in content}
                                  if _is_json_mt(mt))
                for mt in json_mts:
                    cells.append(_ResponseCell(code, "content", mt))
                for orig_mt in sorted(content):
                    norm = _norm_mt(orig_mt)
                    if norm and not _is_json_mt(norm):
                        excluded.append({"status": code,
                                         "media_type": norm})
            operations.append(_Operation(
                path=path, method=method,
                operation_id=op.get("operationId")
                if isinstance(op.get("operationId"), str) else None,
                params=params, body_site=body_site, responses=responses,
                cells=cells, excluded_media=excluded))
    return operations


# ============================================================ path matching
def _template_match(template: str, concrete: str) -> Optional[dict[str, str]]:
    """结构匹配：字面段全等、变量段非空。返回 path 变量原始字符串映射。"""
    t_parts = template.split("/")
    c_parts = concrete.split("/")
    if len(t_parts) != len(c_parts):
        return None
    values: dict[str, str] = {}
    for t, c in zip(t_parts, c_parts):
        m = re.fullmatch(r"\{([^}]+)\}", t)
        if m:
            if not c:
                return None
            values[m.group(1)] = c
        elif t != c:
            return None
    return values


def _match_operation(operations: list[_Operation], call: _Call):
    """返回 (operation, matched_template, ambiguity_reason)。"""
    # 1) 精确路径
    exact = [op for op in operations
             if op.path == call.path and op.method == call.method]
    if exact:
        return exact[0], call.path, None
    # 精确路径存在但不支持该方法 → 明确未匹配（不退化为模板匹配）
    exact_path_ops = [op for op in operations if op.path == call.path]
    if exact_path_ops:
        return None, None, (
            f"精确路径 {call.path} 存在，但未声明 {call.method.upper()} 方法")
    # 2) 唯一模板匹配（仅在声明了该方法的 operation 中判定）
    candidates = []
    for op in operations:
        if op.method != call.method:
            continue
        values = _template_match(op.path, call.path)
        if values is not None:
            candidates.append((op, values))
    if len(candidates) == 1:
        return candidates[0][0], candidates[0][0].path, None
    if len(candidates) > 1:
        templates = sorted(op.path for op, _ in candidates)
        return None, None, (
            f"具体路径 {call.path} 同时命中多个模板 "
            f"{templates}，无法唯一归属")
    return None, None, (
        f"没有路径模板匹配 {call.method.upper()} {call.path}")


# ============================================================ validation
def _format_hits(hits, entry: str) -> str:
    """确定性列出未支持关键字及其位置（去重、保持首次出现顺序）。"""
    parts: list[str] = []
    seen: set[tuple[str, str]] = set()
    for kw, loc in hits:
        where = loc.hint(entry)
        if (kw, where) in seen:
            continue
        seen.add((kw, where))
        parts.append(f"{kw}（{where}）")
    return "、".join(parts)


def _reenter(graph: SchemaGraph):
    """Rebuild the full schema node at a cycle-marker site so a value that
    reaches a self-/mutual reference can be checked inside the recursive
    part (same-branch known failures must still fail)."""
    def _build(file: str, pointer: str):
        try:
            return graph.build_at(file, pointer)
        except (KeyError, IndexError, ValueError):
            return None
    return _build


def _coerce_scalar(value: Any, node) -> tuple[Any, bool]:
    """把 HTTP 文本值尝试 coerce 为参数 schema 的标量类型。"""
    if not isinstance(value, str) or node is None or node.type is None:
        return value, False
    types = set(node.type)
    low = value.strip().lower()
    if "boolean" in types and low in ("true", "false"):
        return low == "true", True
    if "integer" in types:
        try:
            return int(value.strip()), True
        except ValueError:
            pass
    if "number" in types:
        try:
            return float(value.strip()), True
        except ValueError:
            pass
    return value, False


def _assess_value(graph: SchemaGraph, node, value: Any):
    """Three-state verdict of one parameter/body value along its schema.

    Returns ``(state, errors, hits)`` where ``hits`` are ``(keyword, loc)``
    pairs for pending unsupported keywords / cycles / invalid nodes.
    """
    result = assess(node, value, reenter=_reenter(graph))
    return result.state, result.errors, result.hits


def _check_value(graph: SchemaGraph, label: str, node, value: Any,
                 failures: list[str], undetermined: list[str],
                 entry: str) -> None:
    if node is None:
        return  # 声明了参数但无 schema —— 无取值约束
    if node.invalid:
        undetermined.append(
            f"{label}的约束无法判定（{node.invalid}）")
        return
    if node.is_cycle:
        undetermined.append(
            f"{label}的约束无法判定（循环引用）")
        return
    effective = value
    state, errors, hits = _assess_value(graph, node, effective)
    if state == TRI_FAIL:
        coerced, ok = _coerce_scalar(value, node)
        if ok:
            cstate, cerrors, chits = _assess_value(graph, node, coerced)
            if cstate != TRI_FAIL:
                state, errors, hits, effective = \
                    cstate, cerrors, chits, coerced
            else:
                # coerce 成功但代入值仍失败：报告代入后的真实约束失败
                # （如 "80" 超出 maximum），而不是原始文本的类型错误
                errors = cerrors
    if state == TRI_FAIL:
        failures.append(
            f"{label}不满足契约约束: {'; '.join(errors[:2])}")
        return
    if state == TRI_PENDING:
        undetermined.append(
            f"{label}能否通过无法判定: {_format_hits(hits, entry)}")


def _check_body(graph: SchemaGraph, label: str, node, value: Any,
                failures: list[str], undetermined: list[str],
                entry: str) -> None:
    """请求体 / 响应体的 schema 三态核对。

    根节点自身无法解析/为循环由调用方提前拦截；这里处理取值沿图触达的
    已知失败（优先）、循环 / 不可解析节点与未支持关键字（待定）。
    """
    state, errors, hits = _assess_value(graph, node, value)
    if state == TRI_FAIL:
        failures.append(f"{label}不满足 schema: " + "; ".join(errors[:3]))
    elif state == TRI_PENDING:
        undetermined.append(
            f"{label}能否通过无法判定: {_format_hits(hits, entry)}")


def _is_required(param: dict) -> bool:
    return param.get("in") == "path" or bool(param.get("required"))


def _check_parameters(graph: SchemaGraph, op: _Operation,
                      path_values: dict[str, str],
                      call: _Call, entry: str) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    undetermined: list[str] = []

    # path：模板变量必须都有对应参数与取值
    for name in sorted(path_values):
        param, node = op.params.get(name, (None, None))
        raw = path_values[name]
        if param is None or param.get("in") != "path":
            failures.append(
                f"路径变量 {name!r} 在操作参数中未声明为 in:path 参数")
            continue
        _check_value(graph, f"路径参数 {name!r}={raw!r}", node, raw,
                     failures, undetermined, entry)

    provided_maps = (
        ("query", call.query, False),
        ("header", call.headers, True),
        ("cookie", call.cookies, False),
    )
    loc_zh = {"query": "查询", "header": "请求头", "cookie": "Cookie"}
    for location, provided, ci in provided_maps:
        lookup = {k.lower(): (k, v) for k, v in provided.items()} \
            if ci else None
        for name in sorted(op.params):
            param, node = op.params[name]
            if param.get("in") != location:
                continue
            if ci:
                hit = lookup.get(name.lower())
                present = hit is not None
                value = hit[1] if hit else None
            else:
                present = name in provided
                value = provided.get(name)
            if not present:
                if _is_required(param):
                    failures.append(
                        f"缺少必填{loc_zh[location]}参数 {name!r}")
                continue
            _check_value(graph, f"{loc_zh[location]}参数 {name!r}", node,
                         value, failures, undetermined, entry)
    return failures, undetermined


def _content_map(site: Optional[tuple[str, str, dict]]) -> dict[str, Any]:
    if site is None:
        return {}
    content = site[2].get("content")
    return content if isinstance(content, dict) else {}


def _schema_node(graph: SchemaGraph, site: tuple[str, str, dict],
                 mt: str):
    """Schema node of a JSON content entry located at the (deref'd) site."""
    file, pointer, _obj = site
    return graph.build_at(file, ptr.join(pointer, "content", mt, "schema"))


# ============================================================ per-call eval
def _resolve_response_status(op: _Operation, status: str):
    """精确 → 1XX..5XX → default。返回 (code, site) 或 None。"""
    if status in op.responses:
        return status, op.responses[status]
    range_key = f"{status[0]}XX"
    if range_key in op.responses:
        return range_key, op.responses[range_key]
    if "default" in op.responses:
        return "default", op.responses["default"]
    return None


def _entity(part: dict) -> Optional[dict]:
    """request/response 中缺省 body 表示无实体。"""
    if isinstance(part, dict) and "body" in part:
        return part
    return None


def _evaluate(graph: SchemaGraph, operations: list[_Operation],
              call: _Call) -> dict:
    base_row = {
        "line": call.line, "method": call.method.upper(),
        "path": call.path, "status": call.response["status"],
        "operation": None, "matched_template": None, "cell": None,
    }
    op, template, reason = _match_operation(operations, call)
    if op is None:
        return {**base_row,
                "outcome": AMBIGUOUS if "同时命中" in reason else UNMATCHED,
                "reasons": [reason]}
    base_row["operation"] = f"{op.method.upper()} {op.path}"
    base_row["matched_template"] = template

    failures: list[str] = []
    undetermined: list[str] = []
    excluded: list[str] = []

    # ---- 请求参数
    path_values = _template_match(template, call.path) or {}
    pf, pu = _check_parameters(graph, op, path_values, call, graph.entry)
    failures.extend(pf)
    undetermined.extend(pu)

    # ---- 请求体（requestBody 站点带 required 语义）
    req_site = op.body_site
    req_entity = _entity(call.request)
    if req_site is None:
        if req_entity is not None:
            failures.append("操作未声明 requestBody，但调用携带了请求体")
    else:
        content = _content_map(req_site)
        required = bool(req_site[2].get("required"))
        if req_entity is None:
            if required:
                failures.append("requestBody 为必填，但调用未携带请求体")
        elif not content:
            failures.append(
                "requestBody 未声明 content，但调用携带了请求体")
        else:
            mt = _norm_mt(call.request.get("media_type"))
            if not mt:
                undetermined.append(
                    "请求体未提供 media_type，无法判定 content 条目")
            elif not _is_json_mt(mt):
                excluded.append(
                    f"请求媒体类型 {mt} 非 JSON，不纳入 JSON 覆盖格")
            elif mt not in {_norm_mt(k) for k in content}:
                failures.append(
                    f"请求媒体类型 {mt} 未在 requestBody.content 中声明")
            else:
                media = next(v for k, v in content.items()
                             if _norm_mt(k) == mt)
                if isinstance(media, dict) and \
                        isinstance(media.get("schema"), dict):
                    node = _schema_node(graph, req_site, mt)
                    if node.invalid or node.is_cycle:
                        undetermined.append(
                            f"请求体 {mt} 的 schema 无法判定"
                            f"（{node.invalid or '循环引用'}）")
                    else:
                        _check_body(graph, f"请求体 {mt}", node,
                                    call.request.get("body"),
                                    failures, undetermined, graph.entry)

    # ---- 响应状态码归属
    resolved = _resolve_response_status(op, call.response["status"])
    if resolved is None:
        failures.append(
            f"状态码 {call.response['status']} 未在操作响应中声明"
            "（精确值 / 范围键 / default 均无匹配）")
        return _verdict(base_row, failures, undetermined, excluded)

    code, resp_site = resolved
    resp_obj = resp_site[2]
    resp_content = resp_obj.get("content")
    resp_content = resp_content if isinstance(resp_content, dict) else {}
    resp_entity = _entity(call.response)

    cell: Optional[_ResponseCell] = None
    if not resp_content:
        cell = _find_cell(op, code, "no_content", None)
        if resp_entity is not None:
            failures.append(
                f"响应 {code} 声明为无 content，但调用携带了响应实体")
    else:
        mt = _norm_mt(call.response.get("media_type")) if resp_entity \
            else ""
        if resp_entity is None:
            failures.append(f"响应 {code} 声明了 content，但调用无响应实体")
        elif not mt:
            undetermined.append(
                f"响应 {code} 携带实体但未提供 media_type，无法判定内容条目")
        elif not _is_json_mt(mt):
            excluded.append(
                f"响应 {code} 的调用媒体类型 {mt} 非 JSON，"
                "不纳入 JSON 覆盖格")
        elif mt not in {_norm_mt(k) for k in resp_content}:
            failures.append(
                f"响应 {code} 的调用媒体类型 {mt} 未在声明的 content 中")
        else:
            cell = _find_cell(op, code, "content", mt)
            media = next(v for k, v in resp_content.items()
                         if _norm_mt(k) == mt)
            if isinstance(media, dict) and \
                    isinstance(media.get("schema"), dict):
                node = graph.build_at(
                    resp_site[0],
                    ptr.join(resp_site[1], "content", mt, "schema"))
                if node.invalid or node.is_cycle:
                    undetermined.append(
                        f"响应 {code} {mt} 的 schema 无法判定"
                        f"（{node.invalid or '循环引用'}）")
                else:
                    _check_body(graph, f"响应 {code} {mt} 载荷", node,
                                call.response.get("body"),
                                failures, undetermined, graph.entry)

    # ---- 汇总：任一已知约束失败即失败（失败优先）；否则待定阻止覆盖
    if excluded:
        return {**base_row, "outcome": EXCLUDED, "reasons": excluded}
    if failures:
        return {**base_row, "outcome": FAILED,
                "reasons": failures + undetermined}
    if undetermined:
        return {**base_row, "outcome": UNDETERMINED,
                "reasons": undetermined}
    if cell is None:
        # 声明了 content 但调用实体无法定位到具体格（理论上前面已拦截）
        return {**base_row, "outcome": UNDETERMINED,
                "reasons": ["无法定位调用对应的响应覆盖格"]}
    return {**base_row, "outcome": COVERED, "cell": cell.key,
            "resolved_status": code, "reasons": []}


def _find_cell(op: _Operation, status: str, kind: str,
               media_type: Optional[str]) -> Optional[_ResponseCell]:
    for cell in op.cells:
        if cell.status == status and cell.kind == kind \
                and cell.media_type == media_type:
            return cell
    return None


def _verdict(base_row, failures, undetermined, excluded):
    if excluded:
        return {**base_row, "outcome": EXCLUDED, "reasons": excluded}
    if failures:
        return {**base_row, "outcome": FAILED,
                "reasons": failures + undetermined}
    return {**base_row, "outcome": UNDETERMINED,
            "reasons": undetermined}
