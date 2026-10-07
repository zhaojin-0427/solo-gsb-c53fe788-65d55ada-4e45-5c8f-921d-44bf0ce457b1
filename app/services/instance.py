"""Generate minimal valid instances and validate instances against nodes.

The generator produces the *smallest* value satisfying a normalized node
(fewer object keys, shorter strings/arrays), and follows reference cycles with
a depth budget so recursive schemas terminate. The validator is the same
constraint set the comparator reasons about; it is used to verify that a
generated counter-example passes exactly one side.

Call coverage does not use the replay validator directly: :func:`assess`
walks *along the actual value* and returns a three-state verdict
(pass / fail / pending) so a value whose ``child`` reaches a self- or
mutually-referential node is never optimistically counted as covered —
known constraints failing inside the recursive part still fail, otherwise
the row is undetermined with the schema location reported.
"""
from __future__ import annotations

from typing import Any, Optional

from .schemagraph import SchemaNode, UNSET

MAX_DEPTH = 8


class ValidationError(Exception):
    def __init__(self, message: str, path: str = ""):
        super().__init__(message)
        self.message = message
        self.path = path


def _json_type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "unknown"


def type_allows(node: SchemaNode, value_type: str, value: Any = None) -> bool:
    if node.type is None:
        return True
    allowed = set(node.type)
    if node.nullable:
        allowed.add("null")
    if value_type in allowed:
        return True
    # an integer also satisfies number
    if value_type == "integer" and "number" in allowed:
        return True
    return False


def validate(node: SchemaNode, value: Any, path: str = "",
             depth: int = 0) -> list[str]:
    """Return a list of validation error messages (empty => valid).

    Legacy two-state semantics: ``is_cycle`` nodes are *transparent*
    (optimistically accepted), ``invalid`` nodes yield one error, and
    ``oneOf``/``anyOf`` branches whose verdict is undecidable (cycles /
    invalid / unsupported keywords reached by the value) neither pass nor
    fail. Used by endpoint/structural replay (presence-and-contract checks);
    schema-level witness self-check and replay use the three-state
    :func:`check_tri` instead so a pending side is never a faked pass.
    """
    return _assess(node, value, path=path, depth=depth,
                   mode="replay").errors


# ============================================================ three-state
# 沿实际取值对 schema 图的三态裁决（调用覆盖专用）：
#   PASS     确定通过：取值触达的所有已知约束通过，且无循环/不可解析/
#            未支持的未决因素；
#   FAIL     确定失败：取值触达的已知（可判定）约束已经失败——未支持或
#            不可判定的约束只会更严格，不可能挽回该失败；
#   PENDING  待定：已知约束全过，但取值沿图触达了循环引用、无法解析的
#            模式或 pattern 等未支持关键字，能否通过无法定论。
TRI_PASS = "pass"
TRI_FAIL = "fail"
TRI_PENDING = "pending"


def check_tri(node: SchemaNode, value: Any, *, reenter=None,
              path: str = "") -> "TriCheck":
    """Three-state schema verdict for witness self-check / replay.

    Unlike the legacy boolean :func:`validate` (which optimistically treats
    undecidable branches as neither passing nor failing, and can therefore
    mistake ``oneOf`` 一过一待定 for a definite pass), this returns the same
    pass / fail / pending verdict the coverage engine uses — including the
    locations of every decisive undecidable factor (unsupported keywords such
    as ``not`` / ``pattern``, recursive nodes, unresolvable schemas). A
    pending side is never reported as a verified one-sided pass.
    """
    result = _assess(node, value, path=path, depth=0,
                     mode="coverage", reenter=reenter,
                     visited=frozenset(), reentries=0)
    return TriCheck(state=result.state, errors=list(result.errors),
                    hits=list(result.hits))


class TriCheck:
    """Three-state verdict: ``state`` is pass / fail / pending."""
    __slots__ = ("state", "errors", "hits")

    def __init__(self, state: str, errors=None, hits=None):
        self.state = state
        self.errors: list[str] = errors or []
        self.hits: list = hits or []

    @property
    def valid(self) -> bool:
        return self.state == TRI_PASS

    @property
    def pending(self) -> bool:
        return self.state == TRI_PENDING


class Assessment:
    __slots__ = ("state", "errors", "hits")

    def __init__(self, state: str, errors=None, hits=None):
        self.state = state
        self.errors: list[str] = errors or []
        self.hits: list = hits or []


