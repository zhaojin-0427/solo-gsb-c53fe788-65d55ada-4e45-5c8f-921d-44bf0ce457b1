# OpenAPI 契约演进工作台

在服务端升级场景下，对两份 **OpenAPI 3.x** 文档（基线 / 候选）做契约兼容性比对：

- **请求模式不可收窄**：升级后的服务端必须继续接受旧客户端能发送的一切；
- **响应模式不可放宽**：升级后的服务端不得返回旧客户端无法理解的新形态。

对每个端点、每个媒体类型给出 `兼容 / 破坏性 / 无法判定` 判定，破坏性结果定位到
**JSON Pointer**、解释命中的规则，并生成一份**只在单侧校验通过的最小 JSON 反例**，
可直接在页面上**重放**（对两侧模式重新校验）。端点被删除（`ENDPOINT_REMOVED`）
时，反例是一份**最小 JSON 请求**（方法、已代入具体值的路径、必填 query/header/
cookie 参数、适用时的最小请求体）：重放按两份文档核对**端点存在性**与**基线请求
约束**，明确展示“基线可接受、候选无此端点”。参数可选变必填 / 新增必填、参数或
requestBody 被删除（`PARAMETER_MADE_REQUIRED` / `PARAMETER_ADDED_REQUIRED` /
`PARAMETER_REMOVED`）、响应状态码删除（`STATUS_REMOVED`）、媒体类型删除
（`MEDIA_REMOVED`）等**结构性 finding** 没有可双侧校验的根 schema，其反例是一份
**可编辑的结构化描述符**，重放按 finding 语义分别核对基线是否声明/接受该参数、
请求体或响应、候选是否缺失或收紧，而不再因缺少根 schema 指针而失败。

## 能力一览

- 解析内部与**本地跨文件** `$ref`（ZIP 文档包内任意相对路径）；外部
  `http(s)://` 引用不会被抓取，按“无法解析”报告；
- 展开并合并 `allOf`，识别 `oneOf` / `anyOf` 分支（按结构指纹配对）；
- 比较 `required`、`nullable`（OAS 3.0）/ `type: null`（3.1）、枚举、
  `type`、数值与长度边界、`multipleOf`、数组 `items`、
  `additionalProperties` 等；
- **循环引用安全终止**（自引用、相互引用均不会死循环，统计中可见终止次数）；
- `pattern`、`not`、`if/then/else`、`contains`、`prefixItems` 等
  **未支持的关键字一律标记为“无法判定”**，绝不静默忽略；
- **反例三态自检 / 重放**：破坏性 finding 的最小反例对双侧模式给出
  「确定通过 / 确定失败 / 待定」三态结论——`oneOf` 一过一待定（另一支含
  未支持的 `not` 等）**不宣称单侧通过**，待定原因与 `not` 的模式位置随
  finding 与重放响应返回（兼容旧布尔字段：待定为 `null`）；旧报告可直接
  重放并得到修正后的三态结论；
- 结果可按严重度（破坏性 / 无法判定 / 兼容）筛选，按端点分组或扁平浏览；
- **调用覆盖视图**：上传 NDJSON 调用记录，按「响应状态码 × JSON 媒体类型」
  生成覆盖格，展示已覆盖格/总格与缺口，未匹配 / 校验失败 / 无法判定逐行列明
  原因；调用值的 child 触达**自引用 / 相互引用循环**或无法解析的模式节点时，
  引擎沿实际值重展开被引模式继续核对——同一分支的已知约束失败优先判**失败**，
  否则记为**无法判定**（列出模式位置，不计格）；触达 `pattern` 等未支持
  关键字时同样记为**无法判定**（列出关键字与位置，不计覆盖）；未触达的
  可选循环属性仍可覆盖；`oneOf` / `anyOf` 组合分支按「确定通过 / 确定失败 /
  待定」三态裁决，且与同层属性 / 未支持关键字等 facet 按**交集**裁决——
  本层已知失败或组合整体确定失败才判失败，本层 facet 待定即使组合通过也判
  无法判定且不计格；分支的未决命中只在仍可能改变组合结论时才向上传递——
  确定失败分支（如缺少必填属性）里的 `not` 等命中直接丢弃，`anyOf` 已有
  确定通过时其余分支的待定命中不再传播，嵌套属性的组合确定通过即正常
  计格（见下文「调用覆盖」）；结果随分析保存并确定性导出；
- **覆盖缺口补测样本**：按选定文档侧的**已保存覆盖结果**，为每个未覆盖的
  状态码 × JSON 媒体格生成符合现有 NDJSON 调用格式、可下载的补测样本，
  并用现有三态判定复核样本确实补上目标格；精确码抢占范围、范围抢占
  default、模板路径被其他端点抢占，或必填值/响应载荷模式无法可靠构造时
  逐格说明原因，绝不输出归错格的样本；无缺口返回空 NDJSON（只读，不改写
  原覆盖与迁移结论，见下文「覆盖缺口补测样本」）；
- **请求迁移评估**：上传**基线时期** NDJSON 请求记录（兼容旧调用行，
  忽略 `response`），同一具体路径在基线与候选两侧**分别**匹配端点并
  绑定参数（模板变量改名不复用基线绑定），逐行给出两侧「确定通过 /
  确定失败 / 待定」三态原因；仅基线确定通过且候选确定失败记
  **受影响**，双侧确定通过记**未受影响**，基线确定失败记**不适用**，
  其余记**待核**；按基线端点汇总受影响请求与逐行原因，结果随分析
  保存并确定性导出（见下文「请求迁移评估」）；
- **确定性 JSON 导出**（键排序、无多余空白），响应头携带 SHA-256 指纹；
- **契约发布放行裁决**：对完整 `report.findings` 逐项登记带理由与 UTC 到期时间
  的例外，返回「阻断 / 待核 / 可放行」及每项生效或失效原因；例外绑定报告版本，
  报告重算、finding 消失或例外过期后旧许可不会继续生效；
