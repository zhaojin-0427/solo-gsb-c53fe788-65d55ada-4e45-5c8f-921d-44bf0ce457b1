"""请求迁移评估（Request Migration Assessment）引擎。

在已有分析（基线 / 候选两份 OpenAPI 文档包）上，上传一份**基线时期**的
NDJSON 请求记录，逐行评估该请求在两侧文档下的可接受性，并给出迁移结论：

* 每行是一个**请求**对象：``method``、``path``、``query``、``headers``、
  ``cookies`` 及可选 ``request.media_type`` / ``request.body``；
  **兼容旧调用行**（含 ``response`` 的行），但 ``response`` 被整体忽略——
  迁移评估只关心请求能否被新服务端接受，不复用覆盖结论中的响应校验与
  响应覆盖格；
* 同一具体路径在**两侧分别**做端点匹配与参数绑定（先精确路径、再唯一
  模板）：模板变量改名（如基线 ``/pets/{petId}`` → 候选
  ``/pets/{id}``）时，候选侧按自己的模板重新绑定路径取值，**不复用
  基线的参数绑定**；
* 每侧独立给出**三态**结论——``pass``（确定通过）/ ``fail``（确定失败）/
  ``pending``（待定）：候选缺端点（无模板匹配、或精确路径存在但未声明
  该方法）为**确定失败**；同时命中多个模板为**待定（待核）**；
  ``pattern`` / ``not`` / 循环引用 / 无法解析节点等未支持约束按既有的
  沿取值三态裁决（与调用覆盖同一套 ``assess``）记为待定；
* 行级迁移分类：
    - ``affected``（受影响）：**仅**基线确定通过且候选确定失败；
    - ``unaffected``（未受影响）：两侧都确定通过；
    - ``not_applicable``（不适用）：基线确定失败（该请求本来就不是
      基线契约下的合法请求，迁移结论无从谈起）；
    - ``pending``（待核）：其余一切情况（任一侧待定）。
* 按**基线端点**汇总受影响请求与逐行原因；结果确定性生成，随分析持久化。

复用说明：路径匹配（``_match_operation`` / ``_template_match``）、参数
归集（``_gather_params`` 经 ``_index_operations``）与参数 / 请求体的
三态校验（``_check_parameters`` / ``_check_body`` 等）直接复用调用覆盖
引擎的同一套实现；**不复用**覆盖的响应格、响应校验与 ``covered`` 结论——
覆盖结论回答“调用覆盖了哪个响应格”，迁移结论回答“请求能否被候选侧
接受”，两者语义不同，互不替代。
"""
from __future__ import annotations

import json
from typing import Any

from .coverage import (
    HTTP_METHODS,
    _Call,
    _check_body,
    _check_parameters,
    _content_map,
    _entity,
    _index_operations,
    _is_json_mt,
    _match_operation,
    _norm_mt,
    _schema_node,
    _template_match,
)
from .schemagraph import SchemaGraph

SCHEMA_VERSION = "1.0"
KIND = "request_migration"

# 行级迁移分类
AFFECTED = "affected"
UNAFFECTED = "unaffected"
NOT_APPLICABLE = "not_applicable"
PENDING = "pending"
INVALID = "invalid"

CLASSIFICATION_LABELS = {
    AFFECTED: "受影响",
    UNAFFECTED: "未受影响",
    NOT_APPLICABLE: "不适用",
    PENDING: "待核",
    INVALID: "行格式错误",
}

# 单侧三态
SIDE_PASS = "pass"
SIDE_FAIL = "fail"
SIDE_PENDING = "pending"


class MigrationError(ValueError):
    """上传内容整体无法用于迁移评估（例如没有任何可解析的行）。"""


