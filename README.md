# 招投标公告采集 API

面向招投标专员的公共采购公告采集服务：同步指定公告列表页与详情页，抽取
**公告编号、项目编号、公告类型、发布时间、投标截止时间、引用编号**，沿引用链
归并首次公告、更正公告与撤销公告，自动识别缺失引用、引用成环、并行更正等冲突，
并输出每条链的状态、有效截止时间与来源链接。

**多采购目录隔离**：两个采购目录可能出现相同公告编号（如都有 `X-001`）。
公告的业务身份是「列表入口 `list_url` + 公告编号 `notice_no`」的组合，
同编号公告按列表入口分别保存、互不覆盖；更正/撤销只在本入口内沿编号查找
引用，本入口缺失的引用标记为 `missing_reference` 冲突，绝不借用另一入口的
同号公告。

技术栈：FastAPI + BeautifulSoup4 + SQLite，Docker Compose 同时启动采集 API
与一个内置的双目录示例公告站。

## 目录结构

```
.
├── app/                    # 采集 API
│   ├── config.py           # 环境变量配置
│   ├── db.py               # SQLite 连接、表结构与旧库自动迁移
│   ├── fetcher.py          # HTTP 抓取（超时 + 有限重试）
│   ├── parser.py           # 列表页/详情页解析（BeautifulSoup）
│   ├── chains.py           # 按列表入口分作用域的引用链归并、冲突识别
│   ├── service.py          # 同步编排与持久化（按入口隔离的失败容错）
│   ├── models.py           # 请求/响应模型
│   └── main.py             # FastAPI 路由
├── sample_site/            # 双目录示例公告站（仅 Python 标准库）
│   ├── server.py
│   └── Dockerfile
├── tests/                  # pytest 测试（28 个用例，含双目录与迁移）
├── Dockerfile              # API 镜像
├── docker-compose.yml      # api + sample-site 两个服务
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 采集 API：http://localhost:8000 （交互式文档 /docs）
- 示例公告站首页：http://localhost:8080/
  - 目录一（综合采购目录）：http://localhost:8080/announcements
  - 目录二（专项采购目录）：http://localhost:8080/announcements-2
  - 两个目录都有编号 `X-001`、`X-002`，但内容与截止时间不同

分别同步两个采购目录：

```bash
# 目录一
curl -X POST http://localhost:8000/api/v1/sync \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://sample-site:8080/announcements"}'

# 目录二（同编号 X-001 另存一行，不覆盖目录一）
curl -X POST http://localhost:8000/api/v1/sync \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://sample-site:8080/announcements-2"}'
```

按列表入口查询引用链（两个入口的 `X-001` 各成一条独立链）：

```bash
curl "http://localhost:8000/api/v1/chains?list_url=http%3A%2F%2Fsample-site%3A8080%2Fannouncements-2"
```

按编号 + 列表入口查询单条公告：

```bash
curl "http://localhost:8000/api/v1/announcements/X-001?list_url=http%3A%2F%2Fsample-site%3A8080%2Fannouncements"
curl "http://localhost:8000/api/v1/announcements/X-001?list_url=http%3A%2F%2Fsample-site%3A8080%2Fannouncements-2"
# 不带 list_url 且编号命中多个入口时返回 409，并在 detail.list_urls 给出可选入口
curl "http://localhost:8000/api/v1/announcements/X-001"
```

SQLite 数据文件存放在 Docker 卷 `api-data`（容器内 `/data/procurement.db`），
容器重建后数据不丢失。旧版（`notice_no` 单列唯一）数据库首次启动时自动迁移
为组合唯一，历史公告迁移后仍可查询。

## 本地开发（无 Docker 时）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 终端 1：示例公告站
SAMPLE_PORT=8080 python -I sample_site/server.py

# 终端 2：API
DB_PATH=./data/procurement.db uvicorn app.main:app --reload --port 8000

# 测试
pytest -q
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/v1/sync` | 同步指定列表页及其全部详情页，请求体 `{"list_url": "..."}` |
| GET | `/api/v1/chains` | 返回全部引用链 `chains` 与孤立异常公告 `orphans`，可带 `?list_url=` 只看某一入口 |
| GET | `/api/v1/announcements/{notice_no}` | 查询单条公告；同号多入口时必带 `?list_url=`，否则 409 |
| GET | `/api/v1/sync-runs?limit=20` | 同步执行记录，可带 `?list_url=` 过滤 |

链、公告与孤立异常的响应体均包含 `list_url` 字段，标明所属列表入口。

`/api/v1/chains` 响应示例（节选）：

```json
{
  "chains": [
    {
      "chain_id": "X-001",
      "list_url": "http://sample-site:8080/announcements-2",
      "project_no": "PRJ-X2",
      "status": "ok",
      "effective_deadline": "2026-12-18 15:00:00",
      "announcements": [
        {
          "notice_no": "X-001",
          "list_url": "http://sample-site:8080/announcements-2",
          "notice_type": "first",
          "deadline": "2026-12-15 09:30:00",
          "ref_notice_no": null,
          "source_url": "http://sample-site:8080/notice2/X-001",
          "conflict_reasons": [],
          "effective_deadline": "2026-12-18 15:00:00"
        }
      ]
    }
  ],
  "orphans": []
}
```

## 业务规则

### 多入口隔离（同编号不覆盖、不串用）

- 公告唯一身份：`UNIQUE(list_url, notice_no)`；同步新目录时同编号公告另存
  一行，重复同步同一目录只更新本入口的那一行。
- 更正/撤销的引用编号**只在本入口内解析**：本入口找不到被引用公告即判
  `missing_reference`，即使另一入口存在同号公告也不借用（示例站目录一的
  `X-009` 引用只在目录二存在的 `X-010`，目录一判冲突、目录二的 `X-010`
  自身保持正常）。
