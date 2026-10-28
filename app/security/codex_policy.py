"""Codex CODEX_HOME policy: shell env isolation + execpolicy allowlist.

Codex 0.164.0 (and 0.165.x) supports:
- ``shell_environment_policy`` in ``config.toml`` / ``-c`` (applies to agent-spawned
  commands, not Codex's own API client).
- ``prefix_rule`` files under ``$CODEX_HOME/rules/`` (mainly gate commands that
  escalate *outside* the sandbox; there is no default-deny for unmatched
  in-sandbox commands).

The worker prepares a dedicated ``CODEX_HOME`` per run so agent-spawned children
do not inherit the model key (``inherit = "core"`` + default KEY/SECRET/TOKEN
excludes; PATH is not set in policy so the sandbox helper stays resolvable),
and so a small read-only tool set is allowlisted while interpreters, package
managers, network tools and known bypass prefixes are forbidden. See SECURITY.md.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

# Vars re-injected via ``set`` after ``inherit = "core"``. Do **not** put PATH
# here: a ``set.PATH`` replaces Codex's PATH and drops the arg0 helper directory
# that contains ``codex-linux-sandbox``, so bubblewrap fails with
# ``execvp codex-linux-sandbox: No such file or directory``. Core inherit keeps
# PATH/HOME/USER/TMP* from the scrubbed Codex process env; default excludes
# strip KEY/SECRET/TOKEN (model key). Never include model credentials here.
_SET_ENV_KEYS = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
)

# Codex default excludes when ``ignore_default_excludes = false``.
_DEFAULT_EXCLUDE_RE = re.compile(r"(KEY|SECRET|TOKEN)", re.IGNORECASE)

REVIEW_RULES = r"""# autoreview-bot review-agent execpolicy (Codex Starlark).
# Allow a small read-only inspection set; forbid interpreters, package managers,
# network tools, opaque shells, build/test runners, and known allowlist bypass
# prefixes. Rules mainly gate escalation outside the sandbox (Codex has no
# default-deny for unmatched in-sandbox commands); see SECURITY.md.

prefix_rule(
    pattern = ["rg"],
    decision = "allow",
    justification = "Read-only search",
)

prefix_rule(
    pattern = ["rg", "--pre"],
    decision = "forbidden",
    justification = "rg preprocessor hooks are forbidden",
)

prefix_rule(
    pattern = ["rg", "--pre-glob"],
    decision = "forbidden",
    justification = "rg preprocessor hooks are forbidden",
)

prefix_rule(
    pattern = ["cat"],
    decision = "allow",
    justification = "Read file contents",
)

prefix_rule(
    pattern = ["ls"],
    decision = "allow",
    justification = "List directory",
)

prefix_rule(
    pattern = ["head"],
    decision = "allow",
    justification = "Read file head",
)

prefix_rule(
    pattern = ["git", "diff"],
    decision = "allow",
    justification = "Read-only git diff",
)

prefix_rule(
    pattern = ["git", "log"],
    decision = "allow",
    justification = "Read-only git log",
)

prefix_rule(
    pattern = ["git", "show"],
    decision = "allow",
    justification = "Read-only git show",
)

prefix_rule(
    pattern = ["git", "status"],
    decision = "allow",
    justification = "Read-only git status",
)

prefix_rule(
    pattern = ["git", "-c"],
    decision = "forbidden",
    justification = "git config overrides that can change helpers are forbidden",
)

prefix_rule(
    pattern = ["git", "--config"],
    decision = "forbidden",
    justification = "git config overrides that can change helpers are forbidden",
)

prefix_rule(
    pattern = ["sed", "-e"],
    decision = "forbidden",
    justification = "sed expression forms that can invoke helpers are forbidden",
)

prefix_rule(
    pattern = ["sed", "--expression"],
    decision = "forbidden",
    justification = "sed expression forms that can invoke helpers are forbidden",
)

prefix_rule(
    pattern = ["find"],
    decision = "forbidden",
    justification = "find can run arbitrary commands via -exec/-ok; forbid the binary",
)

prefix_rule(
    pattern = ["python"],
    decision = "forbidden",
    justification = "Do not execute repository or interpreter code",
)

prefix_rule(
    pattern = ["python3"],
    decision = "forbidden",
    justification = "Do not execute repository or interpreter code",
)

prefix_rule(
    pattern = ["node"],
    decision = "forbidden",
    justification = "Do not execute repository or interpreter code",
)

