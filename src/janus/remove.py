"""Retiring a request (D83, J3): the file on main gets `removed: true` under spec. A line edit, not a YAML rewrite, so
the file's comments and layout stay; Shikigami's /validate parses and checks the result before any PR is opened."""

import re

SPEC = re.compile(r"^spec:\s*(#.*)?$")
REMOVED = re.compile(r"^(\s+)removed:\s*true\s*(#.*)?$", re.MULTILINE)
REMOVED_KEY = re.compile(r"^\s+removed:")


class Unremovable(ValueError):
    pass


def is_removed(text: str) -> bool:
    return REMOVED.search(text) is not None


def mark_removed(text: str) -> str:
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if SPEC.match(line))
    except StopIteration:
        raise Unremovable("no top-level block-style spec") from None
    end = start + 1
    indent = None
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        if not line[0].isspace():
            break
        indent = indent or line[: len(line) - len(line.lstrip())]
        end = i + 1
        if line.startswith(indent) and not line[len(indent)].isspace() and REMOVED_KEY.match(line):
            lines[i] = f"{indent}removed: true"
            return "\n".join(lines) + "\n"
    if indent is None:
        raise Unremovable("spec has no fields")
    lines.insert(end, f"{indent}removed: true")
    return "\n".join(lines) + "\n"
