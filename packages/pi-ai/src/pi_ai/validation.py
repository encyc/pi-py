"""工具参数校验。

对应上游 ``packages/ai/src/utils/validation.ts``（v1.3.0，353 行）。
导出面与上游一致：:func:`validate_tool_call` 与 :func:`validate_tool_arguments`。

**形状差异（如实登记，非等价）**

上游用 **typebox** 两套机制校验：

1. ``Value.Convert(schema, args)`` —— typebox 的就地类型转换；
2. ``Compile(schema)`` —— typebox 编译出校验器，取 ``.Check`` / ``.Errors``。

本端 ``Tool.parameters`` 是**纯 JSON Schema dict**（Pydantic 侧无 typebox），因此
只走上游那条"**非** typebox schema"分支：``normalizeOptionalNulls`` →
``coerceWithJsonSchema`` → 自研 JSON Schema 校验器。具体差异：

======================  ====================================  ==============================
上游                    本端                                   影响
======================  ====================================  ==============================
``Value.Convert``       **未复刻**                            见下"为什么可以不复刻"
typebox ``Compile``     私有 ``_validate``（JSON Schema 子集）  校验范畴为下游工具 schema 实际
                                                              用到的关键字；见 ``_SUPPORTED``
typebox ``.Errors()``   同形状的 ``(path, message)`` 列表      ``  - <path>: <message>`` 前缀与
                                                              首尾文案一致；**单条 message 的
                                                              措辞不保证与 typebox 逐字相同**
``WeakMap`` 验证器缓存   无缓存                                纯性能差异；``validatorCache`` 是
                                                              按 schema 对象身份记忆化的透明
                                                              缓存，无可观察语义
``structuredClone``     ``copy.deepcopy``                     等价
======================  ====================================  ==============================

**为什么可以不复刻 ``Value.Convert``**：它只在 typebox schema 上做就地转换；对纯
JSON Schema，随后无条件执行的 ``coerceWithJsonSchema``（上游 :327，其触发条件是
"schema 上没有 TypeBox.Kind 符号"——本端**永远**成立）已经覆盖了同等转换，并且
上游自己的用例期望值正是由 ``coercePrimitiveByType`` 的规则产生的。本端把上游
``test/validation.test.ts`` 的全部用例逐条移植为 Python 测试以验证这一点。

本端校验器支持的 JSON Schema 关键字：``$ref`` / ``type`` / ``enum`` / ``const`` /
``properties`` / ``required`` / ``additionalProperties`` / ``items``（含元组形式）/
``minItems`` / ``maxItems`` / ``uniqueItems`` / ``minProperties`` / ``maxProperties`` /
``minimum`` / ``maximum`` / ``exclusiveMinimum`` / ``exclusiveMaximum`` / ``multipleOf`` /
``minLength`` / ``maxLength`` / ``pattern`` / ``allOf`` / ``anyOf`` / ``oneOf``。
**未支持**的关键字被忽略（不报错）：``format`` / ``not`` / ``if-then-else`` /
``patternProperties`` / ``dependencies`` / ``contains`` 等；若下游工具 schema 用到
这些，校验会偏宽松——需要时按需补。

**模块级状态**：上游有 ``validatorCache``（``WeakMap``，透明缓存）。本端不保留任何
模块级可变状态，故 ``pi_ai.utils.validation`` 转发层无"状态被复制"风险。
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .types import Tool, ToolCall

__all__ = ["validate_tool_call", "validate_tool_arguments"]

#: ``$ref`` 解析的最大深度，防御自引用 schema。
_MAX_REF_DEPTH = 16


@dataclass
class _ValidationError:
    """一条校验错误。形状对应 typebox 的 ``TLocalizedValidationError`` 子集。"""

    keyword: str
    instance_path: str  # JSON Pointer，如 "/metadata/enabled"
    message: str
    params: dict[str, Any] = field(default_factory=dict)


# ============================================================
# JSON 类型判定 / 基本转换（逐条对应上游 :30-131）
# ============================================================


def _is_number(value: Any) -> bool:
    """``typeof value === "number"``：``bool`` 不算（Python 的 bool 是 int 子类）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_integer_number(value: Any) -> bool:
    """``typeof value === "number" && Number.isInteger(value)``。"""
    if not _is_number(value):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


def _matches_json_type(value: Any, type_name: str) -> bool:
    """对应上游 ``matchesJsonType``（:30-49）。"""
    if type_name == "number":
        return _is_number(value)
    if type_name == "integer":
        return _is_integer_number(value)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "null":
        return value is None
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "object":
        return isinstance(value, dict)
    return False


