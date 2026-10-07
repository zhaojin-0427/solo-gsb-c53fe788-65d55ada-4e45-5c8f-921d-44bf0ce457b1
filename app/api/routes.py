from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import Response
from datetime import datetime, timezone
from pydantic import BaseModel, Field

from ..analysis import analyze, filter_report, from_envelope
from ..config import settings
from ..services.loader import PackageError
from ..services.coverage import CoverageError, build_coverage
from ..services.gapsamples import GapSampleError, build_gap_samples, \
    render_ndjson
from ..services.migration import MigrationError, build_migration
from ..services.release import build_release_decision, iso_utc, parse_datetime, \
    report_version, utc_now
from ..services.replay import replay
from ..store import store

router = APIRouter(prefix="/api")

SEVERITY_VALUES = {"breaking", "undetermined", "compatible"}
COVERAGE_SIDES = ("baseline", "candidate")


class ReleaseExceptionIn(BaseModel):
    reason: str = Field(min_length=1)
    expires_at: str


@router.get("/health")
def health():
    return {
        "status": "ok",
        "storage": store.status(),
        "max_upload_mb": settings.max_upload_mb,
        "time": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/analyses", status_code=201)
async def create_analysis(baseline: UploadFile, candidate: UploadFile):
    b_bytes = await baseline.read()
    c_bytes = await candidate.read()
    _check_size(baseline.filename, b_bytes)
    _check_size(candidate.filename, c_bytes)
    try:
        result = analyze(baseline.filename or "baseline.yaml", b_bytes,
                         candidate.filename or "candidate.yaml", c_bytes)
    except PackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # defensive: surface parse bugs as 422
        raise HTTPException(status_code=422,
                            detail=f"分析失败: {exc.__class__.__name__}: {exc}")

    created_at = datetime.now(timezone.utc).isoformat()
    store.save(
        analysis_id=result["id"],
        baseline_name=baseline.filename or "baseline",
        candidate_name=candidate.filename or "candidate",
        baseline_meta=result["baseline_meta"],
        candidate_meta=result["candidate_meta"],
        report_doc=result["report_doc"],
        stats=result["stats"],
        created_at=created_at,
        packages=result["packages"])
    body = dict(result["report_doc"])
    body["stored"] = True
    body["storage"] = store.status()
    body["fingerprint"] = result["fingerprint"]
    return body


def _check_size(name, data):
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"文件 {name} 超过大小限制 "
                   f"{settings.max_upload_mb}MB")


@router.get("/analyses")
def list_analyses(limit: int = 50):
    return {"items": store.list_analyses(min(max(limit, 1), 200)),
            "storage": store.status()}


@router.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: str):
    record = store.get(analysis_id)
    if not record:
        raise HTTPException(status_code=404, detail="分析不存在")
    return record["report"]


@router.delete("/analyses/{analysis_id}")
def delete_analysis(analysis_id: str):
    if not store.get(analysis_id):
        raise HTTPException(status_code=404, detail="分析不存在")
    store.delete(analysis_id)
    return {"deleted": analysis_id}


def _parse_severity(severity: str | None) -> set[str]:
    if not severity:
        return set(SEVERITY_VALUES)
    values = {s.strip() for s in severity.split(",") if s.strip()}
    unknown = values - SEVERITY_VALUES
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"未知严重度筛选: {sorted(unknown)}，"
                   f"可选 breaking/undetermined/compatible")
    return values


@router.get("/analyses/{analysis_id}/export")
def export_analysis(analysis_id: str, severity: str | None = None):
    record = store.get(analysis_id)
    if not record:
        raise HTTPException(status_code=404, detail="分析不存在")
    doc = record["report"]
    sev = _parse_severity(severity)
    if sev != SEVERITY_VALUES:
        doc = filter_report(doc, sev)
    coverage = store.get_coverage(analysis_id) or {}
    if coverage.get("baseline") is not None or \
            coverage.get("candidate") is not None:
        doc = dict(doc)
        doc["call_coverage"] = {
            side: coverage[side] for side in COVERAGE_SIDES
            if coverage.get(side) is not None}
    migration = store.get_migration(analysis_id)
    if migration is not None:
        doc = dict(doc)
        doc["request_migration"] = migration
    payload = _dumps_deterministic(doc)
    suffix = "all" if sev == SEVERITY_VALUES else "-".join(sorted(sev))
    filename = f"contract-report-{analysis_id}-{suffix}.json"
    return Response(
        content=payload, media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename}"',
                 "X-Report-Fingerprint": _fingerprint(payload)})


