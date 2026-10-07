from datetime import datetime, timezone

from app.services.release import (BLOCKED, PENDING_REVIEW, RELEASABLE,
                                   build_release_decision,
                                   findings_version, iso_utc, parse_datetime)


def _finding(fid, severity="breaking", rule="ENUM_NARROWED",
             message="finding"):
    return {
        "id": fid,
        "rule": rule,
        "rule_title": rule,
        "severity": severity,
        "endpoint": {"path": "/p", "method": "GET"},
        "location": "response",
        "media_type": "application/json",
        "status_code": "200",
        "message": message,
        "pointer": {"baseline": None, "candidate": "/x"},
        "file": {"baseline": None, "candidate": None},
        "witness": {"self_check": {
            "baseline_state": "pass",
            "candidate_state": "pending",
        }},
    }


def _report(findings):
    return {
        "analysis_id": "a1",
        "report_version": findings_version(findings),
        "findings": findings,
    }


def _exception(report, fid, reason="accepted risk", expires="2030-01-01T00:00:00Z"):
    return {
        "analysis_id": "a1",
        "report_version": report["report_version"],
        "finding_id": fid,
        "reason": reason,
        "expires_at": expires,
        "created_at": "2026-01-01T00:00:00Z",
    }


def test_decision_reads_full_findings_and_ignores_self_check_pending():
    breaking = _finding("b")
    pending = _finding("u", "undetermined", "UNSUPPORTED_KEYWORD")
    report = _report([breaking, pending])
    decision = build_release_decision(
        report, [], now=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert decision["decision"] == BLOCKED
    assert decision["summary"]["remaining_breaking"] == 1
    assert decision["summary"]["remaining_undetermined"] == 1
    assert decision["evaluation"]["endpoint_summary_used"] is False
    assert decision["evaluation"]["witness_self_check_used_as_severity"] is False
    assert decision["items"][0]["basis"]["severity"] == "breaking"
    assert decision["items"][0]["basis"]["witness_self_check"]
    assert decision["items"][0]["basis"]["witness_self_check"]
    assert decision["items"][0]["basis"]["witness_self_check"]["used_as_severity"] is False


def test_active_exception_waives_breaking_then_undetermined_remains_pending():
    breaking = _finding("b")
    undetermined = _finding("u", "undetermined", "FORMAT_CHANGED")
    report = _report([breaking, undetermined])
    decision = build_release_decision(
        report, [_exception(report, "b")],
        now=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert decision["decision"] == PENDING_REVIEW
    by_id = {x["finding_id"]: x for x in decision["items"]}
    assert by_id["b"]["treatment"] == "waived"
    assert by_id["b"]["active_exception"]["state"] == "active"
    assert by_id["u"]["treatment"] == "pending_review"


def test_all_gating_findings_waived_is_releasable_and_compatible_not_gating():
    report = _report([
        _finding("b"),
        _finding("u", "undetermined", "FORMAT_CHANGED"),
        _finding("c", "compatible", "ENDPOINT_ADDED"),
    ])
    exceptions = [_exception(report, "b"), _exception(report, "u")]
    decision = build_release_decision(report, exceptions)

    assert decision["decision"] == RELEASABLE
    by_id = {x["finding_id"]: x for x in decision["items"]}
    assert by_id["c"]["treatment"] == "not_gating"


def test_expired_exception_does_not_waive_and_explains_reason():
    report = _report([_finding("b")])
    expired = _exception(report, "b", expires="2025-12-31T23:59:59Z")
    decision = build_release_decision(
        report, [expired],
        now=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert decision["decision"] == BLOCKED
    item = decision["items"][0]
    assert item["treatment"] == "blocked"
    assert item["current_version_exception"]["state"] == "expired"
    assert "已于 UTC 2025-12-31T23:59:59Z 到期" in item["reason"]


def test_same_finding_id_in_recomputed_report_makes_old_exception_stale():
    old_finding = _finding("same", message="old conclusion")
    old_report = _report([old_finding])
    new_finding = _finding("same", message="new conclusion")
    new_report = _report([new_finding])
    assert old_report["report_version"] != new_report["report_version"]

    decision = build_release_decision(
        new_report, [_exception(old_report, "same")],
        now=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert decision["decision"] == BLOCKED
    item = decision["items"][0]
    assert item["active_exception"] is None
    assert item["stale_exceptions"][0]["state"] == "stale"
    assert "已重算" in item["reason"]


def test_exception_for_disappeared_finding_is_missing_and_not_waiving():
    report = _report([_finding("present")])
    exception = _exception(report, "gone")
    decision = build_release_decision(report, [exception])

    assert decision["decision"] == BLOCKED
    assert decision["summary"]["exceptions_missing_finding"] == 1
    assert decision["exceptions"][0]["state"] == "missing_finding"


def test_utc_parser_accepts_iso_and_rejects_naive_datetime():
    assert iso_utc(parse_datetime("2026-01-01T01:00:00+01:00")) == \
        "2026-01-01T00:00:00Z"
    try:
        parse_datetime("2026-01-01T00:00:00")
    except ValueError as exc:
        assert "时区" in str(exc)
    else:
        raise AssertionError("naive datetime must be rejected")