def _get_schema_types(schema: dict[str, Any]) -> list[str]:
    """对应上游 ``getSchemaTypes``（:20-28）。"""
    raw = schema.get("type")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str)]
    return []


def _js_number(value: str) -> float:
    """``Number(value)``：JS 把空串与空白串当 0，Python 的 float 会抛错。"""
    text = value.strip()
    if text == "":
        return 0.0
    try:
        return float(text)
    except ValueError:
        return float("nan")


def _normalize_parsed_number(parsed: float) -> int | float:
    """JS 只有一种 number；Python 区分为 int/float。

    整数值统一返回 ``int``，使 JSON 序列化结果与 JS 一致（``42`` 而非 ``42.0``）。
    """
    if parsed.is_integer() and abs(parsed) < 2**53:
        return int(parsed)
    return parsed


def _coerce_primitive_by_type(value: Any, type_name: str) -> Any:
    """按 JSON 类型做基本转换。逐条对应上游 ``coercePrimitiveByType``（:59-131）。"""
    if type_name == "number":
        if value is None:
            return 0
        if isinstance(value, str) and value.strip() != "":
            parsed = _js_number(value)
            if parsed == parsed and parsed not in (float("inf"), float("-inf")):
                return _normalize_parsed_number(parsed)
        if isinstance(value, bool):
            return 1 if value else 0
        return value

    if type_name == "integer":
        if value is None:
            return 0
        if isinstance(value, str) and value.strip() != "":
            parsed = _js_number(value)
            if (
                parsed == parsed
                and parsed not in (float("inf"), float("-inf"))
                and parsed.is_integer()
            ):
                return _normalize_parsed_number(parsed)
        if isinstance(value, bool):
            return 1 if value else 0
        return value

    if type_name == "boolean":
        if value is None:
            return False
        if isinstance(value, str):
            if value == "true":
                return True
            if value == "false":
                return False
        if _is_number(value):
            if value == 1:
                return True
            if value == 0:
                return False
        return value

    if type_name == "string":
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if _is_number(value):
            return str(_normalize_parsed_number(float(value)))
        return value

    if type_name == "null":
        if value == "":
            return None
        if _is_number(value) and value == 0:
            return None
        if value is False:
            return None
        return value

    return value


# ============================================================
# 递归转换（逐条对应上游 :133-238）
# ============================================================


def _apply_schema_object_coercion(value: dict[str, Any], schema: dict[str, Any]) -> None:
    """对应上游 ``applySchemaObjectCoercion``（:133-154）。"""
    properties = schema.get("properties")
    defined_keys = set(properties.keys()) if isinstance(properties, dict) else set()

    if isinstance(properties, dict):
        for key, property_schema in properties.items():
            if key not in value:
                continue
            if isinstance(property_schema, dict):
                value[key] = _coerce_with_json_schema(value[key], property_schema)

    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        for key in list(value.keys()):
            if key in defined_keys:
                continue
            value[key] = _coerce_with_json_schema(value[key], additional)


def _apply_schema_array_coercion(value: list[Any], schema: dict[str, Any]) -> None:
    """对应上游 ``applySchemaArrayCoercion``（:156-173）。"""
    items = schema.get("items")
    if isinstance(items, list):
        for index in range(len(value)):
            if index < len(items) and isinstance(items[index], dict):
                value[index] = _coerce_with_json_schema(value[index], items[index])
        return
    if isinstance(items, dict):
        for index in range(len(value)):
            value[index] = _coerce_with_json_schema(value[index], items)


def _coerce_with_union_schema(value: Any, schemas: list[Any]) -> Any:
    """对应上游 ``coerceWithUnionSchema``（:175-192）。

    先看是否已有某个分支**原样**通过；否则逐分支深拷贝后尝试转换，取首个转换后
    通过校验的分支。
    """
    for schema in schemas:
        if isinstance(schema, dict) and _is_valid(value, schema):
            return value

    for schema in schemas:
        if not isinstance(schema, dict):
            continue
        candidate = copy.deepcopy(value)
        coerced = _coerce_with_json_schema(candidate, schema)
        if _is_valid(coerced, schema):
            return coerced
    return value