- PostgreSQL 持久化分析记录与原始文档包（用于反例重放）；数据库不可用时
  自动降级为进程内存储；
- Docker Compose 一键启动（Web + PostgreSQL）。

## 一键启动（Docker Compose）

需要本机已安装 Docker 与 Docker Compose。

```bash
docker compose up --build
```

启动后访问：

- 工作台页面：<http://localhost:8000/>
- 发布放行页面：打开工作台 →「比对结果」→「发布放行」标签
- 请求迁移页面：打开工作台 →「比对结果」→「请求迁移」标签
- API 文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/api/health>

`docker compose down` 停止；加 `-v` 同时删除数据库卷。

一键启动后，可在页面「比对结果 → **调用覆盖**」标签中选择**基线**或**候选**侧、
上传 NDJSON 调用记录，查看覆盖矩阵与缺口（含自引用/相互引用循环、无法解析
节点的「无法判定」行与模式位置），或直接用下文的 HTTP API 上传/查询/导出。
保存后的覆盖结论同样体现在 `GET /api/analyses/{id}/coverage/{side}` 与
确定性导出中，页面、API、导出三者逐行一致。在该标签中还可直接查看并下载
「**覆盖缺口补测样本**」NDJSON（见下文专节）。

也可在页面「比对结果 → **发布放行**」标签中查看完整 findings 的逐项依据、
为指定破坏性或无法判定 finding 填写理由和 UTC 到期时间，顶部会显示
**阻断 / 待核 / 可放行**；或直接调用下文的发布裁决 HTTP API。

一键启动后，还可在页面「比对结果 → **请求迁移**」标签中上传基线时期的
NDJSON 请求记录，查看受影响 / 未受影响 / 待核 / 不适用的逐行双侧结论与
按端点汇总（示例文件 `examples/requests-baseline.ndjson`），或用下文的
请求迁移 HTTP API 上传 / 查询 / 导出。


### 上传内容

- 单个 `.yaml / .yml / .json` OpenAPI 文档；
- 或一个 `.zip` 文档包：包内可含多份相互引用的 YAML/JSON 文件，
  根文档通过 `openapi` 字段自动识别（歧义时优先 `openapi.*` / `index.*`）。

仓库 `examples/baseline` 与 `examples/candidate` 是一份刻意包含各类变化的示例
（跨文件引用、`allOf`、`oneOf`、循环引用、枚举收窄/放宽、nullable、
`pattern` 未支持关键字等），可将两个目录分别打成 ZIP 后上传。
`examples/calls-baseline.ndjson` 是配套的调用记录示例，供「调用覆盖」功能上传
（也可用 `curl -F file=@...` 直接提交，见下文「调用覆盖」）。
`examples/requests-baseline.ndjson` 是配套的**基线时期请求记录**示例，供
「请求迁移评估」上传（含新格式请求行与一行带 `response` 的旧调用行，
见下文「请求迁移评估」）。

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 可选：启动 PostgreSQL 后通过环境变量指向它；不配置则使用内存存储
export DATABASE_URL="postgresql://openapi:openapi@localhost:5432/openapi_contract"

uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
pytest -q
```

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/analyses` | multipart 上传 `baseline`、`candidate`，创建分析 |
| `GET` | `/api/analyses` | 历史分析列表 |
| `GET` | `/api/analyses/{id}` | 获取完整报告 |
| `GET` | `/api/analyses/{id}/export?severity=breaking,undetermined` | 确定性 JSON 导出（可按严重度筛选；含已保存的调用覆盖与请求迁移评估） |
| `GET` | `/api/analyses/{id}/release-decision` | 查询契约发布裁决、逐项依据、例外生效/失效原因 |
| `PUT` | `/api/analyses/{id}/release-exceptions/{fid}` | 为指定 finding 登记/更新带理由与 UTC 到期时间的例外（也兼容 `POST`） |
| `POST` | `/api/analyses/{id}/findings/{fid}/replay` | 对反例（可在 body 中用 `{"instance": ...}` 覆盖）做双侧校验重放 |
| `POST` | `/api/analyses/{id}/coverage/{side}` | 上传 NDJSON 调用记录，计算 `baseline`/`candidate` 侧调用覆盖 |
| `GET` | `/api/analyses/{id}/coverage` | 查询两侧调用覆盖的可用性与汇总 |
| `GET` | `/api/analyses/{id}/coverage/{side}` | 获取某侧完整调用覆盖文档（矩阵、缺口、逐行原因） |
| `GET` | `/api/analyses/{id}/coverage/{side}/gap-samples` | 按已保存覆盖为每个未覆盖格生成补测样本与逐格原因（JSON 说明） |
| `GET` | `/api/analyses/{id}/coverage/{side}/gap-samples.ndjson` | 下载可直接重新上传的补测样本 NDJSON（无缺口为空文件） |
| `GET` | `/api/analyses/{id}/coverage/{side}/export` | 确定性导出某侧调用覆盖 JSON（带 SHA-256 指纹头） |
| `DELETE` | `/api/analyses/{id}/coverage/{side}` | 删除某侧调用覆盖结果 |
| `POST` | `/api/analyses/{id}/migration` | 上传基线时期 NDJSON 请求记录，计算请求迁移评估（双侧三态 + 受影响汇总） |
| `GET` | `/api/analyses/{id}/migration` | 获取已保存的请求迁移评估文档 |
| `GET` | `/api/analyses/{id}/migration/export` | 确定性导出请求迁移评估 JSON（带 SHA-256 指纹头） |
| `DELETE` | `/api/analyses/{id}/migration` | 删除请求迁移评估结果 |
| `DELETE` | `/api/analyses/{id}` | 删除分析 |
| `GET` | `/api/health` | 服务与存储状态 |

导出响应头：

