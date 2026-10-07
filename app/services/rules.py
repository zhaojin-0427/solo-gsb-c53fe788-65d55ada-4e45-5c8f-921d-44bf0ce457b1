"""Catalogue of every rule the workbench can emit.

Compatibility is always judged in the upgrade direction *baseline -> candidate*:

* request bodies / parameters: the accepted value set must not be narrowed
  (the server must keep accepting everything it accepted before);
* responses: the delivered value set must not be widened (a client built for
  the baseline must keep understanding every value the server may emit).
"""
from __future__ import annotations

# Severity levels
BREAKING = "breaking"
UNDETERMINED = "undetermined"
COMPATIBLE = "compatible"
SEVERITIES = (BREAKING, UNDETERMINED, COMPATIBLE)

RULES: dict[str, dict[str, str]] = {
    # ---- endpoint level ---------------------------------------------------
    "ENDPOINT_REMOVED": {
        "severity": BREAKING,
        "title": "端点被删除",
        "detail": "基线中存在的 operation 在候选版本中消失，旧客户端的调用将得到 404。",
    },
    "ENDPOINT_ADDED": {
        "severity": COMPATIBLE,
        "title": "新增端点",
        "detail": "候选版本新增 operation，不影响既有客户端。",
    },
    # ---- parameters -------------------------------------------------------
    "PARAMETER_REMOVED": {
        "severity": BREAKING,
        "title": "参数被删除",
        "detail": "客户端仍会发送该参数，服务端可能忽略或拒绝，属于契约收窄/破坏。",
    },
    "PARAMETER_ADDED_REQUIRED": {
        "severity": BREAKING,
        "title": "新增必填参数",
        "detail": "旧客户端不会提供该参数，请求将被拒绝（请求侧收窄）。",
    },
    "PARAMETER_MADE_REQUIRED": {
        "severity": BREAKING,
        "title": "可选参数变为必填",
        "detail": "旧客户端可以省略该参数，升级后请求被拒绝（请求侧收窄）。",
    },
    "PARAMETER_MADE_OPTIONAL": {
        "severity": COMPATIBLE,
        "title": "必填参数变为可选",
        "detail": "放宽请求侧约束，旧客户端继续正常工作。",
    },
    "PARAMETER_ADDED_OPTIONAL": {
        "severity": COMPATIBLE,
        "title": "新增可选参数",
        "detail": "新增可选参数不影响旧客户端。",
    },
    "PARAMETER_LOCATION_CHANGED": {
        "severity": BREAKING,
        "title": "参数位置改变",
        "detail": "参数从 query/header/path 等位置迁移，旧客户端的传参位置不再被读取。",
    },
    # ---- media types ------------------------------------------------------
    "MEDIA_REMOVED": {
        "severity": BREAKING,
        "title": "媒体类型被删除",
        "detail": "客户端使用的 Content-Type / Accept 不再被支持。",
    },
    "MEDIA_ADDED": {
        "severity": COMPATIBLE,
        "title": "新增媒体类型",
        "detail": "服务端新增可处理/可返回的媒体类型，不影响既有协商。",
    },
    # ---- status codes -----------------------------------------------------
    "STATUS_REMOVED": {
        "severity": BREAKING,
        "title": "响应状态码被删除",
        "detail": "针对该状态码编写处理逻辑的客户端将无法匹配响应。",
    },
    "STATUS_ADDED": {
        "severity": COMPATIBLE,
        "title": "新增响应状态码",
        "detail": "客户端通常走默认分支处理未知状态码，属于附加信息。",
    },
    # ---- schema: type -----------------------------------------------------
    "TYPE_CHANGED": {
        "severity": BREAKING,
        "title": "类型改变",
        "detail": "JSON 类型互不兼容，请求侧收窄或响应侧放宽（视方向而定均为破坏）。",
    },
    "TYPE_NARROWED": {
        "severity": BREAKING,
        "title": "类型收窄",
        "detail": "候选类型集合是基线的真子集（请求侧不可收窄；若出现在响应侧则同样视为破坏）。",
    },
    "TYPE_WIDENED": {
        "severity": BREAKING,
        "title": "类型放宽",
        "detail": "响应侧可能返回客户端无法处理的新类型；请求侧放宽本身兼容，但响应方向为破坏。",
    },
    # ---- schema: enum -----------------------------------------------------
    "ENUM_NARROWED": {
        "severity": BREAKING,
        "title": "枚举收窄",
        "detail": "基线允许的枚举值在候选中被删除。请求侧旧值会被拒绝；响应侧说明取值空间变小但被删值仍可能由旧数据产生，按破坏报告。",
    },
    "ENUM_WIDENED": {
        "severity": BREAKING,
        "title": "枚举放宽",
        "detail": "响应侧新增枚举值，旧客户端可能无法识别；请求侧放宽本身兼容，响应方向为破坏。",
    },
    # ---- schema: nullability ----------------------------------------------
    "NULLABLE_REMOVED": {
        "severity": BREAKING,
        "title": "不可再为 null",
        "detail": "请求侧：旧客户端发送 null 会被拒绝（收窄）；响应侧：null 取值空间被移除。",
    },
    "NULLABLE_ADDED": {
        "severity": BREAKING,
        "title": "允许 null",
        "detail": "响应侧现在可能返回 null，未做 null 防御的客户端会失败；请求侧放宽本身兼容，响应方向为破坏。",
    },
    # ---- schema: required / properties ------------------------------------
    "REQUIRED_ADDED": {
        "severity": BREAKING,
        "title": "新增必填属性",
        "detail": "请求侧：旧客户端不发送该字段，请求被拒绝（收窄）。响应侧：服务端承诺新增必须存在的字段属兼容增强，但按“响应不可放宽”的对称保守规则在此呈现，实际影响通常为良性。",
    },
    "REQUIRED_REMOVED": {
        "severity": BREAKING,
        "title": "必填属性变为可选",
        "detail": "响应侧：客户端依赖该字段必然存在，升级后可能缺失（响应放宽）。请求侧放宽本身兼容，请求方向不报告此项。",
    },
    "PROPERTY_REMOVED": {
        "severity": BREAKING,
        "title": "属性被删除",
        "detail": "响应侧：客户端读取的字段消失（放宽）；请求侧：当 additionalProperties=false 时旧载荷被拒绝，否则仅提示。",
    },
    # ---- schema: ranges / lengths -----------------------------------------
    "NUMBER_BOUNDS_NARROWED": {
        "severity": BREAKING,
        "title": "数值范围收窄",
        "detail": "minimum 抬高或 maximum 降低，基线接受/承诺的值被排除。",
    },
    "NUMBER_BOUNDS_WIDENED": {
        "severity": BREAKING,
        "title": "数值范围放宽",
        "detail": "响应侧现在可能返回超出客户端预期的值；请求侧放宽本身兼容，响应方向为破坏。",
    },
    "LENGTH_BOUNDS_NARROWED": {
        "severity": BREAKING,
        "title": "长度/集合规模收窄",
        "detail": "minLength/maxLength/minItems/maxItems 收紧，基线值被排除。",
    },
    "LENGTH_BOUNDS_WIDENED": {
        "severity": BREAKING,
        "title": "长度/集合规模放宽",
        "detail": "响应侧允许更长字符串/更多元素，可能超出客户端处理假设。",
    },
    "MULTIPLE_OF_ADDED": {
        "severity": BREAKING,
        "title": "新增 multipleOf 约束",
        "detail": "新增倍数约束会拒绝/排除不满足的基线值。",
    },
    "MULTIPLE_OF_CHANGED": {
        "severity": BREAKING,
        "title": "multipleOf 改变",
        "detail": "倍数约束发生变化，可能拒绝/排除基线值。",
    },
    # ---- schema: arrays / maps --------------------------------------------
    "ITEMS_SCHEMA_CHANGED": {
        "severity": BREAKING,
        "title": "数组元素模式不兼容",
        "detail": "items 模式的破坏性变化会传递到整个数组。",
    },
    "ADDPROPS_FALSE_ADDED": {
        "severity": BREAKING,
        "title": "additionalProperties 变为 false",
        "detail": "请求侧：旧客户端发送的额外字段现在被拒绝（收窄）。",
    },
    "ADDPROPS_FALSE_REMOVED": {
        "severity": BREAKING,
        "title": "additionalProperties 解除 false",
        "detail": "响应侧现在可能携带任意额外字段（放宽）；请求侧为兼容增强。",
    },
    "ADDPROPS_SCHEMA_BROKEN": {
        "severity": BREAKING,
        "title": "附加属性模式不兼容",
        "detail": "additionalProperties 模式的破坏性变化会作用于所有未显式声明的字段。",
    },
    # ---- composition ------------------------------------------------------
    "ONEOF_BRANCH_REMOVED": {
        "severity": BREAKING,
        "title": "oneOf/anyOf 分支被删除",
        "detail": "请求侧：仅匹配该分支的载荷被拒绝（收窄）。响应侧：候选承诺的可辨识形态减少，客户端依赖的分支可能不再出现，按破坏报告。",
    },
    "ONEOF_BRANCH_ADDED": {
        "severity": BREAKING,
        "title": "oneOf/anyOf 新增分支",
        "detail": "响应侧现在可能返回新形态的数据（放宽）；请求侧新增分支本身兼容，响应方向为破坏。",
    },
    "ONEOF_BRANCH_INCOMPATIBLE": {
        "severity": BREAKING,
        "title": "oneOf/anyOf 匹配分支不兼容",
        "detail": "按结构指纹配对的分支之间存在破坏性差异。",
    },
    "ONEOF_UNMATCHED_STRUCTURE": {
        "severity": UNDETERMINED,
        "title": "组合分支结构无法可靠配对",
        "detail": "两边分支数量不同或无法按稳定指纹配对，分支级包含关系无法自动判定。",
    },
    "COMPOSITION_MIXED": {
        "severity": UNDETERMINED,
        "title": "allOf 与其他组合/约束混合",
        "detail": "allOf 同时内联 oneOf/anyOf/not 等复杂构造，交集语义无法可靠展开。",
    },
    # ---- unknown / unsupported / structural errors ------------------------
    "UNSUPPORTED_KEYWORD": {
        "severity": UNDETERMINED,
        "title": "存在未支持的 Schema 关键字",
        "detail": "关键字无法被规则引擎解释，为避免漏报破坏，该节点标记为无法判定。",
    },
    "UNRESOLVED_REF": {
        "severity": UNDETERMINED,
        "title": "$ref 无法解析",
        "detail": "引用指向包内不存在的文件、JSON Pointer 路径或外部 HTTP 地址；外部引用不被抓取。",
    },
    "TYPE_AMBIGUOUS": {
        "severity": UNDETERMINED,
        "title": "多重类型无法比较",
        "detail": "type 为多个非 null 类型的数组，兼容规则无法在不做数据流分析的情况下判定。",
    },
    "SCHEMA_INVALID": {
        "severity": UNDETERMINED,
        "title": "模式自身无效或自相矛盾",
        "detail": "例如 allOf 中出现互斥类型、无法满足的枚举交集等。",
    },
    "FORMAT_CHANGED": {
        "severity": UNDETERMINED,
        "title": "format 语义变更",
        "detail": "format（如 date-time/email/自定义格式）的校验强弱取决于实现，不自动判定。",
    },
    "NO_SCHEMA": {
        "severity": UNDETERMINED,
        "title": "缺少模式定义",
        "detail": "媒体类型存在但未给出 schema，无法比较其载荷结构。",
    },
}