prefix_rule(
    pattern = ["nodejs"],
    decision = "forbidden",
    justification = "Do not execute repository or interpreter code",
)

prefix_rule(
    pattern = ["npm"],
    decision = "forbidden",
    justification = "Package managers are forbidden",
)

prefix_rule(
    pattern = ["npx"],
    decision = "forbidden",
    justification = "Package managers are forbidden",
)

prefix_rule(
    pattern = ["yarn"],
    decision = "forbidden",
    justification = "Package managers are forbidden",
)

prefix_rule(
    pattern = ["pnpm"],
    decision = "forbidden",
    justification = "Package managers are forbidden",
)

prefix_rule(
    pattern = ["pip"],
    decision = "forbidden",
    justification = "Package managers are forbidden",
)

prefix_rule(
    pattern = ["pip3"],
    decision = "forbidden",
    justification = "Package managers are forbidden",
)

prefix_rule(
    pattern = ["uv"],
    decision = "forbidden",
    justification = "Package managers are forbidden",
)

prefix_rule(
    pattern = ["poetry"],
    decision = "forbidden",
    justification = "Package managers are forbidden",
)

prefix_rule(
    pattern = ["curl"],
    decision = "forbidden",
    justification = "Network tools are forbidden",
)

prefix_rule(
    pattern = ["wget"],
    decision = "forbidden",
    justification = "Network tools are forbidden",
)

prefix_rule(
    pattern = ["http"],
    decision = "forbidden",
    justification = "Network tools are forbidden",
)

prefix_rule(
    pattern = ["ssh"],
    decision = "forbidden",
    justification = "Network tools are forbidden",
)

prefix_rule(
    pattern = ["scp"],
    decision = "forbidden",
    justification = "Network tools are forbidden",
)

prefix_rule(
    pattern = ["nc"],
    decision = "forbidden",
    justification = "Network tools are forbidden",
)

prefix_rule(
    pattern = ["bash"],
    decision = "forbidden",
    justification = "Opaque shell wrappers are forbidden; use allowlisted tools directly",
)

prefix_rule(
    pattern = ["sh"],
    decision = "forbidden",
    justification = "Opaque shell wrappers are forbidden; use allowlisted tools directly",
)

prefix_rule(
    pattern = ["zsh"],
    decision = "forbidden",
    justification = "Opaque shell wrappers are forbidden; use allowlisted tools directly",
)

prefix_rule(
    pattern = ["dash"],
    decision = "forbidden",
    justification = "Opaque shell wrappers are forbidden; use allowlisted tools directly",
)

prefix_rule(
    pattern = ["make"],
    decision = "forbidden",
    justification = "Build and test runners are forbidden",
)

prefix_rule(
    pattern = ["cmake"],
    decision = "forbidden",
    justification = "Build and test runners are forbidden",
)

prefix_rule(
    pattern = ["cargo"],
    decision = "forbidden",
    justification = "Build and test runners are forbidden",
)

prefix_rule(
    pattern = ["go"],
    decision = "forbidden",
    justification = "Build and test runners are forbidden",
)

prefix_rule(
    pattern = ["gradle"],
    decision = "forbidden",
    justification = "Build and test runners are forbidden",
)

prefix_rule(
    pattern = ["mvn"],
    decision = "forbidden",
    justification = "Build and test runners are forbidden",
)

prefix_rule(
    pattern = ["pytest"],
    decision = "forbidden",
    justification = "Build and test runners are forbidden",
)

prefix_rule(
    pattern = ["ruby"],
    decision = "forbidden",
    justification = "Do not execute repository or interpreter code",
)

prefix_rule(
    pattern = ["perl"],
    decision = "forbidden",
    justification = "Do not execute repository or interpreter code",
)

prefix_rule(
    pattern = ["php"],
    decision = "forbidden",
    justification = "Do not execute repository or interpreter code",
)

prefix_rule(
    pattern = ["docker"],
    decision = "forbidden",
    justification = "Container and orchestration tools are forbidden",
)

prefix_rule(
    pattern = ["kubectl"],
    decision = "forbidden",
    justification = "Container and orchestration tools are forbidden",
)