- 并行更正、成环、撤销终止、链归并等全部计算均在单个列表入口作用域内进行。

### 链归并

- 公告类型映射：含「首次」→ `first`，含「更正」→ `correction`，
  含「撤销」→ `revocation`。
- 更正/撤销通过「引用编号」指向上一条公告；沿引用反向追溯，链标识取本入口
  内链上首次公告的公告编号（不同入口允许出现相同 `chain_id`）。
- **同一项目编号下的不同首次公告保持独立**，不做合并（示例中 PRJ-G 有两条链）。
- 链状态：
  - `ok`：结构正常，返回有效截止时间；
  - `cancelled`：链上存在撤销公告，整条链失效，有效截止时间为 `null`；
  - `conflict`：链上存在冲突，**不推测截止时间**，有效截止时间为 `null`。
- 有效截止时间：取链上明确给出的最新截止时间；更正未给出截止时间时沿用链上
  此前的截止时间（继承已有数据，而非推测）。
- 撤销公告之后又挂接的公告不再属于该链，作为孤立异常公告处理
  （原因 `entry_after_revocation`）。

### 冲突识别（冲突即不推测截止时间）

| 冲突原因 | 触发条件 |
| --- | --- |
| `missing_reference` | 引用编号在**本入口**库内不存在；或更正/撤销公告未填引用编号 |
| `reference_cycle` | 引用关系成环（含自环） |
| `parallel_correction` | 同一公告被两个及以上公告引用（并行更正） |
| `first_notice_with_reference` | 首次公告却携带引用编号（结构异常） |

无法归入任何链的公告进入 `orphans`，每条都带状态、冲突原因、所属入口与
来源链接，且有效截止时间为 `null`。

### 详情抓取失败的容错（按入口隔离）

- 列表页抓取失败：本次同步标记 `failed`，不动库内现有数据。
- 单条详情页抓取/解析失败：
  - 本入口此前成功过 → **保留本入口上次完整结果**，仅记录 `last_fetch_error`
    与同步时间，同步状态为 `partial`，响应中 `kept_last` 计数；
  - 本入口从未成功 → 在本入口写入占位记录并归入孤立异常，避免静默丢失；
  - **另一个入口下同编号的公告不会被改动**（示例站目录二 `Z-001` 故障不影响
    目录一任何公告）。
- 每次同步后按列表入口分作用域、基于库内现有数据全量重算链状态。

### 旧版 SQLite 自动迁移

旧表在 `notice_no` 上有单列 UNIQUE 约束。`init_db` 检测到旧结构后会把表
重建为 `UNIQUE(list_url, notice_no)`：

- 历史数据原样保留（`list_url` 为空的行补空串），同步记录 `sync_runs` 不动；
- 迁移幂等，重复启动不会重复迁移；
- 迁移后的历史公告可继续通过 API（含 `?list_url=` 过滤）查询。

## 示例公告站内置场景

| 入口 | 编号 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 目录一 | A-001 ~ A-003 | 首次 + 两条更正（中间一条无截止时间） | `ok`，有效截止取最新，中间项继承 |
| 目录一 | B-001 ~ B-003 | 首次 + 更正 + 撤销 | `cancelled`，有效截止为 null |
| 目录一 | C-001 ~ C-003 | 两条更正并行引用同一首次公告 | `conflict`（parallel_correction） |
| 目录一 | D-001 | 引用不存在的 X-999 | 孤立，missing_reference |
| 目录一 | E-001 / E-002 | 互相引用成环 | 孤立，reference_cycle |
| 目录一 | F-001 | 更正公告无引用编号 | 孤立，missing_reference |
| 目录一 | G-001 / G-101（+G-102） | 同项目两个独立首次公告 | 两条独立链 |
| 目录一 | H-001 | 可切换 500 故障 | 验证失败保留上次结果 |
| 目录一 | X-001 / X-002 | 本入口自己的同号公告与更正 | 独立链，截止 2026-12-08 |
| 目录一 | X-009 | 引用只在目录二存在的 X-010 | 孤立，missing_reference，不跨入口借用 |
| 目录二 | X-001 / X-002 | 与目录一同编号、内容不同 | 独立链，截止 2026-12-18，互不覆盖 |
| 目录二 | Y-001 | 目录二独立首次公告 | 正常链 |
| 目录二 | X-010 | 只在本入口存在的被引用公告 | 正常链，不被目录一污染 |
| 目录二 | Z-001 | 可切换 500 故障 | 本入口失败不影响目录一 |

故障开关（仅示例站，可指定目录与编号）：

```bash
# 目录二 Z-001 详情 500
curl "http://localhost:8080/fail-toggle?enabled=1&catalog=/announcements-2&no=Z-001"
# 目录一 H-001 详情 500（默认目标）
curl "http://localhost:8080/fail-toggle?enabled=1"
# 恢复
curl "http://localhost:8080/fail-toggle?enabled=0&catalog=/announcements-2&no=Z-001"
curl "http://localhost:8080/fail-status"
```

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/procurement.db` | SQLite 文件路径 |
| `FETCH_TIMEOUT` | `10` | 单次 HTTP 超时秒数 |
| `FETCH_RETRIES` | `2` | 网络错误/5xx 的重试次数（4xx 不重试） |
| `RETRY_BACKOFF` | `0.5` | 重试退避基数秒 |
| `KEEP_LAST_ON_DETAIL_FAILURE` | `true` | 详情失败时保留本入口上次完整结果 |
| `SAMPLE_PORT`（仅示例站） | `8080` | 示例站监听端口 |
