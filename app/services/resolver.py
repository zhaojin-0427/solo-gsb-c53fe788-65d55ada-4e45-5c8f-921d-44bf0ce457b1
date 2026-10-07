"""Resolve local/in-package JSON references (``$ref``).

External references (http/https/ftp...) are deliberately never fetched: they
resolve to an error that the engine reports as UNRESOLVED_REF.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass

from . import pointer as ptr


@dataclass
class RefResult:
    ok: bool
    file: str = ""
    pointer: str = ""
    target: object = None
    error: str = ""
    external: bool = False


_EXTERNAL_SCHEMES = ("http://", "https://", "ftp://", "ftps://", "//")


def _resolve_file(base_file: str, file_part: str, files: dict[str, dict]) -> str | None:
    if file_part in ("", ".", "./"):
        return base_file
    base_dir = posixpath.dirname(base_file)
    candidate = posixpath.normpath(posixpath.join(base_dir, file_part)) if base_dir \
        else posixpath.normpath(file_part)
    candidate = candidate.lstrip("./")
    if candidate in files:
        return candidate
    # case-insensitive fallback (zip members may differ in casing)
    lower = candidate.lower()
    for key in files:
        if key.lower() == lower:
            return key
    return None


class RefResolver:
    def __init__(self, files: dict[str, dict], entry: str):
        self.files = files
        self.entry = entry

    def is_external(self, ref: str) -> bool:
        return ref.lower().startswith(_EXTERNAL_SCHEMES)

    def resolve(self, ref: str, base_file: str | None = None,
                _seen: frozenset[tuple[str, str]] = frozenset()) -> RefResult:
        """Resolve a reference, transparently following reference chains."""
        base_file = base_file or self.entry
        ref = ref.strip()
        if self.is_external(ref):
            return RefResult(False, external=True,
                             error=f"外部引用不会被抓取: {ref}")
        try:
            file_part, frag = ptr.parse_ref(ref)
        except ValueError as exc:
            return RefResult(False, error=str(exc))

        target_file = _resolve_file(base_file, file_part, self.files)
        if target_file is None:
            return RefResult(False, error=f"引用的文件不在包内: {file_part or base_file}")

        doc = self.files[target_file]
        try:
            target = ptr.resolve(doc, frag)
        except (KeyError, IndexError, ValueError) as exc:
            return RefResult(False, file=target_file, pointer=frag,
                             error=f"JSON Pointer 目标不存在: {frag} ({exc})")

        key = (target_file, frag)
        if key in _seen:
            return RefResult(False, file=target_file, pointer=frag, target=target,
                             error="检测到 $ref 循环")
        # follow chains
        if isinstance(target, dict) and isinstance(target.get("$ref"), str):
            nxt = self.resolve(target["$ref"], target_file, _seen | {key})
            if not nxt.ok:
                return nxt
            return nxt
        return RefResult(True, file=target_file, pointer=frag, target=target)

    def location_hint(self, ref: str, base_file: str | None = None) -> str:
        base_file = base_file or self.entry
        try:
            file_part, frag = ptr.parse_ref(ref)
        except ValueError:
            return ref
        tf = _resolve_file(base_file, file_part, self.files) or file_part or base_file
        return ptr.format_ref_location(tf, self.entry, frag)
