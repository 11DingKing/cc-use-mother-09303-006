# 职业教育资源授权后端

面向职业教育课程包跨境/跨机构交付场景的授权核验与组包后端。围绕
`domain/contract.json` 的角色（资源提供院校、接收院校、版权管理员）、状态
（登记、授权、组包、交付、撤回）与不变量（地域许可、依赖图谱、授权快照、
半成品清理）实现，纯 Python 3.11 标准库，SQLite 持久化，无第三方依赖。

## 业务能力

- **资源摘要与版本依赖**：登记资源摘要（标题、摘要值、类型、元数据）与必选/可选依赖。
- **授权条款记录**：权利主体、地域范围、机构范围（白名单/黑名单/全部）、
  接收方资格条件、授权依据、起止期限。
- **组包前逐项核验**：对清单及其必选依赖闭包逐项执行
  `LICENSED / TIME_WINDOW / TERRITORY / ORG_SCOPE / RECIPIENT_QUALIFICATION / DEPENDENCY`
  核验；任一项不过即整体拒绝（如“仅本国校内使用”的实训手册不得整包发往海外）。
- **授权快照固定**：核验通过时把每项资源的授权版本、条款、逐项核验结果与
  SHA-256 摘要固化进不可变包清单；此后许可再变化也不改写历史快照。
- **谱系留存**：许可授予、部分授权、撤回、替代材料、组包成功、组包拒绝
  全部进入不可变历史与时间线，版本以 `supersedes` 串链。
- **重复组包幂等**：同一机构下相同 `idempotency_key` 的成功组包返回同一冻结包；
  失败不占用幂等键，允许修正清单后重试。
- **失败无半成品**：核验失败只落 FAILED 记录（可审计、不可下载）；制品以
  临时文件写入并 `fsync`，通过原子改名才可见，异常时回滚事务并清理临时文件。
- **影响分析**：受权人员可反查某项权利的各个版本被哪些已交付包固定，
  并标注包内版本是否为当前 head。
- **跨机构隔离**：资源、授权、包、谱系、影响分析、下载均按归属机构过滤；
  无权对象一律返回 404，不泄露存在性。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/licensing/`：后端实现
  - `store.py`：SQLite schema 与数据访问（不可变授权历史、包冻结、谱系索引）。
  - `policy.py`：逐项核验引擎与依赖闭包扩展。
  - `service.py`：用例编排、角色与机构边界、快照固定、原子组包、谱系与影响分析。
  - `canon.py`：规范化 JSON 与 SHA-256 摘要。
  - `api.py` / `__main__.py`：HTTP/JSON 接口与启动入口。
- `tools/check_contract.py`：契约命令行检查。
- `tests/`：契约回归、领域服务端到端（16 个场景）、HTTP 接口端到端。

## 运行

```bash
# 1) 准备令牌映射（生产鉴权用 Bearer Token）
cat > tokens.json <<'JSON'
{
  "tkn-prov-1": {"subject_id":"u-prov","org_id":"ORG_CN_VOC","roles":["PROVIDER"],"display_name":"中方教务"},
  "tkn-admin-1": {"subject_id":"u-admin","org_id":"ORG_CN_VOC","roles":["COPYRIGHT_ADMIN"],"display_name":"版权管理员"}
}
JSON

# 2) 启动
LICENSE_TOKENS_FILE=tokens.json \
LICENSE_DB=data/licensing.sqlite3 \
LICENSE_ARTIFACT_DIR=data/artifacts \
python3 -m licensing --host 0.0.0.0 --port 8080
```

本地开发可加 `--dev-auth`，用 `X-Subject` / `X-Org` / `X-Roles` 请求头鉴权
（不得用于生产）。`GET /health` 无需鉴权。

## HTTP 接口

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/resources` | PROVIDER | 登记资源摘要与依赖 |
| GET | `/resources` / `/resources/{id}` | 本机构 | 资源列表/详情（含当前 head 授权） |
| POST | `/resources/{id}/licenses` | COPYRIGHT_ADMIN | 授予许可；`"partial": true` 为部分授权 |
| POST | `/resources/{id}/revocation` | COPYRIGHT_ADMIN | 撤回当前许可 |
| POST | `/replacements` | COPYRIGHT_ADMIN | 登记替代材料（可 `revoke_old` 同步撤回旧许可） |
| GET | `/resources/{id}/license-history` | 本机构 | 授权版本链 |
| GET | `/resources/{id}/lineage` | 本机构 | 谱系时间线 |
| GET | `/resources/{id}/impact` | 本机构 | 权利变化影响的已交付包 |
| POST | `/packages` | PROVIDER | 逐项核验并组包，支持 `idempotency_key` |
| GET | `/packages` / `/packages/{id}` | 本机构 | 包列表/详情 |
| GET | `/packages/{id}/download` | 本机构 | 下载冻结快照清单（失败构建 404） |
| GET | `/builds/failed` | 本机构 | 失败组包记录（仅审计信息，无制品） |

组包请求示例：

```json
{
  "name": "中法合作课程包",
  "resource_ids": ["r-video-v1"],
  "idempotency_key": "cn-fr-2026sp-01",
  "recipient": {
    "org_id": "ORG_FRN_PARTNER",
    "org_name": "法国伙伴校",
    "country": "FR",
    "attrs": {"accredited": true, "level": "vocational"}
  }
}
```

核验失败返回 `422 BUILD_REJECTED`，`details` 给出逐资源、逐检查项原因；
成功返回 201，包详情内含每个条目的固定快照与整包 `snapshot_digest`。

## 数据与安全约定

- SQLite 开启外键约束；所有写请求在服务层以全局锁串行化，配合事务保证一致性。
- 授权历史（`license_history`）与谱系事件（`lineage_events`）只追加、不更新不删除；
  撤回/替代只改变 head 指向与历史状态。
- 制品文件只以 `{package_id}.json` 完整形态出现于制品目录；不存在可下载的半成品。
- 所有机构边界在服务层强制，HTTP 层不提供跨机构查询参数。

## 验证

```bash
python3 -m unittest discover -s tests -v          # 18 个测试全部通过
python3 -m compileall -q src tools tests          # 编译检查
python3 tools/check_contract.py domain/contract.json
```