- `Content-Disposition: attachment; filename="contract-report-<id>-<severity>.json"`
- `X-Report-Fingerprint: <sha256>`（与导出字节一一对应，可用于校验确定性）

## 契约发布放行裁决（Release Gate）

裁决固定读取当前完整 `report.findings`，逐项使用 finding 自身的
`severity`：

1. 仍有未被有效例外覆盖的 **breaking** → `blocked`（阻断）；
2. breaking 均已处理，但仍有未被有效例外覆盖的 **undetermined**
   → `pending_review`（待核）；
3. 两类剩余项均为 0 → `releasable`（可放行）。

端点严重度只是聚合视图；页面严重度/文本筛选只改变可见项；反例自检的
`pending` 只说明生成的反例仍有未决校验因素。它们都**不能替代 finding 的
`severity`**。兼容项（`compatible`）不参与发布阻断或待核计数。

每个例外包含：

- `reason`：非空理由；
- `expires_at`：ISO 8601 到期时间，**必须带时区**；UTC 可写作
  `2030-01-01T00:00:00Z` 或 `2030-01-01T00:00:00+00:00`；
- 绑定键：`analysis_id + report_version + finding_id`。

`report_version` 是完整 `report.findings` 内容的 SHA-256。分析 ID 只由基线
与候选文档包内容计算，因此同两份文档包在比较器更新后重算会得到**相同分析
ID、不同 report_version** 并覆盖原报告；旧例外会标记为 `stale`，不会让新
结论沿用旧许可。finding 在当前版本中消失时，对应例外为
`missing_finding`；到期时间已过为 `expired`；只有同时匹配当前报告版本、
finding ID 且未过期时才为 `active`。未知 finding ID 的登记请求会被拒绝
（`404`）。裁决和登记例外只读取/写入发布许可数据，不会修改原始比对报告
或已保存的调用覆盖。

查询裁决：

```bash
curl -s "http://localhost:8000/api/analyses/$AID/release-decision" \
  -o release-decision.json
```

登记或更新某个 finding 的例外：

```bash
FID=$(python3 -c 'import json; d=json.load(open("/tmp/report.json")); print(next(f["id"] for f in d["findings"] if f["severity"] == "breaking"))')

curl -s -X PUT \
  "http://localhost:8000/api/analyses/$AID/release-exceptions/$FID" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"已确认灰度窗口内无旧客户端调用","expires_at":"2030-01-01T00:00:00Z"}'
```

响应中的 `decision.decision` 为 `blocked` / `pending_review` /
`releasable`，`decision.items[]` 给出每个 finding 的严重度来源、裁决处理
（`blocked` / `pending_review` / `waived` / `not_gating`）及具体原因；
`decision.exceptions[]` 保留每项登记记录的 `active` / `expired` / `stale`
/ `missing_finding` 状态与原因，便于审计。

页面使用方式：上传或打开历史分析后，进入「**发布放行**」标签。该页面不受
顶部严重度或文本筛选影响，始终展示完整 findings；在破坏性/无法判定项右侧
填写理由和 UTC 到期时间，点击「登记例外」即可，顶部裁决与逐项原因会即时
刷新。

## 调用覆盖（Call Coverage）

在已有分析上选择**基线或候选**一侧文档，上传一份 **NDJSON**（每行一个 JSON 对象）
调用记录，引擎把每个调用归并到该侧 operation 的某个**响应覆盖格**，统计
「已覆盖格 / 总格」与缺口，并对未匹配、校验失败、无法判定**逐行给出原因**。
结果随分析一起持久化，并可确定性导出。

### NDJSON 行格式

每行一个 JSON 对象：

| 字段 | 说明 |
| --- | --- |
| `method` | HTTP 方法（大小写不敏感） |
| `path` | 具体请求路径，以 `/` 开头（不含 query） |
| `query` / `headers` / `cookies` | 参数键值对象，可省略（默认 `{}`） |
| `request` | 请求描述：`media_type` + `body`；**缺省 `body` 键表示无请求实体** |
| `response` | 响应描述：整数（或数字字符串）`status`、可选 `media_type` 与 JSON `body`；**缺省 `body` 键表示无响应实体** |

请求/响应头的参数名按 HTTP 惯例**大小写不敏感**匹配，query/cookie 名大小写敏感；
query/header/cookie/path 的文本值会按参数 schema 尝试标量 coerce（如 `"10"`→整数）。

仓库自带示例 `examples/calls-baseline.ndjson`（覆盖、失败、未匹配、非 JSON
排除以及取值触达自引用 `parent` 导致的无法判定 / 递归部分已知失败各若干行）。

### 归属与覆盖规则

1. **路径匹配**：先匹配精确路径；精确路径存在但不支持该方法 → 未匹配；
   否则在声明了该方法的路径模板中做结构匹配（字面段全等、`{var}` 段非空），
   **唯一命中**才归属；同时命中多个模板 → **未归属（ambiguous）**。
2. **覆盖格**：每个 operation 的格为「响应状态码 × JSON 媒体类型」；无 `content`
   的响应单列一个**无实体格**。状态码依次匹配 **精确值 → `1XX`…`5XX` 范围键 →
   `default`**。非 JSON 媒体类型（如 `application/xml`、`text/csv`）不生成格，
   调用携带这些媒体时记「非 JSON 媒体排除」并在 operation 上列明。
   JSON 媒体指 `application/json` 及 `*/*+json`（如 `application/vnd.api+json`）。