@router.get("/analyses/{analysis_id}/release-decision")
def get_release_decision(analysis_id: str):
    record = store.get(analysis_id)
    if not record:
        raise HTTPException(status_code=404, detail="分析不存在")
    exceptions = store.list_release_exceptions(analysis_id)
    return build_release_decision(record["report"], exceptions)


@router.put("/analyses/{analysis_id}/release-exceptions/{finding_id}")
@router.post("/analyses/{analysis_id}/release-exceptions/{finding_id}")
def register_release_exception(analysis_id: str, finding_id: str,
                               payload: ReleaseExceptionIn):
    record = store.get(analysis_id)
    if not record:
        raise HTTPException(status_code=404, detail="分析不存在")
    report = record["report"]
    finding = next((f for f in report.get("findings", [])
                    if f.get("id") == finding_id), None)
    if not finding:
        raise HTTPException(
            status_code=404,
            detail=("finding 不存在：例外只能登记到当前完整 report.findings "
                    f"中存在的 ID；未知 ID {finding_id!r} 已拒绝"))
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="例外理由不能为空")
    try:
        expires = parse_datetime(payload.expires_at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    now = utc_now()
    if expires <= now:
        raise HTTPException(
            status_code=400,
            detail=("例外到期时间必须晚于当前 UTC 时间；当前为 "
                    f"{iso_utc(now)}，提交值为 {iso_utc(expires)}"))
    version = report_version(report)
    exception = {
        "analysis_id": analysis_id,
        "report_version": version,
        "finding_id": finding_id,
        "reason": reason,
        "expires_at": iso_utc(expires),
        "created_at": iso_utc(now),
    }
    store.save_release_exception(exception)
    decision = build_release_decision(
        report, store.list_release_exceptions(analysis_id), now=now)
    return {"exception": exception, "decision": decision,
            "stored": True, "storage": store.status()}


@router.post("/analyses/{analysis_id}/findings/{finding_id}/replay")
async def replay_finding(request: Request, analysis_id: str,
                         finding_id: str):
    record = store.get(analysis_id)
    if not record:
        raise HTTPException(status_code=404, detail="分析不存在")
    packages = store.get_packages(analysis_id)
    if not packages:
        raise HTTPException(status_code=409,
                            detail="原始文档包不可用，无法重放")
    finding = None
    for f in record["report"]["findings"]:
        if f["id"] == finding_id:
            finding = f
            break
    if not finding:
        raise HTTPException(status_code=404, detail="结果不存在")

    override = None
    try:
        body = await request.json()
    except Exception:
        body = None
    if isinstance(body, dict) and "instance" in body:
        override = body["instance"]

    witness = finding.get("witness") or {}
    # replay module expects side descriptors
    witness = dict(witness)
    witness.setdefault("direction",
                       "response" if finding.get("location") == "response"
                       else "request")
    # structural replay dispatches on witness.kind but needs the rule to pick
    # the right semantic checks; backfill context for older stored reports
    witness.setdefault("rule", finding.get("rule"))
    for key in ("location", "media_type", "status_code"):
        witness.setdefault(key, finding.get(key))
    pkg_b = from_envelope(packages["baseline"])
    pkg_c = from_envelope(packages["candidate"])
    result = replay(pkg_b, pkg_c, witness, instance_override=override,
                    rule=finding.get("rule"))
    result["finding_id"] = finding_id
    result["rule"] = finding["rule"]
    result["message"] = finding["message"]
    return result


def _dumps_deterministic(doc) -> str:
    import json
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _fingerprint(payload: str) -> str:
    import hashlib
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- 调用覆盖
@router.post("/analyses/{analysis_id}/coverage/{side}", status_code=201)
async def upload_coverage(analysis_id: str, side: str, file: UploadFile):
    if side not in COVERAGE_SIDES:
        raise HTTPException(
            status_code=400,
            detail=f"未知文档侧 {side!r}，可选 baseline/candidate")
    record = store.get(analysis_id)
    if not record:
        raise HTTPException(status_code=404, detail="分析不存在")
    packages = store.get_packages(analysis_id)
    if not packages:
        raise HTTPException(status_code=409,
                            detail="原始文档包不可用，无法计算调用覆盖")
    raw = await file.read()
    _check_size(file.filename, raw)
    pkg = from_envelope(packages[side])
    try:
        doc = build_coverage(pkg, side, raw,
                             source_file=file.filename or "calls.ndjson")
    except CoverageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # defensive: surface engine bugs as 422
        raise HTTPException(status_code=422,
                            detail=f"调用覆盖计算失败: "
                                   f"{exc.__class__.__name__}: {exc}")
    store.save_coverage(analysis_id, side, doc)
    body = dict(doc)
    body["stored"] = True
    body["storage"] = store.status()
    return body


@router.get("/analyses/{analysis_id}/coverage")
def list_coverage(analysis_id: str):
    if not store.get(analysis_id):
        raise HTTPException(status_code=404, detail="分析不存在")
    coverage = store.get_coverage(analysis_id) or {}
    out = []
    for side in COVERAGE_SIDES:
        doc = coverage.get(side)
        out.append({
            "side": side,
            "available": doc is not None,
            "source_file": doc.get("source_file") if doc else None,
            "summary": doc.get("summary") if doc else None,
        })
    return {"analysis_id": analysis_id, "sides": out,
            "storage": store.status()}


@router.get("/analyses/{analysis_id}/coverage/{side}")
def get_coverage(analysis_id: str, side: str):
    if side not in COVERAGE_SIDES:
        raise HTTPException(
            status_code=400,
            detail=f"未知文档侧 {side!r}，可选 baseline/candidate")
    if not store.get(analysis_id):
        raise HTTPException(status_code=404, detail="分析不存在")
    coverage = store.get_coverage(analysis_id) or {}
    doc = coverage.get(side)
    if doc is None:
        raise HTTPException(status_code=404,
                            detail=f"该分析尚未上传{side}侧调用覆盖")
    return doc


@router.delete("/analyses/{analysis_id}/coverage/{side}", status_code=200)
def delete_coverage(analysis_id: str, side: str):
    if side not in COVERAGE_SIDES:
        raise HTTPException(
            status_code=400,
            detail=f"未知文档侧 {side!r}，可选 baseline/candidate")
    if not store.get(analysis_id):
        raise HTTPException(status_code=404, detail="分析不存在")
    coverage = store.get_coverage(analysis_id) or {}
    if coverage.get(side) is None:
        raise HTTPException(status_code=404,
                            detail=f"该分析尚未上传{side}侧调用覆盖")
    store.delete_coverage(analysis_id, side)
    return {"deleted": analysis_id, "side": side}


@router.get("/analyses/{analysis_id}/coverage/{side}/export")
def export_coverage(analysis_id: str, side: str):
    if side not in COVERAGE_SIDES:
        raise HTTPException(
            status_code=400,
            detail=f"未知文档侧 {side!r}，可选 baseline/candidate")
    if not store.get(analysis_id):
        raise HTTPException(status_code=404, detail="分析不存在")
    coverage = store.get_coverage(analysis_id) or {}
    doc = coverage.get(side)
    if doc is None:
        raise HTTPException(status_code=404,
                            detail=f"该分析尚未上传{side}侧调用覆盖")
    payload = _dumps_deterministic(doc)
    filename = f"call-coverage-{analysis_id}-{side}.json"
    return Response(
        content=payload, media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename}"',
                 "X-Coverage-Fingerprint": _fingerprint(payload)})


