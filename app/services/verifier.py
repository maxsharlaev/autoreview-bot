from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from app.paths import data_file

SEVERITIES = {"P0", "P1", "P2", "P3"}
ALIGNMENT = {"satisfied", "unclear", "unmet"}
PREV_STATUSES = {"resolved", "still_open", "regressed", "obsolete", "needs_human"}


@dataclass
class FindingView:
    stable_id: str
    severity: str
    confidence: float | None
    category: str
    path: str
    line: int | None
    title: str
    scenario: str
    evidence: str
    recommendation: str
    blocking_candidate: bool


@dataclass
class PreviousFindingView:
    stable_id: str
    status: str
    evidence: str
    path: str
    line: int | None


@dataclass
class VerifiedReview:
    summary: str
    task_alignment_status: str
    issue_key: str | None
    unmet_acceptance_criteria: list[str]
    findings: list[FindingView]
    previous_findings: list[PreviousFindingView]
    coverage: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)


def _schema() -> dict[str, Any]:
    path = data_file("schemas", "review_output.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _stable_id(finding: dict[str, Any]) -> str:
    provided = str(finding.get("id") or "").strip()
    if provided and provided != "stable-content-hash":
        return provided[:64]
    basis = "|".join(
        [
            str(finding.get("category") or ""),
            str(finding.get("path") or ""),
            str(finding.get("title") or ""),
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def parse_json_payload(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


def verify_review(
    payload: dict[str, Any],
    *,
    expected_head_sha: str,
    changed_paths: set[str],
    max_findings: int,
) -> VerifiedReview:
    jsonschema.validate(payload, _schema())
    reviewed_sha = payload.get("reviewed_head_sha") or expected_head_sha
    if reviewed_sha != expected_head_sha:
        payload["reviewed_head_sha"] = expected_head_sha

    findings: list[FindingView] = []
    seen: set[str] = set()
    previous_paths = {
        str(item.get("path") or "") for item in payload.get("previous_findings") or [] if str(item.get("path") or "")
    }
    for item in payload.get("findings") or []:
        path = str(item.get("path") or "")
        if changed_paths and path and path not in changed_paths and path not in previous_paths:
            continue
        severity = str(item.get("severity") or "P2")
        if severity not in SEVERITIES:
            severity = "P2"
        stable_id = _stable_id(item)
        if stable_id in seen:
            continue
        seen.add(stable_id)
        confidence = item.get("confidence")
        findings.append(
            FindingView(
                stable_id=stable_id,
                severity=severity,
                confidence=float(confidence) if confidence is not None else None,
                category=str(item.get("category") or ""),
                path=path,
                line=int(item["line"]) if item.get("line") is not None else None,
                title=str(item.get("title") or ""),
                scenario=str(item.get("scenario") or ""),
                evidence=str(item.get("evidence") or ""),
                recommendation=str(item.get("recommendation") or ""),
                blocking_candidate=bool(item.get("blocking_candidate")),
            )
        )
        if len(findings) >= max_findings:
            break

    previous: list[PreviousFindingView] = []
    for item in payload.get("previous_findings") or []:
        status = str(item.get("status") or "needs_human")
        if status not in PREV_STATUSES:
            status = "needs_human"
        previous.append(
            PreviousFindingView(
                stable_id=str(item.get("id") or ""),
                status=status,
                evidence=str(item.get("evidence") or ""),
                path=str(item.get("path") or ""),
                line=int(item["line"]) if item.get("line") is not None else None,
            )
        )

    alignment = payload.get("task_alignment") or {}
    status = str(alignment.get("status") or "unclear")
    if status not in ALIGNMENT:
        status = "unclear"
    coverage = payload.get("coverage") or {}
    return VerifiedReview(
        summary=str(payload.get("summary") or ""),
        task_alignment_status=status,
        issue_key=alignment.get("issue_key"),
        unmet_acceptance_criteria=list(alignment.get("unmet_acceptance_criteria") or []),
        findings=findings,
        previous_findings=previous,
        coverage={
            "files_total": int(coverage.get("files_total") or 0),
            "files_reviewed": int(coverage.get("files_reviewed") or 0),
            "truncated": bool(coverage.get("truncated")),
            "skipped_paths": list(coverage.get("skipped_paths") or []),
        },
        raw=payload,
    )