def _pass(result: Assessment) -> Assessment:
    result.state = TRI_PASS
    return result


def assess(node: SchemaNode, value: Any, *, reenter=None) -> Assessment:
    """Three-state verdict of ``value`` against the parsed schema graph.

    Walks *along the actual value* — resolved local ``$ref`` chains, nested
    properties, array items and ``oneOf``/``anyOf`` branches. Reference cycles
    reached by the value are not optimistically accepted: when the value
    re-enters a self-/mutually-referential node, the schema at that site is
    re-expanded (via ``reenter``) and checked again against the remaining
    value nesting, so that known constraints failing inside the recursive
    part make the verdict FAIL, while genuinely undecidable recursion (or an
    unresolvable/invalid node reached by the value) makes it PENDING with
    the schema location reported. Optional properties absent from the value
    are never visited.

    ``reenter(file, pointer) -> SchemaNode`` rebuilds the full node at a
    cycle-marker site; when omitted the cycle is reported as PENDING without
    re-expansion.

    Same-level facets and ``oneOf``/``anyOf`` are adjudicated as an
    **intersection**: a known facet failure (or the combinator failing
    outright) yields FAIL; otherwise facet-level PENDING — a value whose
    ``child`` reaches a recursive node, or an unsupported keyword relevant
    to the value type — stays PENDING with locations reported, *even when a
    combinator branch already passes* (e.g. ``anyOf`` with one definite
    pass). The combinator itself still follows exactly-one (``oneOf``) /
    at-least-one (``anyOf``) semantics, and only branch hits that can
    still change the combination conclusion propagate upward: hits inside
    a definitely-failed branch (e.g. an unsupported ``not`` next to a
    missing required property) are discarded, as are pending-branch hits
    once ``anyOf`` already has a definite pass — so a nested property's
    combinator never poisons its parent into PENDING when the combination
    itself is settled. A genuinely pending branch still blocks ``oneOf``
    (exactly-one undecidable) while ``anyOf`` may count the cell.
    """
    return _assess(node, value, path="", depth=0,
                   mode="coverage", reenter=reenter, visited=frozenset(),
                   reentries=0)


