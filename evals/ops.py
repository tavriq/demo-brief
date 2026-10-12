"""Edits to a JSON document by JSON-pointer paths (RFC 6901): a small subset of JSON Patch
(RFC 6902) — ``remove``, ``replace`` and ``add``. Stdlib only.

``add`` into a list inserts before the index (``-`` appends), into an object sets the key.
``replace`` and ``remove`` need the path to exist. Ops apply in order to a deep copy; the base
is never changed. A bad op raises OpError naming the op and the path.
"""

from __future__ import annotations

import copy

OPS = ("remove", "replace", "add")


class OpError(ValueError):
    pass


def parse_pointer(path: str) -> list[str]:
    """"/scenarios/2/role" -> ["scenarios", "2", "role"]; "" is the whole document."""
    if path == "":
        return []
    if not isinstance(path, str) or not path.startswith("/"):
        raise OpError(f"path must start with '/': {path!r}")
    return [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]


def _index(token: str, size: int, path: str, allow_end: bool = False) -> int:
    if allow_end and token == "-":
        return size
    if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
        raise OpError(f"{path}: {token!r} is not a list index")
    i = int(token)
    if i > size or (i == size and not allow_end):
        raise OpError(f"{path}: index {i} out of range (size {size})")
    return i


def _parent(doc, tokens: list[str], path: str):
    node = doc
    for token in tokens[:-1]:
        if isinstance(node, dict):
            if token not in node:
                raise OpError(f"{path}: no key {token!r}")
            node = node[token]
        elif isinstance(node, list):
            node = node[_index(token, len(node), path)]
        else:
            raise OpError(f"{path}: cannot go into {type(node).__name__}")
    return node


def apply_op(doc, op: dict):
    """The document after one op; ``doc`` may be modified in place (apply_ops copies first)."""
    if not isinstance(op, dict) or op.get("op") not in OPS or "path" not in op:
        raise OpError(f"bad op: {op!r}")
    kind, path = op["op"], op["path"]
    if kind != "remove" and "value" not in op:
        raise OpError(f"{kind} {path}: no value")
    tokens = parse_pointer(path)
    if not tokens:
        if kind == "remove":
            raise OpError("cannot remove the whole document")
        return copy.deepcopy(op["value"])
    parent, last = _parent(doc, tokens, path), tokens[-1]
    value = copy.deepcopy(op.get("value"))
    if isinstance(parent, dict):
        if kind != "add" and last not in parent:
            raise OpError(f"{kind} {path}: no key {last!r}")
        if kind == "remove":
            del parent[last]
        else:
            parent[last] = value
    elif isinstance(parent, list):
        i = _index(last, len(parent), path, allow_end=kind == "add")
        if kind == "remove":
            del parent[i]
        elif kind == "replace":
            parent[i] = value
        else:
            parent.insert(i, value)
    else:
        raise OpError(f"{kind} {path}: parent is {type(parent).__name__}")
    return doc


def apply_ops(base, ops: list[dict]):
    """A deep copy of ``base`` with ``ops`` applied in order."""
    if not isinstance(ops, list):
        raise OpError("ops must be a list")
    doc = copy.deepcopy(base)
    for op in ops:
        doc = apply_op(doc, op)
    return doc