def _load_gap_inputs(analysis_id: str, side: str):
    """Shared preconditions for gap-sample endpoints."""
    if side not in COVERAGE_SIDES:
        raise HTTPException(
            status_code=400,
            detail=f"未知文档侧 {side!r}，可选 baseline/candidate")
    if not store.get(analysis_id):
        raise HTTPException(status_code=404, detail="分析不存在")
    coverage = store.get_coverage(analysis_id) or {}
    doc = coverage.get(side)
    if doc is None:
        raise HTTPException(
            status_code=409,
            detail=f"该分析尚未保存{side}侧调用覆盖结果，"
                   "请先上传 NDJSON 调用记录生成覆盖后再补测缺口")
    packages = store.get_packages(analysis_id)
    if not packages or not packages.get(side):
        raise HTTPException(status_code=409,
                            detail="原始文档包不可用，无法生成补测样本")
    return doc, from_envelope(packages[side])


@router.get("/analyses/{analysis_id}/coverage/{side}/gap-samples")
def get_gap_samples(analysis_id: str, side: str):
    """为已保存覆盖结果的未覆盖格生成补测样本（JSON 说明文档）。"""
    doc, pkg = _load_gap_inputs(analysis_id, side)
    try:
        result = build_gap_samples(pkg, side, doc)
    except GapSampleError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return result


