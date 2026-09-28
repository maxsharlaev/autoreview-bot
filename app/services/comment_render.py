from __future__ import annotations

from dataclasses import dataclass, field

from app.config import LanguageYaml, PublicReposYaml
from app.services.constants import REVIEW_MARKER
from app.services.issue_key import remove_issue_key
from app.services.verifier import FindingView, VerifiedReview
from app.services.visibility import Visibility


@dataclass
class FindingTransitionView:
    stable_id: str
    status: str
    path: str
    line: int | None = None
    severity: str = ""
    title: str = ""
    scenario: str = ""
    evidence: str = ""
    recommendation: str = ""
    category: str = ""


@dataclass
class RenderInput:
    repository: str
    pr_number: int
    head_sha: str
    mode: str
    run_id: str
    issue_key: str | None
    jira_warning: str | None
    verified: VerifiedReview
    transitions: list[FindingTransitionView]
    duration_ms: int | None
    model: str
    error_code: str | None = None
    language: LanguageYaml | None = None
    visibility: Visibility | None = None
    public_repos: PublicReposYaml | None = None
    private_channels_configured: bool = False
    public_alignment: str | None = None
    redacted_prior_ids: set[str] = field(default_factory=set)


_LABELS = {
    "en": {
        "scenario": "Scenario",
        "evidence": "Evidence",
        "recommendation": "Recommendation",
        "transitions": "Previous finding transitions",
        "status": "Status",
        "finding": "Finding",
        "path": "Path",
        "findings": "Findings",
        "clean": "No blocking findings.",
        "previous_open": "Still open from previous review",
        "unmet": "Unmet acceptance criteria",
        "skipped": "Skipped files",
        "verdict": "Verdict",
        "mode": "Mode",
        "alignment": "Task alignment",
        "coverage": "Coverage",
        "run": "Run",
        "error": "Error",
        "files": "files",
        "model": "model",
        "public_summary": "Review completed. See the findings below.",
        "public_issue": "Potential issue.",
        "prior_issue": "Previously reported issue.",
        "details_hidden": "Details are hidden because the repository is public.",
    },
    "ru": {
        "scenario": "Коротко",
        "evidence": "Доказательство",
        "recommendation": "Рекомендация",
        "transitions": "Переходы прошлых замечаний",
        "status": "Статус",
        "finding": "Замечание",
        "path": "Путь",
        "findings": "Замечания",
        "clean": "Критических замечаний нет.",
        "previous_open": "Ещё открыто с прошлого ревью",
        "unmet": "Незакрытые критерии приёмки",
        "skipped": "Пропущенные файлы",
        "verdict": "Вердикт",
        "mode": "Режим",
        "alignment": "Соответствие задаче",
        "coverage": "Покрытие",
        "run": "Запуск",
        "error": "Ошибка",
        "files": "файлов",
        "model": "модель",
        "public_summary": "Ревью завершено. Замечания приведены ниже.",
        "public_issue": "Возможная проблема.",
        "prior_issue": "Ранее найденная проблема.",
        "details_hidden": "Подробности скрыты, поскольку репозиторий публичный.",
    },
}


def _count(findings: list[FindingView], severity: str) -> int:
    return sum(1 for item in findings if item.severity == severity)


def _public_alignment(status: str) -> str:
    return {"satisfied": "matches", "unmet": "mismatch", "unclear": "unknown"}.get(status, "unknown")


def _redacted_prior_card(severity: str, path: str, line: int | None, labels: dict[str, str]) -> list[str]:
    loc = f"{path}:{line}" if line else path
    return [f"#### [{severity}] {labels['prior_issue']}", f"`previous` · `{loc}`", ""]


def _redacted_finding_card(severity: str, path: str, line: int | None, labels: dict[str, str]) -> list[str]:
    loc = f"{path}:{line}" if line else path
    return [f"#### [{severity}] {labels['public_issue']}", f"`finding` · `{loc}`", ""]


def _finding_card(
    *,
    severity: str,
    title: str,
    stable_id: str,
    category: str,
    path: str,
    line: int | None,
    scenario: str,
    evidence: str,
    recommendation: str,
    labels: dict[str, str],
    badge: str | None = None,
) -> list[str]:
    loc = f"{path}:{line}" if line else path
    heading = f"#### [{badge}] {title}" if badge else f"#### [{severity}] {title}"
    meta = f"`{stable_id}` · `{category}` · `{loc}`" if category else f"`{stable_id}` · `{loc}`"
    return [
        heading,
        meta,
        "",
        f"- {labels['scenario']}: {scenario or '—'}",
        f"- {labels['evidence']}: {evidence or '—'}",
        f"- {labels['recommendation']}: {recommendation or '—'}",
        "",
    ]


def _transition_table(transitions: list[FindingTransitionView], labels: dict[str, str]) -> list[str]:
    lines = [
        f"### {labels['transitions']}",
        "",
        f"| {labels['status']} | {labels['finding']} | {labels['path']} |",
        "| --- | --- | --- |",
    ]
    for item in transitions:
        loc = f"{item.path}:{item.line}" if item.line else item.path
        title = item.title or item.stable_id
        lines.append(f"| `{item.status}` | {title} | `{loc}` |")
    lines.append("")
    return lines


