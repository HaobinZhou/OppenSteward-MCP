# OppenSteward-MCP

让网页端 ChatGPT 只读访问本机 **Oppen Project Steward** 和 **Stepwise R Project** 项目的治理文件。提供项目发现、治理目录浏览、文档搜索与读取；不开放数据、源码、Results、Deliverables 或 Audit。

支持 **Windows、macOS、Linux**。**目前仅在 macOS 完成测试；Windows 和 Linux 尚未验证。** Windows 已包含原生文件句柄及 ACL 兼容实现；并不表示已完成 Windows 测试。可选后台管理脚本 `service.py` 仅用于 macOS；三个平台均可前台运行 MCP 或由 Tunnel 启动 stdio 进程。

Only macOS has been tested. Windows and Linux support is implemented but unverified.

## 接入与运行

Status: frozen

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。下载或克隆此仓库，在项目目录运行 `uv sync --locked`。Windows 使用 PowerShell，macOS/Linux 使用终端；下面的 `uv run python ...` 命令适用于三个平台。

### Secure MCP Tunnel：无需公网入站端口

采用 OpenAI 官方 [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)，由 `tunnel-client` 启动本机 stdio 服务。需要对应工作区的 Tunnel 权限、Tunnel ID 和 runtime key；账号是否具备这些功能以 OpenAI 的控制台为准。安装对应平台的 [官方 tunnel-client](https://github.com/openai/tunnel-client/releases/latest)，将其加入 PATH，或在 `.env` 指定其可执行文件路径。

复制 `.env.example` 为 `.env`，编辑其中的扫描目录、`OPPEN_TUNNEL_ID` 和 `CONTROL_PLANE_API_KEY`。macOS/Linux 可用 `cp .env.example .env`；Windows 可用 `Copy-Item .env.example .env`。不要把真实密钥提交到 Git。

```sh
uv sync --locked
uv run python run.py setup
uv run python run.py tunnel init --dry-run
uv run python run.py tunnel init
uv run python run.py tunnel doctor
uv run python run.py tunnel run
```

在 ChatGPT 开发者模式的应用设置中选择对应 Tunnel 并连接。此方式不需要填写公网域名、不启动本项目的 HTTP 端口，也不使用 `.runtime/owner-access.txt`。`CONTROL_PLANE_API_KEY` 用于官方 Tunnel 运行时认证，与下述 HTTP 登录口令不同。该密钥经子进程环境传递，不写入本项目生成的命令参数或 JSON 配置。Tunnel 子进程继承本机环境。

`init` 使用官方 `sample_mcp_stdio_local` 创建命名 profile；profile 生命周期由官方客户端管理。改动项目位置、Python 环境或 Tunnel ID 后，重新检查/更新 profile，运行 `doctor` 后再启动。`run` 需持续运行，本机休眠或退出后不可用。本项目已测试 stdio 协议及启动器参数；尚未使用真实 OpenAI Tunnel 账号完成端到端接入验证。

OAuth 的浏览器登录服务不会随 stdio 自动转发，因此这里使用 Tunnel 身份与权限体系。公网 OAuth 采用下面的独立模式。[官方 Tunnel 配置说明](https://github.com/openai/tunnel-client/blob/master/docs/configuration.md)

### HTTP + OAuth：已有 HTTPS 反向代理

复制 `.env.http.example` 为 `.env`，将 `OPPEN_PUBLIC_URL` 改为自己的完整 HTTPS origin（不含 `/mcp`），并填写扫描目录：

```sh
uv run python run.py setup
uv run python run.py serve
```

默认转发目标为 **`127.0.0.1:8766`**；ChatGPT 中填写 **`https://你的域名/mcp`**，选择 OAuth。授权页要求本机 `.runtime/owner-access.txt` 中的随机口令并明确同意治理文件只读。setup 重复执行不会轮换已有口令或有效授权；口令不会打印到终端。

frpc、HTTPS 证书与其他反向代理由部署者配置。本项目仅监听 loopback。将域名的全部路径转发到同一端口，包括 `/.well-known/*`、`/authorize`、`/consent`、`/register`、`/token`、`/revoke`、`/mcp` 与 `/files/*`；不要只转发 `/mcp`。`/mcp/` 在内部作为别名处理，OAuth resource 始终是 `/mcp`，不产生降级到 HTTP 的重定向。代理头不会改变 issuer。

macOS 可选后台运行：

```sh
uv run python service.py start
uv run python service.py status
uv run python service.py restart
uv run python service.py stop
# 可选用户 LaunchAgent：登录自动运行
uv run python service.py install
uv run python service.py uninstall
```

后台进程的文件访问权限取决于启动账户及 macOS 隐私授权。Windows/Linux 使用前台命令，或由部署者的进程管理器运行同一命令。不要以管理员/root 身份启动服务来规避目录权限。

## 环境变量配置

Status: frozen

优先级为：显式 CLI 覆盖（例如 `serve --transport`）> 系统环境变量 > `.env` > 兼容的 `config.local.json` > 默认值。默认读取本项目目录中的 `.env`；全局 `--config /path/config.local.json` 可指定 JSON 位置，同时从其旁边读取 `.env`。文件路径相对该配置目录解析，支持 `~`；列表必须是 JSON 字符串数组。`.env` 按 UTF-8 读取，不执行 shell，不展开 `${VAR}`。

| 变量 | 默认值 / 含义 |
| --- | --- |
| `OPPEN_TRANSPORT` | 新安装 `stdio`；旧 JSON 未指定 transport 时保持 `http` |
| `OPPEN_SCAN_ROOTS` | 当前用户主目录；可配置多个本机目录 |
| `OPPEN_EXCLUDE_ROOTS` | `[]`；排除子树 |
| `OPPEN_STATE_DIR` | `.runtime`；HTTP 口令、OAuth 数据库与本机日志 |
| `OPPEN_SKILL_ROOT` | 空；自动查找 `~/.codex/skills`、`~/.agents/skills` 下的两个技能 |
| `OPPEN_PUBLIC_URL` | `http://127.0.0.1:8766`；公网 HTTP 部署必须换为真实 HTTPS origin |
| `OPPEN_HOST` / `OPPEN_PORT` | `127.0.0.1` / `8766`；仅允许 loopback，端口 1024–65535 |
| `OPPEN_SCAN_INTERVAL` | `300` 秒，至少 10 秒 |
| `OPPEN_SCAN_SECONDS` | `90` 秒，每批扫描预算 |
| `OPPEN_MAX_SCAN_DIRS` | `500000`，每批目录上限 |
| `OPPEN_EXTRA_REDIRECT_URIS` | `[]`；额外精确 OAuth 回调，不支持通配符 |
| `OPPEN_TUNNEL_ID` | OpenAI Platform 中的 `tunnel_...` ID |
| `OPPEN_TUNNEL_PROFILE` | `oppen-steward`；官方客户端命名 profile |
| `OPPEN_TUNNEL_CLIENT` | `tunnel-client`；也可填绝对可执行文件路径，Windows 可为 `.exe` |
| `CONTROL_PLANE_API_KEY` | 仅 Tunnel 模式需要；使用 runtime key |

Windows 的 `.env` 路径推荐正斜线，如 `OPPEN_SCAN_ROOTS=["C:/Users/Me/Projects","D:/Research"]`。`OPPEN_SKILL_ROOT` 应指向同时包含两个技能目录的父目录；技能无需随本仓库打包。未安装技能时项目发现仍可用，`get_skill_guide` 会返回明确错误。

`config.example.json` 仅作旧 JSON 配置参考。`configure` 命令保留兼容性，但 `.env`/环境变量仍优先于写入的 JSON；避免在多个来源维护冲突值。HTTP 更换公网 origin 会使旧授权失效，需要重新连接。保留同一 state 目录与 origin 的重启会保留有效 OAuth 授权。

`.env`、`.runtime` 和 `config.local.json` 已忽略。将项目与 `.env` 存放在自己的私有目录；POSIX 的运行目录/凭据使用 0700/0600，Windows 的运行状态使用仅当前账户的受保护 DACL。Windows ACL 设置失败会使 HTTP 初始化失败。不要将 state 目录指向其他用途的共享目录。

## 项目发现与文件访问契约

Status: frozen

默认扫描当前用户主目录；通过 `OPPEN_SCAN_ROOTS` 设置任意多个本机管理目录，通过 `OPPEN_EXCLUDE_ROOTS` 排除目录。扫描采用广度优先队列分批续扫，单批默认 90 秒或 500,000 个目录，未完成自动继续，完整一轮后每 300 秒开始下一轮。跳过系统、依赖、缓存和运行状态目录，远端只返回扫描计数及状态。目录权限不足或尚未扫完时报告 `partial`，不能据此宣称整机发现完成。远端不能改变扫描根。

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

按项目 ID 和相对路径读取，禁止绝对路径、`..`、NUL、反斜线和符号链接。macOS/Linux 使用逐组件 `openat` / `O_NOFOLLOW`；Windows 使用原生文件句柄，访问期间保留祖先句柄并拒绝写入/删除共享、reparse point（含目录联接）和硬链接。两条分支都检查根目录身份并拒绝特殊文件。Windows 当前限定普通本机磁盘目录，不支持 UNC、设备命名空间或云占位文件；同步软件或编辑器占用导致检查失败时拒绝访问。Windows 分支尚未实机验证。文件内容始终作为不可信内容返回。

允许的治理文档每次最多读取 256 KiB，默认 64 KiB，offset/length 均为字节；`next_offset` 非空时继续。UTF-8 模式会替换跨块截断字符；`base64` 用于精确重建允许文档的字节，不会绕过白名单。返回大小、mtime 和块 SHA-256；跨块文件变化时应从头重读。

`search` 在白名单文档的名称和至多 256 KiB 的 UTF-8 正文中做字面匹配，默认最多 30 条结果，每次最多检查 10,000 个文件/10 秒；超限返回 `truncated`。`fetch` 读取搜索结果并提供引用与分页信息。HTTP 模式返回受认证保护的引用 URL；stdio 模式返回 `oppen-steward://` 标识符，用 `fetch`/`read_file` 继续读取，它不是浏览器下载链接。`/files/*` 要求 Bearer token，且再次验证治理白名单，不生成公开下载链接。

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

本节适用于 HTTP 模式。stdio 模式没有应用层 OAuth 或登录页，工具声明 `noauth`；本机访问由启动进程的操作系统账户控制，连接 ChatGPT 时还依赖官方 Tunnel 的工作区权限与 runtime key。不要使用无认证的通用网络桥接器转发 stdio。

HTTP 模式基于官方 MCP SDK 的授权码流程，使用 S256 PKCE、精确回调匹配、固定 issuer、`resource` 绑定、scope 检查和 Bearer 验证。只支持 `governance:read`。每个工具都显式声明 OAuth2 和该 scope，并在 `_meta.securitySchemes` 中提供兼容声明；401/403 认证 challenge 同时给出所需 scope。提供 OAuth Authorization Server Metadata 与 Protected Resource Metadata，所有授权成功/回调错误响应包含 RFC 9207 `iss`。

授权页通过 10 分钟有效的浏览器 Cookie、CSRF token、Origin 校验和本人访问口令保护；页面不允许嵌入 iframe。授权码有效 120 秒、只能兑换一次。访问令牌有效 1 小时，刷新令牌有效 30 天；刷新时轮换令牌，重复使用旧刷新令牌会撤销该授权链。SQLite 事务负责一次性消费与新令牌写入。Bearer/刷新令牌只以 SHA-256 索引保存，服务重启后保持有效；issuer 或服务权限范围改变会撤销旧客户端、授权码和令牌。从历史广泛访问范围升级到 `governance:read` 会撤销旧授权，本机登录口令保持不变。

页面采用 `Referrer-Policy: same-origin`：同源表单保留浏览器生成的 Origin，跨站导航不传 Referer。不能改为 `no-referrer`，该策略会使原生表单 POST 的 Origin 变成 `null`，导致合法授权被拒绝。服务仍拒绝 `null` 和非本服务来源，保留 Cookie、CSRF 和口令校验。该行为依据 [Fetch 标准的 Origin 请求头规则](https://fetch.spec.whatwg.org/#append-a-request-origin-header)。

授权页的 CSP `form-action` 仅允许同源提交及本次已验证的精确 OAuth 回调路径，覆盖浏览器对表单 303 回调的检查；错误口令重试页沿用同一规则。中间件不重复添加另一条会拦截合法回调的 CSP，其他页面采用默认同源策略，所有页面保持禁止 iframe 嵌入。

动态注册只接受 ChatGPT 回调或管理员在本机配置的精确回调。DCR 注册不授予文件权限，必须经过本人登录及同意。支持 public client、client_secret_post、client_secret_basic。单进程最多 1,000 个注册客户端；认证端点每分钟最多 60 次，授权页 POST 每分钟最多 10 次；请求体上限 1 MiB。适合单所有者使用，不提供多租户隔离。

```sh
# 撤销所有客户端和令牌，ChatGPT 需要重新连接
uv run python run.py revoke-all
# 更换本机登录口令，同时撤销所有 OAuth 授权（服务读取最新哈希）
uv run python run.py rotate-password
```

## 验证与贡献

```sh
uv sync --locked --dev
uv run ruff check .
uv run pytest -q
```

测试覆盖配置优先级、UTF-8 路径、两种传输的权限声明、真实 stdio 子进程、治理白名单、路径越界、索引失效、OAuth 重放/刷新/撤销和授权后的 MCP 调用。Windows 专用测试包括目录联接拒绝、祖先句柄锁定和 ACL；在 macOS 上会明确跳过。可选真实浏览器回归的安装与运行方法见 [CONTRIBUTING.md](CONTRIBUTING.md)。

**验证状态：仅 macOS 已测试；Windows 和 Linux 未验证。** 仓库包含三平台 CI 配置，但未将尚未运行的 GitHub Actions 视为测试通过。

本项目使用 Oppen Project Steward 管理。[治理 registry](.oppen-project-steward/registry.md) 注册 README 的当前契约章节；实现与测试分别拥有代码和可执行验证。当前机器证据位于 `.oppen-project-steward/Audit/Runs/verification/current/`，高风险契约审计位于 `.oppen-project-steward/Audit/Contracts/`。Audit 不通过 MCP 暴露。

贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全反馈见 [SECURITY.md](SECURITY.md)。本仓库代码采用 [MIT License](LICENSE)。两个外部技能、MCP SDK 与官方 Tunnel 客户端各自适用其许可证；本仓库不重新分发它们的源码或二进制。内部 Python 模块名 `oppenproject` 保留兼容性，公开项目名为 OppenSteward-MCP。
