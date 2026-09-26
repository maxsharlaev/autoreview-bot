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
