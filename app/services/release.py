"""Release gate evaluation for contract findings.

The gate is deliberately computed from the complete ``report.findings`` list.
Endpoint severities are only an aggregate view, UI filters can hide findings,
and witness self-check state describes only the generated counter-example;
none of those may replace a finding's own ``severity``.

Exceptions are bound to a report content version in addition to analysis ID.
Analysis IDs are derived from the uploaded package contents, so the same ID
can be recomputed after comparator changes. An exception created for an old
finding set must therefore become stale instead of silently licensing new
conclusions.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional

from ..serialization import canonical_json

BLOCKED = "blocked"
PENDING_REVIEW = "pending_review"
RELEASABLE = "releasable"

BREAKING = "breaking"
UNDETERMINED = "undetermined"
COMPATIBLE = "compatible"

ACTIVE = "active"
EXPIRED = "expired"
STALE = "stale"
MISSING_FINDING = "missing_finding"


def findings_version(findings: list[dict[str, Any]]) -> str:
    """Return a SHA-256 version for the complete findings payload."""
    import hashlib
    return hashlib.sha256(
        canonical_json(findings).encode("utf-8")).hexdigest()


def report_version(report: dict[str, Any]) -> str:
    version = report.get("report_version")
    if isinstance(version, str) and version:
        return version
    return findings_version(list(report.get("findings") or []))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise ValueError("时间必须是 ISO 8601 字符串")
    if dt.tzinfo is None:
        raise ValueError("到期时间必须携带时区偏移；UTC 请使用 Z 或 +00:00")
    return dt.astimezone(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso(dt: datetime) -> str:
    return iso_utc(dt)


def _short_version(value: str) -> str:
    return value[:12]


def _exception_record(exc: dict[str, Any]) -> dict[str, Any]:
    expires = parse_datetime(exc["expires_at"])
    created = parse_datetime(exc.get("created_at")) if exc.get("created_at") \
        else expires
    return {
        "analysis_id": exc.get("analysis_id"),
        "report_version": exc["report_version"],
        "finding_id": exc["finding_id"],
        "reason": exc["reason"],
        "expires_at": _iso(expires),
        "created_at": _iso(created),
    }


def build_release_decision(report: dict[str, Any],
                           exceptions: list[dict[str, Any]],
                           now: Optional[datetime] = None
                           ) -> OrderedDict[str, Any]:
    """Evaluate release status using every finding and bound exceptions."""
    current_time = (now or utc_now())
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    current_time = current_time.astimezone(timezone.utc)

    # Complete findings are the only severity source. Do not accept endpoint
    # summaries, filtered lists, or witness self-check states here.
    findings = list(report.get("findings") or [])
    version = report_version(report)
    findings_by_id = {str(f.get("id")): f for f in findings}

    analyzed: list[dict[str, Any]] = []
    active_by_finding: dict[str, dict[str, Any]] = {}
    current_exception_by_finding: dict[str, dict[str, Any]] = {}
    stale_by_finding: dict[str, list[dict[str, Any]]] = {}

    def sort_key(exc: dict[str, Any]):
        try:
            return parse_datetime(exc.get("created_at") or
                                  exc.get("expires_at"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    for raw in sorted(exceptions, key=sort_key, reverse=True):
        exc = _exception_record(raw)
        bound_version = exc["report_version"]
        finding_id = exc["finding_id"]
        expires = parse_datetime(exc["expires_at"])
        current_finding = findings_by_id.get(finding_id)

        if current_finding is None:
            state = MISSING_FINDING
            if bound_version != version:
                reason = (
                    f"例外绑定的是报告版本 {_short_version(bound_version)}；"
                    f"当前完整 findings 已重算为版本 "
                    f"{_short_version(version)}，且 finding {finding_id} "
                    "已消失，旧许可不生效。")
            else:
                reason = (
                    f"当前报告版本 {_short_version(version)} 的完整 findings 中"
                    f"不存在 finding {finding_id}，该例外不能生效。")
            stale_by_finding.setdefault(finding_id, []).append(
                {**exc, "state": state, "reason": reason,
                 "registered_reason": exc["reason"]})
        elif bound_version != version:
            state = STALE
            reason = (
                f"例外绑定的是报告版本 {_short_version(bound_version)}；"
                f"当前完整 findings 已重算为版本 "
                f"{_short_version(version)}，即使 finding ID 仍为 "
                f"{finding_id}，旧许可也不自动沿用。")
            stale_by_finding.setdefault(finding_id, []).append(
                {**exc, "state": state, "reason": reason,
                 "registered_reason": exc["reason"]})
        elif expires <= current_time:
            state = EXPIRED
            reason = (
                f"例外已于 UTC {_iso(expires)} 到期；当前判定时间为 UTC "
                f"{_iso(current_time)}，不能放行该 finding。")
            current_exception_by_finding[finding_id] = {
                **exc, "state": state, "reason": reason,
                "registered_reason": exc["reason"]}
        else:
            state = ACTIVE
            effective_reason = (
                f"例外绑定当前报告版本 {_short_version(version)} 与 finding "
                f"{finding_id}，UTC 到期时间 {_iso(expires)} 晚于当前判定时间 "
                f"{_iso(current_time)}，许可生效。")
            active = {**exc, "state": state, "reason": effective_reason,
                      "registered_reason": exc["reason"]}
            current_exception_by_finding[finding_id] = active
            active_by_finding[finding_id] = active

        state_reason = effective_reason if state == ACTIVE else reason
        analyzed.append({**exc, "state": state, "reason": state_reason,
                         "registered_reason": exc["reason"]})

    items: list[dict[str, Any]] = []
    remaining_breaking = 0
    remaining_undetermined = 0
    waived_breaking = 0
    waived_undetermined = 0

    for f in findings:
        finding_id = str(f.get("id"))
        severity = f.get("severity")
        active = active_by_finding.get(finding_id)
        current_exception = current_exception_by_finding.get(finding_id)
        stale_exceptions = stale_by_finding.get(finding_id) or []

        if severity == BREAKING:
            if active:
                treatment = "waived"
                treatment_reason = active["reason"]
                waived_breaking += 1
            else:
                treatment = BLOCKED
                if current_exception and current_exception["state"] == EXPIRED:
                    treatment_reason = current_exception["reason"]
                elif stale_exceptions:
                    treatment_reason = stale_exceptions[0]["reason"]
                else:
                    treatment_reason = (
                        "完整 report.findings 中该项 severity=breaking；"
                        "未找到绑定当前报告版本、finding ID 且未过期的例外，"
                        "因此构成发布阻断。")
                remaining_breaking += 1
        elif severity == UNDETERMINED:
            if active:
                treatment = "waived"
                treatment_reason = active["reason"]
                waived_undetermined += 1
            else:
                treatment = PENDING_REVIEW
                if current_exception and current_exception["state"] == EXPIRED:
                    treatment_reason = current_exception["reason"]
                elif stale_exceptions:
                    treatment_reason = stale_exceptions[0]["reason"]
                else:
                    treatment_reason = (
                        "完整 report.findings 中该项 severity=undetermined；"
                        "未找到绑定当前报告版本、finding ID 且未过期的例外，"
                        "因此需要人工待核。")
                remaining_undetermined += 1
        else:
            treatment = "not_gating"
            treatment_reason = (
                "完整 report.findings 中该项 severity=compatible；"
                "兼容项本身不参与阻断或待核计数。")

        sc = ((f.get("witness") or {}).get("self_check") or {})
        item = OrderedDict([
            ("finding_id", finding_id),
            ("severity", severity),
            ("rule", f.get("rule")),
            ("rule_title", f.get("rule_title")),
            ("endpoint", f.get("endpoint")),
            ("location", f.get("location")),
            ("media_type", f.get("media_type")),
            ("status_code", f.get("status_code")),
            ("message", f.get("message")),
            ("pointer", f.get("pointer")),
            ("file", f.get("file")),
            ("basis", OrderedDict([
                ("source", "report.findings"),
                ("severity_field", "severity"),
                ("severity", severity),
                ("rule", f.get("rule")),
                ("message", f.get("message")),
                ("witness_self_check", OrderedDict([
                    ("baseline_state", sc.get("baseline_state")),
                    ("candidate_state", sc.get("candidate_state")),
                    ("used_as_severity", False),
                ])),
            ])),
            ("treatment", treatment),
            ("gating", treatment in (BLOCKED, PENDING_REVIEW)),
            ("reason", treatment_reason),
            ("active_exception", active),
            ("current_version_exception",
             current_exception if current_exception and not active else None),
            ("stale_exceptions", stale_exceptions),
        ])
        items.append(item)

    if remaining_breaking:
        decision = BLOCKED
        decision_reason = (
            f"仍有 {remaining_breaking} 个破坏性 finding 未被有效例外覆盖，"
            "发布裁决为阻断。")
    elif remaining_undetermined:
        decision = PENDING_REVIEW
        decision_reason = (
            f"破坏性 finding 已处理，但仍有 {remaining_undetermined} 个"
            "无法判定 finding 未被有效例外覆盖，发布裁决为待核。")
    else:
        decision = RELEASABLE
        decision_reason = (
            "完整 findings 中不存在剩余破坏性或无法判定项；"
            "兼容项不阻断发布，裁决为可放行。")

    state_counts = {ACTIVE: 0, EXPIRED: 0, STALE: 0,
                    MISSING_FINDING: 0}
    for exc in analyzed:
        state_counts[exc["state"]] += 1

    return OrderedDict([
        ("analysis_id", report.get("analysis_id")),
        ("report_version", version),
        ("decided_at", _iso(current_time)),
        ("decision", decision),
        ("decision_label", {
            BLOCKED: "阻断", PENDING_REVIEW: "待核",
            RELEASABLE: "可放行",
        }[decision]),
        ("reason", decision_reason),
        ("evaluation", OrderedDict([
            ("severity_source", "report.findings[*].severity"),
            ("endpoint_summary_used", False),
            ("filtered_findings_used", False),
            ("witness_self_check_used_as_severity", False),
            ("binding_key",
             "analysis_id + report_version + finding_id"),
            ("report_version_algorithm",
             "sha256(canonical_json(report.findings))"),
        ])),
        ("summary", OrderedDict([
            ("total_findings", len(findings)),
            ("breaking",
             sum(1 for f in findings if f.get("severity") == BREAKING)),
            ("undetermined",
             sum(1 for f in findings if f.get("severity") == UNDETERMINED)),
            ("compatible",
             sum(1 for f in findings if f.get("severity") == COMPATIBLE)),
            ("remaining_breaking", remaining_breaking),
            ("remaining_undetermined", remaining_undetermined),
            ("waived_breaking", waived_breaking),
            ("waived_undetermined", waived_undetermined),
            ("exceptions_active", state_counts[ACTIVE]),
            ("exceptions_expired", state_counts[EXPIRED]),
            ("exceptions_stale", state_counts[STALE]),
            ("exceptions_missing_finding",
             state_counts[MISSING_FINDING]),
        ])),
        ("items", items),
        ("exceptions", analyzed),
    ])