def _coerce_with_json_schema(value: Any, schema: dict[str, Any]) -> Any:
    """对应上游 ``coerceWithJsonSchema``（:194-238）。

    .. note::
       注意上游用 ``candidate !== nextValue``（**严格不等**）判断"是否发生了转换"。
       Python 必须用 ``is not``（身份）而非 ``!=``：``True != 1`` 为 False，
       会让 ``1 -> True`` 这类转换被漏判。
    """
    next_value = value

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for nested in all_of:
            if isinstance(nested, dict):
                next_value = _coerce_with_json_schema(next_value, nested)

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        next_value = _coerce_with_union_schema(next_value, any_of)

    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        next_value = _coerce_with_union_schema(next_value, one_of)

    schema_types = _get_schema_types(schema)
    matches_union_member = len(schema_types) > 1 and any(
        _matches_json_type(next_value, t) for t in schema_types
    )
    if schema_types and not matches_union_member:
        for schema_type in schema_types:
            candidate = _coerce_primitive_by_type(next_value, schema_type)
            if candidate is not next_value:
                next_value = candidate
                break

    if "object" in schema_types and isinstance(next_value, dict):
        _apply_schema_object_coercion(next_value, schema)

    if "array" in schema_types and isinstance(next_value, list):
        _apply_schema_array_coercion(next_value, schema)

    return next_value


def _normalize_optional_nulls(value: Any, schema: dict[str, Any]) -> None:
    """把可选且不可空属性的 ``null`` 视作"未提供"并删除。

    对应上游 ``normalizeOptionalNulls``（:240-269）——**就地修改** ``value``。
    """
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, list):
            for index in range(min(len(value), len(items))):
                if isinstance(items[index], dict):
                    _normalize_optional_nulls(value[index], items[index])
        elif isinstance(items, dict):
            for item in value:
                _normalize_optional_nulls(item, items)
        return

    if not isinstance(value, dict):
        return
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return

    required = set(schema.get("required") or [])
    for key, property_schema in properties.items():
        if key not in value:
            continue
        if not isinstance(property_schema, dict):
            continue
        accepts_null = _sub_schema_accepts_null(property_schema)
        if (
            value[key] is None
            and key not in required
            and not isinstance(property_schema.get("$ref"), str)
            and accepts_null is False
        ):
            del value[key]
        else:
            _normalize_optional_nulls(value[key], property_schema)


def _sub_schema_accepts_null(schema: dict[str, Any]) -> bool | None:
    """子 schema 是否接受 ``null``；无法判定时返回 ``None``。

    对应上游 ``getSubSchemaValidator(schema)?.Check(null)``：编译失败时为
    ``undefined``，而 ``undefined === false`` 为假，故**不会**触发删除。本端用
    ``None`` 表达同一语义。
    """
    try:
        return _is_valid(None, schema)
    except Exception:  # noqa: BLE001 - 任何解析问题都视作"无法判定"
        return None


# ============================================================
# JSON Schema 校验（替代 typebox ``Compile`` / ``.Check`` / ``.Errors``）
# ============================================================


def _resolve_ref(schema: dict[str, Any], root: dict[str, Any], depth: int) -> dict[str, Any]:
    """解析本地 ``$ref``（``#/$defs/x`` / ``#/definitions/x`` / 任意 JSON Pointer）。"""
    ref = schema.get("$ref")
    if not isinstance(ref, str) or depth > _MAX_REF_DEPTH or not ref.startswith("#"):
        return schema
    pointer = ref[1:]
    if pointer in ("", "/"):
        return root
    target: Any = root
    for raw_token in pointer.lstrip("/").split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(target, dict) and token in target:
            target = target[token]
        elif isinstance(target, list) and token.isdigit() and int(token) < len(target):
            target = target[int(token)]
        else:
            return schema
    if not isinstance(target, dict):
        return schema
    # 合并同级兄弟关键字（JSON Schema 2020-12 允许 $ref 与兄弟关键字共存）
    siblings = {k: v for k, v in schema.items() if k != "$ref"}
    if siblings:
        merged = dict(target)
        merged.update(siblings)
        return merged
    return target


def _type_error_message(expected: Any) -> str:
    expected_text = "/".join(expected) if isinstance(expected, list) else str(expected)
    return f"Expected {expected_text}"


