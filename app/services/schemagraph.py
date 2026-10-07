"""Build a normalized, dereferenced *schema graph* from an OpenAPI package.

Each schema object becomes a :class:`SchemaNode`. Construction:

* follows local / in-package ``$ref`` chains (external http(s) refs are
  rejected, never fetched);
* merges ``allOf`` members into the node's facets (intersection semantics);
* keeps ``oneOf`` / ``anyOf`` as branch lists;
* terminates reference *cycles* with a dedicated ``is_cycle`` node — both the
  graph traversal and later comparison/minimal-instance walks stop there.

Every schema is addressed as a *site* ``(file, json_pointer)``. A cycle is a
reference back to a site that is currently being constructed on the active
construction path; completed sites are shared through a cache.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Optional

from . import pointer as ptr
from .resolver import RefResolver
from .rules import SUPPORTED_KEYWORDS, SEMANTICALLY_UNSUPPORTED


@dataclass(frozen=True)
class FacetLoc:
    file: str
    pointer: str

    def hint(self, entry: str) -> str:
        return ptr.format_ref_location(self.file, entry, self.pointer)


UNSET = object()


@dataclass
class SchemaNode:
    loc: FacetLoc
    type: Optional[list[str]] = None      # None => any; may include "null"
    enum: Optional[list] = None
    const: Any = UNSET
    nullable: bool = False                # OAS 3.0
    format: Optional[str] = None
    properties: dict[str, "SchemaNode"] = field(default_factory=dict)
    required: set[str] = field(default_factory=set)
    items: Optional["SchemaNode"] = None
    additional: Any = UNSET               # bool | SchemaNode
    one_of: list["SchemaNode"] = field(default_factory=list)
    any_of: list["SchemaNode"] = field(default_factory=list)
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    ex_min: Optional[float] = None
    ex_max: Optional[float] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    min_items: Optional[int] = None
    max_items: Optional[int] = None
    min_props: Optional[int] = None
    max_props: Optional[int] = None
    unique_items: Optional[bool] = None
    multiple_of: Optional[float] = None
    read_only: bool = False
    write_only: bool = False
    deprecated: bool = False
    default: Any = UNSET
    unsupported: list[tuple[str, FacetLoc]] = field(default_factory=list)
    invalid: Optional[str] = None
    is_cycle: bool = False
    is_bool: Optional[bool] = None
    facet_loc: dict[str, FacetLoc] = field(default_factory=dict)
    branch_kind: Optional[str] = None


@dataclass
class BuildStats:
    cycle_terminations: int = 0
    unresolved_refs: int = 0
    unsupported_keywords: int = 0
    invalid_schemas: int = 0


_TYPE_ORDER = ["null", "boolean", "integer", "number", "string", "array",
               "object"]


def _norm_type(raw: Any) -> tuple[Optional[list[str]], bool]:
    if raw is None:
        return None, False
    if isinstance(raw, str):
        return [raw], False
    if isinstance(raw, list):
        types = [t for t in raw if isinstance(t, str)]
        non_null = [t for t in types if t != "null"]
        return types, len(non_null) > 1
    return None, False


class SchemaGraph:
    def __init__(self, files: dict[str, dict], entry: str,
                 resolver: Optional[RefResolver] = None):
        self.files = files
        self.entry = entry
        self.resolver = resolver or RefResolver(files, entry)
        self.stats = BuildStats()
        self._cache: dict[tuple[str, str], SchemaNode] = {}

    # ------------------------------------------------------------ public API
    def build_at(self, file: str, pointer: str) -> SchemaNode:
        return self._build_site(file, pointer, frozenset())

    def build_value(self, value: Any, file: str, pointer: str) -> SchemaNode:
        return self._build_value(value, file, pointer, frozenset())

    # ------------------------------------------------------------- raw site
    def _raw_at(self, file: str, pointer: str):
        return ptr.resolve(self.files[file], pointer)

    def _bool_node(self, file, pointer, value):
        node = SchemaNode(loc=FacetLoc(file, pointer), is_bool=value)
        if not value:
            node.invalid = "false 模式不允许任何值"
            self.stats.invalid_schemas += 1
        return node

    def _cycle(self, file, pointer):
        self.stats.cycle_terminations += 1
        return SchemaNode(loc=FacetLoc(file, pointer), is_cycle=True)

    def _build_value(self, value, file, pointer,
                     active: frozenset) -> SchemaNode:
        """Build a schema given as an inline value (property/items/member).

        ``value`` lives at (file, pointer); if it is a ``$ref`` object the
        reference is resolved (possibly into another file) and built there.
        """
        if isinstance(value, bool):
            return self._bool_node(file, pointer, value)
        if not isinstance(value, dict):
            n = SchemaNode(loc=FacetLoc(file, pointer))
            n.invalid = "模式不是对象"
            self.stats.invalid_schemas += 1
            return n
        if isinstance(value.get("$ref"), str):
            ref = value["$ref"]
            ref_loc = FacetLoc(file, ptr.join(pointer, "$ref"))
            if self.resolver.is_external(ref):
                self.stats.unresolved_refs += 1
                n = SchemaNode(loc=FacetLoc(file, pointer))
                n.invalid = f"外部 $ref 不被抓取: {ref}"
                n.facet_loc["$ref"] = ref_loc
                return n
            res = self.resolver.resolve(ref, file)
            if not res.ok:
                self.stats.unresolved_refs += 1
                n = SchemaNode(loc=FacetLoc(file, pointer))
                n.invalid = res.error
                n.facet_loc["$ref"] = ref_loc
                return n
            target = (res.file, res.pointer)
            if target in active:
                return self._cycle(res.file, res.pointer)
            # OAS 3.1 siblings alongside $ref: build target, then merge sibs
            siblings = {k: v for k, v in value.items() if k != "$ref"}
            target_node = self._build_site(res.file, res.pointer, active)
            if siblings:
                sib_node = self._build_inline(siblings, file, pointer,
                                              active | {target})
                return self._merge_two(target_node, sib_node,
                                       loc=target_node.loc)
            return target_node
        # plain inline object at its own site
        return self._build_site(file, pointer, active)

    def _build_inline(self, obj: dict, file: str, pointer: str,
                      active: frozenset) -> SchemaNode:
        """Build facets directly from an inline dict at a synthetic site."""
        node = SchemaNode(loc=FacetLoc(file, pointer))
        self._read_facets(node, file, pointer, obj, active |
                          {(file, pointer)})
        return node

    def _build_site(self, file: str, pointer: str,
                    active: frozenset) -> SchemaNode:
        key = (file, pointer)
        if key in active:
            return self._cycle(file, pointer)
        if key in self._cache:
            return self._cache[key]

        try:
            raw = self._raw_at(file, pointer)
        except (KeyError, IndexError, ValueError):
            n = SchemaNode(loc=FacetLoc(file, pointer))
            n.invalid = "JSON Pointer 目标不存在"
            self.stats.invalid_schemas += 1
            self._cache[key] = n
            return n
        if isinstance(raw, bool):
            n = self._bool_node(file, pointer, raw)
            self._cache[key] = n
            return n
        if not isinstance(raw, dict):
            n = SchemaNode(loc=FacetLoc(file, pointer))
            n.invalid = "模式不是对象"
            self.stats.invalid_schemas += 1
            self._cache[key] = n
            return n

        # follow a leading $ref chain
        if isinstance(raw.get("$ref"), str):
            node = self._build_value(raw, file, pointer, active)
            if not node.is_cycle:
                self._cache[key] = node
            return node

        node = SchemaNode(loc=FacetLoc(file, pointer))
        self._cache[key] = node  # reserve; active-set handles true cycles
        child_active = active | {key}
        self._read_facets(node, file, pointer, raw, child_active)
        return node

    # --------------------------------------------------------- facet reading
    def _read_facets(self, node: SchemaNode, file: str, pointer: str,
                     s: dict, active: frozenset) -> None:
        """Read facets from one concrete schema dict and merge into ``node``.

        ``allOf`` members are built as separate nodes and intersected.
        """
        # allOf: build member nodes first
        all_of = s.get("allOf")
        own = {k: v for k, v in s.items() if k != "allOf"}
        member_nodes: list[SchemaNode] = []
        if isinstance(all_of, list) and all_of:
            for i, member in enumerate(all_of):
                mp = ptr.join(pointer, "allOf", i)
                member_nodes.append(
                    self._build_value(member, file, mp, active))
        elif "allOf" in s and not isinstance(all_of, list):
            node.invalid = "allOf 不是数组"
            self.stats.invalid_schemas += 1

        # own facets
        own_node = SchemaNode(loc=FacetLoc(file, pointer))
        self._scan_keywords(own_node, file, pointer, own)
        self._read_direct_facets(own_node, file, pointer, own, active)
        result = own_node
        for m in member_nodes:
            result = self._merge_two(result, m, loc=node.loc)
        # adopt onto node
        self._adopt(node, result)

    def _scan_keywords(self, node, file, pointer, s):
        for kw in s:
            if kw in SEMANTICALLY_UNSUPPORTED:
                node.unsupported.append(
                    (kw, FacetLoc(file, ptr.join(pointer, kw))))
                self.stats.unsupported_keywords += 1
            elif kw not in SUPPORTED_KEYWORDS and kw != "$ref":
                node.unsupported.append(
                    (kw, FacetLoc(file, ptr.join(pointer, kw))))
                self.stats.unsupported_keywords += 1

    def _read_direct_facets(self, node: SchemaNode, file: str, pointer: str,
                            s: dict, active: frozenset) -> None:
        def loc(facet):
            return FacetLoc(file, ptr.join(pointer, facet))

        if "type" in s:
            t, ambiguous = _norm_type(s["type"])
            if ambiguous:
                node.unsupported.append(("type[]", loc("type")))
                self.stats.unsupported_keywords += 1
            if t is not None:
                node.type = t
                node.facet_loc["type"] = loc("type")
        if "const" in s:
            node.const = s["const"]
            node.facet_loc["const"] = loc("const")
        if isinstance(s.get("enum"), list):
            node.enum = s["enum"]
            node.facet_loc["enum"] = loc("enum")
        if s.get("nullable") is True:
            node.nullable = True
            node.facet_loc["nullable"] = loc("nullable")
        if s.get("format") is not None:
            node.format = s["format"]
            node.facet_loc["format"] = loc("format")
        if s.get("readOnly") is True:
            node.read_only = True
        if s.get("writeOnly") is True:
            node.write_only = True
        if s.get("deprecated") is True:
            node.deprecated = True
        if "default" in s:
            node.default = s["default"]

        if isinstance(s.get("properties"), dict):
            for name, val in s["properties"].items():
                pp = ptr.join(pointer, "properties", name)
                child = self._build_value(val, file, pp, active)
                node.properties[name] = child
                node.facet_loc[f"property:{name}"] = FacetLoc(file, pp)
        if isinstance(s.get("required"), list):
            node.required = {r for r in s["required"] if isinstance(r, str)}
            if node.required:
                node.facet_loc["required"] = loc("required")
                for r in node.required:
                    node.facet_loc[f"required:{r}"] = loc("required")

        if "items" in s:
            node.items = self._build_value(
                s["items"], file, ptr.join(pointer, "items"), active)
            node.facet_loc["items"] = loc("items")
        if "additionalProperties" in s:
            av = s["additionalProperties"]
            if isinstance(av, bool):
                node.additional = av
            else:
                node.additional = self._build_value(
                    av, file, ptr.join(pointer, "additionalProperties"),
                    active)
            node.facet_loc["additionalProperties"] = loc(
                "additionalProperties")

        if isinstance(s.get("oneOf"), list):
            node.one_of = [
                self._build_value(br, file,
                                  ptr.join(pointer, "oneOf", i), active)
                for i, br in enumerate(s["oneOf"])]
            node.facet_loc["oneOf"] = loc("oneOf")
            node.branch_kind = "oneOf"
        if isinstance(s.get("anyOf"), list):
            node.any_of = [
                self._build_value(br, file,
                                  ptr.join(pointer, "anyOf", i), active)
                for i, br in enumerate(s["anyOf"])]
            node.facet_loc["anyOf"] = loc("anyOf")
            node.branch_kind = node.branch_kind or "anyOf"

        if isinstance(s.get("minimum"), (int, float)):
            node.minimum = s["minimum"]
            node.facet_loc["minimum"] = loc("minimum")
        if isinstance(s.get("maximum"), (int, float)):
            node.maximum = s["maximum"]
            node.facet_loc["maximum"] = loc("maximum")
        self._take_exclusive(node, s, "exclusiveMinimum", "minimum",
                             loc, is_min=True)
        self._take_exclusive(node, s, "exclusiveMaximum", "maximum",
                             loc, is_min=False)
        if isinstance(s.get("minLength"), int):
            node.min_length = s["minLength"]
            node.facet_loc["minLength"] = loc("minLength")
        if isinstance(s.get("maxLength"), int):
            node.max_length = s["maxLength"]
            node.facet_loc["maxLength"] = loc("maxLength")
        if isinstance(s.get("minItems"), int):
            node.min_items = s["minItems"]
            node.facet_loc["minItems"] = loc("minItems")
        if isinstance(s.get("maxItems"), int):
            node.max_items = s["maxItems"]
            node.facet_loc["maxItems"] = loc("maxItems")
        if isinstance(s.get("minProperties"), int):
            node.min_props = s["minProperties"]
        if isinstance(s.get("maxProperties"), int):
            node.max_props = s["maxProperties"]
        if isinstance(s.get("uniqueItems"), bool):
            node.unique_items = s["uniqueItems"]
        if isinstance(s.get("multipleOf"), (int, float)) and \
                s["multipleOf"] > 0:
            node.multiple_of = s["multipleOf"]
            node.facet_loc["multipleOf"] = loc("multipleOf")

    def _take_exclusive(self, node, s, key, bound_key, loc, is_min):
        v = s.get(key)
        if isinstance(v, bool) and v is True:
            bound = s.get(bound_key)
            if isinstance(bound, (int, float)):
                if is_min:
                    node.ex_min = bound
                    node.facet_loc["exclusiveMinimum"] = loc(bound_key)
                else:
                    node.ex_max = bound
                    node.facet_loc["exclusiveMaximum"] = loc(bound_key)
        elif isinstance(v, (int, float)):
            if is_min:
                node.ex_min = v
                node.facet_loc["exclusiveMinimum"] = loc(key)
            else:
                node.ex_max = v
                node.facet_loc["exclusiveMaximum"] = loc(key)

    # ------------------------------------------------------------- merging
    def _adopt(self, target: SchemaNode, src: SchemaNode) -> None:
        for f in SchemaNode.__dataclass_fields__:
            if f in ("loc",):
                continue
            setattr(target, f, getattr(src, f))

    def _merge_two(self, a: SchemaNode, b: SchemaNode,
                   loc: FacetLoc) -> SchemaNode:
        """Intersection (allOf) of two built nodes."""
        out = SchemaNode(loc=loc)
        # validity / cycles propagate
        if a.invalid:
            out.invalid = a.invalid
        if b.invalid and not out.invalid:
            out.invalid = b.invalid
        out.unsupported = list(a.unsupported) + list(b.unsupported)
        if a.is_cycle:
            # b contributes whatever static facets exist; a is transparent
            self._copy_facets(out, b)
            return out
        if b.is_cycle:
            self._copy_facets(out, a)
            return out

        # types: intersection
        types = [t for t in (a.type, b.type) if t is not None]
        if types:
            inter = set(types[0])
            for t in types[1:]:
                inter &= set(t)
            if not inter and a.type and b.type:
                out.invalid = "allOf 合并后类型互斥（交集为空）"
                self.stats.invalid_schemas += 1
            out.type = [t for t in _TYPE_ORDER if t in inter] + \
                       [t for t in inter if t not in _TYPE_ORDER]
            out.facet_loc["type"] = a.facet_loc.get("type") or \
                b.facet_loc.get("type")

        # enum: intersection
        enums = [e for e in (a.enum, b.enum) if e is not None]
        if enums:
            vals = list(enums[0])
            for e in enums[1:]:
                vals = [v for v in vals if _json_member(v, e)]
            out.enum = vals
            if not vals and a.enum and b.enum:
                out.invalid = "allOf 合并后枚举交集为空"
                self.stats.invalid_schemas += 1
            out.facet_loc["enum"] = a.facet_loc.get("enum") or \
                b.facet_loc.get("enum")

        out.const = a.const if a.const is not UNSET else b.const
        out.nullable = a.nullable or b.nullable
        out.format = a.format or b.format
        out.read_only = a.read_only or b.read_only
        out.write_only = a.write_only or b.write_only
        out.deprecated = a.deprecated or b.deprecated
        out.default = a.default if a.default is not UNSET else b.default

        # properties union (first definition wins per side order)
        out.properties = dict(a.properties)
        for k, v in b.properties.items():
            out.properties.setdefault(k, v)
        out.required = set(a.required) | set(b.required)
        for key, fl in a.facet_loc.items():
            out.facet_loc.setdefault(key, fl)
        for key, fl in b.facet_loc.items():
            out.facet_loc.setdefault(key, fl)

        # items: intersection
        if a.items is not None and b.items is not None:
            out.items = self._merge_two(a.items, b.items,
                                        loc=a.items.loc)
        else:
            out.items = a.items or b.items

        # additionalProperties: false is strongest; true weakest
        av, bv = a.additional, b.additional
        if av is False or bv is False:
            out.additional = False
        elif isinstance(av, SchemaNode) and isinstance(bv, SchemaNode):
            out.additional = self._merge_two(av, bv, loc=av.loc)
        elif isinstance(av, SchemaNode):
            out.additional = av
        elif isinstance(bv, SchemaNode):
            out.additional = bv
        elif av is True or bv is True:
            out.additional = True
        else:
            out.additional = UNSET

        out.one_of = _dedup(a.one_of + b.one_of)
        out.any_of = _dedup(a.any_of + b.any_of)
        if out.one_of:
            out.branch_kind = "oneOf"
        elif out.any_of:
            out.branch_kind = "anyOf"

        out.minimum = _max_opt(a.minimum, b.minimum)
        out.maximum = _min_opt(a.maximum, b.maximum)
        out.ex_min = _max_opt(a.ex_min, b.ex_min)
        out.ex_max = _min_opt(a.ex_max, b.ex_max)
        out.min_length = _max_opt(a.min_length, b.min_length)
        out.max_length = _min_opt(a.max_length, b.max_length)
        out.min_items = _max_opt(a.min_items, b.min_items)
        out.max_items = _min_opt(a.max_items, b.max_items)
        out.min_props = _max_opt(a.min_props, b.min_props)
        out.max_props = _min_opt(a.max_props, b.max_props)
        out.unique_items = (a.unique_items if a.unique_items is None
                            else a.unique_items) and \
                           (b.unique_items if b.unique_items is None
                            else b.unique_items)
        if a.multiple_of is not None and b.multiple_of is not None:
            out.multiple_of = _lcm_numbers(a.multiple_of, b.multiple_of)
        else:
            out.multiple_of = a.multiple_of or b.multiple_of
        if out.multiple_of is not None:
            out.facet_loc["multipleOf"] = a.facet_loc.get("multipleOf") or \
                b.facet_loc.get("multipleOf")

        # mixed allOf + oneOf/anyOf is not reliably decidable
        if (a.one_of or a.any_of) and (b.one_of or b.any_of or b.type):
            pass
        return out

    def _copy_facets(self, out, src):
        for f in SchemaNode.__dataclass_fields__:
            if f in ("loc", "unsupported", "invalid", "is_cycle"):
                continue
            cur = getattr(out, f)
            val = getattr(src, f)
            if f in ("type", "enum", "items", "format", "const",
                     "minimum", "maximum", "ex_min", "ex_max",
                     "min_length", "max_length", "min_items", "max_items",
                     "min_props", "max_props", "unique_items",
                     "multiple_of", "is_bool", "branch_kind", "default"):
                if cur in (None, UNSET):
                    setattr(out, f, val)
            elif f == "additional":
                if cur is UNSET:
                    setattr(out, f, val)
            elif f in ("properties",):
                merged = dict(val)
                merged.update(cur)
                setattr(out, f, merged)
            elif f == "required":
                setattr(out, f, set(cur) | set(val))
            elif f in ("one_of", "any_of"):
                setattr(out, f, _dedup(val + cur))
            elif f == "facet_loc":
                merged_d = dict(val)
                merged_d.update(cur)
                setattr(out, f, merged_d)
            elif f in ("nullable", "read_only", "write_only", "deprecated"):
                setattr(out, f, cur or val)


def _max_opt(a, b):
    return a if b is None else (b if a is None else max(a, b))


def _min_opt(a, b):
    return a if b is None else (b if a is None else min(a, b))


def _dedup(nodes):
    out, seen = [], set()
    for n in nodes:
        k = (n.loc.file, n.loc.pointer)
        if k in seen:
            continue
        seen.add(k)
        out.append(n)
    return out


def _json_member(value, values):
    import json
    key = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return any(json.dumps(v, sort_keys=True, ensure_ascii=False,
                          default=str) == key for v in values)


def _lcm_numbers(a, b):
    try:
        fa, fb = Fraction(str(a)), Fraction(str(b))
        g = Fraction(_gcd(fa.numerator, fb.numerator),
                     _lcm(fa.denominator, fb.denominator))
        return float(fa * fb / g) if g else None
    except (ValueError, ZeroDivisionError):
        return None


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return abs(a)


def _lcm(a, b):
    return abs(a * b) // _gcd(a, b) if a and b else 0