3. **判定**：仅当**请求参数**（必填存在性 + schema）、**请求体**（required、
   content 媒体类型、body schema）、**响应体**（status 归属、content 与实体一致性、
   body schema）的**可判定约束全部通过**时才覆盖该格；取值沿 schema 图触达
   无法解析节点 → **无法判定**（报告模式位置，不给覆盖）；任一约束失败 →
   **校验失败**（同一分支的已知失败优先：即使该分支取值同时触达循环或
   未支持约束，仍判失败）。
   已知约束全部通过后，引擎还会**沿调用实际取值遍历解析后的 schema 图**
   （本地 `$ref`、嵌套属性、数组元素、`oneOf`/`anyOf` 组合分支）：调用值的
   `child` 等属性触达**自引用 / 相互引用节点**时，会沿值重展开被引模式——
   递归部分的已知约束失败则判失败，否则该值能否通过无法定论，记为
   **无法判定**并给出模式位置（跨文件时含文件名，如
   `models/pet.yaml#/Pet`），不增加已覆盖格；**未触达的可选循环属性仍可
   覆盖**；能否通过仍依赖 `pattern`、`not`、`contains` 等**未支持关键字**时，
   同样记为**无法判定**并列出关键字及其位置，与取值类型无关的关键字（如
   整数值上的 `pattern`）不会误报。
   组合分支按**三态**裁决——确定通过 / 确定失败 / 待定（已知约束全过但仍
   依赖未支持或不可判定约束），且**同层 facet 与组合结果按交集裁决**：
   节点自身的属性 / 类型 / 未支持关键字等约束与 `oneOf`/`anyOf` 组合结论
   取交集——**本层已知失败，或组合整体确定失败时判校验失败**；否则本层
   约束待定（取值的 `child` 等属性触达自引用 / 相互引用节点，或命中
   `pattern` 等未支持关键字）**即使组合分支已经确定通过，也报告模式位置、
   判无法判定且不计覆盖格**，绝不把组合结论直接当作节点结论。组合本身仍按
   下列规则裁决：
   - `oneOf` 要求**恰好一个**分支通过：一个分支确定通过、另一分支含
     `pattern` 等未支持约束或触达循环**仍可能通过**时，是否“恰好一个”无法
     定论 → 该行记为**无法判定**（列出待定关键字及位置，不计覆盖格）；
     **两个分支都确定通过 → 校验失败**。
   - `anyOf` 要求**至少一个**分支通过：组合本身在任一分支确定通过时成立；
     没有确定通过、仅剩待定分支时才记**无法判定**。
   分支的未决命中（未支持关键字、循环、不可解析节点）只在**仍可能改变
   组合结论**时才向父节点传递，嵌套属性的组合结论不会因此被污染：
   - 确定失败的分支不可能翻案——例如某分支因缺少必填属性确定失败，即使
     它同时含未支持的 `not`，该 `not` 命中也直接丢弃；此时 `oneOf` /
     `anyOf` 另一支确定通过即组合确定通过，该行记 **covered 并计格**；
   - `anyOf` 已有一个确定通过时，其余分支无论待定与否都不可能改变
     「至少一个」的成立，其待定命中同样不再传播（一过一待定**可计格**）；
   - `oneOf` 一过一待定（是否「恰好一个」无定论）或零通过仍有待定分支
     时，组合结论为待定，待定分支的命中随结论上报（**不计格**）。
   注意组合通过只裁决组合关键字：例如 schema 同时含自引用 `properties.child`
   与 `oneOf`/`anyOf` 时，若调用值沿 `child` 触达循环使本层属性 facet 待定，
   即使 `anyOf` 已有确定通过的分支，交集结论仍是**无法判定**（不计格）；
   **未触达的可选 `child` 不阻断覆盖**。
4. **零分母**：一个 operation 只有非 JSON 媒体（或无任何可覆盖格）时标记
   **「无适用格」**，不参与覆盖率分母。

### 实际请求与响应示例

针对示例文档包创建分析后，对**基线**侧上传调用记录：

```bash
curl -s -X POST http://localhost:8000/api/analyses \
  -F baseline=@examples/baseline.zip \
  -F candidate=@examples/candidate.zip -o /tmp/report.json

AID=$(python3 -c "import json;print(json.load(open('/tmp/report.json'))['analysis_id'])")

curl -s -X POST "http://localhost:8000/api/analyses/$AID/coverage/baseline" \
  -F file=@examples/calls-baseline.ndjson
```

上传体是 multipart（字段名 `file`）。响应（节选）形如：

```json
{
  "schema_version": "1.0",
  "analysis_side": "baseline",
  "source_file": "calls-baseline.ndjson",
  "summary": {
    "operations_total": 5, "operations_applicable": 5,
    "operations_without_cells": 0,
    "cells_total": 7, "cells_covered": 6, "cells_gap": 1,
    "calls_total": 12, "calls_covered": 6,
    "calls_validation_failed": 3, "calls_undetermined": 1,
    "calls_unmatched": 1, "calls_ambiguous": 0,
    "calls_excluded_media": 1, "calls_invalid": 0
  },
  "operations": [
    {
      "path": "/pets/{petId}", "method": "GET", "applicable": true,
      "covered": 2, "total": 2,
      "cells": [
        {"key": "200|application/json", "status": "200", "kind": "content",
         "media_type": "application/json", "covered": true,
         "covered_by_line": 3},
        {"key": "404|(no content)", "status": "404", "kind": "no_content",
         "media_type": null, "covered": true, "covered_by_line": 4}
      ],
      "gaps": [], "excluded_media": []
    }
  ],
  "rows": [
    {"line": 1, "outcome": "covered", "method": "GET", "path": "/pets",
     "status": "200", "operation": "GET /pets",
     "matched_template": "/pets", "cell": "200|application/json",
     "resolved_status": "200", "reasons": []},
    {"line": 11, "outcome": "undetermined", "method": "GET",
     "path": "/pets/8", "status": "200", "operation": "GET /pets/{petId}",
     "matched_template": "/pets/{petId}", "cell": null,
     "reasons": ["响应 200 application/json 载荷能否通过无法判定: "
                 "循环引用（models/pet.yaml#/Pet）"]},
    {"line": 12, "outcome": "validation_failed", "method": "GET",
     "path": "/pets/9", "status": "200", "operation": "GET /pets/{petId}",
     "matched_template": "/pets/{petId}", "cell": null,
     "reasons": ["响应 200 application/json 载荷不满足 schema: "
                 "/parent/id: 类型 string 不被允许 ['integer']"]},
    {"line": 7, "outcome": "validation_failed", "method": "GET",
     "path": "/pets", "status": "200", "operation": "GET /pets",
     "matched_template": "/pets", "cell": null,
     "reasons": ["查询参数 'status'不满足契约约束: #: 值不在枚举 [...] 中"]},
    {"line": 8, "outcome": "unmatched", "method": "GET",
     "path": "/unknown/path", "status": "200", "operation": null,
     "matched_template": null, "cell": null,
     "reasons": ["没有路径模板匹配 GET /unknown/path"]}
  ]
}
```