# Keywords understood by the engine. Anything outside this set on a schema
# node yields UNSUPPORTED_KEYWORD (annotation keywords are exempted below).
SUPPORTED_KEYWORDS = {
    "$ref", "type", "enum", "nullable", "format", "items", "properties",
    "required", "additionalProperties", "allOf", "oneOf", "anyOf",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "minItems", "maxItems", "multipleOf",
    "minimumProperties", "maximumProperties",  # tolerated spellings
    "minProperties", "maxProperties",
    "uniqueItems", "description", "title", "example", "examples", "default",
    "deprecated", "readOnly", "writeOnly", "discriminator", "xml",
    "externalDocs", "$comment", "schema",  # OpenAPI wrapper `schema:` lives elsewhere
    "const",  # treated like a single-value enum
    "definitions",  # JSON Schema draft-4 style holder, ignored at this level
}

# Keywords recognized but intentionally not compared semantically; their
# presence alone is NOT reported (purely descriptive annotations).
ANNOTATION_KEYWORDS = {
    "description", "title", "example", "examples", "default", "deprecated",
    "readOnly", "writeOnly", "discriminator", "xml", "externalDocs", "$comment",
    "definitions",
}

# Keywords that force an undetermined result when present (we know them but
# cannot decide compatibility for them).
SEMANTICALLY_UNSUPPORTED = {
    "not", "pattern", "contains", "propertyNames", "if", "then", "else",
    "patternProperties", "unevaluatedProperties", "unevaluatedItems",
    "minContains", "maxContains", "dependentRequired", "dependentSchemas",
    "prefixItems",
}


def rule_info(code: str) -> dict[str, str]:
    return RULES[code]


def severity_of(code: str) -> str:
    return RULES[code]["severity"]
