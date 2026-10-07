"""Load an OpenAPI document *package* (single file or ZIP of local files).

Only local, in-package files are accepted: ``http(s)://`` references are never
fetched and are reported later by the resolver.
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field

import yaml


class PackageError(ValueError):
    pass


@dataclass
class LoadedPackage:
    files: dict[str, dict]            # normalized file key -> parsed document
    entry: str                        # file key of the root OpenAPI document
    warnings: list[str] = field(default_factory=list)

    @property
    def root(self) -> dict:
        return self.files[self.entry]


_YAML_SUFFIXES = (".yaml", ".yml", ".json")
_OPENAPI_HINTS = ("openapi", "swagger")


def _parse_bytes(raw: bytes, name: str) -> dict:
    text = raw.decode("utf-8-sig")
    try:
        if name.endswith(".json"):
            doc = json.loads(text)
        else:
            doc = yaml.safe_load(text)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise PackageError(f"文件 {name} 解析失败: {exc}") from exc
    if not isinstance(doc, dict):
        raise PackageError(f"文件 {name} 的顶层不是对象")
    return doc


def _is_openapi_root(doc: dict) -> bool:
    if not isinstance(doc, dict):
        return False
    if "openapi" in doc:
        return isinstance(doc.get("openapi"), str)
    if "swagger" in doc:  # tolerate swagger 2; comparison is best effort
        return isinstance(doc.get("swagger"), str)
    return False


def _normalize_key(name: str) -> str:
    name = name.replace("\\", "/").lstrip("./")
    # drop leading slashes and drive letters
    while name.startswith("/"):
        name = name[1:]
    parts = [p for p in name.split("/") if p not in ("", ".")]
    return "/".join(parts) if parts else name


def _looks_like_doc(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(_YAML_SUFFIXES)


def load_package(filename: str, raw: bytes) -> LoadedPackage:
    """Parse one uploaded artifact into a package.

    * ``.zip``  -> every contained yaml/json file becomes a package member;
      the OpenAPI root is detected from content, falling back to openapi.* /
      index.* / the first member.
    * otherwise -> treated as a single YAML/JSON document.
    """
    lower = filename.lower()
    if lower.endswith(".zip"):
        return _load_zip(raw)
    doc = _parse_bytes(raw, filename)
    key = _normalize_key(filename) or "openapi.yaml"
    if not _looks_like_doc(key):
        key += ".yaml"
    if not _is_openapi_root(doc):
        raise PackageError("文档缺少 openapi 字段，不是 OpenAPI 3.x 文档")
    return LoadedPackage(files={key: doc}, entry=key)


def _load_zip(raw: bytes) -> LoadedPackage:
    files: dict[str, dict] = {}
    warnings: list[str] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise PackageError("不是有效的 ZIP 归档") from exc

    names: list[str] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = _normalize_key(info.filename)
        base = name.rsplit("/", 1)[-1]
        if base.startswith("."):
            continue
        if not _looks_like_doc(name):
            warnings.append(f"已忽略非 YAML/JSON 文件: {info.filename}")
            continue
        if "__MACOSX" in info.filename:
            continue
        data = zf.read(info)
        doc = _parse_bytes(data, name)
        if name in files:
            raise PackageError(f"ZIP 内文件路径冲突（规范化后）: {name}")
        files[name] = doc
        names.append(name)

    if not files:
        raise PackageError("ZIP 中没有找到 YAML/JSON 文件")

    # Choose the entry: content marker first...
    roots = [n for n in names if _is_openapi_root(files[n])]
    entry: str | None = None
    if len(roots) == 1:
        entry = roots[0]
    elif roots:
        # ...then conventional names...
        for cand in ("openapi.yaml", "openapi.yml", "openapi.json",
                     "index.yaml", "index.yml", "index.json"):
            for n in roots:
                if n == cand or n.endswith("/" + cand):
                    entry = n
                    break
            if entry:
                break
        if entry is None:  # ...otherwise the shallowest root wins
            entry = sorted(roots, key=lambda n: (n.count("/"), n))[0]
    else:
        raise PackageError("ZIP 中没有任何文档包含 openapi 字段")

    return LoadedPackage(files=files, entry=entry, warnings=warnings)


def summarize(pkg: LoadedPackage) -> dict:
    root = pkg.root
    paths = root.get("paths") or {}
    ops = 0
    for item in paths.values():
        if isinstance(item, dict):
            ops += sum(1 for k, v in item.items()
                       if k.lower() in ("get", "post", "put", "patch",
                                        "delete", "head", "options", "trace")
                       and isinstance(v, dict))
    return {
        "openapi": root.get("openapi") or root.get("swagger"),
        "title": ((root.get("info") or {}).get("title")),
        "version": ((root.get("info") or {}).get("version")),
        "entry": pkg.entry,
        "file_count": len(pkg.files),
        "files": sorted(pkg.files),
        "path_count": len(paths),
        "operation_count": ops,
        "warnings": pkg.warnings,
    }