def _assess(node: SchemaNode, value: Any, *, path: str, depth: int,
            mode: str, reenter=None, visited: frozenset = frozenset(),
            reentries: int = 0, _skip_self: bool = False) -> Assessment:
    """Recursive verdict core.

    ``depth`` bounds nesting for the legacy ``replay`` mode (which
    optimistically truncates). In ``coverage`` mode ordinary property /
    array nesting is walked fully — a non-recursive schema is finite — and
    only recursive **re-entries** are bounded by ``reentries``
    (:data:`MAX_DEPTH`): once exceeded the value stays 待定 (it is never
    optimistically accepted). ``visited`` is the set of definition sites
    active on the current value path / combinator branch (revisiting one
    means the value reached a recursive node); ``_skip_self`` marks a node
    just re-expanded by :func:`_revisit`; ``mode`` is ``"coverage"``
    (three-state) or ``"replay"`` (legacy witness/replay semantics)."""
    errors: list[str] = []
    hits: list = []

    def fail(msg: str) -> Assessment:
        return Assessment(TRI_FAIL, errors + [msg], hits)

    if node is None or node.is_bool is True:
        return _pass(Assessment(TRI_PASS))
    if node.is_bool is False:
        return fail(f"{path or '#'}: false 模式不允许任何值")
    if node.is_cycle:
        if mode == "replay":
            # 旧的重放语义：循环标记透明（乐观接受），不展开。
            return Assessment(TRI_PASS)
        return _revisit(node.loc, value, path, reenter, visited,
                        reentries)
    if node.invalid:
        if mode == "replay":
            return fail(f"{path or '#'}: 模式不可判定（{node.invalid}）")
        return Assessment(TRI_PENDING, [],
                          [("模式不可判定", node.loc)])

    site_key = (node.loc.file, node.loc.pointer)
    if mode == "coverage" and not _skip_self and site_key in visited:
        # 取值沿图重新触达了一个已在当前路径上展开的定义 → 递归节点
        return _revisit(node.loc, value, path, reenter, visited,
                        reentries)
    if mode == "coverage":
        child_visited = visited if _skip_self else visited | {site_key}
    else:
        child_visited = visited

    vt = _json_type(value)
    type_failed = False
    if not type_allows(node, vt, value):
        errors.append(
            f"{path or '#'}: 类型 {vt} 不被允许 {sorted(node.type or [])}")
        type_failed = True

    # 与取值类型相关的未支持关键字（coverage 三态下报告位置）
    if mode == "coverage" and not type_failed:
        for kw, loc in node.unsupported:
            applies = _UNSUPPORTED_KW_TYPES.get(kw)
            if applies is None or vt in applies:
                hits.append((kw, loc))

    child_state = TRI_PASS

    def absorb(result: Assessment) -> None:
        """Fold a child verdict in: FAIL dominates; otherwise PENDING sticks;
        hits/errors are collected for location reporting."""
        nonlocal child_state
        for h in result.hits:
            if h not in hits:
                hits.append(h)
        if result.errors:
            errors.extend(result.errors)
        if result.state == TRI_FAIL:
            child_state = TRI_FAIL
        elif result.state == TRI_PENDING and child_state != TRI_FAIL:
            child_state = TRI_PENDING

    if not type_failed and value is not None:
        if node.enum is not None and not any(
                _json_safe_equal(value, e) for e in node.enum):
            errors.append(f"{path or '#'}: 值不在枚举 {node.enum} 中")
        if node.const is not UNSET and node.const is not None and \
                not _json_safe_equal(value, node.const):
            errors.append(
                f"{path or '#'}: 值必须等于常量 {node.const!r}")

        if vt in ("integer", "number"):
            if node.minimum is not None and value < node.minimum:
                errors.append(
                    f"{path or '#'}: {value} 小于 minimum {node.minimum}")
            if node.maximum is not None and value > node.maximum:
                errors.append(
                    f"{path or '#'}: {value} 大于 maximum {node.maximum}")
            if node.ex_min is not None and value <= node.ex_min:
                errors.append(
                    f"{path or '#'}: {value} 必须大于 exclusiveMinimum "
                    f"{node.ex_min}")
            if node.ex_max is not None and value >= node.ex_max:
                errors.append(
                    f"{path or '#'}: {value} 必须小于 exclusiveMaximum "
                    f"{node.ex_max}")
            if node.multiple_of is not None and \
                    not _is_multiple(value, node.multiple_of):
                errors.append(
                    f"{path or '#'}: {value} 不是 {node.multiple_of} 的倍数")

        if vt == "string":
            if node.min_length is not None and len(value) < node.min_length:
                errors.append(
                    f"{path or '#'}: 字符串长度小于 minLength "
                    f"{node.min_length}")
            if node.max_length is not None and len(value) > node.max_length:
                errors.append(
                    f"{path or '#'}: 字符串长度超过 maxLength "
                    f"{node.max_length}")

        if vt == "array":
            if node.min_items is not None and len(value) < node.min_items:
                errors.append(
                    f"{path or '#'}: 数组元素少于 minItems "
                    f"{node.min_items}")
            if node.max_items is not None and len(value) > node.max_items:
                errors.append(
                    f"{path or '#'}: 数组元素超过 maxItems "
                    f"{node.max_items}")
            if node.unique_items:
                seen = [_json_key(v) for v in value]
                if len(set(seen)) != len(seen):
                    errors.append(f"{path or '#'}: 数组元素不唯一")
            if node.items is not None:
                for i, item in enumerate(value):
                    if mode == "replay":
                        if depth + 1 >= MAX_DEPTH:
                            break
                    absorb(_assess(node.items, item,
                                   path=f"{path}/{i}", depth=depth + 1,
                                   mode=mode, reenter=reenter,
                                   visited=child_visited,
                                   reentries=reentries))

        if vt == "object":
            if node.min_props is not None and len(value) < node.min_props:
                errors.append(
                    f"{path or '#'}: 属性数少于 minProperties "
                    f"{node.min_props}")
            if node.max_props is not None and len(value) > node.max_props:
                errors.append(
                    f"{path or '#'}: 属性数超过 maxProperties "
                    f"{node.max_props}")
            for req in node.required:
                if req not in value:
                    errors.append(
                        f"{path or '#'}: 缺少必填属性 {req!r}")
            for pname, pnode in node.properties.items():
                if pname not in value:
                    continue
                if mode == "replay" and depth + 1 >= MAX_DEPTH:
                    continue
                absorb(_assess(pnode, value[pname],
                               path=f"{path}/{_esc(pname)}",
                               depth=depth + 1,
                               mode=mode, reenter=reenter,
                               visited=child_visited,
                               reentries=reentries))
            extra = [k for k in value if k not in node.properties]
            if node.additional is False and extra:
                errors.append(
                    f"{path or '#'}: 不允许额外属性 {sorted(extra)}")
            elif isinstance(node.additional, SchemaNode):
                for k in sorted(extra):
                    if mode == "replay" and depth + 1 >= MAX_DEPTH:
                        continue
                    absorb(_assess(node.additional, value[k],
                                   path=f"{path}/{_esc(k)}",
                                   depth=depth + 1,
                                   mode=mode, reenter=reenter,
                                   visited=child_visited,
                                   reentries=reentries))

    # ---- 组合分支：replay 模式按旧语义（循环/不可判定/未支持分支为
    # 「未定」，既不计通过也不计失败）枚举组合错误；coverage 模式走
    # _combine 的三态裁决。
    if mode == "replay":
        if node.one_of and depth < MAX_DEPTH:
            states = [_replay_branch_state(b, value, depth)
                      for b in node.one_of]
            n_pass = states.count("pass")
            if n_pass >= 2:
                errors.append(
                    f"{path or '#'}: 同时满足 oneOf 的 {n_pass} 个分支"
                    "（要求恰好一个）")
            elif n_pass == 0 and "undecided" not in states:
                errors.append(f"{path or '#'}: 不满足 oneOf 的任何分支")
        if node.any_of and depth < MAX_DEPTH:
            states = [_replay_branch_state(b, value, depth)
                      for b in node.any_of]
            if "pass" not in states and "undecided" not in states:
                errors.append(f"{path or '#'}: 不满足 anyOf 的任何分支")
        return Assessment(TRI_FAIL if errors else TRI_PASS, errors, hits)

    # coverage 三态：按**节点约束的交集**裁决本层 facet 与组合结果——
    # 本层已知失败（或组合整体确定失败）→ FAIL；否则本层待定（取值沿
    # properties.child 等触达循环 / 不可解析节点 / 未支持关键字）即使
    # oneOf/anyOf 组合确定通过，也必须保留 PENDING 并报告模式位置，不得
    # 直接把组合结论当作节点结论而误计覆盖格。
    facet_failed = bool(errors) or child_state == TRI_FAIL
    facet_pending = child_state == TRI_PENDING or bool(hits)

    combinator: Optional[Assessment] = None
    if node.one_of:
        combinator = _combine(node.one_of, value, path,
                              reenter, visited, reentries, exactly_one=True)
    elif node.any_of:
        combinator = _combine(node.any_of, value, path,
                              reenter, visited, reentries, exactly_one=False)

    if combinator is not None:
        for h in combinator.hits:
            if h not in hits:
                hits.append(h)
        all_errors = errors + [e for e in combinator.errors if e not in errors]
        # 组合整体确定失败 → 节点确定失败（本层待定也无法挽回）
        if facet_failed or combinator.state == TRI_FAIL:
            return Assessment(TRI_FAIL, all_errors, hits)
        # 本层已知约束待定：即使组合确定通过（anyOf 至少一个 / oneOf
        # 恰好一个），交集仍无法定论
        if facet_pending or combinator.state == TRI_PENDING:
            return Assessment(TRI_PENDING, all_errors, hits)
        return Assessment(TRI_PASS, all_errors, hits)

    if facet_failed:
        return Assessment(TRI_FAIL, errors, hits)
    if facet_pending:
        return Assessment(TRI_PENDING, errors, hits)
    return Assessment(TRI_PASS, errors, hits)


