from __future__ import annotations

import ast
import hashlib
from pathlib import Path

_CACHE: dict[str, tuple[str, dict[str, str]]] = {}


def _index_file(path: Path) -> tuple[str, dict[str, str]]:
    key = str(path)
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", {}
    file_hash = hashlib.sha1(source.encode("utf-8", "replace")).hexdigest()
    cached = _CACHE.get(key)
    if cached and cached[0] == file_hash:
        return cached
    funcs: dict[str, str] = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        _CACHE[key] = (file_hash, funcs)
        return _CACHE[key]

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                seg = ast.get_source_segment(source, child) or ""
                funcs[prefix + child.name] = hashlib.sha1(seg.encode("utf-8", "replace")).hexdigest()
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + "::")

    visit(tree, "")
    _CACHE[key] = (file_hash, funcs)
    return _CACHE[key]


def split_node_id(node_id: str) -> tuple[str, str]:
    file_part, _, rest = node_id.partition("::")
    rest = rest.split("[", 1)[0]
    return file_part, rest


def body_hash(repo_root: Path, node_id: str) -> str:
    file_part, qual = split_node_id(node_id)
    path = repo_root / file_part
    file_hash, funcs = _index_file(path)
    if qual and qual in funcs:
        return funcs[qual]
    return file_hash or hashlib.sha1(node_id.encode()).hexdigest()
