"""Compare two OpenAPI packages under server-upgrade compatibility rules.

Direction: baseline (what old clients/servers use today) -> candidate (the
upgraded server).

* request side: the accepted value set must not be NARROWED
  (请求不可收窄);
* response side: the delivered value set must not be WIDENED
  (响应不可放宽).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from . import pointer as ptr
from .endpointrequest import BuildFailure, build_request, select_body_media
from .instance import minimal_instance, check_tri, format_pending_hits, \
    TRI_PASS, TRI_FAIL, TRI_PENDING, reenter_for
from .rules import BREAKING, COMPATIBLE, UNDETERMINED, rule_info
from .schemagraph import SchemaGraph, SchemaNode, FacetLoc, UNSET

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head",
                "options", "trace")
REQUEST = "request"
RESPONSE = "response"

# --------------------------------------------------------------------- models
@dataclass
class Witness:
    kind: str
    note: str
    instance: Any = None
    method: Optional[str] = None
    path: Optional[str] = None
    location: Optional[str] = None
    media_type: Optional[str] = None
    status_code: Optional[str] = None
    parameter: Optional[str] = None
    param_in: Optional[str] = None
    direction: Optional[str] = None
    rule: Optional[str] = None
    replay_path: Optional[str] = None
    facet_pointer_baseline: Optional[str] = None
    facet_pointer_candidate: Optional[str] = None
    self_check: Optional[dict] = None
    baseline: Optional[dict] = None
    candidate: Optional[dict] = None
    # endpoint-level request witnesses (ENDPOINT_REMOVED)
    request_template: Optional[str] = None
    request_body: Optional[dict] = None
    verifiable: Optional[bool] = None
    unverifiable_reason: Optional[str] = None

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if v is not None}
        return d


@dataclass
class Finding:
    rule: str
    severity: str
    path: str
    method: str
    location: str
    media_type: Optional[str]
    status_code: Optional[str]
    pointer_baseline: Optional[str]
    pointer_candidate: Optional[str]
    file_baseline: Optional[str]
    file_candidate: Optional[str]
    facet: Optional[str]
    message: str
    witness: Optional[dict] = None
    id: str = ""


@dataclass
class EndpointReport:
    path: str
    method: str
    operation_id: Optional[str]
    severity: str = COMPATIBLE
    finding_ids: list[str] = field(default_factory=list)


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    endpoints: list[EndpointReport] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


@dataclass
class _Ctx:
    pkg: Any
    graph: SchemaGraph

    @property
    def entry(self):
        return self.pkg.entry

    def root(self):
        return self.pkg.files[self.pkg.entry]

    def hint(self, loc: Optional[FacetLoc]) -> Optional[str]:
        return loc.hint(self.pkg.entry) if loc else None


@dataclass
class Scope:
    path: str
    method: str
    location: str
    media_type: Optional[str]
    status_code: Optional[str]
    direction: str
    schema_loc_b: Optional[tuple] = None
    schema_loc_c: Optional[tuple] = None


@dataclass
class FCompare:
    rule: str
    severity: str
    floc_b: Optional[FacetLoc]
    floc_c: Optional[FacetLoc]
    facet: Optional[str]
    message: str
    witness_hint: Any = None
    relpath: str = ""


# ----------------------------------------------------------------- public API
def compare_packages(base_pkg, cand_pkg) -> Report:
    cb = _Ctx(base_pkg, SchemaGraph(base_pkg.files, base_pkg.entry))
    cc = _Ctx(cand_pkg, SchemaGraph(cand_pkg.files, cand_pkg.entry))
    return Comparator(cb, cc).run()


_SEV_ORDER = {COMPATIBLE: 0, UNDETERMINED: 1, BREAKING: 2}
_LOC_ORDER = {"parameter": 0, "requestBody": 1, "response": 2, "": 0}


class Comparator:
    def __init__(self, b: _Ctx, c: _Ctx):
        self.b, self.c = b, c
        self.findings: list[Finding] = []
        self.ep_index: dict[tuple[str, str], EndpointReport] = {}
        self.ep_sev: dict[tuple[str, str], str] = {}
        self.ep_opid: dict[tuple[str, str], Optional[str]] = {}

    def run(self) -> Report:
        b_paths = self.b.root().get("paths") or {}
        c_paths = self.c.root().get("paths") or {}
        for p in sorted(set(b_paths) | set(c_paths)):
            bi = b_paths.get(p)
            ci = c_paths.get(p)
            bi = bi if isinstance(bi, dict) else {}
            ci = ci if isinstance(ci, dict) else {}
            bm = {m: bi[m] for m in HTTP_METHODS if isinstance(bi.get(m), dict)}
            cm = {m: ci[m] for m in HTTP_METHODS if isinstance(ci.get(m), dict)}
            for method in sorted(set(bm) | set(cm)):
                self._endpoint(p, method, bm.get(method), cm.get(method),
                               bi.get("parameters") if isinstance(
                                   bi.get("parameters"), list) else [],
                               ci.get("parameters") if isinstance(
                                   ci.get("parameters"), list) else [])
        return self._finalize()

    # ------------------------------------------------------------- findings
    def _bump(self, key, sev):
        cur = self.ep_sev.get(key, COMPATIBLE)
        if _SEV_ORDER[sev] > _SEV_ORDER[cur]:
            self.ep_sev[key] = sev

    def _add(self, rule, path, method, *, location="", media_type=None,
             status_code=None, floc_b=None, floc_c=None, facet=None,
             message=None, witness: Optional[Witness] = None,
             severity=None) -> Finding:
        info = rule_info(rule)
        f = Finding(
            rule=rule, severity=severity or info["severity"],
            path=path, method=method.upper(), location=location,
            media_type=media_type, status_code=status_code,
            pointer_baseline=floc_b.pointer if floc_b else None,
            pointer_candidate=floc_c.pointer if floc_c else None,
            file_baseline=floc_b.file if floc_b else None,
            file_candidate=floc_c.file if floc_c else None,
            facet=facet, message=message or info["title"],
            witness=witness.to_dict() if witness else None)
        f.id = self._id(f)
        self.findings.append(f)
        self._bump((path, method.upper()), f.severity)
        return f

    @staticmethod
    def _id(f: Finding) -> str:
        import hashlib
        payload = "|".join(str(x) for x in (
            f.rule, f.path, f.method, f.location, f.media_type or "",
            f.status_code or "", f.pointer_baseline or "",
            f.pointer_candidate or "", f.facet or "", f.message))
        return hashlib.sha1(payload.encode()).hexdigest()[:12]

    # ------------------------------------------------------------- endpoint
    def _endpoint(self, path, method, bop, cop, bpath_params, cpath_params):
        key = (path, method.upper())
        op_pointer = f"/paths{ptr.build([path, method])}"
        if bop is None:
            self.ep_opid[key] = cop.get("operationId")
            self._add("ENDPOINT_ADDED", path, method,
                      floc_c=FacetLoc(self.c.entry, op_pointer))
            return
        if cop is None:
            self.ep_opid[key] = bop.get("operationId")
            self._add("ENDPOINT_REMOVED", path, method,
                      floc_b=FacetLoc(self.b.entry, op_pointer),
                      witness=self._endpoint_removed_witness(path, method))
            return
        self.ep_opid[key] = cop.get("operationId") or bop.get("operationId")
        self._parameters(path, method, bop, cop, bpath_params, cpath_params)
        self._request_body(path, method, bop, cop)
        self._responses(path, method, bop, cop)

    # -------------------------------------------------- endpoint-level witness
    def _endpoint_removed_witness(self, path: str, method: str) -> Witness:
        """Build a concrete, minimal old-client request for the removed op.

        Breaking is certain by construction; the *witness* may be
        unverifiable when the baseline request cannot be synthesised
        reliably, in which case the finding keeps its breaking severity and
        carries an explicit reason instead of a faked one-sided replay.
        """
        w = Witness(
            kind="endpoint", method=method.upper(), path=path,
            direction=REQUEST,
            request_template=path,
            note="旧客户端对已删除端点发起的调用将得到 404。")
        try:
            built = build_request(self.b.graph, path, method)
        except BuildFailure as exc:
            w.verifiable = False
            w.unverifiable_reason = str(exc)
            w.self_check = {
                "endpoint_exists": {"baseline": True, "candidate": False},
                "baseline_accepts": None,
                "verifiable": False,
                "reason": str(exc)}
            w.note = (f"{w.note}  ⚠ 无法可靠构造基线请求（{exc}），"
                      "破坏性结论保留，但反例不可验证。")
            return w

        req = built["request"]
        w.instance = req
        w.path = req["path"]
        body_desc = built["body"]
        if body_desc:
            w.request_body = body_desc
            w.media_type = body_desc["media_type"]
        w.location = ""
        caveats = built.get("caveats") or []
        w.self_check = {
            "endpoint_exists": {"baseline": True, "candidate": False},
            "baseline_accepts": True,
            "verifiable": True,
            "caveats": caveats}
        suffix = f"  注意：{'；'.join(caveats)}" if caveats else ""
        w.note = (
            f"{w.note}  反例为按基线契约构造的最小请求"
            f"（{method.upper()} {req['path']}）：基线可接受，候选无此端点。"
            f"{suffix}")
        w.verifiable = True
        return w

    # ----------------------------------------------------------- parameters
    def _gather_params(self, ctx: _Ctx, op_params, path_params,
                       path, method):
        """Return {name: (param_dict, FacetLoc)}; refs resolved locally."""
        out: dict[str, tuple[dict, FacetLoc]] = {}

        def put(lst, base_pointer_tmpl):
            if not isinstance(lst, list):
                return
            for i, item in enumerate(lst):
                if not isinstance(item, dict):
                    continue
                if "$ref" in item:
                    res = ctx.graph.resolver.resolve(item["$ref"], ctx.entry)
                    if not res.ok or not isinstance(res.target, dict):
                        continue
                    p = res.target
                    loc = FacetLoc(res.file, res.pointer)
                else:
                    p = item
                    loc = FacetLoc(ctx.entry, base_pointer_tmpl(i))
                name = p.get("name")
                if isinstance(name, str):
                    out[name] = (p, loc)

        # path-level then operation-level (latter overrides)
        put(path_params,
            lambda i: f"/paths{ptr.build([path, 'parameters', i])}")
        put(op_params if isinstance(op_params, list) else [],
            lambda i: f"/paths{ptr.build([path, method, 'parameters', i])}")
        return out

    def _parameters(self, path, method, bop, cop, bpp, cpp):
        bp = self._gather_params(self.b, bop.get("parameters"), bpp,
                                 path, method)
        cp = self._gather_params(self.c, cop.get("parameters"), cpp,
                                 path, method)
        for name in sorted(set(bp) | set(cp)):
            bparam, bloc = bp.get(name, (None, None))
            cparam, cloc = cp.get(name, (None, None))
            if bparam and not cparam:
                self._add(
                    "PARAMETER_REMOVED", path, method, location="parameter",
                    floc_b=bloc,
                    message=f"参数 {name!r} ({bparam.get('in')}) 被删除",
                    witness=self._param_witness(path, method, bparam,
                                                bloc, REQUEST,
                    note="旧客户端仍会发送该参数。",
                    rule="PARAMETER_REMOVED"))
                continue
            if cparam and not bparam:
                required = _param_required(cparam)
                self._add(
                    "PARAMETER_ADDED_REQUIRED" if required
                    else "PARAMETER_ADDED_OPTIONAL",
                    path, method, location="parameter", floc_c=cloc,
                    message=f"参数 {name!r} ({cparam.get('in')}) "
                            f"{'必填' if required else '可选'} 新增",
                    witness=self._param_witness(path, method, cparam, cloc,
                                                REQUEST,
                    note="旧客户端不会发送该参数。",
                    rule="PARAMETER_ADDED_REQUIRED") if required else None)
                continue
            if bparam.get("in") != cparam.get("in"):
                self._add("PARAMETER_LOCATION_CHANGED", path, method,
                          location="parameter", floc_b=bloc, floc_c=cloc,
                          message=f"参数 {name!r} 位置 {bparam.get('in')} → "
                                  f"{cparam.get('in')}")
                continue
            br, cr = _param_required(bparam), _param_required(cparam)
            if not br and cr:
                self._add("PARAMETER_MADE_REQUIRED", path, method,
                          location="parameter", floc_b=bloc, floc_c=cloc,
                          facet="required",
                          message=f"可选参数 {name!r} 变为必填",
                          witness=self._param_witness(path, method, bparam,
                                                      bloc, REQUEST,
                          note="省略该参数的旧请求将被拒绝。",
                          rule="PARAMETER_MADE_REQUIRED"))
            elif br and not cr:
                self._add("PARAMETER_MADE_OPTIONAL", path, method,
                          location="parameter", floc_b=bloc, floc_c=cloc,
                          facet="required",
                          message=f"必填参数 {name!r} 变为可选")

            bs, bsl = _param_schema(bparam, bloc)
            cs, csl = _param_schema(cparam, cloc)
            if bs is None and cs is None:
                continue
            if bs is None or cs is None:
                self._add("NO_SCHEMA", path, method, location="parameter",
                          floc_b=bloc if bs is None else None,
                          floc_c=cloc if cs is None else None,
                          message=f"参数 {name!r} 单侧缺少 schema，无法比较")
                continue
            bn = self.b.graph.build_at(bsl[0], bsl[1]) if isinstance(bs, dict) \
                else None
            cn = self.c.graph.build_at(csl[0], csl[1]) if isinstance(cs, dict) \
                else None
            if bn is None or cn is None:
                continue
            scope = Scope(path, method, "parameter", None, None, REQUEST)
            for fc in self._compare_nodes(bn, cn, REQUEST, scope):
                self._emit_schema_finding(fc, path, method, "parameter",
                                          None, None, bn, cn, bsl, csl,
                                          param_name=name)

    def _param_witness(self, path, method, param, loc, direction, note="",
                       rule=None):
        node = None
        schema, schema_loc = _param_schema(param, loc)
        if isinstance(schema, dict):
            node = self.b.graph.build_at(schema_loc[0], schema_loc[1])
        instance = {
            "method": method.upper(),
            "path": path,
            "parameter": param.get("name"),
            "in": param.get("in"),
            "value": _minimal_param_value(param),
        }
        if node is not None and not node.invalid and not node.is_cycle:
            try:
                instance["value"] = minimal_instance(node)
            except Exception:
                pass
        return Witness(
            kind="parameter", method=method.upper(), path=path,
            parameter=param.get("name"), param_in=param.get("in"),
            direction=direction, rule=rule,
            instance=instance,
            note=note or "参数变化的最小重放载荷。")

    # -------------------------------------------------------- request body
    def _deref_container(self, ctx: _Ctx, container, inline_pointer: str = ""):
        if not isinstance(container, dict):
            return None
        if "$ref" in container:
            res = ctx.graph.resolver.resolve(container["$ref"], ctx.entry)
            if not res.ok:
                return {"_loc": FacetLoc(ctx.entry, inline_pointer),
                        "_unresolved": True}
            d = dict(res.target)
            d["_loc"] = FacetLoc(res.file, res.pointer)
            return d
        d = dict(container)
        d["_loc"] = FacetLoc(ctx.entry, inline_pointer)
        return d

    def _request_body(self, path, method, bop, cop):
        bp = f"/paths{ptr.build([path, method, 'requestBody'])}"
        cp = f"/paths{ptr.build([path, method, 'requestBody'])}"
        brb = self._deref_container(self.b, bop.get("requestBody"), bp)
        crb = self._deref_container(self.c, cop.get("requestBody"), cp)
        if brb is None and crb is None:
            return
        if brb is None:
            if crb.get("required"):
                self._add("PARAMETER_ADDED_REQUIRED", path, method,
                          location="requestBody", floc_c=crb["_loc"],
                          message="新增了必填 requestBody",
                          witness=self._body_witness(
                              path, method, crb, self.c.graph,
                              note="旧客户端不会发送 body，请求被拒绝。",
                              rule="PARAMETER_ADDED_REQUIRED"))
            return
        if crb is None:
            self._add("PARAMETER_REMOVED", path, method,
                      location="requestBody", floc_b=brb["_loc"],
                      message="requestBody 被删除",
                      witness=self._body_witness(
                          path, method, brb, self.b.graph,
                          note="旧客户端仍会发送 body。",
                          rule="PARAMETER_REMOVED"))
            return
        if not brb.get("required") and crb.get("required"):
            self._add("PARAMETER_MADE_REQUIRED", path, method,
                      location="requestBody", floc_b=brb["_loc"],
                      floc_c=crb["_loc"], facet="required",
                      message="requestBody 由可选变为必填",
                      witness=self._body_witness(
                          path, method, brb, self.b.graph,
                          note="不发送 body 的旧请求将被拒绝。",
                          rule="PARAMETER_MADE_REQUIRED"))
        self._content(path, method, "requestBody",
                      brb.get("content"), crb.get("content"),
                      brb["_loc"], crb["_loc"], REQUEST)

    def _media_removed_witness(self, path, method, location, media_type,
                               status_code, direction, media, media_loc):
        """Structured counter-example descriptor for MEDIA_REMOVED."""

        body_value = None
        schema = media.get("schema") if isinstance(media, dict) else None
        if isinstance(schema, dict):
            try:
                node = self.b.graph.build_at(
                    media_loc.file, ptr.join(media_loc.pointer, "schema"))
                if not node.invalid and not node.is_cycle:
                    body_value = minimal_instance(node)
            except Exception:
                body_value = None
        return Witness(
            kind="media", method=method.upper(), path=path,
            location=location, media_type=media_type,
            status_code=str(status_code) if status_code is not None else None,
            direction=direction, rule="MEDIA_REMOVED",
            instance={
                "method": method.upper(),
                "path": path,
                "location": location,
                "status_code": str(status_code)
                if status_code is not None else None,
                "media_type": media_type,
                "body": body_value,
            },
            note=f"媒体类型 {media_type} 不再被"
                 f"{'接受' if direction == REQUEST else '返回'}。")

    def _status_removed_witness(self, path, method, code, response_with_loc,
                                loc):
        """Structured counter-example descriptor for STATUS_REMOVED."""
        resp = {k: v for k, v in response_with_loc.items()
                if not k.startswith("_")}
        content = resp.get("content")
        media_type = None
        body_value = None
        if isinstance(content, dict) and content:
            media_type = select_body_media(resp) or next(iter(content))
            media = content.get(media_type)
            schema = media.get("schema") if isinstance(media, dict) else None
            if isinstance(schema, dict):
                try:
                    node = self.b.graph.build_at(
                        loc.file,
                        ptr.join(loc.pointer, "content", media_type, "schema"))
                    if not node.invalid and not node.is_cycle:
                        body_value = minimal_instance(node)
                except Exception:
                    body_value = None
        return Witness(
            kind="response", method=method.upper(), path=path,
            status_code=str(code), direction=RESPONSE,
            media_type=media_type, rule="STATUS_REMOVED",
            instance={
                "method": method.upper(),
                "path": path,
                "status_code": str(code),
                "media_type": media_type,
                "body": body_value,
            },
            note="针对该状态码编写分支的客户端无法匹配响应。")

    def _body_witness(self, path, method, body_with_loc, graph, note, rule):
        """Structured counter-example descriptor for a requestBody finding."""
        loc = body_with_loc.get("_loc") or FacetLoc(
            graph.entry, f"/paths{ptr.build([path, method, 'requestBody'])}")
        body = {k: v for k, v in body_with_loc.items() if not k.startswith("_")}
        media_type = select_body_media(body)
        body_value: Any = None
        if media_type is not None:
            content = body.get("content") or {}
            media = content.get(media_type)
            schema = media.get("schema") if isinstance(media, dict) else None
            if isinstance(schema, dict):
                try:
                    node = graph.build_at(
                        loc.file,
                        ptr.join(loc.pointer, "content", media_type, "schema"))
                    if not node.invalid and not node.is_cycle:
                        body_value = minimal_instance(node)
                except Exception:
                    body_value = None
        return Witness(
            kind="body", method=method.upper(), path=path,
            direction=REQUEST, media_type=media_type, rule=rule,
            instance={
                "method": method.upper(),
                "path": path,
                "media_type": media_type,
                "body": body_value,
            },
            note=note)

    # ------------------------------------------------------------- response
    def _responses(self, path, method, bop, cop):
        brs = self._deref_responses(self.b, bop.get("responses"), path, method)
        crs = self._deref_responses(self.c, cop.get("responses"), path, method)
        for code in sorted(set(brs) | set(crs), key=_status_sort_key):
            if code not in brs:
                self._add("STATUS_ADDED", path, method, location="response",
                          status_code=code,
                          floc_c=FacetLoc(
                              self.c.entry,
                              f"/paths{ptr.build([path, method, 'responses', code])}"))
                continue
            if code not in crs:
                self._add("STATUS_REMOVED", path, method, location="response",
                          status_code=code,
                          floc_b=brs[code]["_loc"],
                          witness=self._status_removed_witness(
                              path, method, code, brs[code],
                              brs[code]["_loc"]))
                continue
            self._content(path, method, "response",
                          brs[code].get("content"), crs[code].get("content"),
                          brs[code]["_loc"], crs[code]["_loc"], RESPONSE,
                          status_code=code)

    def _deref_responses(self, ctx: _Ctx, raw, path, method):
        out = {}
        if not isinstance(raw, dict):
            return out
        for code, resp in raw.items():
            if isinstance(resp, dict) and "$ref" in resp:
                res = ctx.graph.resolver.resolve(resp["$ref"], ctx.entry)
                if res.ok and isinstance(res.target, dict):
                    d = dict(res.target)
                    d["_loc"] = FacetLoc(res.file, res.pointer)
                    out[str(code)] = d
                continue
            if isinstance(resp, dict):
                d = dict(resp)
                d["_loc"] = FacetLoc(
                    ctx.entry,
                    f"/paths{ptr.build([path, method, 'responses', code])}")
                out[str(code)] = d
        return out

    # --------------------------------------------------------------- content
    def _content(self, path, method, location, bc, cc, bloc, cloc,
                 direction, status_code=None):
        bc = bc if isinstance(bc, dict) else {}
        cc = cc if isinstance(cc, dict) else {}
        for mt in sorted(set(bc) | set(cc)):
            bm, cm = bc.get(mt), cc.get(mt)
            bmloc = FacetLoc(bloc.file,
                             ptr.join(bloc.pointer, "content", mt))
            cmloc = FacetLoc(cloc.file,
                             ptr.join(cloc.pointer, "content", mt))
            if bm is None:
                self._add("MEDIA_ADDED", path, method, location=location,
                          media_type=mt, status_code=status_code,
                          floc_c=cmloc)
                continue
            if cm is None:
                self._add("MEDIA_REMOVED", path, method, location=location,
                          media_type=mt, status_code=status_code,
                          floc_b=bmloc,
                          witness=self._media_removed_witness(
                              path, method, location, mt, status_code,
                              direction, bm, bmloc))
                continue
            bs = bm.get("schema") if isinstance(bm, dict) else None
            cs = cm.get("schema") if isinstance(cm, dict) else None
            if not isinstance(bs, dict) and not isinstance(cs, dict):
                continue
            if not isinstance(bs, dict) or not isinstance(cs, dict):
                self._add("NO_SCHEMA", path, method, location=location,
                          media_type=mt, status_code=status_code,
                          floc_b=bmloc if not isinstance(bs, dict) else None,
                          floc_c=cmloc if not isinstance(cs, dict) else None,
                          message="单侧媒体类型缺少 schema，无法比较")
                continue
            bn = self.b.graph.build_at(bmloc.file,
                                       ptr.join(bmloc.pointer, "schema"))
            cn = self.c.graph.build_at(cmloc.file,
                                       ptr.join(cmloc.pointer, "schema"))
            scope = Scope(path, method, location, mt, status_code, direction)
            for fc in self._compare_nodes(bn, cn, direction, scope):
                self._emit_schema_finding(fc, path, method, location, mt,
                                          status_code, bn, cn,
                                          (bmloc.file,
                                           ptr.join(bmloc.pointer, "schema")),
                                          (cmloc.file,
                                           ptr.join(cmloc.pointer, "schema")),
                                          direction=direction)

    def _emit_schema_finding(self, fc: FCompare, path, method, location, mt,
                             status_code, bn, cn, bsl, csl,
                             param_name=None, direction=REQUEST):
        witness = self._witness(fc, bn, cn,
                                Scope(path, method, location, mt,
                                      status_code, direction, bsl, csl))
        if param_name:
            witness.kind = "parameter"
            witness.parameter = param_name
            witness.param_in = location
        self._add(fc.rule, path, method, location=location, media_type=mt,
                  status_code=status_code, floc_b=fc.floc_b,
                  floc_c=fc.floc_c, facet=fc.facet, message=fc.message,
                  witness=witness, severity=fc.severity)

    # ============================================================= schemas ==
    def _compare_nodes(self, b: SchemaNode, c: SchemaNode, direction: str,
                       scope: Scope, relpath: str = "",
                       stack: frozenset = frozenset()) -> list[FCompare]:
        key = (b.loc.file, b.loc.pointer, c.loc.file, c.loc.pointer,
               direction, relpath)
        if key in stack:
            return []
        stack = stack | {key}
        out: list[FCompare] = []

        # validity / unresolved-ref / cycles first
        if b.invalid and not b.is_cycle:
            rule = "UNRESOLVED_REF" if _is_ref_error(b.invalid) \
                else "SCHEMA_INVALID"
            return [FCompare(rule, UNDETERMINED,
                             b.facet_loc.get("$ref") or b.loc, None,
                             "$ref", f"基线侧{_invalid_phrase(b.invalid)}")]
        if c.invalid and not c.is_cycle:
            rule = "UNRESOLVED_REF" if _is_ref_error(c.invalid) \
                else "SCHEMA_INVALID"
            return [FCompare(rule, UNDETERMINED,
                             None, c.facet_loc.get("$ref") or c.loc,
                             "$ref", f"候选侧{_invalid_phrase(c.invalid)}")]
        if b.is_cycle or c.is_cycle:
            return []  # comparison of recursive tails terminates
        if b.unsupported or c.unsupported:
            for kw, loc in b.unsupported:
                out.append(FCompare("UNSUPPORTED_KEYWORD", UNDETERMINED,
                                    loc, None, kw,
                                    f"基线侧未支持关键字 {kw!r}，节点无法判定"))
            for kw, loc in c.unsupported:
                out.append(FCompare("UNSUPPORTED_KEYWORD", UNDETERMINED,
                                    None, loc, kw,
                                    f"候选侧未支持关键字 {kw!r}，节点无法判定"))
            return out

        if (b.format or c.format) and b.format != c.format:
            out.append(FCompare(
                "FORMAT_CHANGED", UNDETERMINED,
                b.facet_loc.get("format"), c.facet_loc.get("format"),
                "format",
                f"format {b.format!r} → {c.format!r}，校验强度依赖实现"))

        # const
        if b.const not in (UNSET, None) and c.const not in (UNSET, None) \
                and not _json_eq(b.const, c.const):
            out.append(FCompare(
                "TYPE_CHANGED", BREAKING,
                b.facet_loc.get("const"), c.facet_loc.get("const"),
                "const", f"常量 {_short(b.const)} → {_short(c.const)}",
                ("const", b.const), relpath))
            return out + self._compositions(b, c, direction, scope,
                                            relpath, stack)

        # enums
        enum_finds = self._compare_enums(b, c, direction, relpath)
        out.extend(enum_finds)

        # types
        type_finds = self._compare_types(b, c, direction, relpath)
        if type_finds:
            out.extend(type_finds)
            return out + self._compositions(b, c, direction, scope,
                                            relpath, stack)
        out.extend(self._compare_nullable(b, c, direction, relpath))

        if not _types_can_recurse(b, c):
            return out + self._compositions(b, c, direction, scope,
                                            relpath, stack)

        primary = _primary_type(c) or _primary_type(b)
        if primary == "object":
            out.extend(self._compare_object(b, c, direction, scope, relpath,
                                            stack))
        elif primary == "array":
            out.extend(self._compare_array(b, c, direction, scope, relpath,
                                           stack))
        elif primary == "string":
            out.extend(self._compare_string(b, c, direction, relpath))
        elif primary in ("integer", "number"):
            out.extend(self._compare_number(b, c, direction, relpath))

        out.extend(self._compositions(b, c, direction, scope, relpath, stack))
        return out

    def _compare_enums(self, b, c, direction, relpath):
        out = []
        if b.enum is not None and c.enum is not None:
            removed = [v for v in b.enum if not _member(v, c.enum)]
            added = [v for v in c.enum if not _member(v, b.enum)]
            if removed:
                out.append(FCompare(
                    "ENUM_NARROWED", BREAKING,
                    b.facet_loc.get("enum"), c.facet_loc.get("enum"),
                    "enum", f"删除枚举值 {_short(removed)}",
                    ("enum", removed[0]), relpath))
            if added and direction == RESPONSE:
                out.append(FCompare(
                    "ENUM_WIDENED", BREAKING,
                    b.facet_loc.get("enum"), c.facet_loc.get("enum"),
                    "enum", f"新增枚举值 {_short(added)}",
                    ("enum_widen", added[0]), relpath))
        elif b.enum is None and c.enum is not None:
            # baseline open, candidate restricted
            if direction == REQUEST:
                # pick a value outside candidate enum (string/int fallback)
                out.append(FCompare(
                    "ENUM_NARROWED", BREAKING,
                    b.facet_loc.get("enum"), c.facet_loc.get("enum"),
                    "enum", "新增枚举约束，基线允许的非枚举值将被拒绝",
                    ("enum_new", c.enum, b), relpath))
            # response: restricting output is fine
        elif b.enum is not None and c.enum is None:
            if direction == RESPONSE:
                out.append(FCompare(
                    "ENUM_WIDENED", BREAKING,
                    b.facet_loc.get("enum"), c.facet_loc.get("enum"),
                    "enum", "删除枚举约束，响应值域变为无界",
                    ("enum_drop", b.enum[0] if b.enum else None, c),
                    relpath))
            # request: accepting more is fine
        return out

    def _compare_types(self, b, c, direction, relpath):
        bt, ct = _type_set(b), _type_set(c)
        if bt == ct:
            return []
        only_b, only_c = bt - ct, ct - bt
        if not only_b and only_c and bt:
            # candidate is a strict superset
            if direction == RESPONSE:
                t = sorted(only_c)[0]
                return [FCompare(
                    "TYPE_WIDENED", BREAKING,
                    b.facet_loc.get("type"), c.facet_loc.get("type"),
                    "type", f"类型放宽，新增 {sorted(only_c)}",
                    ("type_widen", t), relpath)]
            return []
        if only_b and not only_c and ct:
            t = sorted(only_b)[0]
            return [FCompare(
                "TYPE_NARROWED", BREAKING,
                b.facet_loc.get("type"), c.facet_loc.get("type"),
                "type", f"类型收窄，删除 {sorted(only_b)}",
                ("type_narrow", t), relpath)]
        if only_b and only_c:
            t = sorted(only_b)[0]
            return [FCompare(
                "TYPE_CHANGED", BREAKING,
                b.facet_loc.get("type"), c.facet_loc.get("type"),
                "type", f"类型 {sorted(bt)} → {sorted(ct)}",
                ("type_change", t), relpath)]
        # one side untyped -> treat as compatible but flag undetermined only
        # when the typed side has exotic multiple types
        return []

    def _compare_nullable(self, b, c, direction, relpath):
        bn, cn = "null" in _type_set(b), "null" in _type_set(c)
        if bn and not cn:
            return [FCompare("NULLABLE_REMOVED", BREAKING,
                             b.facet_loc.get("nullable")
                             or b.facet_loc.get("type"),
                             c.facet_loc.get("nullable")
                             or c.facet_loc.get("type"),
                             "nullable", "不再允许 null",
                             ("nullable_removed", None), relpath)]
        if not bn and cn and direction == RESPONSE:
            return [FCompare("NULLABLE_ADDED", BREAKING,
                             b.facet_loc.get("nullable")
                             or b.facet_loc.get("type"),
                             c.facet_loc.get("nullable")
                             or c.facet_loc.get("type"),
                             "nullable", "现在允许 null，旧客户端可能空指针",
                             ("nullable_added", None), relpath)]
        return []

    # --------------------------------------------------------------- object
    def _compare_object(self, b, c, direction, scope, relpath, stack):
        out = []
        bp = self._filter_props(b.properties, direction)
        cp = self._filter_props(c.properties, direction)
        badd, cadd = b.additional, c.additional
        for name in sorted(set(bp) | set(cp)):
            child_rel = f"{relpath}/{_esc(name)}"
            bn, cn = bp.get(name), cp.get(name)
            if bn and not cn:
                if direction == REQUEST and cadd is not False:
                    continue  # still accepted (open object)
                floc = b.facet_loc.get(
                    f"property:{name}",
                    FacetLoc(b.loc.file,
                             ptr.join(b.loc.pointer, "properties", name)))
                suffix = ("（additionalProperties=false，旧请求被拒绝）"
                          if direction == REQUEST else "，旧客户端读不到该字段")
                out.append(FCompare(
                    "PROPERTY_REMOVED", BREAKING, floc,
                    c.facet_loc.get("additionalProperties"),
                    f"property:{name}", f"属性 {name!r} 被删除{suffix}",
                    ("prop_removed", name), child_rel))
                continue
            if cn and not bn:
                floc = c.facet_loc.get(
                    f"property:{name}",
                    FacetLoc(c.loc.file,
                             ptr.join(c.loc.pointer, "properties", name)))
                if name in c.required and direction == REQUEST:
                    out.append(FCompare(
                        "REQUIRED_ADDED", BREAKING,
                        c.facet_loc.get(f"required:{name}"), floc,
                        f"required:{name}",
                        f"新增必填属性 {name!r}，旧请求缺少该字段",
                        ("required_added", name), child_rel))
                elif name in c.required and direction == RESPONSE:
                    out.append(FCompare(
                        "REQUIRED_ADDED", COMPATIBLE, None,
                        c.facet_loc.get(f"required:{name}"),
                        f"required:{name}",
                        f"响应新增必有属性 {name!r}（兼容增强）",
                        None, child_rel))
                continue
            # both present
            if name in b.required and name not in c.required \
                    and direction == RESPONSE:
                out.append(FCompare(
                    "REQUIRED_REMOVED", BREAKING,
                    b.facet_loc.get(f"required:{name}",
                                    FacetLoc(b.loc.file,
                                             ptr.join(b.loc.pointer,
                                                      "required"))),
                    c.facet_loc.get(f"required:{name}"),
                    f"required:{name}",
                    f"响应属性 {name!r} 由必有变为可选",
                    ("required_removed", name), child_rel))
            if name not in b.required and name in c.required \
                    and direction == REQUEST:
                out.append(FCompare(
                    "REQUIRED_ADDED", BREAKING,
                    b.facet_loc.get(f"required:{name}"),
                    c.facet_loc.get(f"required:{name}",
                                    FacetLoc(c.loc.file,
                                             ptr.join(c.loc.pointer,
                                                      "required"))),
                    f"required:{name}",
                    f"请求属性 {name!r} 变为必填，旧请求将被拒绝",
                    ("required_added", name), child_rel))
            out.extend(self._compare_nodes(bn, cn, direction,
                                           scope,
                                           child_rel, stack))

        # additionalProperties
        if badd is not UNSET and cadd is not UNSET:
            if badd is not False and cadd is False and direction == REQUEST:
                out.append(FCompare(
                    "ADDPROPS_FALSE_ADDED", BREAKING,
                    b.facet_loc.get("additionalProperties"),
                    c.facet_loc.get("additionalProperties"),
                    "additionalProperties",
                    "additionalProperties 变为 false，含额外字段的旧请求被拒绝",
                    ("addprops_false", None), relpath))
            elif badd is False and cadd is not False and \
                    direction == RESPONSE:
                out.append(FCompare(
                    "ADDPROPS_FALSE_REMOVED", BREAKING,
                    b.facet_loc.get("additionalProperties"),
                    c.facet_loc.get("additionalProperties"),
                    "additionalProperties",
                    "解除 additionalProperties:false，响应可携带任意额外字段",
                    ("addprops_unfalse", None), relpath))
            elif isinstance(badd, SchemaNode) and isinstance(cadd, SchemaNode):
                for f in self._compare_nodes(
                        badd, cadd, direction,
                        scope,
                        f"{relpath}/*", stack):
                    if f.severity == UNDETERMINED:
                        out.append(FCompare(
                            "UNSUPPORTED_KEYWORD", UNDETERMINED,
                            f.floc_b, f.floc_c, "additionalProperties",
                            f"附加属性模式无法判定: {f.message}",
                            None, f.relpath))
                    else:
                        out.append(FCompare(
                            "ADDPROPS_SCHEMA_BROKEN", f.severity,
                            f.floc_b, f.floc_c, "additionalProperties",
                            f"附加属性模式: {f.message}",
                            f.witness_hint, f.relpath))
        elif badd is UNSET and cadd is False and direction == REQUEST:
            out.append(FCompare(
                "ADDPROPS_FALSE_ADDED", BREAKING, None,
                c.facet_loc.get("additionalProperties"),
                "additionalProperties",
                "新增 additionalProperties:false，含额外字段的旧请求被拒绝",
                ("addprops_false", None), relpath))
        return out

    @staticmethod
    def _filter_props(props, direction):
        out = {}
        for name, node in props.items():
            if direction == REQUEST and node.read_only:
                continue
            if direction == RESPONSE and node.write_only:
                continue
            out[name] = node
        return out

    # ---------------------------------------------------------------- array
    def _compare_array(self, b, c, direction, scope, relpath, stack):
        out = []
        if (b.min_items or 0) < (c.min_items or 0):
            out.append(FCompare(
                "LENGTH_BOUNDS_NARROWED", BREAKING,
                b.facet_loc.get("minItems"), c.facet_loc.get("minItems"),
                "minItems",
                f"minItems {b.min_items or 0} → {c.min_items}",
                ("min_items", b.min_items or 0), relpath))
        bmx, cmx = b.max_items, c.max_items
        if bmx is None and cmx is not None and direction == REQUEST:
            out.append(FCompare(
                "LENGTH_BOUNDS_NARROWED", BREAKING,
                b.facet_loc.get("maxItems"), c.facet_loc.get("maxItems"),
                "maxItems", f"新增 maxItems={cmx}",
                ("max_items_new", cmx, b), relpath))
        elif bmx is not None and cmx is None and direction == RESPONSE:
            out.append(FCompare(
                "LENGTH_BOUNDS_WIDENED", BREAKING,
                b.facet_loc.get("maxItems"), c.facet_loc.get("maxItems"),
                "maxItems", f"删除 maxItems={bmx}，响应数组长度无上限",
                None, relpath))
        elif bmx is not None and cmx is not None and bmx != cmx:
            if cmx < bmx and direction == REQUEST:
                out.append(FCompare(
                    "LENGTH_BOUNDS_NARROWED", BREAKING,
                    b.facet_loc.get("maxItems"), c.facet_loc.get("maxItems"),
                    "maxItems", f"maxItems {bmx} → {cmx}",
                    ("max_items", cmx + 1, b), relpath))
            elif cmx > bmx and direction == RESPONSE:
                out.append(FCompare(
                    "LENGTH_BOUNDS_WIDENED", BREAKING,
                    b.facet_loc.get("maxItems"), c.facet_loc.get("maxItems"),
                    "maxItems", f"maxItems {bmx} → {cmx}", None, relpath))
        if b.items and c.items:
            nested = self._compare_nodes(
                b.items, c.items, direction,
                scope,
                f"{relpath}/0", stack)
            for f in nested:
                if f.severity == BREAKING:
                    out.append(FCompare(
                        "ITEMS_SCHEMA_CHANGED", BREAKING,
                        f.floc_b, f.floc_c, "items",
                        f"数组元素: {f.message}", f.witness_hint, f.relpath))
                else:
                    out.append(f)
        return out

    # --------------------------------------------------------------- string
    def _compare_string(self, b, c, direction, relpath=""):
        out = []
        if (b.min_length or 0) < (c.min_length or 0):
            out.append(FCompare(
                "LENGTH_BOUNDS_NARROWED", BREAKING,
                b.facet_loc.get("minLength"), c.facet_loc.get("minLength"),
                "minLength",
                f"minLength {b.min_length or 0} → {c.min_length}",
                ("min_length", b.min_length or 0), relpath))
        bmx, cmx = b.max_length, c.max_length
        if bmx is None and cmx is not None and direction == REQUEST:
            out.append(FCompare(
                "LENGTH_BOUNDS_NARROWED", BREAKING,
                b.facet_loc.get("maxLength"), c.facet_loc.get("maxLength"),
                "maxLength", f"新增 maxLength={cmx}",
                ("max_length_new", cmx), relpath))
        elif bmx is not None and cmx is None and direction == RESPONSE:
            out.append(FCompare(
                "LENGTH_BOUNDS_WIDENED", BREAKING,
                b.facet_loc.get("maxLength"), c.facet_loc.get("maxLength"),
                "maxLength", f"删除 maxLength={bmx}，响应长度无上限",
                None, relpath))
        elif bmx is not None and cmx is not None and bmx != cmx:
            if cmx < bmx and direction == REQUEST:
                out.append(FCompare(
                    "LENGTH_BOUNDS_NARROWED", BREAKING,
                    b.facet_loc.get("maxLength"), c.facet_loc.get("maxLength"),
                    "maxLength", f"maxLength {bmx} → {cmx}",
                    ("max_length", cmx + 1), relpath))
            elif cmx > bmx and direction == RESPONSE:
                out.append(FCompare(
                    "LENGTH_BOUNDS_WIDENED", BREAKING,
                    b.facet_loc.get("maxLength"), c.facet_loc.get("maxLength"),
                    "maxLength", f"maxLength {bmx} → {cmx}", None, relpath))
        return out

    # --------------------------------------------------------------- number
    def _compare_number(self, b, c, direction, relpath=""):
        out = []
        blo, bhi = _eff_bounds(b)
        clo, chi = _eff_bounds(c)
        if clo is not None and (blo is None or clo > blo):
            out.append(FCompare(
                "NUMBER_BOUNDS_NARROWED", BREAKING,
                b.facet_loc.get("minimum") or b.facet_loc.get(
                    "exclusiveMinimum"),
                c.facet_loc.get("minimum") or c.facet_loc.get(
                    "exclusiveMinimum"),
                "minimum", f"下界抬高 {blo} → {clo}",
                ("num_low", blo, b, c), relpath))
        if chi is not None and (bhi is None or chi < bhi):
            out.append(FCompare(
                "NUMBER_BOUNDS_NARROWED", BREAKING,
                b.facet_loc.get("maximum") or b.facet_loc.get(
                    "exclusiveMaximum"),
                c.facet_loc.get("maximum") or c.facet_loc.get(
                    "exclusiveMaximum"),
                "maximum", f"上界压低 {bhi} → {chi}",
                ("num_high", bhi, b, c), relpath))
        if direction == RESPONSE:
            if blo is not None and (clo is None or clo < blo):
                out.append(FCompare(
                    "NUMBER_BOUNDS_WIDENED", BREAKING,
                    b.facet_loc.get("minimum"),
                    c.facet_loc.get("minimum"), "minimum",
                    f"下界放宽 {blo} → {clo}",
                    ("num_widen_lo", blo, b, c), relpath))
            if bhi is not None and (chi is None or chi > bhi):
                out.append(FCompare(
                    "NUMBER_BOUNDS_WIDENED", BREAKING,
                    b.facet_loc.get("maximum"),
                    c.facet_loc.get("maximum"), "maximum",
                    f"上界放宽 {bhi} → {chi}",
                    ("num_widen_hi", bhi, b, c), relpath))
        if b.multiple_of is None and c.multiple_of is not None:
            out.append(FCompare(
                "MULTIPLE_OF_ADDED", BREAKING,
                b.facet_loc.get("multipleOf"),
                c.facet_loc.get("multipleOf"), "multipleOf",
                f"新增 multipleOf={c.multiple_of}",
                ("multiple_of", c.multiple_of, b), relpath))
        elif b.multiple_of is not None and c.multiple_of is not None and \
                b.multiple_of != c.multiple_of:
            out.append(FCompare(
                "MULTIPLE_OF_CHANGED", BREAKING,
                b.facet_loc.get("multipleOf"),
                c.facet_loc.get("multipleOf"), "multipleOf",
                f"multipleOf {b.multiple_of} → {c.multiple_of}",
                ("multiple_of", c.multiple_of, b), relpath))
        return out

    # ---------------------------------------------------------- composition
    def _compositions(self, b, c, direction, scope, relpath, stack):
        bb = b.one_of or b.any_of
        cc = c.one_of or c.any_of
        if not bb and not cc:
            return []
        bkind = "oneOf" if b.one_of else "anyOf"
        ckind = "oneOf" if c.one_of else "anyOf"
        bloc = b.facet_loc.get(bkind)
        cloc = c.facet_loc.get(ckind)
        out = []
        if bb and cc and bkind != ckind:
            return [FCompare("ONEOF_UNMATCHED_STRUCTURE", UNDETERMINED,
                             bloc, cloc, bkind,
                             f"组合方式 {bkind} → {ckind}，无法自动配对分支")]
        kind = bkind if bb else ckind
        if bb and not cc:
            return [FCompare("ONEOF_BRANCH_REMOVED", BREAKING,
                             bloc, cloc, kind,
                             f"{kind} 的全部 {len(bb)} 个分支被删除",
                             ("oneof_removed_branch", bb[0]), relpath)]
        if cc and not bb:
            if direction == RESPONSE:
                return [FCompare("ONEOF_BRANCH_ADDED", BREAKING,
                                 bloc, cloc, kind,
                                 f"{kind} 新增 {len(cc)} 个分支",
                                 ("oneof_added_branch", cc[0]), relpath)]
            return []
        pairs, removed, added = _match_branches(bb, cc)
        if removed:
            gone = [bb[i] for i in removed]
            out.append(FCompare(
                "ONEOF_BRANCH_REMOVED", BREAKING, bloc, cloc, kind,
                f"{kind} 删除无法配对的分支 #{removed}",
                ("oneof_removed_branch", gone[0]), relpath))
        if added and direction == RESPONSE:
            new_branch = cc[added[0]]
            out.append(FCompare(
                "ONEOF_BRANCH_ADDED", BREAKING, bloc, cloc, kind,
                f"{kind} 新增无法配对的分支 #{added}",
                ("oneof_added_branch", new_branch), relpath))
        if (removed or added) and not pairs:
            out.append(FCompare(
                "ONEOF_UNMATCHED_STRUCTURE", UNDETERMINED, bloc, cloc, kind,
                f"{kind} 分支指纹无法可靠配对（基线 {len(bb)} / 候选 {len(cc)}）",
                None, relpath))
        for bi, ci in pairs:
            nested = self._compare_nodes(
                bb[bi], cc[ci], direction, scope, relpath, stack)
            for f in nested:
                wrap_rule = "ONEOF_BRANCH_INCOMPATIBLE" \
                    if f.severity == BREAKING else f.rule
                out.append(FCompare(
                    wrap_rule, f.severity,
                    f.floc_b or FacetLoc(
                        bb[bi].loc.file,
                        ptr.join(b.loc.pointer, kind, bi)),
                    f.floc_c or FacetLoc(
                        cc[ci].loc.file,
                        ptr.join(c.loc.pointer, kind, ci)),
                    f.facet or f"{kind}[{bi}]",
                    f"{kind}[{bi}]: {f.message}",
                    f.witness_hint, f.relpath))
        return out

    # ============================================================== witness
    def _witness(self, fc: FCompare, bn: SchemaNode, cn: SchemaNode,
                 scope: Scope) -> Witness:
        instance, note = build_witness(fc, bn, cn, scope.direction)
        # 三态自检：确定通过 / 确定失败 / 待定。一侧仍依赖未支持的
        # 关键字（如 not / pattern）或循环引用时，该侧只是「待定」——
        # 例如基线 oneOf 一支对反例确定通过、另一支含未支持的 not，
        # 是否「恰好一支通过」无法定论，不得标注为已验证的单侧通过。
        cb = check_tri(bn, instance, reenter=reenter_for(self.b.graph))
        cc = check_tri(cn, instance, reenter=reenter_for(self.c.graph))
        sb, sc = cb.state, cc.state
        one_sided = (sb == TRI_PASS and sc == TRI_FAIL) or \
                    (sb == TRI_FAIL and sc == TRI_PASS)
        if sb == TRI_PASS and sc == TRI_FAIL:
            passing_side = "baseline"
            side = "基线"
        elif sb == TRI_FAIL and sc == TRI_PASS:
            passing_side = "candidate"
            side = "候选"
        else:
            passing_side = "both" if sb == TRI_PASS and sc == TRI_PASS \
                else "neither" if sb == TRI_FAIL and sc == TRI_FAIL \
                else "undetermined"
            side = ""
        pending_reasons = format_pending_hits(
            cb.hits, self.b.entry) + format_pending_hits(cc.hits, self.c.entry)
        # 兼容既有布尔字段：待定无法用 true/false 表达 → null；
        # one_side_only 仅在两侧都确定、且恰好一侧通过时为 true。
        def _valid(state):
            return True if state == TRI_PASS else (
                False if state == TRI_FAIL else None)
        self_check = {
            "baseline_valid": _valid(sb),
            "candidate_valid": _valid(sc),
            "baseline_state": sb,
            "candidate_state": sc,
            "one_side_only": one_sided,
            "passing_side": passing_side,
            "verifiable": one_sided,
            "pending_reasons": pending_reasons,
            "baseline_errors": cb.errors,
            "candidate_errors": cc.errors,
        }
        if one_sided:
            note = f"{note}  自检：{side}侧确定通过、另一侧确定失败。"
        elif pending_reasons:
            note = (f"{note}  ⚠ 自检：反例并非已验证的单侧通过——"
                    f"{'；'.join(pending_reasons)}。破坏性结论保留，"
                    "但此反例标记为待定，可在页面编辑器中修改后重放。")
        else:
            note = f"{note}  ⚠ 自检：基线={_state_zh(sb)} 候选={_state_zh(sc)}，" \
                   "无法构造严格单侧反例（结构限制），此例用于定位。"
        w = Witness(
            kind="schema", note=note, instance=instance,
            method=scope.method or None,
            path=scope.path or None,
            location=scope.location,
            media_type=scope.media_type, status_code=scope.status_code,
            direction=scope.direction,
            facet_pointer_baseline=self.b.hint(fc.floc_b),
            facet_pointer_candidate=self.c.hint(fc.floc_c),
            replay_path=fc.relpath or "",
            self_check=self_check)
        w.baseline = {
            "root_file": scope.schema_loc_b[0] if scope.schema_loc_b
            else self.b.entry,
            "root_pointer": scope.schema_loc_b[1] if scope.schema_loc_b
            else None,
        }
        w.candidate = {
            "root_file": scope.schema_loc_c[0] if scope.schema_loc_c
            else self.c.entry,
            "root_pointer": scope.schema_loc_c[1] if scope.schema_loc_c
            else None,
        }
        return w

    # ------------------------------------------------------------- finalize
    def _finalize(self) -> Report:
        # endpoint reports
        endpoints = []
        for (path, method), opid in self.ep_opid.items():
            ep = EndpointReport(path=path, method=method, operation_id=opid,
                                severity=self.ep_sev.get(
                                    (path, method), COMPATIBLE))
            endpoints.append(ep)
        endpoints.sort(key=lambda e: (e.path, e.method))

        # sort findings deterministically
        self.findings.sort(key=lambda f: (
            f.path, f.method, _LOC_ORDER.get(f.location, 3),
            f.status_code or "", f.media_type or "",
            _SEV_ORDER[f.severity], f.rule,
            f.pointer_baseline or "", f.pointer_candidate or "",
            f.facet or ""))
        # ids are content-based already; build endpoint -> ids mapping
        ep_by_key = {(e.path, e.method): e for e in endpoints}
        for f in self.findings:
            ep = ep_by_key.get((f.path, f.method))
            if ep:
                ep.finding_ids.append(f.id)

        stats = {
            "cycles_terminated":
                self.b.graph.stats.cycle_terminations +
                self.c.graph.stats.cycle_terminations,
            "unresolved_refs":
                self.b.graph.stats.unresolved_refs +
                self.c.graph.stats.unresolved_refs,
            "unsupported_keywords":
                self.b.graph.stats.unsupported_keywords +
                self.c.graph.stats.unsupported_keywords,
            "invalid_schemas":
                self.b.graph.stats.invalid_schemas +
                self.c.graph.stats.invalid_schemas,
            "finding_breaking":
                sum(1 for f in self.findings if f.severity == BREAKING),
            "finding_undetermined":
                sum(1 for f in self.findings if f.severity == UNDETERMINED),
            "finding_compatible":
                sum(1 for f in self.findings if f.severity == COMPATIBLE),
        }
        return Report(findings=self.findings, endpoints=endpoints,
                      stats=stats)


# ===================================================================== helpers
def _state_zh(state: str) -> str:
    return {TRI_PASS: "确定通过", TRI_FAIL: "确定失败",
            TRI_PENDING: "待定"}.get(state, state)


def _param_required(param: dict) -> bool:
    if param.get("in") == "path":
        return True
    return bool(param.get("required"))


def _param_schema(param: dict, loc: FacetLoc):
    """Return (raw_schema, (file, pointer)) or (None, None)."""
    if isinstance(param.get("schema"), dict):
        return param["schema"], (loc.file, ptr.join(loc.pointer, "schema"))
    content = param.get("content")
    if isinstance(content, dict) and content:
        mt = next(iter(content))
        media = content[mt]
        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
            return media["schema"], (
                loc.file,
                ptr.join(loc.pointer, "content", mt, "schema"))
    return None, None


def _minimal_param_value(param: dict):
    schema = param.get("schema")
    if isinstance(schema, dict):
        t = schema.get("type")
        if t in ("integer", "number"):
            return schema.get("minimum", 1)
        if t == "boolean":
            return True
        if t == "array":
            return []
        if isinstance(schema.get("enum"), list) and schema["enum"]:
            return schema["enum"][0]
    return "x"


def _type_set(node: SchemaNode) -> set[str]:
    if node.type is None:
        return set()
    t = set(node.type)
    if node.nullable:
        t.add("null")
    if "integer" in t:
        t.add("number")
    return t


def _primary_type(node: SchemaNode) -> Optional[str]:
    if node.type:
        for t in node.type:
            if t != "null":
                return t
    if node.properties:
        return "object"
    if node.items is not None:
        return "array"
    return None


def _types_can_recurse(b: SchemaNode, c: SchemaNode) -> bool:
    bt = _type_set(b) - {"null"}
    ct = _type_set(c) - {"null"}
    if not bt or not ct:
        return True
    return bool(bt & ct)


def _eff_bounds(node: SchemaNode):
    lo = node.minimum
    if node.ex_min is not None:
        lo = node.ex_min if lo is None else max(lo, node.ex_min)
    hi = node.maximum
    if node.ex_max is not None:
        hi = node.ex_max if hi is None else min(hi, node.ex_max)
    return lo, hi


def _member(value, values) -> bool:
    key = _json_key(value)
    return any(_json_key(v) == key for v in values)


def _json_key(v) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)


def _json_eq(a, b) -> bool:
    return _json_key(a) == _json_key(b)


def _short(v, limit=3) -> str:
    try:
        if isinstance(v, list) and len(v) > limit:
            shown = json.dumps(v[:limit], ensure_ascii=False, default=str)
            return shown[:-1] + ", ...]"
        s = json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        s = str(v)
    return s if len(s) <= 100 else s[:97] + "..."


def _esc(t: str) -> str:
    return t.replace("~", "~0").replace("/", "~1")


def _status_sort_key(code: str):
    try:
        return (0, int(code))
    except ValueError:
        return (1, code)


_REF_ERROR_MARKERS = ("$ref", "引用", "JSON Pointer", "外部", "包内", "目标不存在")


def _is_ref_error(message: str) -> bool:
    return any(m in message for m in _REF_ERROR_MARKERS)


def _invalid_phrase(message: str) -> str:
    if _is_ref_error(message):
        return f"存在无法解析的引用: {message}"
    return f"模式无效: {message}"


def _match_branches(blist, clist):
    bf = {}
    for i, n in enumerate(blist):
        bf.setdefault(_fingerprint(n), i)
    cf = {}
    for i, n in enumerate(clist):
        cf.setdefault(_fingerprint(n), i)
    pairs = []
    used_b, used_c = set(), set()
    for sig, bi in bf.items():
        if sig in cf:
            pairs.append((bi, cf[sig]))
            used_b.add(bi)
            used_c.add(cf[sig])
    rem_b = [i for i in range(len(blist)) if i not in used_b]
    rem_c = [i for i in range(len(clist)) if i not in used_c]
    if rem_b and rem_c and len(rem_b) == len(rem_c):
        for bi, ci in zip(sorted(rem_b), sorted(rem_c)):
            pairs.append((bi, ci))
        rem_b, rem_c = [], []
    pairs.sort()
    return pairs, rem_b, rem_c


def _fingerprint(node: SchemaNode) -> str:
    t = tuple(sorted(_type_set(node))) or ("any",)
    parts = ["type=" + ",".join(t),
             "props=" + ",".join(sorted(node.properties)),
             "req=" + ",".join(sorted(node.required)),
             f"one={len(node.one_of)}", f"any={len(node.any_of)}"]
    if node.items is not None:
        parts.append("items")
    if node.enum is not None:
        parts.append("enum=" + _json_key(
            sorted(node.enum, key=lambda v: _json_key(v))))
    return "|".join(parts)


# ========================================================= witness synthesis
def build_witness(fc: FCompare, bn: SchemaNode, cn: SchemaNode,
                  direction: str) -> tuple[Any, str]:
    """Construct a minimal document that validates on one side only.

    The *favorable* root schema is the side the counter-example must satisfy:

    * request narrowing  -> baseline (an old-client request accepted today
      must be rejected by the candidate);
    * response widening   -> candidate (the upgraded server may emit a value
      an old client would reject).

    The offending leaf value is embedded by recursively generating minimal,
    valid ancestor containers along the finding's relative path, so the
    resulting document is valid on the favorable side and invalid on the
    other side precisely at the reported facet.
    """
    hint = fc.witness_hint

    if hint is None:
        if fc.rule == "ONEOF_BRANCH_REMOVED":
            return _branch_example(bn, bn, cn, baseline=True), \
                   "仅匹配基线分支的示例（候选分支已删除）。"
        if fc.rule == "ONEOF_BRANCH_ADDED":
            return _branch_example(cn, bn, cn, baseline=False), \
                   "仅匹配候选新增分支的示例（基线无法识别该形态）。"
        return _safe_minimal(bn), fc.message

    kind = hint[0]
    relpath = fc.relpath or ""
    tokens = _tokens(relpath)

    # kinds whose counter-example must validate on the BASELINE side
    baseline_kinds = {
        "enum", "enum_new", "const", "nullable_removed", "type_narrow",
        "type_change", "required_added", "prop_removed", "addprops_false",
        "num_low", "num_high", "min_length", "max_length",
        "max_length_new", "min_items", "max_items", "max_items_new",
        "multiple_of",
    }
    # handled explicitly above: oneof_added_branch / oneof_removed_branch
    # everything else (enum_widen, nullable_added, type_widen,
    # required_removed, addprops_unfalse, enum_drop, oneof_added...)
    # favors the CANDIDATE side.
    favor_baseline = kind in baseline_kinds
    favor_node = bn if favor_baseline else cn

    # ---- omission-based witnesses (no injection path) ----
    if kind == "required_added":
        name = hint[1]
        doc = _safe_minimal(bn)
        return _omit_at(doc, tokens, name), \
               f"旧客户端最小载荷不携带新必填字段 {name!r}。"
    if kind == "required_removed":
        name = hint[1]
        doc = _safe_minimal(cn)
        return _omit_at(doc, tokens, name), \
               f"候选允许省略 {name!r}，仅候选侧校验通过。"
    if kind == "prop_removed":
        name = hint[1]
        child = _safe_minimal(bn.properties.get(name))
        doc = _set_at(_safe_minimal(bn), tokens, name, child)
        return doc, f"按基线构造、使用已删除字段 {name!r} 的载荷。"
    if kind == "addprops_false":
        ename, evalue = _extra_field(bn)
        doc = _set_at(_safe_minimal(bn), tokens, ename, evalue)
        return doc, f"含额外字段 {ename!r} 的旧请求被候选拒绝。"
    if kind == "addprops_unfalse":
        ename, evalue = _extra_field(cn)
        doc = _set_at(_safe_minimal(cn), tokens, ename, evalue)
        return doc, "候选可携带基线不允许的额外字段，仅候选侧通过。"

    # ---- branch composition witnesses need branch-aware embedding ----
    if kind == "oneof_added_branch":
        branch = hint[1]
        leaf = _safe_minimal(branch)
        return _embed_leaf(cn, tokens, leaf), \
               "仅匹配候选新增分支的形态（基线无此分支）。"
    if kind == "oneof_removed_branch":
        branch = hint[1]
        leaf = _safe_minimal(branch)
        return _embed_leaf(bn, tokens, leaf), \
               "仅匹配基线分支的形态（候选已删除该分支）。"

    # ---- leaf-value witnesses ----
    leaf, note = _leaf_value(kind, hint, bn, cn)
    if leaf is _LEAF_MISSING:
        return _safe_minimal(favor_node), note or fc.message

    return _embed_leaf(favor_node, tokens, leaf), note


_LEAF_MISSING = object()


def _leaf_value(kind, hint, bn, cn):
    """Return (value, note) for the offending leaf, or _LEAF_MISSING."""
    if kind == "enum":
        val = hint[1]
        return val, f"基线允许的枚举值 {_short(val)} 在候选中被删除。"
    if kind == "enum_widen":
        val = hint[1]
        return val, f"候选新增枚举值 {_short(val)}，旧客户端无法识别。"
    if kind == "enum_new":
        _, values, base_node = hint
        val = _value_outside_enum(values, base_node)
        return val, "基线接受该值（无枚举约束），候选枚举拒绝该值。"
    if kind == "enum_drop":
        old_values = bn.enum or []
        val = _value_outside_enum(old_values, cn)
        return val, f"候选允许的新值 {_short(val)} 不在基线枚举中。"
    if kind == "const":
        val = hint[1]
        return val, f"基线常量 {_short(val)} 与候选不一致。"
    if kind == "nullable_removed":
        return None, "基线允许 null，候选拒绝 null。"
    if kind == "nullable_added":
        return None, "候选允许 null（旧客户端未防御），仅候选侧通过。"
    if kind == "type_widen":
        t = hint[1]
        return _value_for_type(t, cn), \
               f"候选新增类型 {t} 的示例，仅候选侧通过。"
    if kind == "type_narrow" or kind == "type_change":
        t = hint[1]
        return _value_for_type(t, bn), \
               f"基线允许的类型 {t} 的值被候选拒绝。"
    if kind in ("num_low", "num_high"):
        _, val, b_node, c_node = hint
        v = val if val is not None else _small_number(b_node)
        return v, f"基线接受的数值 {v} 被候选拒绝。"
    if kind in ("num_widen_lo", "num_widen_hi"):
        _, old_bound, b_node, c_node = hint
        v = _bound_violating_value(kind, old_bound, c_node)
        return v, f"候选允许的数值 {v} 超出基线上界/下界（仅候选侧通过）。"
    if kind == "min_length":
        n = hint[1]
        return "x" * n, f"长度 {n} 的字符串满足基线、违反候选 minLength。"
    if kind == "max_length":
        n = hint[1]
        return "x" * n, f"长度 {n} 的字符串满足基线、违反候选 maxLength。"
    if kind == "max_length_new":
        n = hint[1]
        return "x" * (n + 1), f"长度 {n + 1} 违反候选新增 maxLength={n}。"
    if kind == "min_items":
        n = hint[1]
        return [_array_elem(bn)] * n, \
               f"含 {n} 个元素的数组满足基线、违反候选 minItems。"
    if kind == "max_items" or kind == "max_items_new":
        n = hint[1]
        b_node = hint[2]
        return [_array_elem(b_node)] * min(max(n, 1) + 1, 6), \
               "数组元素数超过候选 maxItems。"
    if kind == "multiple_of":
        m, b_node = hint[1], hint[2]
        v = _non_multiple(m, b_node)
        return v, f"数值 {v} 不满足候选 multipleOf={m}。"
    return _LEAF_MISSING, ""


def _new_branch_leaf(favor_node, other_node):
    """Minimal instance of a branch unique to the favorable side."""
    favor_branches = favor_node.one_of or favor_node.any_of or []
    other_sigs = {_fingerprint(x)
                  for x in (other_node.one_of or other_node.any_of or [])}
    unique = [br for br in favor_branches
              if _fingerprint(br) not in other_sigs]
    target = unique[0] if unique else (favor_branches[0]
                                       if favor_branches else favor_node)
    return _safe_minimal(target)


def _embed_leaf(root_node, tokens, leaf):
    """Build a minimal favorable document with ``leaf`` at ``tokens``.

    Each ancestor container is generated minimally (and hence valid) on the
    favorable side; array indices produce minimally valid sibling items;
    object ancestors keep only required keys plus the targeted property.
    When ``tokens`` is empty the leaf IS the whole document.
    """
    if not tokens:
        return leaf

    def build(node, depth):
        if node is None:
            return {} if depth < len(tokens) else leaf
        if depth >= len(tokens):
            return leaf
        token = tokens[depth]
        last = depth == len(tokens) - 1
        is_array = node.items is not None or (
            token.isdigit() and _primary_type(node) == "array")
        if is_array and token.isdigit():
            idx = int(token)
            count = max(idx + 1, node.min_items or 1)
            if node.max_items is not None:
                count = min(count, node.max_items)
            if idx >= count:  # cannot place the leaf inside a bounded array
                return leaf
            items = []
            for i in range(count):
                if i == idx:
                    items.append(leaf if last
                                 else build(node.items, depth + 1))
                else:
                    items.append(_safe_minimal(node.items))
            return items
        # object
        out: dict = {}

        def value_for(child):
            if child is None:
                return {}
            if last:
                return leaf
            # if the target child is a composition, descend into the branch
            # that matches our leaf when possible
            if child.one_of or child.any_of:
                branches = child.one_of or child.any_of
                match = _branch_matching(branches, leaf)
                if match is not None:
                    return leaf
            return build(child, depth + 1)

        for name in sorted(node.required):
            child = node.properties.get(name)
            if name == token:
                if child is None and isinstance(node.additional, SchemaNode):
                    child = node.additional
                out[name] = value_for(child)
            elif child is not None:
                out[name] = _safe_minimal(child)
        if token not in out:
            child = node.properties.get(token)
            if child is None and isinstance(node.additional, SchemaNode):
                child = node.additional
            if child is not None:
                out[token] = value_for(child)
        return out
    return build(root_node, 0)


def _branch_matching(branches, leaf):
    """Find a branch whose required properties are present in the leaf doc."""
    if not isinstance(leaf, dict):
        return None
    best = None
    best_score = -1
    for br in branches:
        if br.invalid or br.is_cycle:
            continue
        req = set(br.required)
        if req and req.issubset(set(leaf)):
            score = len(set(leaf) & set(br.properties))
            if score > best_score:
                best, best_score = br, score
        elif not req:
            overlap = len(set(leaf) & set(br.properties))
            if overlap > best_score:
                best, best_score = br, overlap
    return best


def _set_at(doc, tokens, key, value):
    """Set ``key=value`` in the object located at ``tokens``."""
    cur = doc if isinstance(doc, dict) else {}
    for t in tokens:
        nxt = cur.get(t)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[t] = nxt
        cur = nxt
    cur[key] = value
    return doc


def _omit_at(doc, tokens, key):
    """Delete a property: ``tokens`` may be the parent or end at the prop."""
    if not tokens:
        if isinstance(doc, dict):
            doc.pop(key, None)
        return doc
    # prop-itself layout: /a/b/c means drop c (key == last token usually)
    parent, last_t = doc, tokens[-1]
    node = doc
    at_parent = True
    for i, t in enumerate(tokens):
        if not isinstance(node, dict):
            at_parent = False
            break
        if i == len(tokens) - 1:
            break
        parent, node = node, node.get(t)
    if at_parent and isinstance(parent, dict):
        if key in parent:
            parent.pop(key, None)  # parent/key layout
        elif last_t in parent:
            parent.pop(last_t, None)
    return doc


def _branch_example(favor_node, bn, cn, baseline):
    """Minimal instance of the favorable node, preferring oneOf branches."""
    branches = favor_node.one_of or favor_node.any_of
    if branches:
        b_sigs = {_fingerprint(x) for x in (bn.one_of or bn.any_of)}
        c_sigs = {_fingerprint(x) for x in (cn.one_of or cn.any_of)}
        unique = [br for br in branches
                  if _fingerprint(br) not in (b_sigs if baseline else c_sigs)]
        target = unique[0] if unique else branches[0]
        return _safe_minimal(target)
    return _safe_minimal(favor_node)


def _safe_minimal(node, depth=0):
    try:
        return minimal_instance(node, depth=depth)
    except Exception:
        return {} if _primary_type(node) in (None, "object") else None


def _small_number(node):
    try:
        from .instance import _smallest_number
        return _smallest_number(node)
    except Exception:
        return 0


def _array_elem(node):
    if node.items:
        return _safe_minimal(node.items)
    return "x"


def _value_for_type(t, node):
    if t in ("integer", "number"):
        return _small_number(node)
    if t == "string":
        return "x" * (node.min_length or 0)
    if t == "boolean":
        return True
    if t == "array":
        return [_array_elem(node)]
    if t == "object":
        return _safe_minimal(node)
    if t == "null":
        return None
    return {}


def _value_outside_enum(values, base_node):
    t = _primary_type(base_node)
    if t in ("integer", "number"):
        v = 0
        existing = {x for x in values if isinstance(x, (int, float))}
        while v in existing:
            v += 1
        lo, _ = _eff_bounds(base_node)
        if lo is not None:
            v = max(v, int(lo))
            while v in existing:
                v += 1
        return v
    if t == "boolean":
        return True if True not in values else False
    s = "x"
    while s in values:
        s += "x"
    if base_node.min_length:
        s = s.ljust(base_node.min_length, "x")
    return s


def _extra_field(node):
    base = set(node.properties or {})
    i = 0
    while f"extra{i}" in base:
        i += 1
    name = f"extra{i}"
    value = _safe_minimal(node.additional) if isinstance(
        node.additional, SchemaNode) else "x"
    return name, value


def _bound_violating_value(kind, old_bound, cand_node):
    """Value satisfying candidate but outside the baseline bound."""
    if kind == "num_widen_hi":
        v = (old_bound + 1) if old_bound is not None else 1
        if cand_node.maximum is not None and v > cand_node.maximum:
            v = cand_node.maximum
    else:  # num_widen_lo
        v = (old_bound - 1) if old_bound is not None else -1
        if cand_node.minimum is not None and v < cand_node.minimum:
            v = cand_node.minimum
    if cand_node.multiple_of is not None:
        v = _round_to_multiple_safe(v, cand_node.multiple_of)
    return v


def _round_to_multiple_safe(v, m):
    from fractions import Fraction
    try:
        fm = Fraction(str(m))
        q = Fraction(str(v)) // fm
        out = float(q * fm)
        return int(out) if float(out).is_integer() else out
    except Exception:
        return v


def _non_multiple(m, base_node):
    from fractions import Fraction
    try:
        fm = Fraction(str(m))
        v = 1
        while Fraction(v) % fm == 0:
            v += 1
        lo, _ = _eff_bounds(base_node)
        if lo is not None:
            v = max(v, int(lo) + 1)
            while Fraction(v) % fm == 0:
                v += 1
        return v
    except Exception:
        return 1


def _tokens(relpath):
    if not relpath:
        return []
    return [t.replace("~1", "/").replace("~0", "~")
            for t in relpath.split("/") if t]


def _inject(doc, relpath, value):
    """Set value at relpath inside the minimal doc.

    Array indices keep the already-generated array elements (only the target
    slot is replaced); missing object ancestors are created as needed.
    """
    toks = _tokens(relpath)
    if not toks:
        return value
    root = dict(doc) if isinstance(doc, dict) else {}
    cur = root
    for t in toks[:-1]:
        nxt = cur.get(t)
        if isinstance(nxt, dict):
            nxt = dict(nxt)
        elif isinstance(nxt, list):
            nxt = list(nxt)
        else:
            nxt = {}
        cur[t] = nxt
        cur = nxt
    last = toks[-1]
    if isinstance(cur, list) and last.isdigit():
        idx = int(last)
        while len(cur) <= idx:
            cur.append(None)
        cur[idx] = value
    else:
        cur[last] = value
    return root


def _set_deep(doc, relpath, key, value):
    toks = _tokens(relpath)
    cur = doc if isinstance(doc, dict) else {}
    for t in toks:
        nxt = cur.get(t)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[t] = nxt
        cur = nxt
    cur[key] = value
    return doc


def _omit_path(doc, relpath, key):
    """Remove the offending property.

    Two layouts occur:
      * relpath is the parent object and ``key`` is the property name;
      * relpath already ends at the property itself.
    """
    toks = _tokens(relpath)
    if not toks:
        if isinstance(doc, dict):
            doc.pop(key, None)
        return doc
    cur = doc
    for t in toks:
        if not isinstance(cur, dict):
            return doc
        cur = cur.get(t)
    if isinstance(cur, dict):
        cur.pop(key, None)  # parent/key layout
    # property-itself layout: walk to parent and drop last token
    parent, target = doc, None
    node = doc
    for t in toks:
        if not isinstance(node, dict):
            return doc
        parent, target, node = node, t, node.get(t)
    if isinstance(parent, dict) and target is not None:
        parent.pop(target, None)
    return doc
