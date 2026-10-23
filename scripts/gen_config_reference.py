#!/usr/bin/env python3
"""Regenerate the CLI flag tables in docs/engineering/27-configuration-reference.md.

The tables between the ``BEGIN GENERATED`` / ``END GENERATED`` markers are
built from ``darwin_platform.__main__.build_parser()`` — the argparse groups,
defaults, choices, and help text — plus each flag's environment-variable
fallback, read from the help text (``Env: X``) and from the
``getattr(args, "dest") or os.environ.get("X")`` pattern in ``__main__.py``.
Everything outside the markers is hand-written and left untouched.

    uv run python scripts/gen_config_reference.py          # rewrite doc 27
    uv run python scripts/gen_config_reference.py --check  # exit 1 if stale

``tests/infra/test_configuration_contract.py`` fails when a flag or env var
is undocumented; run this script to fix it.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "engineering" / "27-configuration-reference.md"
MAIN = ROOT / "src" / "darwin_platform" / "__main__.py"
BEGIN = "<!-- BEGIN GENERATED: serve flags (scripts/gen_config_reference.py) -->"
END = "<!-- END GENERATED: serve flags -->"

# getattr(args, "dest", None) or os.environ.get("ENV") — possibly across lines.
_ENV_FALLBACK = re.compile(
    r'getattr\(\s*args,\s*"(\w+)"[^)]*\)\s*or\s*os\.environ\.get\(\s*"([A-Z][A-Z0-9_]+)"'
)
# val("dest", "ENV") / on("dest", "ENV") / ("dest", "ENV", "LEGACY") tuples.
_ENV_PAIR = re.compile(r'\(\s*"(\w+)",\s*"([A-Z][A-Z0-9_]+)"')
_ENV_IN_HELP = re.compile(r"\b(?:Env|env):?\s+([A-Z][A-Z0-9_]+)|\(or ([A-Z][A-Z0-9_]+)\)")


def _env_map() -> dict[str, list[str]]:
    src = MAIN.read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    for dest, env in _ENV_FALLBACK.findall(src) + _ENV_PAIR.findall(src):
        out.setdefault(dest, [])
        if env not in out[dest]:
            out[dest].append(env)
    return out


def _cell(text: str) -> str:
    # Escape pipes (table cells) and angle brackets (``<issuer>`` would
    # otherwise render as an invisible HTML tag).
    text = " ".join(text.split()).replace("|", "\\|")
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _default(action: argparse.Action) -> str:
    if isinstance(action, argparse._StoreTrueAction):
        return "off"
    if isinstance(action, argparse._StoreFalseAction):
        return "on"
    if action.default in (None, [], argparse.SUPPRESS):
        return "—"
    return f"`{action.default}`"


def render() -> str:
    from darwin_platform.__main__ import build_parser

    parser = build_parser()
    serve = parser._subparsers._group_actions[0].choices["serve"]  # noqa: SLF001
    envs = _env_map()
    blocks: list[str] = []
    for group in serve._action_groups:  # noqa: SLF001
        actions = [
            a for a in group._group_actions  # noqa: SLF001
            if a.option_strings and not isinstance(a, argparse._HelpAction)
        ]
        if not actions:
            continue
        title = "core" if group.title == "options" else group.title
        lines = [f"### `serve` — {title}", ""]
        if group.description:
            lines += [_cell(group.description), ""]
        lines += ["| Flag | Env var | Default | Description |", "| ---- | ------- | ------- | ----------- |"]
        for a in actions:
            flag = ", ".join(f"`{o}`" for o in a.option_strings if o.startswith("--"))
            help_text = a.help or ""
            env = list(envs.get(a.dest, []))
            for m in _ENV_IN_HELP.finditer(help_text):
                name = m.group(1) or m.group(2)
                if name not in env:
                    env.append(name)
            desc = _cell(help_text % {"default": a.default} if "%(" in help_text else help_text)
            if a.choices:
                desc = f"One of: {', '.join(f'`{c}`' for c in a.choices)}. {desc}".strip()
            env_cell = " / ".join(f"`{e}`" for e in env) or "—"
            lines.append(f"| {flag} | {env_cell} | {_default(a)} | {desc} |")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the doc is stale")
    args = ap.parse_args()

    text = DOC.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        sys.exit(f"{DOC.name}: missing generated-region markers")
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    new = f"{head}{BEGIN}\n\n{render()}\n\n{END}{tail}"
    if args.check:
        if new != text:
            print(f"{DOC.relative_to(ROOT)} is stale — run scripts/gen_config_reference.py")
            return 1
        return 0
    DOC.write_text(new, encoding="utf-8")
    print(f"Regenerated serve flag tables in {DOC.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