`outcome` 取值：`covered` / `validation_failed` / `undetermined` /
`unmatched` / `ambiguous`（多模板未归属）/ `excluded_media` / `invalid`
（行本身不是合法 NDJSON 调用对象）。整份文件没有任何可解析行时返回 `400`。

查询与导出：

```bash
# 两侧可用性 + 汇总
curl -s "http://localhost:8000/api/analyses/$AID/coverage"

# 某侧完整文档
curl -s "http://localhost:8000/api/analyses/$AID/coverage/baseline"

# 确定性导出（键排序、无多余空白），指纹在响应头
curl -s -D - "http://localhost:8000/api/analyses/$AID/coverage/baseline/export" \
  -o coverage-baseline.json
# X-Coverage-Fingerprint: <sha256>

# 已保存的覆盖同时内嵌进分析总导出 call_coverage 字段
curl -s "http://localhost:8000/api/analyses/$AID/export" -o report.json
```

重复上传同一侧会**替换**旧结果；`DELETE` 删除指定侧；分析被删除时其覆盖一并删除。
相同 NDJSON 输入永远产生字节一致的导出（含逐行原因，覆盖格只记录最早一行覆盖调用）。

## 覆盖缺口补测样本（Gap Samples）

某侧覆盖结果**保存后**，可针对其中每个**未覆盖响应格**生成一行符合上文
NDJSON 调用格式的**补测样本**：把它和原调用记录合并后重新上传，样本会按
现有覆盖引擎归属并**确实覆盖目标格**（三态判定为 `covered`、`cell` 为该格）。
生成是**只读**的——不会修改已保存的调用覆盖或请求迁移结论；缺少已保存
覆盖结果或原始文档包时接口明确报错（`409`），没有缺口时 NDJSON 为**空文件**。

### 选码与归属安全（与覆盖引擎同一优先级）

- **精确码格**只能用该精确码；**范围格（如 `2XX`）**选用范围内**未被精确码
  抢占**的最小状态码（如同时声明了 `200`，范围格样本用 `201`）；`default`
  格选用既无精确码、也不落入任何 `1XX`…`5XX` 范围键的最小状态码。范围被
  精确码占满、或 `100-599` 全部被精确码/范围键抢占时，该格**不给样本**并
  逐格说明「没有可用状态码」，绝不输出会归到别的响应的行。
- **模板路径抢占**：为 `{var}` 路径变量枚举可信取值（枚举 / 边界 / 类型
  标量），代入后再次走覆盖引擎的路径匹配（精确路径优先、唯一模板才归属）。
  具体路径被同方法精确端点抢占、或同时命中多个模板（ambiguous）时不给样本；
  例如 `GET /p/{id}`（integer）在存在精确端点 `GET /p/x` 时会选用 `/p/0`，
  而字符串枚举只有 `"x"` 时该格只能报告无法生成。
- **请求侧**：只填**必填**路径 / query / header / cookie 参数（可选参数按
  旧客户端惯例省略）与**必填**请求体（优先 `application/json` 与 `*+json`）。
  必填参数或请求体的模式无法解析 / 为循环 / 含 `pattern`、`not` 等未支持
  关键字、枚举边界等构造不出**确定通过**的值，或只有
  `application/octet-stream` 等不可综合媒体时，逐格说明原因。
- **响应侧**：无 `content` 的格生成不带 `body` 键的响应；JSON 媒体格构造
  经三态裁决**确定通过**的最小载荷（`oneOf` 必须恰好一支通过，含 `not` 的
  一过一待定分支不会被乐观采用；必填递归属性、`pattern` 等待定模式只报
  原因）；声明了 JSON content 但无 schema 时用 `{}`。

### 页面与 API

一键启动后，在页面「比对结果 → **调用覆盖**」标签选择文档侧并上传覆盖，
矩阵下方即出现「**覆盖缺口补测样本**」区块，逐格展示可补测样本（含选用的
状态码、归属说明与可复制的调用 JSON）或无法生成的原因；点击
「**⬇ 下载缺口补测样本 NDJSON**」获取与上传格式完全一致的文件（示例包
基线侧：`POST /pets` 的 `400` 无实体格可补，`POST /legacy/decide` 的
`200` 格因请求体 `oneOf` 含未支持的 `not` 而逐格说明）。

```bash
# JSON 说明文档（summary + 每格 generated/request_status/reasons + samples）
curl -s "http://localhost:8000/api/analyses/$AID/coverage/baseline/gap-samples"

# 可下载、可直接重新上传的 NDJSON（无缺口时为空字节；响应头带计数）
curl -s -D - "http://localhost:8000/api/analyses/$AID/coverage/baseline/gap-samples.ndjson" \
  -o gap-samples.ndjson
# X-Gap-Samples-Total / X-Gap-Samples-Generated

# 与原调用合并后重新上传，即按现有覆盖流程验证缺口被补上
cat examples/calls-baseline.ndjson gap-samples.ndjson > combined.ndjson
curl -s -X POST "http://localhost:8000/api/analyses/$AID/coverage/baseline" \
  -F file=@combined.ndjson
```

