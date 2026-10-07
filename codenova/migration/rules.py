"""Deterministic codemods and dependency manifest updates.

The codemods deliberately use AST locations while preserving untouched source
text.  Ambiguous Pydantic v1 constructs are reported as semantic tasks instead
of being changed optimistically.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from codenova.migration.models import (
    MigrationChange,
    MigrationIssue,
    RiskLevel,
    SourceLocation,
)
from codenova.migration.scanner import PythonFileInfo


@dataclass(frozen=True)
class TransformResult:
    source: str
    changes: tuple[MigrationChange, ...]
    issues: tuple[MigrationIssue, ...]


@dataclass(frozen=True)
class ManifestResult:
    source: str
    changes: tuple[MigrationChange, ...]
    matched: bool = False
    version_mismatches: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Replacement:
    start: int
    end: int
    text: bytes
    change: MigrationChange


CONFIG_RENAMES = {
    "allow_population_by_field_name": "populate_by_name",
    "anystr_lower": "str_to_lower",
    "anystr_strip_whitespace": "str_strip_whitespace",
    "anystr_upper": "str_to_upper",
    "keep_untouched": "ignored_types",
    "max_anystr_length": "str_max_length",
    "min_anystr_length": "str_min_length",
    "orm_mode": "from_attributes",
    "schema_extra": "json_schema_extra",
    "validate_all": "validate_default",
}

FIELD_KEYWORD_RENAMES = {
    "max_items": "max_length",
    "min_items": "min_length",
    "regex": "pattern",
}

METHOD_RENAMES = {
    "construct": "model_construct",
    "copy": "model_copy",
    "dict": "model_dump",
    "json": "model_dump_json",
    "parse_obj": "model_validate",
    "parse_raw": "model_validate_json",
    "schema": "model_json_schema",
}

ATTRIBUTE_RENAMES = {
    "__fields__": "model_fields",
    "__fields_set__": "model_fields_set",
}


def _byte_offsets(source: str) -> list[int]:
    offsets = [0]
    total = 0
    for line in source.splitlines(keepends=True):
        total += len(line.encode("utf-8"))
        offsets.append(total)
    return offsets


def _span(node: ast.AST, offsets: list[int]) -> tuple[int, int]:
    start = offsets[node.lineno - 1] + node.col_offset  # type: ignore[attr-defined]
    end = offsets[node.end_lineno - 1] + node.end_col_offset  # type: ignore[attr-defined]
    return start, end


def _node_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _node_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Subscript):
        return _node_name(node.value)
    return ""


def _location(info: PythonFileInfo, node: ast.AST, symbol: str = "") -> SourceLocation:
    return SourceLocation(
        info.relative_path,
        getattr(node, "lineno", 1),
        getattr(node, "col_offset", 0),
        symbol,
    )


class _PydanticVisitor(ast.NodeVisitor):
    def __init__(
        self,
        info: PythonFileInfo,
        offsets: list[int],
        known_model_names: set[str] | None = None,
    ) -> None:
        self.info = info
        self.source_bytes = info.source.encode("utf-8")
        self.offsets = offsets
        self.replacements: list[_Replacement] = []
        self.issues: list[MigrationIssue] = []
        self.class_stack: list[tuple[str, bool, bool]] = []
        self.needs_config_dict = False
        self.needs_settings_config_dict = False
        self.needs_field_validator = False
        self.needs_model_validator = False
        self.has_unsafe_root_validator = False
        self.needs_type_adapter = False
        self.unsafe_validator_calls: set[int] = set()
        self.model_names = set(info.model_classes) | set(known_model_names or ())
        self.settings_names = set(info.settings_classes)
        self.orm_ready_names: set[str] = set()
        self.model_instances: set[str] = set()
        for candidate in ast.walk(info.tree):
            if isinstance(candidate, ast.ClassDef) and candidate.name in self.model_names:
                for child in candidate.body:
                    if not isinstance(child, ast.ClassDef) or child.name != "Config":
                        continue
                    meaningful = [
                        statement for statement in child.body
                        if not (
                            isinstance(statement, ast.Expr)
                            and isinstance(statement.value, ast.Constant)
                            and isinstance(statement.value.value, str)
                        )
                    ]
                    assignments = [
                        statement for statement in meaningful
                        if isinstance(statement, ast.Assign)
                        and len(statement.targets) == 1
                        and isinstance(statement.targets[0], ast.Name)
                    ]
                    transformable = len(assignments) == len(meaningful) and not any(
                        statement.targets[0].id
                        in {"fields", "getter_dict", "json_dumps", "json_loads", "smart_union", "underscore_attrs_are_private"}
                        for statement in assignments
                    )
                    if transformable and any(
                        statement.targets[0].id == "orm_mode"
                        and isinstance(statement.value, ast.Constant)
                        and statement.value.value is True
                        for statement in assignments
                    ):
                        self.orm_ready_names.add(candidate.name)
            if isinstance(candidate, (ast.Assign, ast.AnnAssign)):
                value = candidate.value
                if isinstance(value, ast.Call) and _node_name(value.func).rsplit(".", 1)[-1] in self.model_names:
                    targets = candidate.targets if isinstance(candidate, ast.Assign) else [candidate.target]
                    self.model_instances.update(target.id for target in targets if isinstance(target, ast.Name))
            if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
                positional = (*candidate.args.posonlyargs, *candidate.args.args)
                incompatible_signature = (
                    len(positional) > 2
                    or bool(candidate.args.kwonlyargs)
                    or candidate.args.vararg is not None
                    or candidate.args.kwarg is not None
                )
                if incompatible_signature:
                    for decorator in candidate.decorator_list:
                        if isinstance(decorator, ast.Call) and self._pydantic_callable(decorator.func) == "validator":
                            self.unsafe_validator_calls.add(id(decorator))
                for decorator in candidate.decorator_list:
                    if not isinstance(decorator, ast.Call) and self._pydantic_callable(decorator) == "root_validator":
                        self.has_unsafe_root_validator = True
                for argument in (*candidate.args.posonlyargs, *candidate.args.args, *candidate.args.kwonlyargs):
                    annotation = _node_name(argument.annotation) if argument.annotation else ""
                    if annotation.rsplit(".", 1)[-1] in self.model_names:
                        self.model_instances.add(argument.arg)

    def _pydantic_callable(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            imported = self.info.imports.get(node.id, "")
            if imported.startswith("pydantic."):
                return imported.rsplit(".", 1)[-1]
        elif isinstance(node, ast.Attribute):
            receiver = _node_name(node.value).split(".", 1)[0]
            if receiver == "pydantic" or self.info.imports.get(receiver) == "pydantic":
                return node.attr
        return ""

    def _replace(
        self,
        node: ast.AST,
        text: str,
        rule_id: str,
        description: str,
        *,
        risk: RiskLevel = RiskLevel.LOW,
        symbol: str = "",
    ) -> None:
        start, end = _span(node, self.offsets)
        before = self.source_bytes[start:end].decode("utf-8")
        if before == text:
            return
        change = MigrationChange(
            rule_id, description, _location(self.info, node, symbol),
            before, text, risk, True,
        )
        self.replacements.append(_Replacement(start, end, text.encode("utf-8"), change))

    def _issue(
        self,
        node: ast.AST,
        issue_type: str,
        message: str,
        rule: str,
        risk: RiskLevel = RiskLevel.MEDIUM,
    ) -> None:
        self.issues.append(MigrationIssue(
            issue_type, message, _location(self.info, node), risk, rule,
        ))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) and self._pydantic_callable(decorator) == "root_validator":
                self._issue(
                    decorator, "semantic_validator",
                    "Bare root_validator uses post-validation semantics and requires an instance-level model_validator migration.",
                    "pydantic.root_validator", RiskLevel.HIGH,
                )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        bases = {_node_name(base) for base in node.bases}
        is_settings = (
            node.name in self.settings_names
            or any(base in self.settings_names for base in bases)
            or bool(bases & {"BaseSettings", "pydantic.BaseSettings", "pydantic_settings.BaseSettings"})
        )
        is_model = (
            node.name in self.model_names
            or any(base in self.model_names for base in bases)
            or "BaseModel" in bases
            or "pydantic.BaseModel" in bases
        )
        if is_settings:
            self.settings_names.add(node.name)
        if is_model or is_settings:
            is_model = True
            self.model_names.add(node.name)
            if len(node.bases) == 2:
                first_name = _node_name(node.bases[0])
                second_name = _node_name(node.bases[1])
                first_import = self.info.imports.get(first_name.split(".", 1)[0], "")
                first_is_generic = first_name.startswith("Generic") and first_import in {"typing.Generic", "typing_extensions.Generic"}
                second_is_model = second_name in self.model_names or second_name in {"BaseModel", "pydantic.BaseModel"}
                if first_is_generic and second_is_model:
                    first_source = ast.get_source_segment(self.info.source, node.bases[0]) or ast.unparse(node.bases[0])
                    second_source = ast.get_source_segment(self.info.source, node.bases[1]) or ast.unparse(node.bases[1])
                    self._replace(
                        node.bases[0], second_source, "pydantic.generic-base-order",
                        "Move the Pydantic model base before Generic.", risk=RiskLevel.MEDIUM,
                        symbol=node.name,
                    )
                    self._replace(
                        node.bases[1], first_source, "pydantic.generic-base-order",
                        "Move Generic after the Pydantic model base.", risk=RiskLevel.MEDIUM,
                        symbol=node.name,
                    )
        self.class_stack.append((node.name, is_model, is_settings))

        if node.name == "Config" and len(self.class_stack) >= 2 and self.class_stack[-2][1]:
            self._transform_config(node)
            self.class_stack.pop()
            return
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is None and self.class_stack and self.class_stack[-1][1] and self._annotation_allows_none(node.annotation):
            original = ast.get_source_segment(self.info.source, node) or ast.unparse(node)
            self._replace(
                node, f"{original} = None", "pydantic.optional-default",
                "Preserve Pydantic v1 Optional-field behavior with an explicit None default.",
                risk=RiskLevel.LOW,
            )
        self.generic_visit(node)

    def _annotation_allows_none(self, annotation: ast.AST) -> bool:
        if isinstance(annotation, ast.Subscript):
            name = _node_name(annotation.value)
            imported = self.info.imports.get(name, "")
            return name == "typing.Optional" or imported in {"typing.Optional", "typing_extensions.Optional"}
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return self._annotation_allows_none(annotation.left) or self._annotation_allows_none(annotation.right)
        return isinstance(annotation, ast.Constant) and annotation.value is None

    def _transform_config(self, node: ast.ClassDef) -> None:
        pairs: list[str] = []
        for statement in node.body:
            if (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ):
                continue
            if not (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
            ):
                self._issue(
                    node, "semantic_config", "Config contains methods, inheritance, or dynamic statements; migrate it manually.",
                    "pydantic.config-class", RiskLevel.HIGH,
                )
                return
            key = statement.targets[0].id
            if key in {"fields", "getter_dict", "json_dumps", "json_loads", "smart_union", "underscore_attrs_are_private"}:
                self._issue(
                    statement, "removed_config", f"Config.{key} has no direct Pydantic v2 equivalent.",
                    "pydantic.removed-config", RiskLevel.HIGH,
                )
                return
            value = ast.get_source_segment(self.info.source, statement.value) or ast.unparse(statement.value)
            if key == "allow_mutation":
                if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, bool):
                    key = "frozen"
                    value = repr(not statement.value.value)
                else:
                    self._issue(
                        statement, "semantic_config", "Dynamic allow_mutation must be inverted into frozen manually.",
                        "pydantic.allow-mutation", RiskLevel.HIGH,
                    )
                    return
            else:
                key = CONFIG_RENAMES.get(key, key)
            pairs.append(f"{key}={value}")

        indent = " " * node.col_offset
        is_settings = self.class_stack[-2][2]
        config_type = "SettingsConfigDict" if is_settings else "ConfigDict"
        replacement = f"model_config = {config_type}({', '.join(pairs)})"
        self._replace(
            node, replacement, "pydantic.settings-config" if is_settings else "pydantic.config-dict",
            f"Replace inner Config class with model_config/{config_type}.",
            risk=RiskLevel.MEDIUM, symbol=self.class_stack[-2][0],
        )
        if is_settings:
            self.needs_settings_config_dict = True
        else:
            self.needs_config_dict = True

    def visit_Call(self, node: ast.Call) -> None:
        function_name = _node_name(node.func)
        short_name = function_name.rsplit(".", 1)[-1]
        pydantic_short_name = self._pydantic_callable(node.func)

        if pydantic_short_name in {"validator", "root_validator"}:
            short_name = pydantic_short_name
            unsupported = {keyword.arg for keyword in node.keywords} & {"each_item", "skip_on_failure"}
            unsafe_signature = id(node) in self.unsafe_validator_calls
            root_before = (
                short_name == "root_validator"
                and not node.args
                and {keyword.arg for keyword in node.keywords} <= {"pre"}
                and any(
                    keyword.arg == "pre"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords
                )
            )
            if root_before:
                self._rename_root_validator_call(node)
            elif short_name == "root_validator" or unsupported or unsafe_signature:
                if short_name == "root_validator":
                    detail = "post/default root_validator requires instance-level model_validator semantics"
                    self.has_unsafe_root_validator = True
                elif unsupported:
                    detail = f"unsupported options: {', '.join(sorted(unsupported))}"
                else:
                    detail = "the decorated function uses a Pydantic v1 validator signature"
                self._issue(
                    node, "semantic_validator", f"{short_name} requires a semantic migration ({detail}).",
                    f"pydantic.{short_name}", RiskLevel.HIGH,
                )
            else:
                self._rename_validator_call(node)

        if pydantic_short_name == "Field":
            for keyword in node.keywords:
                if keyword.arg == "const":
                    self._issue(
                        keyword, "semantic_field", "Field(const=...) must become a Literal type; frozen=True is not behaviorally equivalent.",
                        "pydantic.field-const", RiskLevel.HIGH,
                    )
                if keyword.arg in FIELD_KEYWORD_RENAMES:
                    old = keyword.arg
                    new = FIELD_KEYWORD_RENAMES[old]
                    start = self.offsets[keyword.lineno - 1] + keyword.col_offset
                    fake = MigrationChange(
                        "pydantic.field-keyword", f"Rename Field({old}=) to {new}=.",
                        _location(self.info, keyword), old, new,
                    )
                    self.replacements.append(_Replacement(start, start + len(old), new.encode(), fake))

        if pydantic_short_name == "parse_obj_as" and len(node.args) == 2 and not node.keywords:
            type_arg = ast.get_source_segment(self.info.source, node.args[0]) or ast.unparse(node.args[0])
            value_arg = ast.get_source_segment(self.info.source, node.args[1]) or ast.unparse(node.args[1])
            self._replace(
                node, f"TypeAdapter({type_arg}).validate_python({value_arg})",
                "pydantic.type-adapter", "Replace parse_obj_as with TypeAdapter.validate_python.",
                risk=RiskLevel.MEDIUM,
            )
            self.needs_type_adapter = True

        if isinstance(node.func, ast.Attribute) and node.func.attr == "from_orm":
            receiver = _node_name(node.func.value)
            if receiver in self.orm_ready_names:
                self._replace_attribute_name(
                    node.func, "model_validate", "pydantic.from-orm",
                    "Replace from_orm with model_validate after enabling from_attributes.",
                )
            else:
                self._issue(
                    node, "semantic_from_orm",
                    f"Could not prove that {receiver}.from_orm has a migrated from_attributes configuration.",
                    "pydantic.from-orm", RiskLevel.HIGH,
                )

        if isinstance(node.func, ast.Attribute) and node.func.attr in METHOD_RENAMES:
            receiver = _node_name(node.func.value)
            in_model = bool(self.class_stack and self.class_stack[-1][1])
            safe_receiver = receiver in {"self", "cls"} and in_model
            safe_receiver = safe_receiver or receiver in self.model_names or receiver in self.model_instances
            if safe_receiver:
                self._replace_attribute_name(
                    node.func, METHOD_RENAMES[node.func.attr], "pydantic.method-rename",
                    f"Rename BaseModel.{node.func.attr} to {METHOD_RENAMES[node.func.attr]}.",
                )
            elif node.func.attr in {"parse_obj", "parse_raw", "construct", "schema"}:
                self._issue(
                    node, "ambiguous_call", f"Could not prove that {receiver}.{node.func.attr} is a Pydantic model call.",
                    "pydantic.method-rename",
                )
        self.generic_visit(node)

    def _rename_root_validator_call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            self._replace_attribute_name(
                node.func, "model_validator", "pydantic.model-validator",
                "Replace pre root_validator with a before model_validator.",
            )
        else:
            self._replace(
                node.func, "model_validator", "pydantic.model-validator",
                "Replace pre root_validator with a before model_validator.",
            )
        self.needs_model_validator = True
        for keyword in node.keywords:
            if keyword.arg == "pre":
                self._replace(
                    keyword, 'mode="before"', "pydantic.model-validator-mode",
                    "Replace pre=True with mode=\"before\".", risk=RiskLevel.MEDIUM,
                )

    def _rename_validator_call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            self._replace_attribute_name(
                node.func, "field_validator", "pydantic.field-validator",
                "Replace validator with field_validator.",
            )
        else:
            self._replace(node.func, "field_validator", "pydantic.field-validator", "Replace validator with field_validator.")
        self.needs_field_validator = True
        for keyword in node.keywords:
            if keyword.arg == "pre":
                value = ast.literal_eval(keyword.value) if isinstance(keyword.value, ast.Constant) else None
                if value is True:
                    self._replace(
                        keyword, 'mode="before"', "pydantic.validator-mode",
                        "Replace pre=True with mode=\"before\".",
                    )
                elif value is False:
                    self._replace(
                        keyword, 'mode="after"', "pydantic.validator-mode",
                        "Replace pre=False with mode=\"after\".",
                    )
                else:
                    self._issue(keyword, "semantic_validator", "Dynamic pre= cannot be mapped safely.", "pydantic.validator-mode")
            elif keyword.arg == "always":
                self._issue(
                    keyword, "semantic_validator", "always=True has changed validation semantics; review validate_default.",
                    "pydantic.validator-always", RiskLevel.HIGH,
                )

    def _replace_attribute_name(
        self, node: ast.Attribute, new_name: str, rule_id: str, description: str,
    ) -> None:
        _, end = _span(node, self.offsets)
        old_bytes = node.attr.encode("utf-8")
        start = end - len(old_bytes)
        change = MigrationChange(
            rule_id, description, _location(self.info, node), node.attr, new_name,
        )
        self.replacements.append(_Replacement(start, end, new_name.encode("utf-8"), change))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in ATTRIBUTE_RENAMES:
            receiver = _node_name(node.value)
            in_model = bool(self.class_stack and self.class_stack[-1][1])
            if (receiver in {"self", "cls"} and in_model) or receiver in self.model_names or receiver in self.model_instances:
                self._replace_attribute_name(
                    node, ATTRIBUTE_RENAMES[node.attr], "pydantic.attribute-rename",
                    f"Rename {node.attr} to {ATTRIBUTE_RENAMES[node.attr]}.",
                )
        self.generic_visit(node)


def _import_insertion(source: str, names: set[str], module: str = "pydantic") -> tuple[int, bytes] | None:
    tree = ast.parse(source)
    imported = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom) and statement.module == module
        for alias in statement.names
    }
    missing = sorted(names - imported)
    if not missing:
        return None
    lines = source.splitlines(keepends=True)
    insert_line = 0
    if lines and lines[0].startswith("#!"):
        insert_line = 1
    coding_cookie = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-_.a-zA-Z0-9]+")
    if insert_line < min(2, len(lines)) and coding_cookie.match(lines[insert_line]):
        insert_line += 1
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str):
        insert_line = max(insert_line, tree.body[0].end_lineno or 0)
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.module == "__future__":
            insert_line = max(insert_line, statement.end_lineno or insert_line)
    byte_pos = len("".join(lines[:insert_line]).encode("utf-8"))
    return byte_pos, f"from {module} import {', '.join(missing)}\n".encode("utf-8")


class PydanticV2RuleEngine:
    """Safe, idempotent Pydantic 1.x -> 2.x deterministic transforms."""

    def transform(
        self,
        info: PythonFileInfo,
        known_model_names: set[str] | None = None,
    ) -> TransformResult:
        offsets = _byte_offsets(info.source)
        visitor = _PydanticVisitor(info, offsets, known_model_names)
        visitor.visit(info.tree)

        # Imported decorators must follow the renamed use site. BaseSettings is
        # split out of mixed Pydantic imports in one replacement so edits never
        # overlap or leave a dangling comma.
        for node in ast.walk(info.tree):
            if isinstance(node, ast.ImportFrom) and node.module == "pydantic":
                if any(alias.name == "BaseSettings" for alias in node.names):
                    pydantic_names: list[str] = []
                    settings_names: list[str] = []
                    for alias in node.names:
                        if alias.name == "BaseSettings":
                            settings_names.append(
                                alias.name + (f" as {alias.asname}" if alias.asname else "")
                            )
                            continue
                        name = alias.name
                        keep_alias = True
                        if name == "validator" and visitor.needs_field_validator and not visitor.unsafe_validator_calls:
                            name = "field_validator"
                            keep_alias = False
                        elif name == "root_validator" and visitor.needs_model_validator and not visitor.has_unsafe_root_validator:
                            name = "model_validator"
                            keep_alias = False
                        elif name == "parse_obj_as" and visitor.needs_type_adapter:
                            name = "TypeAdapter"
                            keep_alias = False
                        pydantic_names.append(
                            name + (f" as {alias.asname}" if keep_alias and alias.asname else "")
                        )
                    lines = []
                    if pydantic_names:
                        lines.append(f"from pydantic import {', '.join(pydantic_names)}")
                    lines.append(f"from pydantic_settings import {', '.join(settings_names)}")
                    visitor._replace(
                        node, "\n".join(lines), "pydantic.base-settings-import",
                        "Move BaseSettings import to pydantic-settings.",
                        risk=RiskLevel.MEDIUM,
                    )
                    continue
                for alias in node.names:
                    if alias.name == "validator" and visitor.needs_field_validator and not visitor.unsafe_validator_calls:
                        visitor._replace(
                            alias, "field_validator",
                            "pydantic.validator-import", "Import field_validator instead of validator.",
                        )
                    if alias.name == "parse_obj_as" and visitor.needs_type_adapter:
                        visitor._replace(
                            alias, "TypeAdapter",
                            "pydantic.type-adapter-import", "Import TypeAdapter instead of parse_obj_as.",
                        )
                    if alias.name == "root_validator" and visitor.needs_model_validator and not visitor.has_unsafe_root_validator:
                        visitor._replace(
                            alias, "model_validator",
                            "pydantic.model-validator-import", "Import model_validator instead of root_validator.",
                            risk=RiskLevel.MEDIUM,
                        )

        required: set[str] = set()
        if visitor.needs_config_dict:
            required.add("ConfigDict")
        if visitor.needs_field_validator and visitor.unsafe_validator_calls:
            required.add("field_validator")
        has_parse_obj_as_import = any(
            imported == "pydantic.parse_obj_as" for imported in info.imports.values()
        )
        if visitor.needs_type_adapter and not has_parse_obj_as_import:
            required.add("TypeAdapter")
        if visitor.needs_model_validator and visitor.has_unsafe_root_validator:
            required.add("model_validator")

        required_settings: set[str] = set()
        if visitor.needs_settings_config_dict:
            required_settings.add("SettingsConfigDict")

        replacements = list(visitor.replacements)
        insertion = _import_insertion(info.source, required)
        if insertion:
            position, text = insertion
            names = text.decode().strip().removeprefix("from pydantic import ")
            change = MigrationChange(
                "pydantic.add-import", f"Import required Pydantic v2 symbols: {names}.",
                SourceLocation(info.relative_path, 1), "", text.decode().rstrip(),
            )
            replacements.append(_Replacement(position, position, text, change))
        settings_insertion = _import_insertion(
            info.source, required_settings, module="pydantic_settings",
        )
        if settings_insertion:
            position, text = settings_insertion
            names = text.decode().strip().removeprefix("from pydantic_settings import ")
            change = MigrationChange(
                "pydantic.add-settings-import",
                f"Import required pydantic-settings symbols: {names}.",
                SourceLocation(info.relative_path, 1), "", text.decode().rstrip(),
                RiskLevel.MEDIUM,
            )
            replacements.append(_Replacement(position, position, text, change))

        # Reject overlapping edits rather than silently corrupting source.
        accepted: list[_Replacement] = []
        for replacement in sorted(replacements, key=lambda item: (item.start, item.end)):
            if accepted and replacement.start < accepted[-1].end:
                visitor.issues.append(MigrationIssue(
                    "codemod_conflict", f"Overlapping deterministic rules: {accepted[-1].change.rule_id} and {replacement.change.rule_id}.",
                    replacement.change.location, RiskLevel.HIGH, replacement.change.rule_id,
                ))
                continue
            accepted.append(replacement)

        output = info.source.encode("utf-8")
        for replacement in reversed(accepted):
            output = output[:replacement.start] + replacement.text + output[replacement.end:]
        return TransformResult(output.decode("utf-8"), tuple(item.change for item in accepted), tuple(visitor.issues))


def update_dependency_manifest(
    path: Path,
    root: Path,
    package: str,
    from_version: str,
    to_version: str,
) -> ManifestResult:
    """Update dependency constraints without reformatting the containing file."""
    source = path.read_text(encoding="utf-8")
    escaped = re.escape(package)
    filename = path.name
    replacements: list[tuple[int, int, str, str]] = []
    mismatches: list[str] = []
    from_major = int(match.group()) if (match := re.search(r"\d+", from_version)) else None
    to_major = int(match.group()) if (match := re.search(r"\d+", to_version)) else None
    target_spec = f">={to_version}" + (f",<{to_major + 1}" if to_major is not None else "")

    def should_update(spec: str) -> bool:
        numeric = re.search(r"\d+", spec)
        if numeric is None:
            return True
        current_major = int(numeric.group())
        if to_major is not None and current_major == to_major:
            return False
        if from_major is not None and current_major != from_major:
            mismatches.append(spec.strip())
            return False
        return True

    if filename in {"pyproject.toml", "setup.py"}:
        # Handles quoted PEP 508 requirements in dependencies and optional-dependencies.
        pattern = re.compile(rf"(?P<quote>['\"])(?P<req>{escaped}(?:\[[^\]]+\])?\s*(?P<spec>(?:[<>=!~]=?\s*[^;'\"]+)*)?(?:\s*;\s*[^'\"]+)?)['\"]", re.IGNORECASE)
        for match in pattern.finditer(source):
            requirement = match.group("req")
            marker = ""
            if ";" in requirement:
                requirement, marker = requirement.split(";", 1)
                marker = ";" + marker
            extras_match = re.match(rf"({escaped}(?:\[[^\]]+\])?)", requirement, re.IGNORECASE)
            if not extras_match:
                continue
            spec = match.group("spec") or ""
            if not should_update(spec):
                continue
            new_req = f"{extras_match.group(1)}{target_spec}{marker}"
            start, end = match.span("req")
            replacements.append((start, end, requirement + marker, new_req))
        if filename == "pyproject.toml":
            # Poetry keeps the package name in a TOML key and the constraint in
            # the value instead of using a PEP 508 requirement string.
            poetry_pattern = re.compile(
                rf"(?im)^(?P<prefix>\s*{escaped}\s*=\s*)(?P<quote>['\"])(?P<spec>[^'\"]*)(?P=quote)(?P<tail>\s*(?:#.*)?)$"
            )
            for match in poetry_pattern.finditer(source):
                spec = match.group("spec")
                if not should_update(spec):
                    continue
                before = match.group(0)
                after = f"{match.group('prefix')}{match.group('quote')}{target_spec}{match.group('quote')}{match.group('tail')}"
                replacements.append((match.start(), match.end(), before, after))
            # Poetry also permits a package-specific dependency subtable, for
            # example ``[tool.poetry.dependencies.pydantic]`` followed by a
            # version key and extras.  Limit the edit to that exact section.
            subtable_pattern = re.compile(
                rf"(?im)^\s*\[tool\.poetry\.dependencies\.{escaped}\]\s*(?:#.*)?$"
            )
            for section in subtable_pattern.finditer(source):
                next_section = re.search(r"(?m)^\s*\[", source[section.end():])
                section_end = section.end() + next_section.start() if next_section else len(source)
                version_pattern = re.compile(
                    r"(?im)^(?P<prefix>\s*version\s*=\s*)(?P<quote>['\"])(?P<spec>[^'\"]*)(?P=quote)(?P<tail>\s*(?:#.*)?)$"
                )
                version = version_pattern.search(source, section.end(), section_end)
                if version is None or not should_update(version.group("spec")):
                    continue
                before = version.group(0)
                after = f"{version.group('prefix')}{version.group('quote')}{target_spec}{version.group('quote')}{version.group('tail')}"
                replacements.append((version.start(), version.end(), before, after))
    elif filename.startswith("requirements") and filename.endswith(".txt"):
        pattern = re.compile(rf"(?im)^(?P<prefix>\s*{escaped}(?:\[[^\]]+\])?)(?P<spec>\s*(?:[<>=!~]=?)[^;#\s]+(?:\s*,\s*[<>=!~]=?[^;#\s]+)*)?(?P<tail>\s*(?:;[^#]+)?(?:#.*)?)$")
        for match in pattern.finditer(source):
            before = match.group(0)
            if not should_update(match.group("spec") or ""):
                continue
            after = f"{match.group('prefix')}{target_spec}{match.group('tail')}"
            replacements.append((match.start(), match.end(), before, after))
    elif filename == "setup.cfg":
        pattern = re.compile(rf"(?im)^(?P<prefix>\s*{escaped})(?P<spec>\s*(?:[<>=!~]=?)[^\s]+)?(?P<tail>\s*)$")
        for match in pattern.finditer(source):
            if not should_update(match.group("spec") or ""):
                continue
            replacements.append((match.start(), match.end(), match.group(0), f"{match.group('prefix')}{target_spec}{match.group('tail')}"))

    matched = bool(replacements)
    changes: list[MigrationChange] = []
    output = source
    for start, end, before, after in reversed(replacements):
        if before == after:
            continue
        output = output[:start] + after + output[end:]
        line = source.count("\n", 0, start) + 1
        changes.append(MigrationChange(
            "dependency.constraint", f"Update {package} constraint from {from_version} to {to_version}.",
            SourceLocation(path.relative_to(root).as_posix(), line), before, after,
        ))
    changes.reverse()
    # A dependency on the target major is considered matched even when no edit
    # is needed; an unrelated major is reported separately by the engine.
    package_present = bool(re.search(rf"(?i)\b{escaped}(?:\[[^\]]+\])?\b", source))
    return ManifestResult(output, tuple(changes), matched or package_present, tuple(mismatches))


def ensure_dependency_manifest(
    path: Path,
    root: Path,
    package: str,
    spec: str,
    source: str | None = None,
) -> ManifestResult:
    """Add a required companion dependency to a supported manifest once."""
    source = path.read_text(encoding="utf-8") if source is None else source
    escaped = re.escape(package)
    if re.search(rf"(?i)(?<![A-Za-z0-9_.-]){escaped}(?![A-Za-z0-9_.-])", source):
        return ManifestResult(source, (), True)

    insertion: tuple[int, str] | None = None
    filename = path.name
    requirement = f"{package}{spec}"
    if filename == "pyproject.toml":
        poetry = re.search(r"(?im)^\s*\[tool\.poetry\.dependencies\]\s*(?:#.*)?$", source)
        if poetry:
            following = re.search(r"(?m)^\s*\[", source[poetry.end():])
            section_end = poetry.end() + following.start() if following else len(source)
            prefix = "" if source[:section_end].endswith("\n") else "\n"
            insertion = (section_end, f'{prefix}{package} = "{spec}"\n')
        else:
            project = re.search(r"(?im)^\s*\[project\]\s*(?:#.*)?$", source)
            if project:
                following = re.search(r"(?m)^\s*\[", source[project.end():])
                section_end = project.end() + following.start() if following else len(source)
                dependencies = re.search(
                    r"(?ms)^\s*dependencies\s*=\s*\[(?P<body>.*?)\]",
                    source[project.end():section_end],
                )
                if dependencies:
                    close = project.end() + dependencies.end() - 1
                    body = dependencies.group("body")
                    if "\n" in body:
                        prefix = "" if not body.strip() or body.rstrip().endswith(",") else ","
                        insertion = (close, f'{prefix}\n    "{requirement}",')
                    else:
                        prefix = ", " if body.strip() else ""
                        insertion = (close, f'{prefix}"{requirement}"')
    elif filename == "setup.py":
        dependencies = re.search(r"(?ms)install_requires\s*=\s*\[(?P<body>.*?)\]", source)
        if dependencies:
            close = dependencies.end() - 1
            body = dependencies.group("body")
            prefix = "" if not body.strip() or body.rstrip().endswith(",") else ","
            insertion = (close, f'{prefix}\n        "{requirement}",')
    elif filename.startswith("requirements") and filename.endswith(".txt"):
        prefix = "" if source.endswith("\n") or not source else "\n"
        insertion = (len(source), f"{prefix}{requirement}\n")
    elif filename == "Pipfile":
        packages = re.search(r"(?im)^\s*\[packages\]\s*$", source)
        if packages:
            following = re.search(r"(?m)^\s*\[", source[packages.end():])
            section_end = packages.end() + following.start() if following else len(source)
            prefix = "" if source[:section_end].endswith("\n") else "\n"
            insertion = (section_end, f'{prefix}{package} = "{spec}"\n')

    if insertion is None:
        return ManifestResult(source, (), False)
    position, text = insertion
    output = source[:position] + text + source[position:]
    line = source.count("\n", 0, position) + 1
    change = MigrationChange(
        "dependency.add", f"Add required migration companion dependency {requirement}.",
        SourceLocation(path.relative_to(root).as_posix(), line), "", text.rstrip(),
        RiskLevel.MEDIUM,
    )
    return ManifestResult(output, (change,), True)


_RESIDUAL_APIS: dict[str, tuple[str, RiskLevel]] = {
    "parse_file": ("parse_file APIs were removed in Pydantic v2", RiskLevel.HIGH),
    "parse_file_as": ("parse_file APIs were removed in Pydantic v2", RiskLevel.HIGH),
    "GenericModel": ("GenericModel should be replaced with BaseModel and Generic", RiskLevel.MEDIUM),
    "json_encoders": ("json_encoders should be reviewed for serializer decorators", RiskLevel.HIGH),
}


def _residual_api_uses(tree: ast.AST) -> list[tuple[str, int]]:
    """Find removed API names in code, ignoring the same text in strings and comments."""
    uses: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifier, line = node.id, node.lineno
        elif isinstance(node, ast.Attribute):
            identifier, line = node.attr, node.end_lineno or node.lineno
        elif isinstance(node, ast.keyword):
            identifier, line = node.arg or "", node.lineno
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            uses.extend(
                (alias.name.rsplit(".", 1)[-1], node.lineno)
                for alias in node.names
                if alias.name.rsplit(".", 1)[-1] in _RESIDUAL_APIS
            )
            continue
        else:
            continue
        if identifier in _RESIDUAL_APIS:
            uses.append((identifier, line))
    return uses


def find_pydantic_v1_residuals(info: PythonFileInfo) -> list[MigrationIssue]:
    issues: list[MigrationIssue] = []
    for identifier, line in _residual_api_uses(info.tree):
        message, risk = _RESIDUAL_APIS[identifier]
        issues.append(MigrationIssue(
            "deprecated_api", message,
            SourceLocation(info.relative_path, line), risk, "pydantic.residual-scan",
        ))
    for node in ast.walk(info.tree):
        moved_base_settings = (
            isinstance(node, ast.ImportFrom)
            and node.module == "pydantic"
            and any(alias.name == "BaseSettings" for alias in node.names)
        ) or (
            isinstance(node, ast.Attribute)
            and node.attr == "BaseSettings"
            and _node_name(node.value) == "pydantic"
        )
        if moved_base_settings:
            issues.append(MigrationIssue(
                "deprecated_api", "BaseSettings moved to the pydantic-settings package",
                _location(info, node), RiskLevel.HIGH, "pydantic.residual-scan",
            ))
    return issues


__all__ = [
    "ManifestResult", "PydanticV2RuleEngine", "TransformResult",
    "ensure_dependency_manifest", "find_pydantic_v1_residuals", "update_dependency_manifest",
]
