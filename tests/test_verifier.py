from app.services.verifier import parse_json_payload, verify_review

HEAD = "a" * 40


def _payload(**overrides):
    data = {
        "schema_version": 1,
        "reviewed_head_sha": HEAD,
        "previous_reviewed_head_sha": None,
        "summary": "Auth check is missing on lookup.",
        "task_alignment": {
            "issue_key": "ABC-1",
            "status": "satisfied",
            "unmet_acceptance_criteria": [],
        },
        "previous_findings": [],
        "findings": [
            {
                "id": "auth-scope",
                "severity": "P1",
                "confidence": 0.9,
                "category": "authorization",
                "path": "app/service.py",
                "line": 10,
                "title": "Missing owner scope",
                "scenario": "Чужой id проходит.",
                "evidence": "get_by_id без фильтра.",
                "recommendation": "Добавить scoped lookup.",
                "blocking_candidate": True,
            }
        ],
        "coverage": {
            "files_total": 1,
            "files_reviewed": 1,
            "truncated": False,
            "skipped_paths": [],
        },
    }
    data.update(overrides)
    return data


def test_verify_keeps_in_diff_finding() -> None:
    verified = verify_review(_payload(), expected_head_sha=HEAD, changed_paths={"app/service.py"}, max_findings=20)
    assert len(verified.findings) == 1
    assert verified.findings[0].stable_id == "auth-scope"


def test_drops_paths_outside_diff() -> None:
    verified = verify_review(_payload(), expected_head_sha=HEAD, changed_paths={"other.py"}, max_findings=20)
    assert verified.findings == []


def test_keeps_finding_on_previous_path() -> None:
    verified = verify_review(
        _payload(
            previous_findings=[
                {
                    "id": "auth-scope",
                    "status": "still_open",
                    "evidence": "still no owner filter",
                    "path": "app/service.py",
                    "line": 10,
                }
            ]
        ),
        expected_head_sha=HEAD,
        changed_paths={"other.py"},
        max_findings=20,
    )
    assert len(verified.findings) == 1
    assert verified.findings[0].stable_id == "auth-scope"


def test_parse_fenced_json() -> None:
    payload = parse_json_payload('```json\n{"ok": true}\n```')
    assert payload == {"ok": True}


def test_p3_severity_accepted() -> None:
    """P3 (code improvement) severity should be accepted without coercion."""
    findings = [
        {
            "id": "style-fix",
            "severity": "P3",
            "confidence": 0.7,
            "category": "style",
            "path": "app/utils.py",
            "line": 5,
            "title": "Consider using list comprehension",
            "scenario": "Loop could be more pythonic.",
            "evidence": "for loop with append",
            "recommendation": "Use [x for x in items].",
            "blocking_candidate": False,
        }
    ]
    verified = verify_review(
        _payload(findings=findings),
        expected_head_sha=HEAD,
        changed_paths={"app/utils.py"},
        max_findings=20,
    )
    assert len(verified.findings) == 1
    assert verified.findings[0].severity == "P3"


def test_unknown_severity_rejected_by_schema() -> None:
    """Unknown severities should be rejected by schema validation."""
    import jsonschema
    import pytest

    findings = [
        {
            "id": "unknown-sev",
            "severity": "P9",
            "confidence": 0.5,
            "category": "misc",
            "path": "app/other.py",
            "line": 1,
            "title": "Unknown severity finding",
            "scenario": "Something unusual.",
            "evidence": "Unusual code.",
            "recommendation": "Review manually.",
            "blocking_candidate": False,
        }
    ]
    with pytest.raises(jsonschema.ValidationError):
        verify_review(
            _payload(findings=findings),
            expected_head_sha=HEAD,
            changed_paths={"app/other.py"},
            max_findings=20,
        )