@router.get("/analyses/{analysis_id}/coverage/{side}/gap-samples.ndjson")
def download_gap_samples_ndjson(analysis_id: str, side: str):
    """下载补测样本 NDJSON（无缺口时为空文件；缺前置条件时明确报错）。"""
    doc, pkg = _load_gap_inputs(analysis_id, side)
    try:
        result = build_gap_samples(pkg, side, doc)
    except GapSampleError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    payload = render_ndjson(result)
    filename = f"coverage-gap-samples-{analysis_id}-{side}.ndjson"
    return Response(
        content=payload, media_type="application/x-ndjson",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename}"',
                 "X-Gap-Samples-Fingerprint": _fingerprint(
                     payload.decode("utf-8")),
                 "X-Gap-Samples-Total": str(result["summary"]["gaps_total"]),
                 "X-Gap-Samples-Generated":
                     str(result["summary"]["samples_generated"])})


# --------------------------------------------------------------- 请求迁移
@router.post("/analyses/{analysis_id}/migration", status_code=201)
async def upload_migration(analysis_id: str, file: UploadFile):
    if not store.get(analysis_id):
        raise HTTPException(status_code=404, detail="分析不存在")
    packages = store.get_packages(analysis_id)
    if not packages:
        raise HTTPException(status_code=409,
                            detail="原始文档包不可用，无法计算请求迁移评估")
    raw = await file.read()
    _check_size(file.filename, raw)
    pkg_b = from_envelope(packages["baseline"])
    pkg_c = from_envelope(packages["candidate"])
    try:
        doc = build_migration(
            pkg_b, pkg_c, raw,
            source_file=file.filename or "requests.ndjson")
    except MigrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # defensive: surface engine bugs as 422
        raise HTTPException(status_code=422,
                            detail=f"请求迁移评估计算失败: "
                                   f"{exc.__class__.__name__}: {exc}")
    store.save_migration(analysis_id, doc)
    body = dict(doc)
    body["stored"] = True
    body["storage"] = store.status()
    return body


@router.get("/analyses/{analysis_id}/migration")
def get_migration(analysis_id: str):
    if not store.get(analysis_id):
        raise HTTPException(status_code=404, detail="分析不存在")
    doc = store.get_migration(analysis_id)
    if doc is None:
        raise HTTPException(status_code=404,
                            detail="该分析尚未上传请求迁移评估")
    return doc


@router.delete("/analyses/{analysis_id}/migration", status_code=200)
def delete_migration(analysis_id: str):
    if not store.get(analysis_id):
        raise HTTPException(status_code=404, detail="分析不存在")
    if store.get_migration(analysis_id) is None:
        raise HTTPException(status_code=404,
                            detail="该分析尚未上传请求迁移评估")
    store.delete_migration(analysis_id)
    return {"deleted": analysis_id}


@router.get("/analyses/{analysis_id}/migration/export")
def export_migration(analysis_id: str):
    if not store.get(analysis_id):
        raise HTTPException(status_code=404, detail="分析不存在")
    doc = store.get_migration(analysis_id)
    if doc is None:
        raise HTTPException(status_code=404,
                            detail="该分析尚未上传请求迁移评估")
    payload = _dumps_deterministic(doc)
    filename = f"request-migration-{analysis_id}.json"
    return Response(
        content=payload, media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename}"',
                 "X-Migration-Fingerprint": _fingerprint(payload)})