错误码：未保存该侧覆盖 / 原始文档包缺失 → `409`；未知文档侧 → `400`；
分析不存在 → `404`。

## 请求迁移评估（Request Migration）

在已有分析上上传一份**基线时期**的 NDJSON 请求记录，引擎把每一行请求
**分别**对照基线与候选两份文档做端点匹配与请求校验，回答“旧客户端发出
的这个请求，升级后的服务端还能不能接受”，并按基线端点汇总**受影响**
请求。它复用调用覆盖的**路径匹配与三态校验**机制，但**不消费覆盖结论**：
覆盖回答“调用覆盖了哪个响应格”（含响应校验），迁移只关心**请求能否被
候选侧接受**，两者语义独立、互不改写——上传 / 删除迁移结果不会影响已
保存的调用覆盖，也不参与发布放行裁决。

### 请求行格式

每行一个 JSON 对象（即**请求行**）：

| 字段 | 说明 |
| --- | --- |
| `method` | HTTP 方法（大小写不敏感） |
| `path` | 具体请求路径，以 `/` 开头（不含 query） |
| `query` / `headers` / `cookies` | 参数键值对象，可省略（默认 `{}`） |
| `request` | 可选请求描述：`media_type` + `body`；**缺省 `body` 键表示无请求实体** |
| `response` | **兼容旧调用行**：存在即被整体忽略（不校验、不计入结论） |

参数名大小写规则与标量 coerce 与调用覆盖一致（header 名大小写不敏感，
query/cookie 敏感，`"10"` 可按参数 schema coerce 为整数）。

### 双侧匹配与三态裁决

1. **两侧独立匹配**：同一具体路径先在基线文档匹配端点并绑定路径 /
   query / header / cookie 参数，再在候选文档**重新**匹配与绑定——
   模板变量改名（如基线 `/items/{itemId}`、候选 `/items/{item_id}`）
   时，候选侧按自己的模板变量名绑定取值并校验候选参数约束，**绝不
   复用基线的参数绑定**。
2. **每侧三态结论**：
   - `pass`（确定通过）：端点唯一归属，且请求参数与请求体的可判定
     约束全部通过、无未决因素；
   - `fail`（确定失败）：候选**缺端点**（无模板匹配，或精确路径存在
     但未声明该方法）、缺少必填参数 / 请求体、取值违反已知约束、
     媒体类型未声明等；
   - `pending`（待定）：**多模板歧义**（同一具体路径同时命中多个
     模板，无法唯一归属）、取值触达 `pattern` / `not` 等未支持
     关键字、自引用 / 相互引用循环或无法解析的模式节点、非 JSON
     请求媒体类型无法判定请求体等——与调用覆盖同一套沿取值三态
     裁决（`oneOf` 一过一待定不宣称通过等规则同样适用）。
3. **行级迁移分类**：
   - `affected`（受影响）：**仅**基线确定通过且候选确定失败；
   - `unaffected`（未受影响）：两侧都确定通过；
   - `not_applicable`（不适用）：基线确定失败（本就不是基线契约下
     的合法请求，迁移结论无从谈起）；
   - `pending`（待核）：其余一切情况（任一侧待定，含基线待定而
     候选确定失败——基线都未能确定通过，不能宣称受影响）。
4. **端点汇总**：按**基线端点**聚合归属行，给出各分类计数、受影响
   行号与逐行原因，并列出这些请求在候选侧归属的端点（候选无对应
   端点时明示）。基线侧未归属任何端点的行只出现在逐行列表中。

### 示例与 API

针对示例文档包创建分析后，上传基线时期请求记录：

```bash
curl -s -X POST "http://localhost:8000/api/analyses/$AID/migration" \
  -F file=@examples/requests-baseline.ndjson
```

响应（节选）形如：

```json
{
  "schema_version": "1.0",
  "kind": "request_migration",
  "source_file": "requests-baseline.ndjson",
  "summary": {
    "requests_total": 14, "affected": 5, "unaffected": 4,
    "not_applicable": 3, "pending": 2, "invalid": 0,
    "endpoints_total": 6, "endpoints_affected": 3
  },
  "endpoints": [
    {"operation": "GET /pets", "method": "GET", "path": "/pets",
     "candidate_operations": ["GET /pets"],
     "requests_total": 5, "affected": 3, "unaffected": 1,
     "not_applicable": 1, "pending": 0,
     "affected_lines": [2, 3, 4],
     "lines": [
       {"line": 2, "classification": "affected",
        "candidate_operation": "GET /pets",
        "reasons": ["缺少必填查询参数 'status'"]}
     ]}
  ],
  "rows": [
    {"line": 2, "method": "GET", "path": "/pets",
     "classification": "affected",
     "baseline": {"state": "pass", "operation": "GET /pets",
                  "matched_template": "/pets", "reasons": []},
     "candidate": {"state": "fail", "operation": "GET /pets",
                   "matched_template": "/pets",
                   "reasons": ["缺少必填查询参数 'status'"]},
     "reasons": ["缺少必填查询参数 'status'"]},
    {"line": 11, "method": "POST", "path": "/legacy/echo",
     "classification": "affected",
     "baseline": {"state": "pass", "operation": "POST /legacy/echo",
                  "matched_template": "/legacy/echo", "reasons": []},
     "candidate": {"state": "fail", "operation": null,
                   "matched_template": null,
                   "reasons": ["没有路径模板匹配 POST /legacy/echo"]},
     "reasons": ["没有路径模板匹配 POST /legacy/echo"]}
  ]
}
```

查询、导出与删除：

```bash
# 已保存的迁移评估
curl -s "http://localhost:8000/api/analyses/$AID/migration"

# 确定性导出（键排序、无多余空白），指纹在响应头
curl -s -D - "http://localhost:8000/api/analyses/$AID/migration/export" \
  -o migration.json
# X-Migration-Fingerprint: <sha256>

# 删除；分析被删除时迁移结果一并删除
curl -s -X DELETE "http://localhost:8000/api/analyses/$AID/migration"

# 已保存的迁移评估同时内嵌进分析总导出 request_migration 字段
curl -s "http://localhost:8000/api/analyses/$AID/export" -o report.json
```