# ============================================================ public API
def build_migration(baseline_pkg, candidate_pkg, raw: bytes,
                    source_file: str = "requests.ndjson") -> dict:
    """对一份基线时期 NDJSON 请求记录做双侧迁移评估。"""
    graphs = {
        "baseline": SchemaGraph(baseline_pkg.files, baseline_pkg.entry),
        "candidate": SchemaGraph(candidate_pkg.files, candidate_pkg.entry),
    }
    operations = {side: _index_operations(graphs[side])
                  for side in ("baseline", "candidate")}

    rows: list[dict] = []
    valid_requests: list[_Call] = []
    for line_no, raw_line in _iter_lines(raw):
        parsed = _parse_request_line(line_no, raw_line)
        if isinstance(parsed, dict) and "__error__" in parsed:
            rows.append({"line": line_no, "classification": INVALID,
                         "method": None, "path": None,
                         "baseline": None, "candidate": None,
                         "reasons": [parsed["__error__"]]})
            continue
        valid_requests.append(parsed)

    if not valid_requests and not rows:
        raise MigrationError("NDJSON 为空或没有任何非空行")
    if not valid_requests:
        raise MigrationError(
            "NDJSON 中没有任何可解析的请求行："
            + "；".join(f"第{r['line']}行 {r['reasons'][0]}"
                        for r in rows[:5]))

    for req in valid_requests:
        rows.append(_evaluate_row(graphs, operations, req))

    endpoints = _aggregate_endpoints(rows)

    def count(cls: str) -> int:
        return sum(1 for r in rows if r["classification"] == cls)

    summary = {
        "requests_total": len(rows),
        "affected": count(AFFECTED),
        "unaffected": count(UNAFFECTED),
        "not_applicable": count(NOT_APPLICABLE),
        "pending": count(PENDING),
        "invalid": count(INVALID),
        "endpoints_total": len(endpoints),
        "endpoints_affected": sum(1 for e in endpoints if e["affected"] > 0),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "generated_by": "OpenAPI Contract Evolution Workbench",
        "source_file": source_file,
        "summary": summary,
        "endpoints": endpoints,
        "rows": rows,
    }


# ============================================================ NDJSON parsing
def _iter_lines(raw: bytes):
    text = raw.decode("utf-8-sig")
    for i, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            yield i, line


def _parse_request_line(line_no: int, raw_line: str) -> Any:
    """解析一行请求记录。

    与调用覆盖的行格式同源，但 ``response`` 整体忽略（兼容旧调用行）：
    迁移评估只消费 method / path / query / headers / cookies / request。
    """
    try:
        obj = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        return {"__error__": f"不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）"}
    if not isinstance(obj, dict):
        return {"__error__": "请求行必须是 JSON 对象"}

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

    # response（旧调用行）整体忽略；复用覆盖引擎的 _Call 结构承载请求部分
    return _Call(line=line_no, method=method, path=path,
                 query=maps["query"], headers=maps["headers"],
                 cookies=maps["cookies"], request=request, response={})


# ============================================================ per-side eval
def _evaluate_side(graph: SchemaGraph, operations, req: _Call) -> dict:
    """单侧三态评估：端点匹配 + 参数绑定 + 请求参数 / 请求体校验。"""
    op, template, reason = _match_operation(operations, req)
    if op is None:
        # 候选缺端点（含精确路径存在但未声明该方法）为确定失败；
        # 多模板歧义无法唯一归属，记待定（待核）。
        state = SIDE_PENDING if "同时命中" in reason else SIDE_FAIL
        return {"state": state, "operation": None,
                "matched_template": None, "reasons": [reason]}

    failures: list[str] = []
    undetermined: list[str] = []

    # ---- 请求参数（路径变量按本侧模板重新绑定，不复用对侧结果）
    path_values = _template_match(template, req.path) or {}
    pf, pu = _check_parameters(graph, op, path_values, req, graph.entry)
    failures.extend(pf)
    undetermined.extend(pu)

    # ---- 请求体（requestBody 站点带 required 语义；非 JSON 媒体记待定）
    _check_request_body(graph, op, req, failures, undetermined)

    if failures:
        state, reasons = SIDE_FAIL, failures + undetermined
    elif undetermined:
        state, reasons = SIDE_PENDING, undetermined
    else:
        state, reasons = SIDE_PASS, []
    return {"state": state,
            "operation": f"{op.method.upper()} {op.path}",
            "matched_template": template,
            "reasons": reasons}


