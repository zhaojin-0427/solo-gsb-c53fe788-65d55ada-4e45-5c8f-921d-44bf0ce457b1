"""覆盖缺口补测样本（Coverage Gap Samples）。

针对某一侧**已保存的调用覆盖结果**，为其中每个**未覆盖响应格**综合一行
符合现有 NDJSON 调用格式的补测调用，使该调用在重新上传时按现有覆盖引擎的
三态裁决**确实落入并覆盖目标格**；无法可靠综合的格逐格给出原因，绝不输出
会归错格的样本。

归属优先级与覆盖引擎完全一致（见 :mod:`app.services.coverage`）：

* **精确码抢占范围键、范围键抢占 default**：目标格是范围键（如 ``2XX``）时，
  选用的具体状态码必须**没有**被同操作的精确码响应抢占；目标格是
  ``default`` 时，选用的具体状态码必须既无精确码、也不落入任何已声明的
  ``1XX``…``5XX`` 范围键——否则该调用会归到别的响应，直接判定该格
  「没有可用状态码」而不是输出归错格的样本；
* **模板路径被其他端点抢占**：为模板变量选取具体值后，必须再次走一遍覆盖
  引擎的路径匹配（精确路径优先、唯一模板才归属），具体路径被同方法的精确
  端点抢占、或同时命中多个模板（ambiguous）时，不输出样本并逐格说明；
* **必填值无法可靠构造**（必填参数 / 必填请求体的模式待定、含未支持关键字、
  只有不可综合的媒体类型等）、**响应体模式待定**或**没有可用状态码**时，
  同样只给原因。

结果只读：不会修改已保存的覆盖结论或请求迁移结论。文档确定性排序，可直接
序列化为可下载的 NDJSON（无缺口时为空字节串）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from . import pointer as ptr
from .coverage import (_Call, _evaluate, _index_operations,
                      _match_operation, _norm_mt, _is_json_mt, COVERED)
from .instance import MAX_DEPTH, assess, minimal_instance, TRI_PASS, \
    TRI_PENDING, _round_to_multiple
from .schemagraph import SchemaGraph, SchemaNode, UNSET

SCHEMA_VERSION = "1.0"

RANGE_KEYS = ("1XX", "2XX", "3XX", "4XX", "5XX")


class GapSampleError(ValueError):
    """补测样本整体无法生成（缺少覆盖结果等前置条件）。"""


@dataclass
class _RequestSpec:
    concrete_path: str
    query: dict
    headers: dict
    cookies: dict
    request_part: dict


# ============================================================ public entry
def build_gap_samples(pkg, side: str, coverage_doc: dict) -> dict:
    """根据已保存覆盖文档为全部未覆盖格生成补测样本 / 逐格原因。

    ``coverage_doc`` 必须是 :func:`app.services.coverage.build_coverage`
    产出并保存的文档（含 ``operations``）。
    """
    if not isinstance(coverage_doc, dict) or \
            not isinstance(coverage_doc.get("operations"), list):
        raise GapSampleError("缺少已保存的调用覆盖结果，无法生成补测样本")

    graph = SchemaGraph(pkg.files, pkg.entry)
    operations = _index_operations(graph)
    op_index = {(op.method.upper(), op.path): op for op in operations}

    cells: list[dict] = []
    samples: list[dict] = []
    for op_doc in coverage_doc["operations"]:
        method = op_doc.get("method")
        path = op_doc.get("path")
        op = op_index.get((method, path)) if isinstance(method, str) \
            and isinstance(path, str) else None
        for gap in op_doc.get("gaps", []):
            if op is None:
                cells.append(_unavailable_cell(
                    op_doc, gap, "原始文档中找不到该操作，无法构造补测样本"))
                continue
            cell_doc, sample = _build_for_gap(graph, operations, op, gap)
            cells.append(cell_doc)
            if sample is not None:
                sample["ordinal"] = len(samples) + 1
                cell_doc["sample_ordinal"] = sample["ordinal"]
                samples.append(sample)

    generated = sum(1 for c in cells if c["generated"])
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "coverage_gap_samples",
        "analysis_side": side,
        "summary": {
            "gaps_total": len(cells),
            "samples_generated": generated,
            "samples_unavailable": len(cells) - generated,
        },
        "cells": cells,
        "samples": samples,
    }


def render_ndjson(doc: dict) -> bytes:
    """确定性序列化补测样本为 NDJSON 字节；无样本时为空。"""
    lines = [
        json.dumps(s["call"], ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"))
        for s in doc.get("samples", [])
    ]
    if not lines:
        return b""
    return ("\n".join(lines) + "\n").encode("utf-8")


def _unavailable_cell(op_doc: dict, gap: dict, reason: str) -> dict:
    return {
        "operation": f"{op_doc.get('method')} {op_doc.get('path')}",
        "method": op_doc.get("method"),
        "path": op_doc.get("path"),
        "cell_key": gap.get("key"),
        "declared_status": gap.get("status"),
        "kind": gap.get("kind"),
        "media_type": gap.get("media_type"),
        "generated": False,
        "request_status": None,
        "resolved_status": None,
        "reasons": [reason],
    }


# ============================================================ per-gap build
def _build_for_gap(graph: SchemaGraph, operations, op,
                   gap: dict) -> tuple[dict, Optional[dict]]:
    declared = str(gap.get("status"))
    cell_base = {
        "operation": f"{op.method.upper()} {op.path}",
        "method": op.method.upper(),
        "path": op.path,
        "cell_key": gap.get("key"),
        "declared_status": declared,
        "kind": gap.get("kind"),
        "media_type": gap.get("media_type"),
        "generated": False,
        "request_status": None,
        "resolved_status": None,
        "reasons": [],
    }

    def fail(reasons: list[str]) -> tuple[dict, Optional[dict]]:
        return {**cell_base, "reasons": reasons}, None

    # ---- 1) 选择实际会归属到目标声明键的具体状态码
    status, status_reason = _select_status(op, declared)
    if status is None:
        return fail([status_reason or "没有可归属到该格的具体状态码"])

    # ---- 2) 构造最小、可判定通过的请求（路径变量 + 必填参数 + 必填请求体）
    try:
        spec = _build_request_spec(graph, operations, op)
    except _BuildFailure as exc:
        return fail([str(exc)])

    # ---- 3) 路径抢占复核：具体路径必须仍唯一归属到本操作
    probe = _make_call(op, spec, status)
    matched, _tmpl, match_reason = _match_operation(operations, probe)
    if matched is not op:
        return fail([
            f"模板路径 {op.path} 代入具体值 {spec.concrete_path} 后"
            f"{match_reason or '未唯一归属到目标操作'}，"
            "补测调用无法落入该操作的目标格"])

    # ---- 4) 构造响应部分（无实体 / JSON 媒体最小载荷）
    response_part, resp_reasons = _build_response_part(graph, op, declared,
                                                       status, gap)
    if resp_reasons:
        return fail(resp_reasons)

    # ---- 5) 用现有覆盖引擎的三态裁决确认样本确实补上该格
    call = {
        "method": op.method.upper(),
        "path": spec.concrete_path,
        "query": spec.query,
        "headers": spec.headers,
        "cookies": spec.cookies,
        "request": spec.request_part,
        "response": response_part,
    }
    verify = _make_call(op, spec, status,
                        response={**response_part, "status": status})
    row = _evaluate(graph, operations, verify)
    target_cell_key = gap.get("key")
    if row["outcome"] != COVERED:
        return fail([
            "补测样本未能通过现有三态覆盖判定（结局 "
            f"{row['outcome']}）：" + "；".join(row.get("reasons", [])[:3])])
    if row.get("operation") != cell_base["operation"] \
            or row.get("cell") != target_cell_key:
        return fail([
            f"补测样本归属到 {row.get('operation')} 的格 {row.get('cell')}，"
            f"不是目标格 {cell_base['operation']} {target_cell_key}"])

    sample = {
        "operation": cell_base["operation"],
        "cell_key": target_cell_key,
        "target_declared_status": declared,
        "request_status": status,
        "resolved_status": row.get("resolved_status", declared),
        "call": call,
    }
    cell_doc = {
        **cell_base,
        "generated": True,
        "request_status": status,
        "resolved_status": row.get("resolved_status", declared),
        "reasons": [],
    }
    return cell_doc, sample


def _make_call(op, spec: _RequestSpec, status: str,
               response: Optional[dict] = None) -> _Call:
    return _Call(
        line=1, method=op.method, path=spec.concrete_path,
        query=spec.query, headers=spec.headers, cookies=spec.cookies,
        request=spec.request_part,
        response=response if response is not None else {"status": status})


# ============================================================ status select
def _select_status(op, declared: str) -> tuple[Optional[str], Optional[str]]:
    """返回真正会归属到 ``declared`` 响应声明键的具体状态码。

    精确码：只能用该精确码本身；范围键：用范围内**未被精确码抢占**的最小学号；
    default：用既无精确码、也不落入任何范围键的最小学号。
    """
    responses = op.responses
    if declared.isdigit():
        if declared not in responses:
            return None, f"操作未声明精确状态码 {declared}"
        return declared, None

    if declared.upper() in RANGE_KEYS:
        if declared not in responses:
            return None, f"操作未声明范围键 {declared}"
        base = int(declared[0])
        for n in range(base * 100, base * 100 + 100):
            code = str(n)
            if code in responses:
                continue  # 精确码抢占范围键
            return code, None
        return None, (
            f"{declared} 范围内的状态码均被精确码响应抢占，"
            "不存在能归属到该范围格的具体状态码")

    if declared == "default":
        if "default" not in responses:
            return None, "操作未声明 default 响应"
        for n in range(100, 600):
            code = str(n)
            if code in responses:
                continue  # 精确码抢占 default
            if f"{code[0]}XX" in responses:
                continue  # 范围键抢占 default
            return code, None
        return None, (
            "100-599 的状态码均被精确码或范围键响应抢占，"
            "不存在能归属到 default 格的具体状态码")

    return None, f"无法识别的响应声明键 {declared!r}"


# ============================================================ request build
class _BuildFailure(Exception):
    """必填请求约束无法可靠构造。"""


_MISSING = object()


def _build_request_spec(graph: SchemaGraph, operations, op) -> _RequestSpec:
    placeholders = set(re.findall(r"\{([^}]+)\}", op.path))
    declared_path = {name for name, (p, _n) in op.params.items()
                     if p.get("in") == "path"}
    missing = placeholders - declared_path
    if missing:
        # 覆盖引擎对未声明 path 参数的变量一律判校验失败，不可能补出覆盖
        raise _BuildFailure(
            "路径模板变量 "
            + "、".join(sorted(missing))
            + " 未声明为 in:path 参数，任何调用都无法通过请求校验")

    path_texts: dict[str, str] = {}
    for name in sorted(op.params):
        param, node = op.params[name]
        if param.get("in") != "path":
            continue
        text = _choose_path_segment(graph, operations, op, name, param, node)
        if text is None:
            raise _BuildFailure(
                f"必填路径参数 {name!r} 无法构造能唯一归属本操作的可信取值")
        path_texts[name] = text

    concrete = _substitute_path(op.path, path_texts)
    if concrete is None:
        raise _BuildFailure(
            f"路径模板 {op.path} 的占位符与 path 参数不一致")

    query: dict[str, Any] = {}
    headers: dict[str, Any] = {}
    cookies: dict[str, Any] = {}
    loc_map = {"query": query, "header": headers, "cookie": cookies}
    loc_zh = {"query": "查询", "header": "请求头", "cookie": "Cookie"}
    for name in sorted(op.params):
        param, node = op.params[name]
        loc = param.get("in")
        if loc not in loc_map:
            continue
        if not _param_required(param):
            continue  # 旧客户端惯例：可选参数一律省略
        value, why = _required_scalar_value(node, as_text=(loc != "query"))
        if why:
            raise _BuildFailure(
                f"必填{loc_zh[loc]}参数 {name!r} 无法可靠构造取值：{why}")
        loc_map[loc][name] = value

    request_part: dict[str, Any] = {}
    req_site = op.body_site
    if req_site is not None and bool(req_site[2].get("required")):
        content = req_site[2].get("content")
        content = content if isinstance(content, dict) else {}
        mt = _select_request_mt(content)
        if mt is None:
            raise _BuildFailure(
                "必填 requestBody 只声明了不可综合的媒体类型（如 "
                "application/octet-stream）")
        node = graph.build_at(
            req_site[0], ptr.join(req_site[1], "content", mt, "schema"))
        body, why = _reliable_body(graph, node)
        if why:
            raise _BuildFailure(f"必填请求体（{mt}）无法可靠构造：{why}")
        request_part = {"media_type": mt, "body": body}

    return _RequestSpec(concrete, query, headers, cookies, request_part)


def _param_required(param: dict) -> bool:
    return param.get("in") == "path" or bool(param.get("required"))


def _substitute_path(template: str, texts: dict[str, str]) -> Optional[str]:
    placeholders = re.findall(r"\{([^}]+)\}", template)
    if set(placeholders) != set(texts):
        return None
    out = template
    for name in placeholders:
        out = out.replace("{" + name + "}", str(texts[name]))
    return out


def _http_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "x"
    return str(value)


def _choose_path_segment(graph, operations, op, name, param, node) \
        -> Optional[str]:
    """枚举路径变量候选文本：既通过参数三态裁决、又唯一归属本操作。"""
    if param.get("in") != "path":
        return None
    if node is not None and (node.invalid or node.is_cycle):
        return None
    if node is not None and node.type and \
            set(node.type) <= {"object", "array"}:
        return None  # 对象/数组无法可靠表示为 URL 路径段
    if node is not None and _node_has_unresolvable(node):
        return None

    candidates = _path_text_candidates(node)
    others = {n: p for n, p in op.params.items()
              if p[0].get("in") == "path" and n != name}
    other_texts = {}
    for other_name, (other_param, other_node) in others.items():
        text = _first_valid_text(other_node)
        if text is None:
            return None
        other_texts[other_name] = text

    for text in candidates:
        values = {**other_texts, name: text}
        concrete = _substitute_path(op.path, values)
        if concrete is None or "//" in concrete:
            continue
        # 引擎会对路径文本按参数 schema 三态裁决（含文本 coerce）
        if node is not None:
            result = assess(node, text)
            if result.state not in (TRI_PASS, TRI_PENDING):
                coerced = _coerce_scalar_text(text, node)
                if coerced is _NONE or (coerced is not _NONE and
                                        assess(node, coerced).state
                                        not in (TRI_PASS, TRI_PENDING)):
                    continue
        probe = _Call(line=1, method=op.method, path=concrete,
                      query={}, headers={}, cookies={}, request={},
                      response={"status": "200"})
        matched, _tmpl, _reason = _match_operation(operations, probe)
        if matched is op:
            return text
    return None


_NONE = object()


def _coerce_scalar_text(text: str, node):
    if node is None or node.type is None:
        return _NONE
    types = set(node.type)
    low = text.strip().lower()
    if "boolean" in types and low in ("true", "false"):
        return low == "true"
    if "integer" in types:
        try:
            return int(text.strip())
        except ValueError:
            return _NONE
    if "number" in types:
        try:
            return float(text.strip())
        except ValueError:
            return _NONE
    return _NONE


def _path_text_candidates(node) -> list[str]:
    """确定性的有限路径段候选文本集合。"""
    if node is None:
        return ["x", "1", "0"]
    texts: list[str] = []

    def add(v):
        t = _http_text(v)
        if t and t not in texts:
            texts.append(t)

    if node.const is not UNSET and node.const is not None:
        add(node.const)
    if node.enum:
        for v in node.enum:
            add(v)
    for raw in _scalar_candidates(node):
        add(raw)
    # 通用兜底：变量段非空即可（无类型约束时）
    for v in ("x", "1", "0", "-1", "true"):
        add(v)
    return texts[:12]


def _first_valid_text(node) -> Optional[str]:
    if node is not None and (node.invalid or node.is_cycle):
        return None
    if node is not None and _node_has_unresolvable(node):
        return None
    for raw in _scalar_candidates(node) + ["x", "1", "0"]:
        text = _http_text(raw)
        if not text:
            continue
        if node is None:
            return text
        result = assess(node, text)
        if result.state in (TRI_PASS, TRI_PENDING):
            return text
        coerced = _coerce_scalar_text(text, node)
        if coerced is not _NONE and \
                assess(node, coerced).state in (TRI_PASS, TRI_PENDING):
            return text
    return None


def _scalar_candidates(node) -> list:
    out: list = []
    try:
        v = minimal_instance(node)
        if not isinstance(v, (dict, list)):
            out.append(v)
    except Exception:
        pass
    types = node.type or []
    for t in types:
        if t == "boolean":
            out.append(False)
        elif t == "integer":
            out.extend([0, 1])
            if node.minimum is not None:
                out.append(node.minimum)
            if node.maximum is not None:
                out.append(node.maximum)
        elif t == "number":
            out.extend([0, 1])
        elif t == "string":
            length = node.min_length or 0
            out.append("x" * length)
            if node.enum:
                out.extend(node.enum)
        elif t == "null":
            pass
    if not types:
        out.extend(["x", 0, 1, False])
    deduped = []
    for v in out:
        if v not in deduped:
            deduped.append(v)
    return deduped


def _required_scalar_value(node, *, as_text: bool) -> tuple[Any, Optional[str]]:
    """必填 query/header/cookie 参数的最小可信值。"""
    if node is None:
        # 声明必填但无 schema —— 无取值约束
        return ("x" if as_text else "x"), None
    if node.invalid:
        return None, f"模式不可判定（{node.invalid}）"
    if node.is_cycle:
        return None, "循环引用"
    if _node_has_unresolvable(node):
        return None, "参数模式含未支持/不可解析约束，取值能否通过无法判定"
    try:
        value = minimal_instance(node)
    except Exception:
        value = _MISSING
    candidates = []
    if value is not _MISSING and not isinstance(value, (dict, list)):
        candidates.append(value)
    # minimal_instance 在边界 + multipleOf 组合下可能返回浮点（如
    # minimum=7,multipleOf=3 给出 9.0），整数类型需要显式整数候选兜底
    if node.type and ("integer" in node.type or "number" in node.type):
        for c in _number_candidates(node):
            if c not in candidates:
                candidates.append(c)
    for value in candidates:
        if isinstance(value, (dict, list)):
            return None, "参数类型为对象/数组，无法可靠表示为 HTTP 标量"
        result = assess(node, value)
        if result.state == TRI_PASS:
            if as_text:
                value = _http_text(value)
            return value, None
    if candidates:
        result = assess(node, candidates[0])
        if result.state == TRI_PENDING:
            return None, "最小值能否通过无法判定"
        return None, "最小值不满足参数约束: " + "; ".join(result.errors[:2])
    return None, "最小值构造失败"


def _select_request_mt(content: dict) -> Optional[str]:
    norms = {_norm_mt(k): k for k in content if isinstance(k, str)}
    if "application/json" in norms:
        return "application/json"
    for norm in sorted(norms):
        if norm.endswith("+json"):
            return norm
    return None


# ============================================================ response build
def _build_response_part(graph: SchemaGraph, op, declared: str, status: str,
                         gap: dict) -> tuple[Optional[dict], list[str]]:
    site = op.responses[declared]
    resp_obj = site[2]
    content = resp_obj.get("content")
    content = content if isinstance(content, dict) else {}
    if gap.get("kind") == "no_content" or not content:
        # 无实体格：response 不含 body 键
        return {"status": int(status)}, []

    mt = gap.get("media_type")
    if not isinstance(mt, str) or not _is_json_mt(_norm_mt(mt)):
        return None, [f"目标格媒体类型 {mt!r} 不是 JSON 媒体，无法生成格"]
    norm = _norm_mt(mt)
    if norm not in {_norm_mt(k) for k in content}:
        return None, [f"响应 {declared} 未声明媒体类型 {mt}"]

    media = next(v for k, v in content.items() if _norm_mt(k) == norm)
    if not isinstance(media, dict) or not isinstance(
            media.get("schema"), dict):
        # 声明了 JSON content 但无 schema：无载荷约束，空对象即可
        return {"status": int(status), "media_type": norm, "body": {}}, []

    node = graph.build_at(site[0],
                          ptr.join(site[1], "content", norm, "schema"))
    body, why = _reliable_body(graph, node)
    if why:
        return None, [f"响应 {declared} {norm} 载荷无法可靠构造：{why}"]
    return {"status": int(status), "media_type": norm, "body": body}, []


# ============================================================ body synthesis
def _node_has_unresolvable(node, *, depth: int = 0,
                           seen: Optional[set] = None) -> bool:
    """沿最小取值可能触达的部分是否存在未支持关键字 / 无效节点。

    与覆盖引擎一致：未触达的可选循环属性不阻断（is_cycle 只在取值实际
    重入时才相关，必填递归属性则在 :func:`_reliable_body` 中被拦截）。
    """
    seen = set() if seen is None else seen
    if node is None or depth >= MAX_DEPTH:
        return False
    key = (node.loc.file, node.loc.pointer)
    if key in seen:
        return False
    seen = seen | {key}
    if node.invalid or node.unsupported:
        return True
    for name in sorted(node.required):
        child = node.properties.get(name)
        if child is not None and _node_has_unresolvable(
                child, depth=depth + 1, seen=seen):
            return True
    for br in (*node.one_of, *node.any_of):
        if _node_has_unresolvable(br, depth=depth + 1, seen=seen):
            return True
    return False


def _reliable_body(graph: Optional[SchemaGraph], node: SchemaNode, *,
                   depth: int = 0, stack: tuple = (),
                   ) -> tuple[Any, Optional[str]]:
    """构造一个经三态裁决**确定通过**的最小 JSON 载荷。

    成功返回 ``(value, None)``；模式待定（无效 / 循环 / 未支持关键字）或
    候选均无法确定通过时返回 ``(None, reason)``。
    """
    if node is None or node.is_bool is True:
        return {}, None
    if node.is_bool is False:
        return None, "false 模式不允许任何值"
    if node.const is not UNSET:
        # const: null 同样是合法具体取值，不能与失败返回的 None 混淆
        if assess(node, node.const).state == TRI_PASS:
            return node.const, None
        return None, f"常量 {node.const!r} 未通过该节点三态判定"
    if node.invalid:
        return None, f"模式无法判定（{node.invalid}）"
    if node.is_cycle:
        return None, "循环引用，有限取值无法证明通过"
    site_key = (node.loc.file, node.loc.pointer)
    if site_key in stack or depth >= MAX_DEPTH:
        return None, "递归/嵌套过深，无法构造有限确定值"
    if node.unsupported:
        return None, ("模式含未支持关键字 "
                      + "、".join(sorted({kw for kw, _ in node.unsupported}))
                      + "，载荷能否通过无法判定")

    # ---- 组合：逐支构造，再用完整节点三态裁决（保证 oneOf 恰好一支）
    branches = list(node.one_of or node.any_of)
    if branches:
        passing: list[Any] = []
        failures: list[str] = []
        for br in branches:
            v, why = _reliable_body(graph, br, depth=depth + 1,
                                    stack=stack + (site_key,))
            if why:
                failures.append(why)
            else:
                passing.append(v)
        if not passing:
            return None, "所有组合分支均无法可靠构造：" + "；".join(
                failures[:2])
        ordered = sorted(passing, key=_value_rank)
        if node.any_of:
            for v in ordered:
                if assess(node, v).state == TRI_PASS:
                    return v, None
            return None, "anyOf 候选取值未通过完整三态判定"
        # oneOf：必须恰好一支通过 —— 由完整节点裁决确认
        for v in ordered:
            result = assess(node, v)
            if result.state == TRI_PASS:
                return v, None
        return None, "无法保证取值恰好满足 oneOf 的一个分支"

    # ---- 非组合：枚举有限候选，全部经完整节点三态裁决
    reasons: list[str] = []
    for v in _body_candidates(graph, node, depth, stack):
        result = assess(node, v)
        if result.state == TRI_PASS:
            return v, None
        if result.state == TRI_PENDING:
            return None, ("载荷能否通过无法判定："
                          + _format_hits(result.hits, graph.entry
                                         if graph is not None else ""))
        reasons.append("；".join(result.errors[:2]) or "不满足约束")
    return None, "最小取值不满足模式约束" + (
        "（" + reasons[0] + "）" if reasons else "")


def _format_hits(hits, entry: str = "") -> str:
    parts = []
    seen = set()
    for kw, loc in hits:
        where = loc.hint(entry) if hasattr(loc, "hint") else str(loc)
        key = (kw, where)
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"{kw}（{where}）")
    return "、".join(parts) if parts else "存在无法判定的模式节点"


def _body_candidates(graph, node: SchemaNode, depth: int,
                     stack: tuple) -> list:
    """非组合节点的确定性有限候选集（最小实例优先）。"""
    cands: list[Any] = []

    def add(v):
        if v not in cands:
            cands.append(v)

    if node.const is not UNSET:
        return [node.const]
    if node.enum:
        for v in node.enum:
            add(v)
    try:
        add(minimal_instance(node))
    except Exception:
        pass

    types = node.type or []
    if not types:
        # 无类型约束：按结构特征决定主要形态，同时保留标量候选
        if node.required or node.properties:
            types = ["object"]
        elif node.items is not None:
            types = ["array"]
        else:
            types = ["object", "array", "string", "integer", "boolean",
                     "null"]
    for t in types:
        for v in _typed_candidates(graph, node, t, depth, stack):
            add(v)
    return cands


def _typed_candidates(graph, node: SchemaNode, t: str, depth: int,
                      stack: tuple) -> list:
    if t == "null":
        return [None]
    if t == "boolean":
        return [False, True]
    if t in ("integer", "number"):
        return _number_candidates(node)
    if t == "string":
        return _string_candidates(node)
    if t == "array":
        return _array_candidates(graph, node, depth, stack)
    if t == "object":
        return _object_candidates(graph, node, depth, stack)
    return []


def _number_candidates(node: SchemaNode) -> list:
    raw = [0, 1]
    if node.minimum is not None:
        raw += [node.minimum, node.minimum + 1]
    if node.ex_min is not None:
        raw += [node.ex_min + 1]
    if node.maximum is not None:
        raw += [node.maximum]
    if node.multiple_of is not None:
        # 向上取整到 multipleOf 的倍数（保证不小于下界），再补一个向下倍数
        from fractions import Fraction
        for base in list(raw):
            try:
                q = Fraction(str(base)) / Fraction(str(node.multiple_of))
                ceil_m = float(-(-q.numerator // q.denominator)) * \
                    float(node.multiple_of)
                floor_m = _round_to_multiple(base, node.multiple_of)
            except (ValueError, ZeroDivisionError):
                continue
            if ceil_m >= base:
                raw.append(ceil_m)
            if floor_m is not None:
                raw.append(floor_m)
    out = []
    integer_only = node.type == ["integer"]
    for c in raw:
        v = int(c) if (integer_only or float(c).is_integer()) else c
        if v not in out:
            out.append(v)
    return out


def _string_candidates(node: SchemaNode) -> list:
    if node.enum and all(isinstance(v, str) for v in node.enum):
        return list(node.enum)
    length = node.min_length or 0
    vals = ["x" * length]
    if node.max_length is not None and length > node.max_length:
        vals.append("")
    return vals


def _array_candidates(graph, node: SchemaNode, depth: int,
                      stack: tuple) -> list:
    if depth + 1 >= MAX_DEPTH:
        return []
    count = node.min_items or 1
    if node.max_items is not None:
        count = min(count, node.max_items)
    count = min(count, 3)
    if node.items is None:
        return [[""] * count]
    item, why = _reliable_body(graph, node.items, depth=depth + 1, stack=stack)
    if why:
        return []
    return [[item] * count]


def _object_candidates(graph, node: SchemaNode, depth: int,
                       stack: tuple) -> list:
    if depth + 1 >= MAX_DEPTH:
        return [{}]
    names = sorted(node.required)
    if node.min_props and len(names) < node.min_props:
        existing = set(node.properties)
        extras = []
        i = 0
        while len(names) + len(extras) < node.min_props:
            cand = f"extra{i}"
            if cand not in existing:
                extras.append(cand)
            i += 1
        names += extras
    body: dict[str, Any] = {}
    for name in names:
        child = node.properties.get(name)
        if child is None:
            if isinstance(node.additional, SchemaNode):
                v, why = _reliable_body(graph, node.additional,
                                        depth=depth + 1, stack=stack)
                if why:
                    return []
                body[name] = v
            elif node.additional is False:
                return []
            else:
                body[name] = ""
            continue
        v, why = _reliable_body(graph, child, depth=depth + 1, stack=stack)
        if why:
            return []
        body[name] = v
    return [body]


def _value_rank(v):
    if isinstance(v, bool):
        return (1, int(v))
    if isinstance(v, (int, float)):
        return (2, abs(v))
    if isinstance(v, str):
        return (3, len(v))
    if isinstance(v, list):
        return (4, len(v))
    if isinstance(v, dict):
        return (5, len(v))
    return (9, 0)