def render_sticky_comment(data: RenderInput) -> str:
    labels = _LABELS[(data.language or LanguageYaml()).details]
    verified = data.verified
    public = data.visibility != "private"
    policy = data.public_repos or PublicReposYaml()
    redact_findings = public and policy.security_findings == "redact"
    prior_ids = data.redacted_prior_ids if public else set()
    p0 = _count(verified.findings, "P0")
    p1 = _count(verified.findings, "P1")
    p2 = _count(verified.findings, "P2")
    p3 = _count(verified.findings, "P3")
    verdict = "findings" if (p0 or p1 or p2) else "clean"
    if data.error_code:
        verdict = data.error_code

    alignment = (
        (data.public_alignment or _public_alignment(verified.task_alignment_status))
        if public
        else verified.task_alignment_status
    )
    issue = (data.issue_key or "none") if public else (data.issue_key or verified.issue_key or "none")
    if public and policy.jira_disclosure == "none":
        issue = "none"
    lines = [
        REVIEW_MARKER,
        "## AI Review (advisory)",
        "",
        f"**{labels['verdict']}:** {verdict}",
        f"**SHA:** `{data.head_sha}`",
        f"**{labels['mode']}:** `{data.mode}`",
        f"**{labels['alignment']}:** `{alignment}` (`{issue}`)",
        f"**{labels['findings']}:** P0={p0} · P1={p1} · P2={p2} · P3={p3}",
        (
            f"**{labels['coverage']}:** {verified.coverage.get('files_reviewed', 0)}/"
            f"{verified.coverage.get('files_total', 0)} {labels['files']}"
        ),
        f"**{labels['run']}:** `{data.run_id}` · {labels['model']} `{data.model}`",
    ]
    if data.jira_warning:
        lines.append(f"**Jira:** `{data.jira_warning}`")
    if data.error_code:
        lines.append(f"**{labels['error']}:** `{data.error_code}`")
    has_findings = bool(verified.findings or data.transitions)
    safe_summary = labels["public_summary"] if redact_findings and has_findings else verified.summary.strip()
    lines.extend(["", safe_summary, "", "---", "", f"### {labels['findings']}", ""])

    if not verified.findings and not data.error_code:
        lines.append(labels["clean"])
    for item in verified.findings:
        if item.stable_id in prior_ids:
            lines.extend(_redacted_prior_card(item.severity, item.path, item.line, labels))
            continue
        if redact_findings:
            lines.extend(_redacted_finding_card(item.severity, item.path, item.line, labels))
            continue
        lines.extend(
            _finding_card(
                severity=item.severity,
                title=item.title,
                stable_id=item.stable_id,
                category=item.category,
                path=item.path,
                line=item.line,
                scenario=item.scenario,
                evidence=item.evidence,
                recommendation=item.recommendation,
                labels=labels,
            )
        )

    transitions = data.transitions
    if public:
        transitions = [
            FindingTransitionView(
                stable_id=item.stable_id,
                status=item.status,
                path=item.path,
                line=item.line,
                title=(
                    labels["prior_issue"]
                    if item.stable_id in prior_ids
                    else labels["public_issue"]
                    if redact_findings
                    else item.title
                ),
            )
            for item in transitions
        ]
    if transitions:
        lines.extend(_transition_table(transitions, labels))

    shown = {item.stable_id for item in verified.findings}
    leftover_open = [
        item
        for item in data.transitions
        if item.status in {"still_open", "regressed", "needs_human"} and item.stable_id not in shown
    ]
    if leftover_open:
        lines.extend([f"### {labels['previous_open']}", ""])
        for item in leftover_open:
            if item.stable_id in prior_ids:
                lines.extend(_redacted_prior_card(item.severity, item.path, item.line, labels))
                continue
            if redact_findings:
                lines.extend(_redacted_finding_card(item.severity, item.path, item.line, labels))
                continue
            lines.extend(
                _finding_card(
                    severity=item.severity,
                    title=item.title or item.stable_id,
                    stable_id=item.stable_id,
                    category="",
                    path=item.path,
                    line=item.line,
                    scenario=item.scenario,
                    evidence=item.evidence,
                    recommendation=item.recommendation,
                    labels=labels,
                    badge=item.status,
                )
            )

    unmet = [] if public else verified.unmet_acceptance_criteria
    if unmet:
        lines.extend([f"### {labels['unmet']}", ""])
        lines.extend(f"- {item}" for item in unmet)
        lines.append("")

    skipped = verified.coverage.get("skipped_paths") or []
    if skipped:
        lines.append(f"<details><summary>{labels['skipped']}</summary>")
        lines.append("")
        lines.extend(f"- `{path}`" for path in skipped[:50])
        lines.append("")
        lines.append("</details>")

    if redact_findings and not data.private_channels_configured and verified.findings:
        lines.extend(["", labels["details_hidden"]])

    result = "\n".join(lines).strip() + "\n"
    if public and policy.jira_disclosure == "none":
        result = remove_issue_key(result, data.issue_key)
    return result


def render_jira_comment(data: RenderInput) -> str:
    verified = data.verified
    p0 = _count(verified.findings, "P0")
    p1 = _count(verified.findings, "P1")
    p2 = _count(verified.findings, "P2")
    p3 = _count(verified.findings, "P3")
    alignment = data.public_alignment or verified.task_alignment_status
    lines = [
        "AI Review",
        f"Caught: PR #{data.pr_number} ({data.repository}) sha {data.head_sha[:12]}",
        f"Checked: task {data.issue_key or 'none'}, alignment {alignment}",
        f"Result: P0={p0} P1={p1} P2={p2} P3={p3} mode={data.mode}",
        f"Run: {data.run_id}",
    ]
    if data.visibility != "private" and (data.public_repos or PublicReposYaml()).security_findings == "redact":
        for item in verified.findings:
            lines.extend(
                [
                    f"[{item.severity}] {item.title} ({item.path}:{item.line or '?'})",
                    f"Scenario: {item.scenario}",
                    f"Evidence: {item.evidence}",
                    f"Recommendation: {item.recommendation}",
                ]
            )
    return "\n".join(lines)


def render_slack_review(data: RenderInput) -> str:
    return render_jira_comment(data)
