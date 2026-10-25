"""Documentation coverage: config keys, env variables and relative links."""

from __future__ import annotations

import re
import typing
from pathlib import Path

import pytest
from app.config import AppConfig, Settings
from app.owners.schema import OwnerYaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
CONFIGURATION = (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
ENVIRONMENT = (ROOT / "docs" / "ENVIRONMENT.md").read_text(encoding="utf-8")

# Owner sections that override top-level sections field by field.
OWNER_POLICY_SECTIONS = {"public_repos", "language", "features", "pr_description", "size_guard", "progress_status"}


def _submodel(annotation: object) -> tuple[type[BaseModel] | None, bool]:
    """Return (model, is_mapping) for a field annotation that contains a pydantic model."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation, False
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is dict and args:
        model, _ = _submodel(args[-1])
        return model, model is not None
    for arg in args:
        model, is_mapping = _submodel(arg)
        if model is not None:
            return model, is_mapping
    return None, False


def _paths(model: type[BaseModel], prefix: str = "") -> list[str]:
    out: list[str] = []
    for name, field in model.model_fields.items():
        path = prefix + name
        sub, is_mapping = _submodel(field.annotation)
        if sub is None:
            out.append(path)
        else:
            out.extend(_paths(sub, path + (".<KEY>." if is_mapping else ".")))
    return out


def _row(path: str) -> list[str] | None:
    for line in CONFIGURATION.splitlines():
        if line.startswith(f"| `{path}` |"):
            return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
    return None


@pytest.mark.parametrize("path", [p for p in _paths(AppConfig) if not p.startswith("owners")])
def test_every_app_config_key_is_documented(path: str) -> None:
    assert _row(path) is not None, f"{path} missing from docs/CONFIGURATION.md"


@pytest.mark.parametrize("path", _paths(OwnerYaml))
def test_every_owner_key_is_documented(path: str) -> None:
    section = path.split(".", 1)[0]
    if section in OWNER_POLICY_SECTIONS or (section == "codex" and not path.endswith("api_key_env")):
        row = _row(path)
        assert row is not None, f"{path} missing from docs/CONFIGURATION.md"
        assert row[3] == "yes", f"{path} must be marked as overridable per owner"
        return
    documented = path.split(".<KEY>.", 1)[0]
    if section == "codex":
        documented = path
        assert _row(documented) is not None, f"{path} missing from docs/CONFIGURATION.md"
        return
    assert f"`owners.<id>.{documented}`" in CONFIGURATION, f"owners.<id>.{documented} missing"


def _settings_env_names() -> set[str]:
    names: set[str] = set()
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        if alias is not None and hasattr(alias, "choices"):
            names.update(str(choice) for choice in alias.choices)
        else:
            names.add(name.upper())
    return names


def _code_env_names() -> set[str]:
    names: set[str] = set()
    for base in ("app", "alembic"):
        for path in (ROOT / base).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            names.update(re.findall(r"os\.environ\.get\(\s*\"([A-Z][A-Z0-9_]+)\"", text))
            names.update(re.findall(r"^[A-Z_]*ENV\s*=\s*\"([A-Z][A-Z0-9_]+)\"", text, re.MULTILINE))
            names.update(
                f"OWNER_<ID>_{suffix}" for suffix in re.findall(r"_env_name_for_owner\([^,]+,\s*\"([A-Z_]+)\"", text)
            )
    return names - {"PATH", "HOME"}


@pytest.mark.parametrize("name", sorted(_settings_env_names() | _code_env_names()))
def test_every_env_var_is_documented(name: str) -> None:
    assert f"`{name}`" in ENVIRONMENT, f"{name} missing from docs/ENVIRONMENT.md"


def test_env_example_is_documented() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.MULTILINE):
        documented = re.sub(r"^OWNER_EXAMPLE_ORG_", "OWNER_<ID>_", name)
        assert f"`{documented}`" in ENVIRONMENT, f"{name} from .env.example missing from docs/ENVIRONMENT.md"


def _owner_env_required_in_code() -> dict[str, bool]:
    """OWNER_<ID>_* suffix -> whether the registry loads it with required=True (within a few lines)."""
    lines = (ROOT / "app" / "owners" / "registry.py").read_text(encoding="utf-8").splitlines()
    out: dict[str, bool] = {}
    for i, line in enumerate(lines):
        match = re.search(r"_env_name_for_owner\([^,]+,\s*\"([A-Z_]+)\"", line)
        if match:
            out[match.group(1)] = "required=True" in "\n".join(lines[i : i + 8])
    return out


@pytest.mark.parametrize(("suffix", "required"), sorted(_owner_env_required_in_code().items()))
def test_owner_env_required_column_matches_code(suffix: str, required: bool) -> None:
    name = f"OWNER_<ID>_{suffix}"
    row = next((line for line in ENVIRONMENT.splitlines() if line.startswith(f"| `{name}` |")), None)
    assert row is not None, f"{name} missing from the per-owner table"
    cell = re.split(r"(?<!\\)\|", row.strip().strip("|"))[2].strip()
    assert cell.startswith("yes") == required, f"{name}: Required column '{cell}' does not match the code"


# --- links -----------------------------------------------------------------

LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def _markdown_files() -> list[Path]:
    files = list(ROOT.glob("*.md")) + list((ROOT / "docs").rglob("*.md"))
    return sorted(files)


def _slug(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*$", text, re.MULTILINE):
        slug = _slug(heading)
        n = counts.get(slug, 0)
        anchors.add(slug if n == 0 else f"{slug}-{n}")
        counts[slug] = n + 1
    return anchors


def _links() -> list[tuple[Path, str]]:
    out = []
    for path in _markdown_files():
        text = FENCE.sub("", path.read_text(encoding="utf-8"))
        text = re.sub(r"`[^`\n]*`", "", text)
        for target in LINK.findall(text):
            if re.match(r"^[a-z]+:", target):
                continue
            out.append((path, target))
    return out


@pytest.mark.parametrize(("source", "target"), _links(), ids=lambda v: str(v).replace(str(ROOT), ""))
def test_relative_links_resolve(source: Path, target: str) -> None:
    file_part, _, anchor = target.partition("#")
    resolved = (source.parent / file_part).resolve() if file_part else source
    assert resolved.exists(), f"{source.relative_to(ROOT)}: broken link {target}"
    if anchor and resolved.suffix == ".md":
        assert anchor in _anchors(resolved), f"{source.relative_to(ROOT)}: missing anchor {target}"


# --- Russian mirror -----------------------------------------------------------

DOCS = ROOT / "docs"
RU = DOCS / "ru"


def _heading_levels(path: Path) -> list[int]:
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    return [len(m) for m in re.findall(r"^(#{1,6})\s", text, re.MULTILINE)]


def test_russian_mirror_has_the_same_files() -> None:
    english = {p.name for p in DOCS.glob("*.md")}
    russian = {p.name for p in RU.glob("*.md")}
    assert english == russian, f"missing in docs/ru: {sorted(english - russian)}; extra: {sorted(russian - english)}"


@pytest.mark.parametrize("name", sorted(p.name for p in DOCS.glob("*.md")))
def test_russian_mirror_has_the_same_heading_structure(name: str) -> None:
    mirror = RU / name
    if not mirror.exists():
        pytest.fail(f"docs/ru/{name} is missing")
    assert _heading_levels(mirror) == _heading_levels(DOCS / name), f"docs/ru/{name}: headings differ from docs/{name}"
