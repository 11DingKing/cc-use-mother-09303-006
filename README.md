# 职业教育资源授权后端

面向职业教育课程包跨境交付的授权核验后端：记录资源摘要、权利主体、地域与
机构范围、期限、版本依赖与接收方资格；组包前逐项核验并**固定授权快照**；
许可撤回、替代材料、部分授权、重复组包全部保留谱系；失败生成绝不留下可下载
半成品；受权人员可追溯某项权利变化影响了哪些包，且无法读取其他机构无权接触
的内容。

## 架构

零第三方依赖，仅使用 Python 3.11+ 标准库（SQLite + http.server）。

| 层 | 模块 | 职责 |
|---|---|---|
| 存储 | `src/licensing_service/store.py` | SQLite 表结构、事务、连接管理 |
| 核验 | `src/licensing_service/access.py` | 地域/机构/期限/版本/部分授权/资格/依赖图谱的纯函数规则 |
| 服务 | `src/licensing_service/services.py` | 登记、授权、撤回、替代、组包快照、交付、影响分析 |
| 接口 | `src/licensing_service/app.py` | HTTP JSON API（`X-User-Id` 头鉴权） |
| 摘要 | `src/licensing_service/canonical.py` | 规范化 JSON + SHA-256 快照哈希 |
| 入口 | `src/licensing_service/server.py` | 服务启动 |

## 数据与规则一览

- **资源摘要**：标题、类型、摘要、版本内容哈希（SHA-256）、版本依赖
  （`depends_on` 构成依赖图谱）。
- **权利主体与授权**：`licenses` 记录许可方机构、`full/partial` 范围、
  地域白名单（ISO 国家码，`"*"` 通配）、机构白名单、生效/终止期限、
  锁定的资源版本；partial 授权通过 `license_grants` 逐要素登记许可/禁止项。
- **接收方资格**：机构、所在地域、资质清单、有效期、合格/暂停状态。
- **固定授权快照**：核验通过后把每项的版本、内容哈希、授权要素、接收方资格
  规范化哈希（`snapshot_digest`），快照随包只增不改；另存剔除时间戳的
  `basis_digest`，交付时重算比对，检出权利漂移。
- **谱系**：`package_lineage`（built/rebuild/material_replaced/license_revoked/
  partial_grant/delivery）+ 领域事件 `events`，版本之间以
  `supersedes_package_id`、授权以 `supersedes_id` 串接，不做物理删除。
- **失败清理**：产物先写 `*.tmp.*`，**数据库事务提交成功后**才
  `os.replace` 原子落位；失败尝试仅写 `package_attempts`（成功/失败都可审计），
  `artifact_path` 恒为 NULL。

## 启动

```bash
PYTHONPATH=src python3 -m licensing_service.server --db data/license.db --port 8080
```

## 验证

```bash
# 单元 + 服务端到端 + HTTP 端到端（19 个测试）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 题目主场景演示（越权发现 → 阻断 → 替代 → 交付 → 撤回 → 影响分析）
python3 tools/demo_scenario.py

# 原契约检查
python3 tools/check_contract.py domain/contract.json
```

## HTTP 接口

所有业务接口需请求头 `X-User-Id: <受权人员ID>`（引导接口
`POST /institutions`、`POST /users` 除外）。错误统一为
`{"error","message","details?"}`，跨机构对象一律返回 404（不可探测）。

| 方法 | 路径 | 说明 |
|---|---|---|
 POST | `/institutions` | 登记机构（权利主体/组包方/接收方） |
 POST | `/users` | 登记受权人员（provider/copyright/recipient/admin） |
 POST | `/resources` | 资源登记（`content_base64`、`depends_on`） |
 POST | `/resources/{id}/versions` | 新版本（旧版本保留，授权锁定版本需重新授权） |
 GET | `/resources`、`/resources/{id}` | 资源摘要（按机构可见性过滤） |
 POST | `/licenses` | 授予授权 full/partial（地域、机构、期限、逐要素清单） |
 POST | `/licenses/{id}/revoke` | 许可撤回（追加谱系，不删记录） |
 GET | `/licenses/{id}`、`/licenses/{id}/impact` | 授权详情 / 权利变化影响的包 |
 POST | `/recipients`、`POST /recipients/{id}/suspend` | 接收方资格登记 / 暂停 |
 POST | `/packages` | 组包：逐项核验 + 固定快照；失败 422 且无产物 |
 GET | `/packages`、`/packages/{id}` | 包列表/详情（快照、条目、谱系、交付记录） |
 GET | `/packages/{id}/lineage` | 完整谱系 |
 POST | `/packages/{id}/replace` | 替代材料后重组新版本（谱系挂接） |
 POST | `/packages/{id}/deliver` | 交付前再核验；权利漂移返回 409 |
 GET | `/packages/{id}/artifact` | 下载产物（仅 verified/delivered 包） |
 GET | `/attempts?name=` | 组包尝试审计（失败记录无产物路径） |

## 访问控制矩阵

| 操作 | provider（本机构） | copyright（本机构） | recipient |
|---|---|---|---|---|
| 资源登记/新版本 | ✓ | ✗ | ✗ |
| 授权/撤回 | ✗ | ✓ | ✗ |
| 组包/替代/交付 | ✓ | ✗ | ✗ |
| 读资源/包 | 本机构 + 已授权给本机构 | 同 provider | 仅已交付给本机构的包 |
| 影响分析 | 可见授权即可查 | 可见授权即可查 | 结果仅含本机构已收包 |

## 主场景示例

```bash
# 1) 整包组包：本国校内手册随包发往 SG，被逐项核验拦截（422，无产物）
curl -s -X POST localhost:8080/packages -H 'X-User-Id: t1' \
  -H 'Content-Type: application/json' \
  -d '{"name":"中新合作课程包","resource_ids":["<手册>","<课件>"],"recipient_id":"<接收方>"}'

# 2) 换入已获 SG 授权的国际版手册后组包成功，快照固定；随后交付
curl -s -X POST localhost:8080/packages/<id>/deliver -H 'X-User-Id: t1'

# 3) 版权方撤回许可后，再交付返回 delivery_blocked（license_status + snapshot_drift）
# 4) GET /licenses/<id>/impact 查看受影响的包及其影响类型
```

## 目录

- `domain/contract.json`：领域角色、状态、不变式与样例（契约层，未改动）。
- `src/domain_contract/`：契约读取与校验。
- `src/licensing_service/`：授权后端实现。
- `tools/check_contract.py`：契约摘要检查；`tools/demo_scenario.py`：主场景演示。
- `tests/`：契约回归、服务端到端、HTTP 端到端测试。
