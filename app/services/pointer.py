"""JSON Pointer (RFC 6901) helpers and $ref target identifiers."""
from __future__ import annotations

from typing import Any, Iterable


def escape(token: Any) -> str:
    """Escape a single JSON Pointer reference token."""
    return str(token).replace("~", "~0").replace("/", "~1")


def parse(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"not a JSON Pointer: {pointer!r}")
    return [t.replace("~1", "/").replace("~0", "~") for t in pointer.split("/")[1:]]


def build(tokens: Iterable[Any]) -> str:
    return "/" + "/".join(escape(t) for t in tokens)


def resolve(doc: Any, pointer: str) -> Any:
    cur = doc
    for token in parse(pointer):
        if isinstance(cur, list):
            cur = cur[int(token)]
        else:
            cur = cur[token]
    return cur


def join(pointer: str, *tokens: Any) -> str:
    return pointer + build(tokens) if tokens else pointer


def parse_ref(ref: str) -> tuple[str, str]:
    """Split a local/file $ref into (file_key, pointer).

    ``#/...`` and ``#`` stay in the same file; ``other.yaml#/...`` addresses
    another document of the package; a bare ``other.yaml`` means its root.
    """
    if "#" in ref:
        file_part, frag = ref.split("#", 1)
    else:
        file_part, frag = ref, ""
    if frag == "":
        frag = "/"
    elif not frag.startswith("/"):
        # URI-style fragment ($id based) is not supported; caller rejects it.
        raise ValueError(f"unsupported non-JSON-Pointer fragment: {ref!r}")
    return file_part, frag


def format_ref_location(file_key: str, entry: str, pointer: str) -> str:
    """Human/UI location of a (possibly cross-file) target."""
    if file_key in ("", entry):
        return f"#{pointer}" if pointer != "/" else "#"
    base = f"{file_key}#{pointer}" if pointer != "/" else file_key
    return base
