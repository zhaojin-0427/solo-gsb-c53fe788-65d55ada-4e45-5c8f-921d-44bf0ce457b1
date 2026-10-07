"""Serialize comparison reports into deterministic JSON payloads."""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any

from .services.rules import RULES
from .services.schemagraph import FacetLoc
from .services.comparator import Report, Finding

SCHEMA_VERSION = "1.0"


def findings_version(findings: list[dict[str, Any]]) -> str:
    """Content version for a complete serialized findings list."""
    return hashlib.sha256(
        canonical_json(findings).encode("utf-8")).hexdigest()


def _loc(ctx, loc: FacetLoc | None) -> str | None:
    return ctx.hint(loc)


def finding_to_dict(f: Finding) -> "OrderedDict[str, Any]":
    return OrderedDict([
        ("id", f.id),
        ("rule", f.rule),
        ("rule_title", RULES[f.rule]["title"]),
        ("severity", f.severity),
        ("endpoint", OrderedDict([
            ("path", f.path),
            ("method", f.method),
        ])),
        ("location", f.location or None),
        ("media_type", f.media_type),
        ("status_code", f.status_code),
        ("facet", f.facet),
        ("pointer", OrderedDict([
            ("baseline", f.pointer_baseline),
            ("candidate", f.pointer_candidate),
        ])),
        ("file", OrderedDict([
            ("baseline", f.file_baseline),
            ("candidate", f.file_candidate),
        ])),
        ("message", f.message),
        ("rule_detail", RULES[f.rule]["detail"]),
        ("witness", f.witness),
    ])


def report_to_dict(report: Report, *, baseline_meta: dict | None = None,
                   candidate_meta: dict | None = None,
                   analysis_id: str | None = None) -> OrderedDict:
    findings = [finding_to_dict(f) for f in report.findings]
    endpoints = [OrderedDict([
        ("path", e.path),
        ("method", e.method),
        ("operation_id", e.operation_id),
        ("severity", e.severity),
        ("finding_ids", list(e.finding_ids)),
    ]) for e in report.endpoints]

    # Version only the complete rule-hit payload. Endpoint aggregates are
    # deliberately not included so endpoint recomputation cannot hide a
    # changed finding set.
    report_version_id = findings_version(findings)

    counts = {"breaking": 0, "undetermined": 0, "compatible": 0}
    for f in report.findings:
        counts[f.severity] += 1

    doc = OrderedDict()
    doc["schema_version"] = SCHEMA_VERSION
    doc["analysis_id"] = analysis_id
    doc["report_version"] = report_version_id
    doc["generated_by"] = "OpenAPI Contract Evolution Workbench"
    doc["packages"] = OrderedDict([
        ("baseline", baseline_meta or {}),
        ("candidate", candidate_meta or {}),
    ])
    doc["summary"] = OrderedDict([
        ("total_findings", len(findings)),
        ("breaking", counts["breaking"]),
        ("undetermined", counts["undetermined"]),
        ("compatible", counts["compatible"]),
        ("endpoints_total", len(endpoints)),
        ("endpoints_breaking",
         sum(1 for e in endpoints if e["severity"] == "breaking")),
        ("endpoints_undetermined",
         sum(1 for e in endpoints if e["severity"] == "undetermined")),
        ("endpoints_compatible",
         sum(1 for e in endpoints if e["severity"] == "compatible")),
        ("engine_stats", OrderedDict(sorted(report.stats.items()))),
    ])
    doc["endpoints"] = endpoints
    doc["findings"] = findings
    return doc


def canonical_json(doc: dict) -> str:
    """Deterministic serialization: sorted keys, no insignificant space."""
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), indent=None)


def pretty_json(doc: dict) -> str:
    """Stable, human-readable export (insertion order from report_to_dict)."""
    return json.dumps(doc, ensure_ascii=False, indent=2)


def export_fingerprint(doc: dict) -> str:
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()