def _validate(  # noqa: C901 - 关键字分派天然多分支，逐关键字对应上游语义
    value: Any,
    schema: dict[str, Any],
    path: str,
    errors: list[_ValidationError],
    root: dict[str, Any],
    depth: int = 0,
) -> None:
    """校验 ``value`` 是否符合 ``schema``，把错误追加进 ``errors``（不短路）。"""
    if depth > 64:
        return
    schema = _resolve_ref(schema, root, depth)
    if not isinstance(schema, dict):
        return

    # ---- type ----
    declared = schema.get("type")
    if isinstance(declared, str):
        if not _matches_json_type(value, declared):
            errors.append(
                _ValidationError("type", path, _type_error_message(declared), {"type": declared})
            )
            return
    elif isinstance(declared, list):
        names = [t for t in declared if isinstance(t, str)]
        if names and not any(_matches_json_type(value, t) for t in names):
            errors.append(
                _ValidationError("type", path, _type_error_message(names), {"type": names})
            )
            return

    # ---- const / enum ----
    if "const" in schema and value != schema["const"]:
        errors.append(
            _ValidationError(
                "const", path, f"Expected {json.dumps(schema['const'], ensure_ascii=False)}"
            )
        )
    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        allowed = ", ".join(json.dumps(v, ensure_ascii=False) for v in schema["enum"])
        errors.append(_ValidationError("enum", path, f"Expected one of {allowed}"))

    # ---- 组合子 ----
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if not isinstance(branches, list) or not branches:
            continue
        branch_results: list[list[_ValidationError]] = []
        for branch in branches:
            if not isinstance(branch, dict):
                continue
            sub_errors: list[_ValidationError] = []
            _validate(value, branch, path, sub_errors, root, depth + 1)
            branch_results.append(sub_errors)
        if keyword == "allOf":
            for sub_errors in branch_results:
                errors.extend(sub_errors)
        elif keyword == "anyOf":
            if branch_results and all(sub_errors for sub_errors in branch_results):
                errors.append(
                    _ValidationError("anyOf", path, "Expected value to match at least one schema")
                )
        else:  # oneOf
            matched = sum(1 for sub_errors in branch_results if not sub_errors)
            if matched == 0 or matched > 1:
                errors.append(
                    _ValidationError("oneOf", path, "Expected value to match exactly one schema")
                )

    # ---- 数值 ----
    if _is_number(value):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(_ValidationError("minimum", path, f"Expected {minimum} or more"))
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(_ValidationError("maximum", path, f"Expected {maximum} or less"))
        exclusive_minimum = schema.get("exclusiveMinimum")
        if isinstance(exclusive_minimum, (int, float)) and value <= exclusive_minimum:
            errors.append(
                _ValidationError(
                    "exclusiveMinimum", path, f"Expected more than {exclusive_minimum}"
                )
            )
        exclusive_maximum = schema.get("exclusiveMaximum")
        if isinstance(exclusive_maximum, (int, float)) and value >= exclusive_maximum:
            errors.append(
                _ValidationError(
                    "exclusiveMaximum", path, f"Expected less than {exclusive_maximum}"
                )
            )
        multiple_of = schema.get("multipleOf")
        if isinstance(multiple_of, (int, float)) and multiple_of != 0:
            quotient = value / multiple_of
            if abs(quotient - round(quotient)) > 1e-9:
                errors.append(
                    _ValidationError("multipleOf", path, f"Expected a multiple of {multiple_of}")
                )

    # ---- 字符串 ----
    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            errors.append(
                _ValidationError("minLength", path, f"Expected at least {min_length} characters")
            )
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            errors.append(
                _ValidationError("maxLength", path, f"Expected at most {max_length} characters")
            )
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.search(pattern, value) is None:
                    errors.append(
                        _ValidationError("pattern", path, f'Expected to match "{pattern}"')
                    )
            except re.error:
                pass

    # ---- 数组 ----
    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(
                _ValidationError("minItems", path, f"Expected at least {min_items} items")
            )
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            errors.append(_ValidationError("maxItems", path, f"Expected at most {max_items} items"))
        if schema.get("uniqueItems") is True:
            seen: list[Any] = []
            duplicate = False
            for item in value:
                if item in seen:
                    duplicate = True
                    break
                seen.append(item)
            if duplicate:
                errors.append(_ValidationError("uniqueItems", path, "Expected unique items"))
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate(item, items, f"{path}/{index}", errors, root, depth + 1)
        elif isinstance(items, list):
            for index, item in enumerate(value):
                if index < len(items) and isinstance(items[index], dict):
                    _validate(item, items[index], f"{path}/{index}", errors, root, depth + 1)

    # ---- 对象 ----
    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            missing = [name for name in required if isinstance(name, str) and name not in value]
            for name in missing:
                errors.append(
                    _ValidationError(
                        "required",
                        path,
                        f"Expected required property {name}",
                        {"requiredProperties": [name]},
                    )
                )
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, property_schema in properties.items():
                if key in value and isinstance(property_schema, dict):
                    _validate(value[key], property_schema, f"{path}/{key}", errors, root, depth + 1)
        additional = schema.get("additionalProperties")
        if additional is False and isinstance(properties, dict):
            for key in value:
                if key not in properties:
                    errors.append(
                        _ValidationError(
                            "additionalProperties", f"{path}/{key}", f"Unexpected property {key}"
                        )
                    )
        elif isinstance(additional, dict):
            defined = set(properties.keys()) if isinstance(properties, dict) else set()
            for key, item in value.items():
                if key not in defined:
                    _validate(item, additional, f"{path}/{key}", errors, root, depth + 1)
        min_properties = schema.get("minProperties")
        if isinstance(min_properties, int) and len(value) < min_properties:
            errors.append(
                _ValidationError(
                    "minProperties", path, f"Expected at least {min_properties} properties"
                )
            )
        max_properties = schema.get("maxProperties")
        if isinstance(max_properties, int) and len(value) > max_properties:
            errors.append(
                _ValidationError(
                    "maxProperties", path, f"Expected at most {max_properties} properties"
                )
            )