重复上传会**替换**旧结果；整份文件没有任何可解析行时返回 `400`。
相同 NDJSON 输入永远产生字节一致的导出（含逐行双侧原因）。
页面使用方式：上传或打开历史分析后，进入「比对结果 → **请求迁移**」
标签，选择 NDJSON 文件并点击「上传 NDJSON 评估迁移」，即可查看汇总
卡片、按端点的受影响请求（逐行原因）与逐行双侧判定表。

## 判定规则（方向：基线 → 候选）

### 请求侧（不可收窄）

新增必填参数/属性、可选变必填、枚举收窄、类型收窄、数值/长度下界抬高或上界压低、
新增 `multipleOf`、`additionalProperties` 变为 `false`、删除参数/媒体类型/端点等
→ **破坏性**。

### 响应侧（不可放宽）

枚举新增取值、类型放宽、允许 `null`、`oneOf` 新增分支、删除必有字段、
数值/长度边界放宽、删除 `additionalProperties:false` 等 → **破坏性**。

### 无法判定

存在未支持的关键字（`pattern`、`not`、`if/then/else`、`contains` …）、
`$ref` 无法解析（含外部引用）、`format` 语义变化、多重非空类型数组、
`oneOf` 分支无法可靠配对、模式自相矛盾等 → **无法判定**，并给出对应 JSON Pointer。

每条破坏性结果包含：

- `rule` / `rule_title` / `rule_detail`：命中规则与解释；
- `pointer.baseline` / `pointer.candidate`：定位（跨文件时另附 `file`）；
- `witness.instance`：最小 JSON 反例；
- `witness.self_check`：生成时对双侧模式的**三态**自检结果；
- 重放接口再次独立校验，并给出失败原因列表。

### 反例自检与重放的三态裁决

模式级反例（参数 / 请求体 / 响应中的 schema 变化）的自检与重放都采用与
调用覆盖一致的**三态**裁决，而不是只有「通过 / 失败」的布尔结论：

- **确定通过（pass）**：取值触达的所有已知约束通过，且不依赖任何未决因素；
- **确定失败（fail）**：取值触达的已知约束已经失败（未支持约束只会更严格，
  不可能挽回该失败）；
- **待定（pending）**：已知约束全过，但取值沿模式图触达了未支持关键字
  （`not`、`pattern`、`if/then/else` …）、自引用 / 相互引用循环或无法解析
  的模式节点，能否通过无法定论——结果中逐点列出原因与模式 JSON Pointer
  （跨文件时带文件名）。

组合关键字遵循标准三态语义：**已知失败压过同支未决命中**——某分支因缺少
必填属性等确定失败时，它同时携带的 `not` 等未决命中直接丢弃；`oneOf`
要求**恰好一个**分支通过：一过一待定（是否恰好一个无定论）**不得宣称通过**，
两个确定通过才判失败；`anyOf` 要求至少一个：同层其他 facet 均已确定时
**一过一待定仍可确定通过**，待定分支的命中不再向上传播。

`witness.self_check` 与重放响应字段在兼容旧布尔字段的前提下表示三态：

- `baseline_state` / `candidate_state`：`pass` / `fail` / `pending`；
- `baseline_valid` / `candidate_valid`：**布尔兼容字段**——确定通过为
  `true`、确定失败为 `false`，**待定无法用布尔表示时为 `null`**（旧消费者
  按真值判断时不会把待定误读为通过）；
- `one_side_only` / `exactly_one_side` / `verifiable`：仅当**两侧都确定、
  且恰好一侧通过**时才为 `true`，因此一过一待定的 `oneOf` 反例（例如基线
  请求体 `oneOf` 一支对 `{}` 确定通过、另一支含未支持的 `not`，候选只允许
  `string`）虽然**破坏性结论保留、finding 保留**，但自检与重放都只会报告
  “基线待定、候选确定失败”，绝不标记为已验证的单侧通过；
- `pending_reasons`：待定原因列表（含 `not` 等关键字及其位置）；
- 页面抽屉用「✓ 通过 / ✗ 失败 / ？ 待定」三张卡片展示根模式与关键字路径
  处的局部校验，待定时顶部明确提示“不能宣称已验证的单侧通过”并列出位置；
  在 JSON 编辑器中改写实例（例如把 `{}` 改成 `"x"`）消除未决因素后重放，
  即可得到确定的单侧通过结论。

**旧报告可重放**：升级前保存的分析只带旧的布尔 `self_check`，重放会从存储
的两份文档重建两侧模式并按三态重新校验根值与局部值——旧报告中被乐观标记为
“基线通过”的一过一待定反例，重放后会如实显示待定，而 finding 本身不变。

示例文档包中的 `POST /legacy/decide` 即为这条链路的可复现实例（基线请求体
`oneOf: [object, object + not(string)]`，候选收窄为仅 `string`）：上传
`examples/baseline` 与 `examples/candidate` 两个 ZIP 后，在结果抽屉中打开该
finding，即可看到 `{}` 反例的基线「？ 待定」、候选「✗ 失败」以及
`…/oneOf/1/not` 的位置提示。

### 端点删除（ENDPOINT_REMOVED）的反例与重放

候选整体移除某个 operation 时，候选侧**不存在可校验的 schema**，因此该结果不
走模式重放，而是生成并核对一份**端点级最小请求**：

- `witness.instance` 形如
  `{"method": "POST", "path": "/shops/5/orders/abc", "query": {...},
  "header": {...}, "cookie": {...}, "body": {...}}`：路径模板占位符已被满足
  基线约束的具体值替换；只包含**必填**参数（可选参数按旧客户端惯例省略）；
  存在**必填**请求体时附最小 JSON `body`，可选请求体则省略并在 `caveats` 说明。
