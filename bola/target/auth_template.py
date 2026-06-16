"""Tiny, pure-string template language for auth requests and credential injection.

A template is plain text with `{{ ... }}` holes. A hole is either a bare variable name
(`{{email}}`) or a call to one of a small fixed function set (`{{base64(token)}}`,
`{{basic(email, password)}}`). Variables resolve against a per-user mapping (the user's `vars`
plus `base_url`, and `credential` when rendering an `inject` value). Function arguments are
themselves variable names, not literals — keeping the language a closed, side-effect-free
substitution rather than an expression evaluator.

This is deliberately not Jinja: the manifest is attacker-adjacent config and the only operations
auth needs are substitution and the base64 forms that HTTP Basic/Bearer require.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Mapping

_HOLE = re.compile(r"\{\{\s*(.*?)\s*\}\}")
_CALL = re.compile(r"^(?P<fn>\w+)\((?P<args>.*)\)$")
_NAME = re.compile(r"^\w+$")


class TemplateError(Exception):
    """A template references an unknown variable or function, or is malformed."""


def _fn_base64(values: list[str]) -> str:
    """`base64(v)` — base64 of a single value."""
    return base64.b64encode(values[0].encode()).decode()


def _fn_basic(values: list[str]) -> str:
    """`basic(user, password)` — base64 of `user:password` (the HTTP Basic credential)."""
    user, password = values
    return base64.b64encode(f"{user}:{password}".encode()).decode()


_FUNCTIONS: dict[str, tuple[int, Callable[[list[str]], str]]] = {
    "base64": (1, _fn_base64),
    "basic": (2, _fn_basic),
}


def render(template: str, variables: Mapping[str, str]) -> str:
    """Render a template, substituting `{{...}}` holes from `variables`.

    Raises TemplateError if a hole names an unknown variable or function, or has the wrong arity.
    """

    def _resolve(match: re.Match[str]) -> str:
        """Replace one matched `{{...}}` hole with its resolved value."""
        return _resolve_hole(match.group(1), variables)

    return _HOLE.sub(_resolve, template)


def _resolve_hole(expr: str, variables: Mapping[str, str]) -> str:
    """Resolve one hole's inner expression — a function call or a bare variable name."""
    call = _CALL.match(expr)
    if call:
        fn = call.group("fn")
        if fn not in _FUNCTIONS:
            raise TemplateError(f"unknown template function {fn!r}")
        arity, func = _FUNCTIONS[fn]
        arg_names = [a.strip() for a in call.group("args").split(",") if a.strip()]
        if len(arg_names) != arity:
            raise TemplateError(f"{fn}() takes {arity} argument(s), got {len(arg_names)}")
        return func([_lookup(name, variables) for name in arg_names])
    if not _NAME.match(expr):
        raise TemplateError(f"malformed template hole {{{{{expr}}}}}")
    return _lookup(expr, variables)


def _lookup(name: str, variables: Mapping[str, str]) -> str:
    """Look up a variable by name, raising TemplateError if it is undefined."""
    if name not in variables:
        raise TemplateError(f"unknown template variable {name!r}")
    return variables[name]


def references_credential(template: str) -> bool:
    """True if the template uses `{{credential}}` as a bare variable (not inside a function)."""
    for hole in _HOLE.findall(template):
        if hole == "credential":
            return True
    return False