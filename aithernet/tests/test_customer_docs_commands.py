"""Documentation command regression (beta.8 phases 10-11).

Every `aithernet …` command shown in the customer journey (docs/getting-started.md) and the
authenticated portal must be a real packaged command, and customer docs must not leak developer
paths or secrets.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_customer_cli_packaging import _registered_paths

_ROOT = Path(__file__).resolve().parent.parent
_DOC = _ROOT / "docs" / "getting-started.md"
_PORTAL = _ROOT / "portal" / "src" / "pages" / "CustomerPortal.tsx"

# A command word is a bare lowercase subcommand name; anything else (flag, placeholder, path,
# shell var, redirect, …) ends the command path.
_WORD = re.compile(r"^[a-z][a-z0-9-]*$")


def _command_paths(text: str) -> set[str]:
    """Extract registered command PATHS from every `aithernet <words>` occurrence."""
    paths: set[str] = set()
    for m in re.finditer(r"aithernet\s+([^\n`]+)", text):
        collected: list[str] = []
        for w in m.group(1).split():
            if not _WORD.match(w):
                break
            collected.append(w)
        if collected:
            paths.add(" ".join(collected))
    return paths


def test_doc_exists():
    assert _DOC.is_file()


def _unknown(documented: set[str], registered: set[str]) -> list[str]:
    """A documented path is valid when a registered command path is a prefix of it (extra trailing
    words are arguments, e.g. `agents provider-status coordinator` -> `agents provider-status`).
    Prose whose first word is not a top-level command is ignored."""
    tops = {p.split()[0] for p in registered}
    bad = []
    for path in documented:
        if not path or path.split()[0] not in tops:
            continue  # prose / global-flag-only invocation
        if not any(path == r or path.startswith(r + " ") for r in registered):
            bad.append(path)
    return sorted(bad)


def test_every_documented_command_is_real():
    unknown = _unknown(_command_paths(_DOC.read_text()), _registered_paths())
    assert not unknown, f"docs reference unknown commands: {unknown}"


def test_portal_commands_are_real():
    if not _PORTAL.is_file():
        return
    unknown = _unknown(_command_paths(_PORTAL.read_text()), _registered_paths())
    assert not unknown, f"portal references unknown commands: {unknown}"


def test_no_developer_paths_or_secrets_in_doc():
    text = _DOC.read_text()
    assert "/home/aditya" not in text
    assert "PRIVATE KEY" not in text
    # no hard-coded API key shapes
    assert "sk-" not in text and "AIza" not in text


def test_no_pip_fallback_for_system_packages():
    text = _DOC.read_text().lower()
    assert "pip install gnuradio" not in text
    assert "do **not** use `pip`" in text or "do not use pip" in text


# -- beta.9 Defect 7: validate documented OPTIONS against the packaged CLI -------------------

_OPT = re.compile(r"--[a-z][a-z0-9-]*")


def _command_options_map() -> dict[str, set[str]]:
    """Map every registered command path -> the set of its real ``--option`` flags (from the
    packaged Typer/Click app, so docs are checked against the SHIPPED CLI, not a copy)."""
    import click
    import typer

    from aithernet.cli import app as cli_app

    root = typer.main.get_command(cli_app)
    out: dict[str, set[str]] = {}

    def _opts(cmd) -> set[str]:
        flags: set[str] = {"--help"}
        for p in getattr(cmd, "params", []):
            for o in list(getattr(p, "opts", [])) + list(getattr(p, "secondary_opts", [])):
                if o.startswith("--"):
                    flags.add(o)
        return flags

    def _walk(cmd, prefix: str) -> None:
        out[prefix] = _opts(cmd)
        if isinstance(cmd, click.Group):
            for name, sub in cmd.commands.items():
                _walk(sub, f"{prefix} {name}".strip())

    _walk(root, "")
    return out


def _documented_invocations(text: str) -> list[tuple[list[str], set[str]]]:
    """Return ``(path_words, options)`` for each ``aithernet …`` invocation in the text."""
    out: list[tuple[list[str], set[str]]] = []
    for m in re.finditer(r"aithernet\s+([^\n`]+)", text):
        body = m.group(1)
        words: list[str] = []
        for w in body.split():
            if not _WORD.match(w):
                break
            words.append(w)
        options = set(_OPT.findall(body))
        if words:
            out.append((words, options))
    return out


def _bad_options(text: str) -> list[str]:
    cmd_opts = _command_options_map()
    paths = set(cmd_opts)
    bad: list[str] = []
    for words, options in _documented_invocations(text):
        if not options:
            continue
        # Resolve to the longest registered command path that is a prefix of the documented words.
        match = ""
        for i in range(len(words), 0, -1):
            cand = " ".join(words[:i])
            if cand in paths:
                match = cand
                break
        if not match:
            continue  # not a recognised command (prose) — covered by the command-path test
        allowed = cmd_opts[match] | cmd_opts.get("", set())  # command opts + global root opts
        for opt in sorted(options):
            if opt not in allowed:
                bad.append(f"{match}: {opt}")
    return sorted(set(bad))


def test_documented_options_are_real_in_doc():
    bad = _bad_options(_DOC.read_text())
    assert not bad, f"docs reference unsupported options: {bad}"


def test_documented_options_are_real_in_portal():
    if not _PORTAL.is_file():
        return
    bad = _bad_options(_PORTAL.read_text())
    assert not bad, f"portal references unsupported options: {bad}"


def test_invalid_content_option_is_gone_everywhere():
    for path in (_DOC, _PORTAL):
        if path.is_file():
            assert "--content" not in path.read_text(), f"{path.name} still uses invalid --content"


def test_run_is_the_documented_primary_workflow():
    text = _DOC.read_text()
    assert 'aithernet run "' in text
    if _PORTAL.is_file():
        assert "aithernet run " in _PORTAL.read_text()


# Obsolete beta.8 onboarding forms that must never appear in the current customer guide (the
# beta.9 onboarding is `aithernet setup` + `aithernet run`). These are matched as substrings.
# NOTE: hosted enrollment (`aithernet enroll`) is a VALID command and may appear in its dedicated
# hosted-enrollment surface; it is intentionally not forbidden. These forms are obsolete or invalid
# in beta.9 and must never appear in any customer doc.
_OBSOLETE_FORMS = (
    "setup --node-name",
    "hardware install --profile",
    "service install",
    "hosted heartbeat",
    "hosted status",
    "git clone",
    "gr-mcp",
)


def test_no_obsolete_onboarding_forms_in_customer_docs():
    for path in (_DOC, _PORTAL):
        if not path.is_file():
            continue
        text = path.read_text()
        for form in _OBSOLETE_FORMS:
            assert form not in text, f"{path.name} contains obsolete onboarding form: {form!r}"


def test_no_stale_release_filename_in_customer_docs():
    # The current guide must not hard-code an OLDER beta filename (the portal pulls the exact
    # filename from release metadata; the in-repo doc must not pin a stale one).
    for path in (_DOC, _PORTAL):
        if not path.is_file():
            continue
        text = path.read_text()
        for stale in ("aithernet_0.8.0~beta.8_amd64.deb", "aithernet-0.8.0-beta.8-ubuntu24.04",
                      "aithernet_0.8.0~beta.7_amd64.deb"):
            assert stale not in text, f"{path.name} pins a stale release filename: {stale!r}"


def test_primary_journey_uses_run_not_submit_start_watch():
    # `aithernet run` must be present; submit/start/watch may appear ONLY in an advanced section.
    if not _PORTAL.is_file():
        return
    text = _PORTAL.read_text()
    assert "aithernet run " in text
    # If the lower-level lifecycle is shown at all, an Advanced section must be present to house it.
    if "mission submit" in text or "mission watch" in text:
        assert "Advanced" in text, "mission lifecycle shown without an Advanced section"


# -- beta.10: the new feature surface must be documented, with the safety statements present ----

def test_beta10_feature_commands_documented():
    text = _DOC.read_text()
    for cmd in ("aithernet agents secrets", "aithernet agents connect coordinator",
                "aithernet agents gateway", "aithernet peers pair", "aithernet run --on",
                "aithernet data setup", "aithernet data status", "aithernet data build",
                "aithernet mission retry"):
        assert cmd in text, f"getting-started.md missing beta.10 command: {cmd!r}"


def test_beta10_no_hidden_cot_and_secret_exclusion_stated():
    text = _DOC.read_text().lower()
    # The mandate requires an explicit no-hidden-chain-of-thought statement.
    assert "hidden chain-of-thought" in text or "hidden private reasoning" in text
    assert "does not" in text and "chain-of-thought" in text
    # And an explicit secret-exclusion statement for research collection.
    assert "secret-scan" in text or "secret exclusion" in text
    assert "quarantine" in text


def test_beta10_drive_is_owner_opt_in_not_default():
    text = _DOC.read_text().lower()
    assert "owner" in text and ("opt-in" in text or "not a default" in text
                                or "not enabled for external customers" in text)
