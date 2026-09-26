from app.services.git_clone import (
    GIT_FORBIDDEN,
    GIT_REF_NOT_FOUND,
    _fetch_specs,
    _redact,
    classify_git_error,
    public_github_url,
)


def test_public_github_url() -> None:
    assert public_github_url("example-org", "example-repo") == ("https://github.com/example-org/example-repo.git")


def test_redact_strips_token_and_explains_403() -> None:
    message = _redact("fatal: Write access to repository not granted. 403 token=github_pat_secret", "github_pat_secret")
    assert "github_pat_secret" not in message
    assert "***" in message
    assert "Contents" in message


def test_classify_deleted_branch() -> None:
    assert classify_git_error("warning: Could not find remote branch fix/seo-audit to clone.") == GIT_REF_NOT_FOUND


def test_classify_forbidden() -> None:
    assert classify_git_error("remote: Write access to repository not granted.\nerror: 403") == GIT_FORBIDDEN


def test_fetch_specs_prefer_sha_and_pull_ref() -> None:
    specs = _fetch_specs("abc123", pr_number=110, head_ref="fix/seo-audit")
    assert specs[0] == "abc123"
    assert specs[1] == "pull/110/head"
    assert "fix/seo-audit" in specs
    assert specs.index("abc123") < specs.index("fix/seo-audit")