def _revisit(loc, value, path, reenter, visited, reentries):
    """The value re-entered a recursive schema definition (``loc``).

    Reaching such a node is inherently **待定** — a finite value proves
    nothing about a recursive schema. The definition is nevertheless
    re-expanded against the same (remaining) value so that **known
    constraints failing inside the recursive part take priority** and make
    the verdict FAIL; only when no known constraint fails is the verdict
    PENDING, reporting the cycle's schema location. Re-expansion is bounded
    by ``reentries`` (:data:`MAX_DEPTH`) so mutually recursive graphs
    terminate: once exhausted, the row stays 待定 rather than being
    optimistically accepted.
    """
    cycle_hit = ("循环引用", loc)
    if reentries >= MAX_DEPTH or reenter is None:
        return Assessment(TRI_PENDING, [], [cycle_hit])
    target = reenter(loc.file, loc.pointer)
    if target is None:
        return Assessment(TRI_PENDING, [], [cycle_hit])
    if target.is_cycle or target.invalid:
        return Assessment(TRI_PENDING, [],
                          [cycle_hit] + ([("模式不可判定", target.loc)]
                                         if target.invalid else []))
    result = _assess(target, value, path=path, depth=0,
                     mode="coverage", reenter=reenter, visited=visited,
                     reentries=reentries + 1, _skip_self=True)
    hits = [cycle_hit] + [h for h in result.hits if h != cycle_hit]
    # 同一分支的已知失败优先；否则触达循环即为待定（带模式位置）
    if result.state == TRI_FAIL:
        return Assessment(TRI_FAIL, result.errors, hits)
    return Assessment(TRI_PENDING, result.errors, hits)