- 重放分别检查两份文档中端点是否存在（候选保留了 path item 但删掉该 method 时
  会明确区分），并把请求重新对照基线 operation 的路径/查询/请求头/Cookie 参数
  与请求体 schema 校验；结论展示“**基线可接受、候选无此端点**”。
- 当基线请求无法**可靠构造**（必填参数的 `$ref` 无法解析、只有
  `application/octet-stream` 等无法综合的请求体媒体类型、最小实例自校验失败
  等），破坏性结论**保留**，但 `witness.verifiable=false` 并给出
  `unverifiable_reason`：页面标记“反例不可验证”，重放不会伪称单侧通过；
  仍可在页面编辑器中手写请求后再次重放。
- 表单类媒体类型（`application/x-www-form-urlencoded` /
  `multipart/form-data`）按结构化 schema 校验 body，并提示未重放实际线缆编码。

反例均可在结果抽屉的 JSON 编辑器中修改后重放；模式级（参数 / 请求体 / 响应中的
schema 变化）反例走双侧 schema 校验，端点级反例走上文的存在性 + 基线约束核对。

### 结构性 finding 的反例与重放

以下 finding 在候选侧（或基线侧）根本不存在可校验的媒体 schema，旧版本会错误地
落入模式重放并报“重放信息缺少根 schema 指针”；现在它们生成**结构化描述符**
反例，重放从存储的两份文档**重新推导**声明与约束：

| 规则 | 反例描述符（可编辑） | 重放核对 |
| --- | --- | --- |
| `PARAMETER_MADE_REQUIRED`（参数） | `{method, path, parameter, in, value}` | 基线声明该参数且**可选**；候选把它收紧为**必填** |
| `PARAMETER_ADDED_REQUIRED`（参数） | 同上 | 基线**未声明**该参数；候选声明且**必填** |
| `PARAMETER_REMOVED`（参数） | 同上 | 基线声明并按参数 schema **接受** `value`；候选**无此参数** |
| `PARAMETER_*`（requestBody） | `{method, path, media_type, body}` | 基线 requestBody 的声明/必填与 `body` 的 schema 校验；候选删除 requestBody 或将其收紧为必填 |
| `STATUS_REMOVED` | `{method, path, status_code, media_type, body}` | 基线声明该状态码（有 schema 时校验 `body`）；候选**无此状态码** |
| `MEDIA_REMOVED` | `{method, path, location, status_code?, media_type, body}` | 基线声明并接受/返回该媒体类型且 `body` 通过其 schema；候选**无此媒体类型** |

抽屉里分别展示“基线核对 / 候选核对”两张卡片（声明情况、`required`、媒体类型、
schema 错误）以及携带值/载荷的独立校验；只有当两侧事实都与 finding 语义一致时
才给出“**反例可验证**”，否则保留破坏性结论并标记“反例不可验证”，可在编辑器中
修改描述符后再次重放。跨文件 `$ref` 的 requestBody / 响应会先解析到实际定义位置
再取 schema。

## 目录结构

```
app/
  api/routes.py          # FastAPI 路由（上传、分析、导出、重放、历史）
  services/
    loader.py            # 单文件 / ZIP 包加载与根文档识别
    pointer.py           # JSON Pointer (RFC 6901) 工具
    resolver.py          # 本地/跨文件 $ref 解析（外部引用拒绝抓取）
    schemagraph.py       # 规范化 Schema 图：$ref、allOf 合并、循环终止
    instance.py          # 最小实例生成 + 模式校验（反例重放基础）+ 沿实际取值的三态裁决（含循环/不可解析节点）
    endpointrequest.py   # 端点级最小 JSON 请求构造与基线请求约束核对
    structuralreplay.py  # 结构性 finding（参数/请求体/状态码/媒体类型增删）的结构化反例与语义重放
    comparator.py        # 兼容性比较引擎与最小反例综合
    coverage.py          # 调用覆盖：NDJSON 解析、路径归属、参数/请求体/响应体约束核对、未支持关键字沿值判定、覆盖格
    gapsamples.py        # 覆盖缺口补测样本：抢占安全选码/路径复核、必填值与最小响应载荷综合、三态判定复核、NDJSON 导出
    migration.py         # 请求迁移评估：双侧独立匹配/绑定、请求参数与请求体三态校验、受影响/未受影响/不适用/待核分类与端点汇总
    release.py           # 发布放行裁决：完整 findings、例外版本绑定、过期/重算原因
    replay.py            # 双侧重放校验（模式级 + 端点级）
    rules.py             # 规则目录与严重度
  analysis.py            # 编排：加载→比较→序列化→存储
  serialization.py       # 确定性 JSON 导出
  db.py / store.py       # PostgreSQL 持久化 + 内存降级（含调用覆盖、发布例外）
  static/                # 原生 JS 前端（无框架、无构建步骤）
examples/                # 基线 / 候选示例文档包 + calls-baseline.ndjson 调用记录 + requests-baseline.ndjson 基线时期请求记录
tests/                   # pytest（引擎/循环 32 项 + API 26 项 + 调用覆盖 52 项 + 请求迁移 19 项 + 缺口补测 16 项）
```

## 说明与边界

- 比对基于 OpenAPI 3.x 的 Schema 约束语义；`format` 默认不做强制校验，
  因此 format 变化只判“无法判定”而非破坏；
- 反例综合在复杂组合约束下是**尽力而为**：报告的 `self_check` 会如实标注
  是「确定通过 / 确定失败 / 待定」，待定（如 `oneOf` 一过一待定）不会被
  标记为已验证的单侧通过，并给出 `not` 等未决因素的位置；不满足严格单侧
  时可在页面编辑器里手动修改实例后再次重放；
- 不抓取任何网络资源，所有分析均在本地/容器内完成。
