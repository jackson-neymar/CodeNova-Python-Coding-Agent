"""Static repository scanner and file/symbol dependency graph builder."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from codenova.migration.models import GraphNode, MigrationIssue, RiskLevel, SourceLocation


SKIP_DIRECTORIES = {
    ".git", ".hg", ".codenova", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", ".venv", "__pycache__", "build", "dist", "node_modules", "venv",
}


@dataclass
class PythonFileInfo:
    path: Path
    relative_path: str
    tree: ast.Module
    source: str
    imports: dict[str, str] = field(default_factory=dict)
    definitions: set[str] = field(default_factory=set)
    model_classes: set[str] = field(default_factory=set)
    settings_classes: set[str] = field(default_factory=set)


@dataclass
class ProjectScan:
    files: list[PythonFileInfo]
    graph: list[GraphNode]
    issues: list[MigrationIssue]
    manifests: list[Path]


def iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    # Virtual environments may use arbitrary directory names.  Detect their
    # marker file instead of relying only on conventional names such as
    # ``.venv`` and never inspect installed third-party code.
    virtualenv_parts = {
        marker.parent.relative_to(root).parts
        for marker in root.rglob("pyvenv.cfg")
        if marker.is_file()
    }
    for path in root.rglob("*.py"):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if any(part in SKIP_DIRECTORIES for part in relative.parts):
            continue
        if any(relative.parts[:len(parts)] == parts for parts in virtualenv_parts):
            continue
        if path.is_file() and not path.is_symlink():
            files.append(path)
    return sorted(files)


def discover_manifests(root: Path) -> list[Path]:
    candidates = [root / "pyproject.toml", root / "setup.cfg", root / "setup.py"]
    candidates.extend(sorted(root.glob("requirements*.txt")))
    candidates.extend([root / "Pipfile", root / "poetry.lock", root / "uv.lock"])
    return [path for path in candidates if path.is_file()]


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Subscript):
        return _name(node.value)
    return ""


def _parse_file(path: Path, root: Path) -> tuple[PythonFileInfo | None, MigrationIssue | None]:
    relative = path.relative_to(root).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        line = getattr(exc, "lineno", 1) or 1
        return None, MigrationIssue(
            "scan_error", f"Could not parse Python source: {exc}",
            SourceLocation(relative, line), RiskLevel.HIGH,
        )

    info = PythonFileInfo(path, relative, tree, source)
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            info.definitions.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                relative_parts = list(Path(relative).with_suffix("").parts)
                package_parts = relative_parts if relative_parts[-1] == "__init__" else relative_parts[:-1]
                if package_parts and package_parts[-1] == "__init__":
                    package_parts.pop()
                ascend = max(0, node.level - 1)
                if ascend:
                    package_parts = package_parts[:-ascend]
                module = ".".join([*package_parts, *([module] if module else [])])
            for alias in node.names:
                info.imports[alias.asname or alias.name] = f"{module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                info.imports[alias.asname or alias.name] = alias.name
    base_aliases = {name for name, imported in info.imports.items() if imported == "pydantic.BaseModel"}
    settings_aliases = {
        name for name, imported in info.imports.items()
        if imported in {"pydantic.BaseSettings", "pydantic_settings.BaseSettings"}
    }
    module_aliases = {name for name, imported in info.imports.items() if imported == "pydantic"}
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    changed = True
    while changed:
        changed = False
        for node in classes:
            bases = {_name(base) for base in node.bases}
            is_settings = bool(
                bases
                & (
                    {"BaseSettings", "pydantic.BaseSettings", "pydantic_settings.BaseSettings"}
                    | settings_aliases
                    | info.settings_classes
                )
            )
            if is_settings and node.name not in info.settings_classes:
                info.settings_classes.add(node.name)
                changed = True
            is_model = bool(bases & ({"BaseModel", "pydantic.BaseModel"} | base_aliases | info.model_classes))
            is_model = is_model or is_settings
            is_model = is_model or any(base == f"{alias}.BaseModel" for alias in module_aliases)
            if is_model and node.name not in info.model_classes:
                info.model_classes.add(node.name)
                changed = True
    return info, None


def scan_project(root: Path) -> ProjectScan:
    """Parse source files and construct a conservative symbol dependency graph."""
    root = root.resolve()
    files: list[PythonFileInfo] = []
    issues: list[MigrationIssue] = []
    for path in iter_python_files(root):
        info, issue = _parse_file(path, root)
        if info is not None:
            files.append(info)
        if issue is not None:
            issues.append(issue)

    # Resolve imported project symbols to their defining nodes.  A flat module
    # index is intentional: unresolved/ambiguous imports remain file nodes and
    # are never used to authorize a risky codemod.
    symbol_owner: dict[str, str] = {}
    for info in files:
        module = info.relative_path.removesuffix(".py").replace("/", ".")
        if module.endswith(".__init__"):
            module = module.removesuffix(".__init__")
        for definition in info.definitions:
            symbol_owner[f"{module}.{definition}"] = f"{info.relative_path}:{definition}"

    dependencies: dict[str, set[str]] = {}
    nodes: dict[str, tuple[str, str, str]] = {}
    for info in files:
        file_node = info.relative_path
        nodes[file_node] = ("file", info.relative_path, "")
        dependencies.setdefault(file_node, set())
        for definition in info.definitions:
            symbol_node = f"{info.relative_path}:{definition}"
            kind = "pydantic_model" if definition in info.model_classes else "symbol"
            nodes[symbol_node] = (kind, info.relative_path, definition)
            dependencies.setdefault(symbol_node, set()).add(file_node)
        for local_name, imported in info.imports.items():
            owner = symbol_owner.get(imported)
            if owner:
                dependencies[file_node].add(owner)
                local_node = f"{info.relative_path}:{local_name}"
                if local_name in info.definitions and local_node in dependencies:
                    dependencies[local_node].add(owner)

        # References to imported names and locally declared model classes create
        # edges used to order semantic review after foundational models.
        used_names = {node.id for node in ast.walk(info.tree) if isinstance(node, ast.Name)}
        for name in used_names:
            imported = info.imports.get(name)
            owner = symbol_owner.get(imported or "")
            if owner:
                dependencies[file_node].add(owner)
            local_model = f"{info.relative_path}:{name}"
            if local_model in nodes:
                dependencies[file_node].add(local_model)

    referenced_by: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    for source, targets in dependencies.items():
        for target in targets:
            if target in referenced_by and target != source:
                referenced_by[target].add(source)

    graph = [
        GraphNode(
            node_id=node_id,
            kind=kind,
            file=file,
            symbol=symbol,
            depends_on=tuple(sorted(target for target in dependencies.get(node_id, set()) if target != node_id)),
            referenced_by=tuple(sorted(referenced_by.get(node_id, set()))),
        )
        for node_id, (kind, file, symbol) in sorted(nodes.items())
    ]
    return ProjectScan(files, graph, issues, discover_manifests(root))


__all__ = [
    "ProjectScan", "PythonFileInfo", "discover_manifests", "iter_python_files", "scan_project",
]