def _replay_branch_state(node: SchemaNode, value: Any, depth: int) -> str:
    """Old replay branch classification: undecidable nodes / unsupported
    keywords reached by the value make the branch ``undecided``; otherwise
    known-constraint errors => ``fail``, clean => ``pass``."""
    r = _assess(node, value, path="", depth=depth + 1,
                mode="coverage", reenter=None, visited=frozenset(),
                reentries=0)
    if r.hits:
        return "undecided"
    return "fail" if r.errors else "pass"


def _combine(branches, value, path, reenter, visited, reentries,
             *, exactly_one: bool) -> Assessment:
    """oneOf（exactly_one=True，恰好一个分支通过）/ anyOf（至少一个）。

    分支状态三态：确定通过 / 确定失败 / 待定。已知约束失败的分支不会
    因为触达循环/未支持约束而翻案——失败优先。

    未决命中（未支持关键字、循环、不可解析节点）只在**仍可能改变组合
    结论**时才向上传递：

    * 确定失败分支的命中（例如分支因缺少必填属性确定失败、同时含未
      支持的 ``not``）不可能翻案，丢弃——否则它会经组合与父属性汇总
      把本应覆盖的行污染成无法判定；
    * 待定分支的命中只有在组合结论本身为待定时才相关：``oneOf`` 一
      过一待定（是否“恰好一个”无定论）或零通过有特定时保留并上报；
      ``anyOf`` 已有一个确定通过时，其余分支无论待定与否都不可能改
      变“至少一个”的成立，命中一并丢弃（可计覆盖格）。
    """
    states: list[str] = []
    errors: list[str] = []
    pending_hits: list = []
    for b in branches:
        r = _assess(b, value, path=path, depth=0,
                    mode="coverage", reenter=reenter, visited=visited,
                    reentries=reentries)
        states.append(r.state)
        if r.state == TRI_PENDING:
            for h in r.hits:
                if h not in pending_hits:
                    pending_hits.append(h)
        errors.extend(r.errors)
    n_pass = states.count(TRI_PASS)
    n_pending = states.count(TRI_PENDING)

    if exactly_one:
        # 两个确定通过 → 确定失败（违反「恰好一个」）
        if n_pass >= 2:
            return Assessment(TRI_FAIL,
                              [f"{path or '#'}: 同时满足 oneOf 的 {n_pass} "
                               "个分支（要求恰好一个）"] + errors)
        # 一个确定通过 + 其余分支仍可能通过（待定）→ 是否恰好一个无定论：
        # 待定分支的未决命中仍可能改变结论，保留并上报（不计覆盖格）
        if n_pass == 1 and n_pending > 0:
            return Assessment(TRI_PENDING, [], pending_hits)
        # 一个确定通过且其余确定失败 → 通过（失败分支的未决命中不影响结论）
        if n_pass == 1:
            return Assessment(TRI_PASS)
        # 没有确定通过：所有分支确定失败 → 失败；否则待定
        if n_pending == 0:
            return Assessment(TRI_FAIL,
                              [f"{path or '#'}: 不满足 oneOf 的任何分支"]
                              + errors)
        return Assessment(TRI_PENDING, [], pending_hits)

    # anyOf：至少一个确定通过即成立——其余分支的未决命中已不可能改变
    # 组合结论，不再向上传播（可计覆盖格）；没有确定通过、仅剩待定
    # 分支时才记待定并上报其命中；全失败才失败
    if n_pass >= 1:
        return Assessment(TRI_PASS)
    if n_pending > 0:
        return Assessment(TRI_PENDING, [], pending_hits)
    return Assessment(TRI_FAIL,
                      [f"{path or '#'}: 不满足 anyOf 的任何分支"]
                      + errors)