def _collect_errors(value: Any, schema: dict[str, Any]) -> list[_ValidationError]:
    errors: list[_ValidationError] = []
    _validate(value, schema, "", errors, schema)
    return errors


def _is_valid(value: Any, schema: dict[str, Any]) -> bool:
    """对应 typebox ``validator.Check(value)``。"""
    return not _collect_errors(value, schema)


def _format_validation_path(error: _ValidationError) -> str:
    """对应上游 ``formatValidationPath``（:282-293）。"""
    if error.keyword == "required":
        required_properties = error.params.get("requiredProperties") or []
        required_property = required_properties[0] if required_properties else None
        if required_property:
            base_path = re.sub(r"^/", "", error.instance_path).replace("/", ".")
            return f"{base_path}.{required_property}" if base_path else required_property
    path = re.sub(r"^/", "", error.instance_path).replace("/", ".")
    return path or "root"


def _format_arguments(arguments: Any) -> str:
    """``JSON.stringify(arguments, null, 2)``。

    ``ensure_ascii=False`` 是必要的：JS 不转义非 ASCII，Python 默认会转成 ``\\uXXXX``，
    中文参数会让报错文本与上游不同。
    """
    return json.dumps(arguments, indent=2, ensure_ascii=False)


# ============================================================
# 公开 API（与上游导出面一致）
# ============================================================


def validate_tool_call(tools: list[Tool], tool_call: ToolCall) -> Any:
    """按名字找到工具并校验其参数。

    对应上游 ``validateToolCall``（:302-308）。

    :raises ValueError: 工具不存在，或参数校验失败。
    """
    for tool in tools:
        if tool.name == tool_call.name:
            return validate_tool_arguments(tool, tool_call)
    raise ValueError(f'Tool "{tool_call.name}" not found')


def validate_tool_arguments(tool: Tool, tool_call: ToolCall) -> Any:
    """校验工具调用参数，返回**已转换**的参数（新对象，不修改入参）。

    对应上游 ``validateToolArguments``（:317-353）。

    流程：

    1. ``tool_call.arguments_error`` 存在 → 直接抛错并附上模型原文（``#10897``）；
    2. 深拷贝参数；把"可选且不可空属性的 ``null``"当未提供删除；
    3. JSON Schema 类型转换（``coerceWithJsonSchema`` 及其递归辅助）；
    4. 校验；通过则返回转换结果，否则抛出带路径明细的 ``ValueError``。

    :raises ValueError: 参数非法。消息形如::

        Validation failed for tool "echo":
          - value: Expected string

        Received arguments:
        { ... }
    """
    if tool_call.arguments_error:
        raise ValueError(
            f'Invalid arguments for tool "{tool_call.name}": {tool_call.arguments_error}'
        )

    args = copy.deepcopy(tool_call.arguments)
    schema: dict[str, Any] = tool.parameters if isinstance(tool.parameters, dict) else {}

    _normalize_optional_nulls(args, schema)

    coerced = _coerce_with_json_schema(args, schema)
    if coerced is not args:
        if isinstance(args, dict) and isinstance(coerced, dict):
            # 与上游一致：就地把转换结果写回原对象（保持后续校验针对同一对象）
            args.clear()
            args.update(coerced)
        else:
            return coerced if _is_valid(coerced, schema) else args

    errors = _collect_errors(args, schema)
    if not errors:
        return args

    detail = (
        "\n".join(f"  - {_format_validation_path(e)}: {e.message}" for e in errors)
        or "Unknown validation error"
    )
    raise ValueError(
        f'Validation failed for tool "{tool_call.name}":\n{detail}\n\n'
        f"Received arguments:\n{_format_arguments(tool_call.arguments)}"
    )