def _check_request_body(graph: SchemaGraph, op, req: _Call,
                        failures: list[str],
                        undetermined: list[str]) -> None:
    """请求体三态核对（镜像覆盖引擎的请求体分支，非 JSON 媒体记待定）。"""
    req_site = op.body_site
    req_entity = _entity(req.request)
    if req_site is None:
        if req_entity is not None:
            failures.append("操作未声明 requestBody，但调用携带了请求体")
        return
    content = _content_map(req_site)
    required = bool(req_site[2].get("required"))
    if req_entity is None:
        if required:
            failures.append("requestBody 为必填，但调用未携带请求体")
        return
    if not content:
        failures.append("requestBody 未声明 content，但调用携带了请求体")
        return
    mt = _norm_mt(req.request.get("media_type"))
    if not mt:
        undetermined.append("请求体未提供 media_type，无法判定 content 条目")
        return
    if not _is_json_mt(mt):
        undetermined.append(
            f"请求媒体类型 {mt} 非 JSON，请求体约束无法判定")
        return
    if mt not in {_norm_mt(k) for k in content}:
        failures.append(f"请求媒体类型 {mt} 未在 requestBody.content 中声明")
        return
    media = next(v for k, v in content.items() if _norm_mt(k) == mt)
    if isinstance(media, dict) and isinstance(media.get("schema"), dict):
        node = _schema_node(graph, req_site, mt)
        if node.invalid or node.is_cycle:
            undetermined.append(
                f"请求体 {mt} 的 schema 无法判定"
                f"（{node.invalid or '循环引用'}）")
        else:
            _check_body(graph, f"请求体 {mt}", node,
                        req.request.get("body"),
                        failures, undetermined, graph.entry)


# ============================================================ row verdict
def _classify(baseline_state: str, candidate_state: str) -> str:
    if baseline_state == SIDE_FAIL:
        return NOT_APPLICABLE
    if baseline_state == SIDE_PASS and candidate_state == SIDE_FAIL:
        return AFFECTED
    if baseline_state == SIDE_PASS and candidate_state == SIDE_PASS:
        return UNAFFECTED
    return PENDING


def _evaluate_row(graphs: dict, operations: dict, req: _Call) -> dict:
    sides = {
        side: _evaluate_side(graphs[side], operations[side], req)
        for side in ("baseline", "candidate")
    }
    classification = _classify(sides["baseline"]["state"],
                               sides["candidate"]["state"])
    reasons = _decisive_reasons(classification, sides)
    return {
        "line": req.line,
        "method": req.method.upper(),
        "path": req.path,
        "classification": classification,
        "baseline": sides["baseline"],
        "candidate": sides["candidate"],
        "reasons": reasons,
    }


def _decisive_reasons(classification: str, sides: dict) -> list[str]:
    """行级原因：受影响列候选失败原因，不适用列基线失败原因，
    待核列两侧待定原因（带侧前缀），未受影响为空。"""
    if classification == AFFECTED:
        return list(sides["candidate"]["reasons"])
    if classification == NOT_APPLICABLE:
        return list(sides["baseline"]["reasons"])
    if classification == PENDING:
        out: list[str] = []
        for side, label in (("baseline", "基线"), ("candidate", "候选")):
            for reason in sides[side]["reasons"]:
                out.append(f"{label}：{reason}")
        return out
    return []


# ============================================================ aggregation
def _aggregate_endpoints(rows: list[dict]) -> list[dict]:
    """按基线端点汇总：受影响请求逐行带原因，其余分类计数。"""
    groups: dict[str, dict] = {}
    for row in rows:
        if row["classification"] == INVALID:
            continue
        baseline = row["baseline"]
        if not baseline or not baseline.get("operation"):
            continue  # 基线侧未归属任何端点（不适用行），不进端点汇总
        key = baseline["operation"]
        group = groups.setdefault(key, {
            "operation": key,
            "method": row["method"],
            "path": baseline["matched_template"] or row["path"],
            "candidate_operations": set(),
            "requests_total": 0,
            "affected": 0,
            "unaffected": 0,
            "not_applicable": 0,
            "pending": 0,
            "affected_lines": [],
            "lines": [],
        })
        group["requests_total"] += 1
        group[row["classification"]] += 1
        candidate_op = row["candidate"].get("operation")
        if candidate_op:
            group["candidate_operations"].add(candidate_op)
        if row["classification"] == AFFECTED:
            group["affected_lines"].append(row["line"])
        group["lines"].append({
            "line": row["line"],
            "classification": row["classification"],
            "candidate_operation": candidate_op,
            "reasons": row["reasons"],
        })

    endpoints = []
    for group in groups.values():
        group["candidate_operations"] = sorted(
            group["candidate_operations"])
        endpoints.append(group)
    # 受影响多的端点排前，其余按操作名确定排序
    endpoints.sort(key=lambda g: (-g["affected"], g["operation"]))
    return endpoints