# Unsupported keyword -> JSON types it constrains. Keywords missing from this
# map (``not``, ``if``/``then``/``else``, unknown keywords, ``type[]`` …) may
# constrain any value type.
_UNSUPPORTED_KW_TYPES = {
    "pattern": {"string"},
    "contains": {"array"},
    "minContains": {"array"},
    "maxContains": {"array"},
    "prefixItems": {"array"},
    "unevaluatedItems": {"array"},
    "propertyNames": {"object"},
    "patternProperties": {"object"},
    "dependentRequired": {"object"},
    "dependentSchemas": {"object"},
    "unevaluatedProperties": {"object"},
}


def unsupported_along_value(node: SchemaNode, value: Any) -> list:
    """Compatibility wrapper: ``(keyword, FacetLoc)`` hits for a value.

    Kept for callers outside the coverage engine; coverage itself uses
    :func:`assess` (which also classifies cycles / invalid nodes and
    adjudicates ``oneOf``/``anyOf`` into the three-state verdict).
    """
    return assess(node, value).hits


def reenter_for(graph):
    """Build a ``reenter`` callback backed by a :class:`SchemaGraph`.

    A value that re-enters a self-/mutually-referential schema site triggers
    a rebuild of the full node there, so known constraints failing inside the
    recursive part still fail while genuinely undecidable recursion stays
    待定.
    """
    def _build(file: str, pointer: str):
        try:
            return graph.build_at(file, pointer)
        except (KeyError, IndexError, ValueError):
            return None
    return _build


def format_pending_hits(hits, entry: str) -> list[str]:
    """Human-readable, de-duplicated reasons for PENDING verdict.

    Every reason carries the schema location (JSON Pointer, prefixed with
    the file for cross-file packages) so the UI/API can show *where* the
    undecidable ``not`` / ``pattern`` keyword or recursive node sits.
    """
    parts: list[str] = []
    seen: set[tuple[str, str]] = set()
    for kw, loc in hits:
        where = loc.hint(entry)
        key = (kw, where)
        if key in seen:
            continue
        seen.add(key)
        if kw == "循环引用":
            parts.append(f"取值沿图重新触达循环引用（{where}）")
        elif kw == "模式不可判定":
            parts.append(f"触达无法解析 / 无效的模式节点（{where}）")
        else:
            parts.append(f"仍依赖未支持的关键字 {kw}（{where}）")
    return parts


def minimal_instance(node: SchemaNode, depth: int = 0,
                     force_type: str | None = None) -> Any:
    """Smallest value that validates against ``node`` (best effort)."""
    if node.is_bool is True or node.is_cycle:
        return _scalar(force_type or "object", node, depth)
    if node.is_bool is False or node.invalid:
        return None
    # composition: prefer the smallest branch
    if (node.one_of or node.any_of) and depth < MAX_DEPTH:
        candidates = []
        for br in (node.one_of or node.any_of):
            v = minimal_instance(br, depth + 1)
            candidates.append(v)
        if candidates:
            return min(candidates, key=_value_rank)
    return _scalar(force_type or _primary_type(node), node, depth)


def _primary_type(node: SchemaNode) -> str:
    if node.const is not UNSET and node.const is not None:
        return _json_type(node.const)
    if node.enum:
        return _json_type(node.enum[0])
    if node.type:
        for t in node.type:
            if t != "null":
                return t
        return "null"
    if node.one_of or node.any_of:
        return "object"
    if node.properties or node.required or node.additional is False:
        return "object"
    if node.items is not None:
        return "array"
    return "string"


def _scalar(t: str, node: SchemaNode, depth: int) -> Any:
    if node.const is not UNSET and node.const is not None:
        return node.const
    if node.enum:
        return _smallest_enum(_enum_for_type(node.enum, t))
    if t == "null":
        return None
    if t == "boolean":
        return False
    if t in ("integer", "number"):
        return _smallest_number(node)
    if t == "string":
        return _smallest_string(node)
    if t == "array":
        return _smallest_array(node, depth)
    if t == "object":
        return _smallest_object(node, depth)
    return {}