prefix_rule(
    pattern = ["sudo"],
    decision = "forbidden",
    justification = "Privilege escalation is forbidden",
)
"""

RULES_FILENAME = "review.rules"


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_shell_environment_policy_toml(parent_env: Mapping[str, str]) -> str:
    """Render the ``[shell_environment_policy]`` table for ``config.toml``.

    Uses ``inherit = "core"`` (not ``none`` + ``set.PATH``): setting PATH in
    ``shell_environment_policy.set`` breaks Codex's Linux sandbox helper lookup
    on 0.164.0. Core inherit keeps PATH from the scrubbed process env; default
    excludes remove the model key from agent children.
    """
    set_pairs: list[str] = []
    for key in _SET_ENV_KEYS:
        value = parent_env.get(key)
        if value is None or value == "":
            continue
        set_pairs.append(f"    {key} = {_toml_string(value)}")
    if "TERM" not in {p.split("=", 1)[0].strip() for p in set_pairs}:
        set_pairs.append(f"    TERM = {_toml_string('dumb')}")
    set_block = "\n".join(set_pairs)
    return (
        "# Generated by autoreview-bot for each Codex review run.\n"
        "# shell_environment_policy applies to agent-spawned commands only;\n"
        "# Codex itself still authenticates with CODEX_API_KEY in its process env.\n"
        "# inherit=core keeps PATH (required for codex-linux-sandbox helpers);\n"
        "# do not set PATH here - it drops the arg0 helper prefix and breaks bwrap.\n"
        "\n"
        "[shell_environment_policy]\n"
        'inherit = "core"\n'
        "ignore_default_excludes = false\n"
        "\n"
        "[shell_environment_policy.set]\n"
        f"{set_block}\n"
        "\n"
        "[analytics]\n"
        "enabled = false\n"
    )


def prepare_codex_home(dest: Path, parent_env: Mapping[str, str]) -> Path:
    """Write ``config.toml`` + ``rules/review.rules`` under ``dest`` and return it."""
    dest.mkdir(parents=True, exist_ok=True)
    rules_dir = dest / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (dest / "config.toml").write_text(
        build_shell_environment_policy_toml(parent_env),
        encoding="utf-8",
    )
    (rules_dir / RULES_FILENAME).write_text(REVIEW_RULES, encoding="utf-8")
    return dest


def _pattern_matches(name: str, pattern: str) -> bool:
    """Case-insensitive glob with ``*`` and ``?`` (Codex EnvironmentVariablePattern)."""
    regex = []
    for ch in pattern:
        if ch == "*":
            regex.append(".*")
        elif ch == "?":
            regex.append(".")
        else:
            regex.append(re.escape(ch))
    return re.fullmatch("".join(regex), name, flags=re.IGNORECASE) is not None


def spawned_command_env(parent_env: Mapping[str, str], codex_home: Path) -> dict[str, str]:
    """Apply the shell_environment_policy from ``codex_home/config.toml`` to ``parent_env``.

    Mirrors Codex's documented ``populate_env`` algorithm (inherit → default
    excludes → custom excludes → set → include_only) so tests can prove a planted
    child process cannot see the model key under the policy we install for Codex.
    """
    raw = (codex_home / "config.toml").read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    policy = data.get("shell_environment_policy") or {}
    inherit = str(policy.get("inherit", "all")).lower()
    ignore_default_excludes = bool(policy.get("ignore_default_excludes", True))
    set_vars = dict(policy.get("set") or {})
    exclude = list(policy.get("exclude") or [])
    include_only = list(policy.get("include_only") or [])

    if inherit == "none":
        env_map: dict[str, str] = {}
    elif inherit == "core":
        core = {
            "HOME",
            "LOGNAME",
            "PATH",
            "SHELL",
            "USER",
            "USERNAME",
            "TMPDIR",
            "TEMP",
            "TMP",
        }
        env_map = {k: v for k, v in parent_env.items() if k in core}
    else:
        env_map = dict(parent_env)

    if not ignore_default_excludes:
        env_map = {k: v for k, v in env_map.items() if not _DEFAULT_EXCLUDE_RE.search(k)}

    if exclude:
        env_map = {k: v for k, v in env_map.items() if not any(_pattern_matches(k, pat) for pat in exclude)}

    for key, val in set_vars.items():
        env_map[str(key)] = str(val)

    if include_only:
        env_map = {k: v for k, v in env_map.items() if any(_pattern_matches(k, pat) for pat in include_only)}

    return env_map


def rules_path(codex_home: Path) -> Path:
    return codex_home / "rules" / RULES_FILENAME
