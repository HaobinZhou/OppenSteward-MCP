# OppenProject

在本机运行的只读 MCP 服务，让网页端 ChatGPT 访问由 **Oppen Project Steward** 和 **Stepwise R Project** 管理的项目的**治理文件**。只提供项目发现、治理目录浏览、治理文档搜索与读取，以及两个技能的说明。

## 接入与运行

Status: frozen

- 工作目录：`/Users/oppen/Desktop/mac_project/OppenProject`
- frpc 本机目标：**`127.0.0.1:8766`**。
- ChatGPT MCP 地址：**`https://project.oppenchow.online/mcp`**。`/mcp/` 作为同一受保护端点的别名在服务内处理，不产生可能退回 HTTP 的重定向；OAuth resource 仍固定为 `/mcp`。
- 此域名的所有路径均转发到同一端口，包括 `/.well-known/*`、`/authorize`、`/consent`、`/register`、`/token`、`/revoke`、`/mcp`。不要只转发 `/mcp`。
- HTTPS 终止、证书和 frpc 配置由现有基础设施负责。服务只绑定 loopback，不读取或修改 frpc 配置，不信任转发头来决定 OAuth issuer。

本机已生成 `config.local.json` 和 `.runtime/owner-access.txt`；后者是登录授权页需要的随机访问口令，只有当前本机账户可读，不通过 MCP 提供，也不写入 Git。OAuth 数据库只保存口令的 scrypt 哈希；原始口令单独保留在这个本机文件，便于本人登录。

```sh
cd /Users/oppen/Desktop/mac_project/OppenProject
# 前台运行
.venv/bin/python run.py serve
# 在当前可访问项目的环境中后台运行，退出终端后继续运行
.venv/bin/python service.py start
.venv/bin/python service.py status
.venv/bin/python service.py restart
.venv/bin/python service.py stop
# 可选：安装用户 LaunchAgent，之后登录自动运行、异常退出自动重启（写入 ~/Library/LaunchAgents）
.venv/bin/python service.py install
.venv/bin/python service.py uninstall
```

日志在 `.runtime/server.log`。默认 `start` 使用脱离终端的独立进程，PID 保存在 `.runtime/process.json`；它不会安装登录项。使用可选 `install` 前先 `stop`，并确保 macOS 允许后台 Python 访问桌面和外置磁盘；默认后台方式沿用调用应用已有的访问权限。已安装 LaunchAgent 的状态/重启使用 `launchctl print gui/$(id -u)/com.oppen.project-mcp` 和 `launchctl kickstart -k gui/$(id -u)/com.oppen.project-mcp`，`uninstall` 可移除。

禁用原始 HTTP access log，避免将 OAuth 查询参数或令牌写入日志。诊断日志只记录固定接口类别、HTTP 方法和状态码、耗时、是否携带 Authorization、Origin 是否同源以及是否使用 `/mcp/` 别名；不记录口令、令牌值、查询参数、请求/响应正文、客户端 IP 或项目文件路径。Mac 必须保持开机和联网；休眠时服务不可用。

重新安装环境使用 Python 3.12+ 与 `uv sync --locked`；精确版本和哈希由 `uv.lock` 固定。使用仍接收修复的官方 MCP Python SDK 1.x 分支，限制 `<2` 避免不兼容的大版本升级。修改配置后重启服务。

在 ChatGPT 的自定义 MCP/应用创建界面填入上述 MCP URL，认证选 OAuth，注册方式选动态客户端注册（DCR）。服务提供 DCR，无需预先填写 client ID/secret。在打开的 OppenProject 授权页输入本机访问口令，确认 `governance:read`。这是单人本机服务，只提供一个所有者账户；界面里的客户端名称不代表服务验证了该应用的品牌身份。

如果界面使用回调专属 URL，服务支持 `https://chatgpt.com/connector/oauth/{callback_id}`；也支持带 issuer 校验的固定回调 `https://chatgpt.com/connector_platform_oauth_redirect`。其他客户端必须由本机管理员将精确的 HTTPS 回调加入 `extra_redirect_uris`，远端工具不能修改这项配置。当前未提供 CIMD，创建连接时选择 DCR。