def _smallest_enum(values: list) -> Any:
    vals = values or [None]
    def rank(v):
        t = _json_type(v)
        base = {"null": 0, "boolean": 1, "integer": 2, "number": 2,
                "string": 3, "array": 4, "object": 5}.get(t, 9)
        size = len(v) if isinstance(v, (list, dict, str)) else 0
        return (base, size, _json_key(v))
    return min(vals, key=rank)


def _enum_for_type(values: list, t: str) -> list:
    matching = [v for v in values if _json_type(v) == t
                or (t == "number" and _json_type(v) == "integer")]
    return matching or values


def _value_rank(v):
    t = _json_type(v)
    base = {"null": 0, "boolean": 1, "integer": 2, "number": 2,
            "string": 3, "array": 4, "object": 5}.get(t, 9)
    size = len(v) if isinstance(v, (list, dict, str)) else 0
    return (base, size)


def _smallest_number(node: SchemaNode):
    candidates = [0, 1, -1]
    if node.minimum is not None:
        candidates.append(node.minimum)
        candidates.append(node.minimum + 1)
    if node.ex_min is not None:
        candidates.append(node.ex_min + 1)
        if _is_int(node.ex_min):
            candidates.append(int(node.ex_min) + 1)
    if node.maximum is not None:
        candidates.append(node.maximum)
    if node.ex_max is not None:
        candidates.append(node.ex_max - 1)
    valid = [c for c in candidates if _number_fits(c, node)]
    if node.multiple_of:
        for c in list(valid):
            m = _round_to_multiple(c, node.multiple_of)
            if m is not None:
                valid.append(m)
        valid = [c for c in valid if _is_multiple(c, node.multiple_of)]
    if valid:
        return min(valid, key=lambda c: (abs(c), c))
    return 0


def _smallest_string(node: SchemaNode) -> str:
    length = node.min_length or 0
    s = "x" * length
    if node.max_length is not None and len(s) > node.max_length:
        s = ""
    return s


def _smallest_array(node: SchemaNode, depth: int) -> list:
    if depth >= MAX_DEPTH or node.items is None:
        count = node.min_items or 0
        return [_scalar("string", SchemaNode(loc=node.loc), depth + 1)
                for _ in range(min(count, 3))]
    count = node.min_items or 1
    if node.max_items is not None:
        count = min(count, node.max_items)
    count = min(count, 3)
    return [minimal_instance(node.items, depth + 1) for _ in range(count)]


def _smallest_object(node: SchemaNode, depth: int) -> dict:
    if depth >= MAX_DEPTH:
        return {}
    out: dict = {}
    # The canonical minimal instance contains ONLY required properties:
    # this keeps it valid on the favorable side and leaves optional fields
    # free to be injected by witness synthesis.
    names = sorted(node.required)
    if node.min_props and len(names) < node.min_props:
        existing = set(node.properties)
        extras: list[str] = []
        i = 0
        while len(names) + len(extras) < node.min_props:
            name = f"extra{i}"
            if name not in existing:
                extras.append(name)
            i += 1
        names += extras
    for name in names[:12]:
        child = node.properties.get(name)
        if child is None:
            if isinstance(node.additional, SchemaNode):
                out[name] = minimal_instance(node.additional, depth + 1)
            elif node.additional is not False:
                out[name] = ""
            continue
        if child.is_cycle:
            out[name] = {}  # break recursive expansion
        else:
            out[name] = minimal_instance(child, depth + 1)
    return out


def _number_fits(c, node: SchemaNode) -> bool:
    if node.minimum is not None and c < node.minimum:
        return False
    if node.maximum is not None and c > node.maximum:
        return False
    if node.ex_min is not None and c <= node.ex_min:
        return False
    if node.ex_max is not None and c >= node.ex_max:
        return False
    return True


def _is_multiple(value, multiple_of) -> bool:
    if value == 0:
        return True
    from fractions import Fraction
    try:
        q = Fraction(str(value)) / Fraction(str(multiple_of))
        return q.denominator == 1
    except (ValueError, ZeroDivisionError):
        return True


def _round_to_multiple(c, m):
    from fractions import Fraction
    try:
        fm = Fraction(str(m))
        q = Fraction(str(c)) / fm
        r = (q.numerator // q.denominator) * fm
        return float(r)
    except (ValueError, ZeroDivisionError):
        return None


def _is_int(v) -> bool:
    return float(v).is_integer()


def _esc(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _json_key(v: Any) -> str:
    import json
    return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)


def _json_safe_equal(a: Any, b: Any) -> bool:
    return _json_key(a) == _json_key(b)
