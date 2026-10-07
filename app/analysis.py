"""End-to-end analysis orchestration: load -> compare -> serialize -> store."""
from __future__ import annotations

import hashlib
from typing import Any

from .services.loader import LoadedPackage, load_package, summarize
from .serialization import (canonical_json, export_fingerprint,
                            report_to_dict)
from .services.comparator import compare_packages


def package_envelope(pkg: LoadedPackage) -> dict[str, Any]:
    return {"entry": pkg.entry, "files": pkg.files,
            "warnings": pkg.warnings}


def from_envelope(env: dict[str, Any]) -> LoadedPackage:
    return LoadedPackage(files=env["files"], entry=env["entry"],
                         warnings=env.get("warnings", []))


def analyze(baseline_name: str, baseline_bytes: bytes,
            candidate_name: str, candidate_bytes: bytes) -> dict:
    base_pkg = load_package(baseline_name, baseline_bytes)
    cand_pkg = load_package(candidate_name, candidate_bytes)
    report = compare_packages(base_pkg, cand_pkg)

    base_meta = {"filename": baseline_name, **summarize(base_pkg)}
    cand_meta = {"filename": candidate_name, **summarize(cand_pkg)}

    # deterministic id from package contents, independent of upload order noise
    identity_src = canonical_json({
        "b": _package_fingerprint_material(base_pkg),
        "c": _package_fingerprint_material(cand_pkg),
    })
    analysis_id = hashlib.sha256(identity_src.encode()).hexdigest()[:16]

    doc = report_to_dict(report, baseline_meta=base_meta,
                         candidate_meta=cand_meta,
                         analysis_id=analysis_id)
    return {
        "id": analysis_id,
        "baseline_meta": base_meta,
        "candidate_meta": cand_meta,
        "report_doc": doc,
        "stats": dict(report.stats),
        "fingerprint": export_fingerprint(doc),
        "packages": {
            "baseline": package_envelope(base_pkg),
            "candidate": package_envelope(cand_pkg),
        },
    }


def _package_fingerprint_material(pkg: LoadedPackage) -> dict:
    return {"entry": pkg.entry, "files": pkg.files}


def filter_report(doc: dict, severities: set[str]) -> dict:
    """Return a copy of an export doc restricted to given severity levels.

    Endpoint lists and summary are recomputed so the export stays internally
    consistent; ordering is preserved (deterministic).
    """
    import copy
    out = copy.deepcopy(doc)
    kept = [f for f in out["findings"] if f["severity"] in severities]
    kept_ids = {f["id"] for f in kept}
    out["findings"] = kept
    new_endpoints = []
    for ep in out["endpoints"]:
        ids = [i for i in ep["finding_ids"] if i in kept_ids]
        if not ids:
            continue
        sev = "compatible"
        for f in kept:
            if f["id"] in ids:
                if f["severity"] == "breaking":
                    sev = "breaking"
                    break
                if f["severity"] == "undetermined":
                    sev = "undetermined"
        ep2 = dict(ep)
        ep2["finding_ids"] = ids
        ep2["severity"] = sev
        new_endpoints.append(ep2)
    out["endpoints"] = new_endpoints
    out["summary"]["total_findings"] = len(kept)
    out["summary"]["breaking"] = sum(
        1 for f in kept if f["severity"] == "breaking")
    out["summary"]["undetermined"] = sum(
        1 for f in kept if f["severity"] == "undetermined")
    out["summary"]["compatible"] = sum(
        1 for f in kept if f["severity"] == "compatible")
    out["summary"]["endpoints_total"] = len(new_endpoints)
    out["summary"]["endpoints_breaking"] = sum(
        1 for e in new_endpoints if e["severity"] == "breaking")
    out["summary"]["endpoints_undetermined"] = sum(
        1 for e in new_endpoints if e["severity"] == "undetermined")
    out["summary"]["endpoints_compatible"] = sum(
        1 for e in new_endpoints if e["severity"] == "compatible")
    out["filter"] = {"severities": sorted(severities)}
    return out