检查 `GET /healthz` 返回 `{"status":"ok","service":"OppenProject"}`。未登录访问 `/mcp` 应返回 401 和 OAuth metadata challenge，而不是文件信息。修改工具或认证元数据后，需要在 ChatGPT 的连接设置中 Refresh，再开始新会话测试（[官方接入说明](https://developers.openai.com/plugins/deploy/connect-chatgpt)）。公网状态可以无凭据检查；完整自动授权测试只在临时回环服务中使用测试口令进行，ChatGPT 账户内的实际授权由用户操作。

## 项目发现与文件访问契约

Status: frozen

默认从 `/Users`、`/Volumes`、`/opt`、`/usr/local` 扫描，覆盖用户目录、外置磁盘和常用自定义安装位置。使用广度优先队列分批续扫，单批最多 90 秒或 500,000 个目录，未完成的批次自动继续；完整一轮后每 300 秒开始下一轮。远端扫描状态只返回时间、计数及是否完成，不返回失败目录的路径。权限错误或尚未扫描完时报告 `partial`，不能据此宣称全盘项目已发现。扫描根和排除目录只由本机 `config.local.json` 配置，MCP 客户端不能扩大范围。

识别规则为管理文件中的独立版本标记行：

| 管理类型 | 识别文件 | 版本 |
| --- | --- | --- |
| Oppen Project Steward | `.oppen-project-steward/registry.md` | v3 |
| 旧版 Steward 布局 | `project.md` | legacy-layout |
| Stepwise R Project | `project.md` | v2、v3 |

这是入口识别，不代表通过 helper 完整验证。服务不运行 adopt、index、migration、R 分析或 shell 命令，不修改被访问项目。项目 ID 由真实绝对目录的 SHA-256 前 20 位确定；每次访问重新检查管理标记与根目录身份。发现时仅在本机遍历目录并读取管理标记，不读取普通数据文件正文。

**对外采用固定治理白名单，默认拒绝其他文件。** 授权涵盖配置范围内所有当前及以后发现的受管理项目，但每个项目只开放下表内容：

| 布局 | 可读文件 |
| --- | --- |
| 当前 Steward | `.oppen-project-steward/registry.md`；同一命名空间中 `Memory/index.md`、`Attention/index.md` 及索引登记的 `entries/M-XXXX.md`、`entries/A-XXXX.md` |
| 旧布局 Steward | `project.md`；根目录 `Memory`、`Attention` 中上述固定索引及已登记条目 |
| R v3 | `project.md`；根目录 `Memory`、`Attention` 中上述固定索引及已登记条目 |
| R v2 | 仅 `project.md`；旧版非结构化 Memory 默认关闭 |

Memory/Attention 索引必须符合对应技能的标记或标题，条目 ID 从索引读取；不存在、未登记或索引不合法时拒绝访问。索引里任意链接、Canonical/Result 注册表和文档正文中的引用**不会**授权目标路径。未登记条目和放在治理目录中的其他文件同样不开放。

**数据、源码、Results、Deliverables、全部 Audit（含 Runs、Contracts、Functions）、README、其他 Canonical 正文、运行状态和凭据均不开放。** 不因文件为 Markdown 或位于受管理项目中就放行。目录浏览只展示白名单文件及其必要父目录；搜索只读取白名单内容；`read_file`、伪造的 `fetch` ID 和 `/files/*` 下载统一受同一检查约束。没有远程扩大白名单的工具或配置项。

这是文件范围限制，不是正文脱敏：治理文件自身的正文、标题及其中记载的数据路径等元信息会发送给 ChatGPT；如果在这些治理文件正文里写入数据内容，它也属于返回正文。服务不会跟随其中的链接、图片引用或执行代码，也不会另行上传项目数据。`get_skill_guide` 只读取两个固定技能的 SKILL.md。

按项目 ID 和相对路径读取，禁止绝对路径、`..`、NUL、反斜线和符号链接。使用逐组件 `openat` / `O_NOFOLLOW`，检查根目录 inode/device，拒绝目录替换、硬链接、FIFO、设备和 socket。文件内容始终作为不可信内容返回。

允许的治理文档每次最多读取 256 KiB，默认 64 KiB，offset/length 均为字节；`next_offset` 非空时继续。UTF-8 模式会替换跨块截断字符；`base64` 用于精确重建允许文档的字节，不会绕过白名单。返回大小、mtime 和块 SHA-256；跨块文件变化时应从头重读。

`search` 在白名单文档的名称和至多 256 KiB 的 UTF-8 正文中做字面匹配，默认最多 30 条结果，每次最多检查 10,000 个文件/10 秒；超限返回 `truncated`。`fetch` 读取搜索结果并提供受认证保护的引用 URL 和分页信息。`/files/*` 要求 Bearer token，且再次验证治理白名单，不生成公开下载链接。

| MCP 工具 | 功能 |
| --- | --- |
| `list_projects` | 按名称、路径或技能筛选项目，返回项目根与治理入口、扫描计数 |
| `refresh_projects` | 开始/继续本机发现，仅返回扫描计数与状态 |
| `project_overview` | 读取治理 registry，定位可用的 Attention/Memory 索引 |
| `list_files` | 分页浏览治理白名单中的目录和文件 |
| `read_file` | 分块读取允许的治理文档 |
| `search` / `fetch` | 跨项目检索及读取允许的治理文档 |
| `get_skill_guide` | 读取两个固定的本机 SKILL.md |

## OAuth 与授权契约

Status: frozen

基于官方 MCP SDK 的授权码流程，使用 S256 PKCE、精确回调匹配、固定 issuer、`resource` 绑定、scope 检查和 Bearer 验证。只支持 `governance:read`。每个工具都显式声明 OAuth2 和该 scope，并在 `_meta.securitySchemes` 中提供兼容声明；401/403 认证 challenge 同时给出所需 scope。提供 OAuth Authorization Server Metadata 与 Protected Resource Metadata，所有授权成功/回调错误响应包含 RFC 9207 `iss`。

授权页通过 10 分钟有效的浏览器 Cookie、CSRF token、Origin 校验和本人访问口令保护；页面不允许嵌入 iframe。授权码有效 120 秒、只能兑换一次。访问令牌有效 1 小时，刷新令牌有效 30 天；刷新时轮换令牌，重复使用旧刷新令牌会撤销该授权链。SQLite 事务负责一次性消费与新令牌写入。Bearer/刷新令牌只以 SHA-256 索引保存，服务重启后保持有效；issuer 或服务权限范围改变会撤销旧客户端、授权码和令牌。此次从广泛项目访问升级到 `governance:read` 会自动撤销旧授权，需在 ChatGPT 重新连接并同意“治理文件只读”；本机登录口令保持不变。

页面采用 `Referrer-Policy: same-origin`：同源表单保留浏览器生成的 Origin，跨站导航不传 Referer。不能改为 `no-referrer`，该策略会使原生表单 POST 的 Origin 变成 `null`，导致合法授权被拒绝。服务仍拒绝 `null` 和非本服务来源，保留 Cookie、CSRF 和口令校验。该行为依据 [Fetch 标准的 Origin 请求头规则](https://fetch.spec.whatwg.org/#append-a-request-origin-header)。

授权页的 CSP `form-action` 仅允许同源提交及本次已验证的精确 OAuth 回调路径，覆盖浏览器对表单 303 回调的检查；错误口令重试页沿用同一规则。中间件不重复添加另一条会拦截合法回调的 CSP，其他页面采用默认同源策略，所有页面保持禁止 iframe 嵌入。

动态注册只接受 ChatGPT 回调或管理员在本机配置的精确回调。DCR 注册不授予文件权限，必须经过本人登录及同意。支持 public client、client_secret_post、client_secret_basic。单进程最多 1,000 个注册客户端；认证端点每分钟最多 60 次，授权页 POST 每分钟最多 10 次；请求体上限 1 MiB。适合单所有者使用，不提供多租户隔离。

```sh
# 撤销所有客户端和令牌，ChatGPT 需要重新连接
.venv/bin/python run.py revoke-all
# 更换本机登录口令，同时撤销所有 OAuth 授权（服务读取最新哈希）
.venv/bin/python run.py rotate-password
```

## 验证

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
# 真实浏览器授权回归：只用临时本机环境及测试口令，跨源回调由另一回环端口接收
OPPENPROJECT_PLAYWRIGHT=/absolute/path/to/node_modules/playwright .venv/bin/pytest tests/test_browser.py -q
# 公网只用无效测试值，验证浏览器 Origin 与口令拒绝流程
node tests/browser_oauth.cjs --playwright=/absolute/path/to/node_modules/playwright --invalid-password
```

测试涵盖四种管理布局的治理白名单、非治理文件的目录/搜索/读取/下载拒绝、伪造 fetch ID、索引失效、旧广泛授权撤销、扫描续接、路径边界、治理文本分块、错误密码与 CSRF、PKCE/回调/resource 校验、授权码并发重放、刷新轮换与撤销、令牌持久化、HTTP Host/Origin 以及认证后的 MCP initialize/tools/search/fetch。

本项目由 `.oppen-project-steward/registry.md` 管理。README 的注册章节是当前契约的唯一文字来源；源码与测试分别拥有实现和可执行验证。测试证据在 `.oppen-project-steward/Audit/Runs/verification/current/`，安全边界审计在 `.oppen-project-steward/Audit/Contracts/`。

协议依据：[OpenAI MCP 认证文档](https://developers.openai.com/plugins/build/auth)、[官方 MCP Python SDK](https://pypi.org/project/mcp/)。
