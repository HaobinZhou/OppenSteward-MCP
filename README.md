# OppenSteward-MCP

在 ChatGPT 网页版里查看你电脑上的项目说明、决策记录和待处理事项，也可以把讨论保存回项目。

OppenSteward-MCP 会找到由 **Oppen Project Steward** 或 **Stepwise R Project** 管理的项目，让 GPT 帮你了解项目进展、查找过去的决定、汇总需要关注的问题。比如，你可以直接问：

- “我有哪些项目还有待处理事项？”
- “这个项目当时为什么选择了这套方案？”
- “帮我找一下几个项目里关于部署方式的决策记录。”
- “把刚才讨论的方案保存到这个项目，下次接着聊。”

默认只开放项目治理文档。开启讨论功能后，GPT 还可以读取、新建和编辑 `Discussion` 中的讨论文件。数据文件、源码文件和分析结果仍不开放，保存讨论也不会运行代码或执行里面的建议。

支持 Windows、macOS 和 Linux。目前只在 macOS 上实际接入过 ChatGPT，Windows 和 Linux 的接入流程还没实测。

## 接入与运行

先安装 Python 3.12 或更新版本，以及 [uv](https://docs.astral.sh/uv/getting-started/installation/)。下载或克隆本仓库，在项目文件夹里打开终端，运行：

```sh
uv sync --locked
```

Windows 可以使用 PowerShell。下面的 `uv run python ...` 命令在三个平台上通用。

接下来选择一种连接方式：没有公网域名，使用 **Secure MCP Tunnel**；已经有域名和 HTTPS 转发，使用 **HTTP + OAuth**。

### 没有公网域名：使用 Secure MCP Tunnel

这种方式通过 OpenAI 官方 Tunnel 连接你的电脑，不需要配置公网入站端口。你需要先在 OpenAI Platform 中取得 **Tunnel ID** 和 **运行密钥（runtime key）**，并确保工作区已开通相应权限。具体准备步骤见 [OpenAI 的 Tunnel 指南](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)。

1. 下载与你的系统对应的 [tunnel-client](https://github.com/openai/tunnel-client/releases/latest)，将它加入 PATH。如果不想配置 PATH，也可以把程序的完整路径填入 `.env` 的 `OPPEN_TUNNEL_CLIENT`。
2. 将 `.env.example` 复制一份，命名为 `.env`。
3. 打开 `.env`，填入 `OPPEN_TUNNEL_ID` 和 `CONTROL_PLANE_API_KEY`，再把 `OPPEN_SCAN_ROOTS` 改成你的项目所在文件夹。路径写法见下面的“环境变量配置”。
4. 依次运行：

```sh
uv run python run.py setup
uv run python run.py tunnel init
uv run python run.py tunnel doctor
uv run python run.py tunnel run
```

`init` 保存连接配置，`doctor` 检查配置，`run` 启动连接。之后在 ChatGPT 开发者模式的应用设置中，选择对应的 Tunnel 并连接。

保持最后一个命令运行；关闭它或让电脑休眠后，GPT 就无法继续访问。以后如果移动了项目文件夹、更换了 Python 环境或 Tunnel ID，需要相应更新 Tunnel 的连接配置。

这种方式使用 Tunnel 密钥认证，不需要下面 HTTP 模式的登录口令。本机 MCP 通信已做测试，通过真实 ChatGPT 账号连接 Tunnel 还未实测。

### 已有域名和 HTTPS 转发：使用 HTTP + OAuth

将 `.env.http.example` 复制为 `.env`，填入自己的域名和项目路径。例如：

```dotenv
OPPEN_PUBLIC_URL=https://projects.example.com
OPPEN_SCAN_ROOTS=["~/Projects"]
```

域名这一项不需要加 `/mcp`。然后运行：

```sh
uv run python run.py setup
uv run python run.py serve
```

把你的 HTTPS 域名转发到 **`127.0.0.1:8766`**，在 ChatGPT 中填写 **`https://你的域名/mcp`**，认证方式选择 **OAuth**。

连接时会打开授权页。登录口令保存在本机的 **`.runtime/owner-access.txt`** 中，打开这个文件即可查看。输入口令并同意授权后，GPT 就能读取允许分享的治理文档。

如果使用 frpc 或其他反向代理，请把**整个域名的请求**转发到这个端口。除了 `/mcp`，登录和授权还要使用其他路径，只转发 `/mcp` 会导致连接失败。HTTPS 证书和转发规则需要你自己配置。

### 在 macOS 后台运行

HTTP 模式可以用下面的命令在后台运行：

```sh
uv run python service.py start    # 启动
uv run python service.py status   # 查看状态
uv run python service.py restart  # 重启，修改配置后使用
uv run python service.py stop     # 停止
```

希望登录电脑后自动启动，可以运行 `uv run python service.py install`；取消自动启动用 `uv run python service.py uninstall`。

这个后台管理脚本只适用于 macOS。Windows 和 Linux 可以保持 `run.py serve` 在终端运行，或交给自己常用的进程管理工具。Tunnel 模式则保持 `run.py tunnel run` 运行即可。

<details>
<summary>部署细节与排查</summary>

HTTP 服务只监听本机回环地址。需要转发的路径包括 `/.well-known/*`、`/authorize`、`/consent`、`/register`、`/token`、`/revoke`、`/mcp` 和 `/files/*`。`/mcp/` 也可以使用，服务会在内部处理，不会重定向到 HTTP；OAuth resource 固定为 `/mcp`，issuer 来自配置的域名，不受代理头影响。

Tunnel 使用本机 stdio 通信，不启动本项目的 HTTP 服务，也不转发 OAuth 登录页。启动器调用官方 `tunnel-client` 的 `sample_mcp_stdio_local` 配置；密钥通过环境变量传递，不写入命令参数或本项目的 JSON 配置。可用 `uv run python run.py tunnel init --dry-run` 查看将要执行的命令。更多选项见 [官方 Tunnel 配置说明](https://github.com/openai/tunnel-client/blob/master/docs/configuration.md)。

如果服务找不到项目，先检查扫描路径和当前账户的文件权限。macOS 还可能需要为启动服务的应用授予文件夹访问权限，无需为此改用管理员或 root 账户。

</details>

## 环境变量配置

日常使用只需要编辑项目文件夹中的 **`.env`**。最常改的是项目路径，例如：

```dotenv
# macOS / Linux
OPPEN_SCAN_ROOTS=["~/Projects", "~/Research"]

# Windows：路径使用正斜线
# OPPEN_SCAN_ROOTS=["C:/Users/Me/Projects", "D:/Research"]

# 不想让 GPT 访问的文件夹
OPPEN_EXCLUDE_ROOTS=["~/Projects/private"]
```

可以填写多个文件夹；不修改时，默认从当前用户的主目录开始查找。路径列表使用上面这种带引号和方括号的写法，`~` 表示用户主目录。

| 设置 | 什么时候需要改 |
| --- | --- |
| `OPPEN_TRANSPORT` | Tunnel 使用 `stdio`，域名转发使用 `http`；两份示例已经分别填好 |
| `OPPEN_SCAN_ROOTS` | 指定你的项目所在文件夹 |
| `OPPEN_EXCLUDE_ROOTS` | 排除不想分享的文件夹及其子文件夹 |
| `OPPEN_DISCUSSION_MODE` | 默认 `off`；`read` 开放讨论读取，`write` 同时允许新建和编辑讨论 |
| `OPPEN_PUBLIC_URL` | HTTP 模式下填写自己的 HTTPS 域名 |
| `OPPEN_PORT` | 默认 `8766`；端口被占用时可以更换，并同步修改转发规则 |
| `OPPEN_TUNNEL_ID` | 填入 OpenAI Platform 中的 Tunnel ID |
| `CONTROL_PLANE_API_KEY` | 填入 Tunnel 的运行密钥 |
| `OPPEN_TUNNEL_CLIENT` | 找不到 `tunnel-client` 命令时，填写程序的完整路径；Windows 可指向 `.exe` |

如果还想让 GPT 查看两个技能的使用说明，可以设置 `OPPEN_SKILL_ROOT`，指向同时包含 `oppen-project-steward` 和 `stepwise-r-project` 文件夹的位置。不填时会尝试在 `~/.codex/skills` 和 `~/.agents/skills` 中查找；没安装技能也能读取已有项目，只是无法提供技能说明。

`.env` 可能包含密钥，请保留在自己的电脑上。仓库已忽略 `.env`、`.runtime` 和 `config.local.json`，它们不会随普通 Git 提交上传。

<details>
<summary>其他设置与旧版本配置</summary>

| 设置 | 默认值与说明 |
| --- | --- |
| `OPPEN_STATE_DIR` | `.runtime`，保存登录口令、OAuth 数据库、讨论编号与重试记录，以及本机日志 |
| `OPPEN_HOST` | `127.0.0.1`，只允许本机回环地址；端口范围为 1024–65535 |
| `OPPEN_SCAN_INTERVAL` | `300` 秒，完成一轮后再次扫描的间隔，最小为 10 秒 |
| `OPPEN_SCAN_SECONDS` | `90` 秒，每批扫描的时间上限 |
| `OPPEN_MAX_SCAN_DIRS` | `500000`，每批最多扫描的文件夹数量 |
| `OPPEN_EXTRA_REDIRECT_URIS` | `[]`，额外允许的 OAuth 回调地址，需要完整匹配，不支持通配符 |
| `OPPEN_TUNNEL_PROFILE` | `oppen-steward`，官方 Tunnel 客户端中保存的连接配置名称 |

同一个设置出现在多处时，优先级是：命令行参数 > 系统环境变量 > `.env` > `config.local.json` > 默认值。新安装默认使用 stdio；旧 JSON 配置未指定连接方式时，仍使用 HTTP。

旧版 `configure` 命令和 JSON 配置可以继续使用，示例见 `config.example.json`。建议日常统一在 `.env` 修改，避免多个文件互相覆盖。相对路径以配置文件所在文件夹为起点；通过 `--config /path/config.local.json` 指定其他位置时，也会读取该文件旁的 `.env`。`.env` 使用 UTF-8 编码，不执行 shell 命令，也不展开 `${VAR}`。

HTTP 模式未填写域名时，默认地址为 `http://127.0.0.1:8766`，仅适合本机使用。远程连接需要填写真实的 HTTPS 域名。

运行状态文件应放在自己的私有文件夹。macOS/Linux 使用 0700/0600 权限，Windows 使用只允许当前账户访问的 ACL；权限设置失败时，HTTP 服务会停止初始化。不要把 `OPPEN_STATE_DIR` 指向其他用途的共享文件夹。

</details>

## GPT 能看到哪些文件

**默认是项目治理文档。** 主要是项目入口、Memory 中的决策记录，以及 Attention 中的待处理事项。讨论功能需要在本机另行开启，具体范围见下面的“保存和继续讨论”。

| 项目类型 | 可以读取的内容 |
| --- | --- |
| 当前 Steward 项目 | `.oppen-project-steward/registry.md`，以及同一文件夹下 Memory、Attention 的索引和已登记条目 |
| 旧版 Steward 项目 | `project.md`，以及项目根目录下 Memory、Attention 的索引和已登记条目 |
| Stepwise R v3 项目 | `project.md`，以及项目根目录下 Memory、Attention 的索引和已登记条目 |
| Stepwise R v2 项目 | 仅 `project.md` |

数据、源码、Results、Deliverables、全部 Audit、README、其他定义文档、运行文件和凭据都不开放。把一个文件放进项目文件夹，或在治理文档里链接到它，都不会让 GPT 获得读取权限；放在 Memory、Attention 中但没有登记的条目也不能读取。

允许读取的文档中，正文、标题和记载的路径会发送给 GPT。因此，如果你把敏感数据直接写进了这些治理文档，GPT 也能看到那部分文字。服务不会继续读取其中链接的文件或图片。

连接后，GPT 可以访问你配置范围内所有已发现项目的治理文档，包括以后新增的项目。如果不想分享某个项目，在本机 `.env` 中将它排除即可；GPT 无法自行扩大访问范围。

服务会定期查找新项目。文件夹较多或部分目录没有访问权限时，扫描状态可能显示 `partial`。这时可以稍后再查看，或检查配置路径及文件权限。发现项目只说明找到了它的管理文件，不会替你验证、迁移或修改项目。

<details>
<summary>开发者参考：识别与读取规则</summary>

Steward v3 通过 `.oppen-project-steward/registry.md` 识别；旧布局 Steward 和 Stepwise R v2/v3 通过 `project.md` 识别，文件中需要有对应的独立版本标记行。项目 ID 根据真实绝对路径的 SHA-256 前 20 位生成，每次访问都会重新检查标记和根目录身份。

Memory、Attention 的索引分别为 `index.md`，条目路径为 `entries/M-XXXX.md`、`entries/A-XXXX.md`。索引需符合对应技能的格式，条目需在索引中登记。R v2 的旧式 Memory 不开放。

`list_files`、`read_file`、`search`、`fetch` 和 `/files/*` 都遵守同一套治理文件列表。讨论只能通过专门的 Discussion 工具访问，不会混入原有读取入口。扫描只读取管理标记，跳过系统、依赖、缓存和运行状态目录；分批扫描未完成时会自动继续。远端扫描诊断包含计数和状态，不包含无关的失败目录路径。

读取只接受项目相对路径，拒绝路径穿越、绝对路径、NUL、反斜线、盘符及替代数据流写法。macOS/Linux 逐层使用文件描述符和 `O_NOFOLLOW`；Windows 使用原生句柄并在读取期间锁定祖先目录。符号链接、硬链接、被替换的根目录和特殊文件均被拒绝。Windows 目前只支持普通本地文件夹，不支持网络共享、目录联接或云端占位文件；同步软件或编辑器占用也可能导致读取失败。

文件默认每次读取 64 KiB，最多 256 KiB，offset/length 以字节计。返回 `next_offset` 时可继续读取；UTF-8 分块可能替换被截断的字符，需要精确字节时使用 base64。结果包含大小、修改时间和块 SHA-256；分块期间文件变化时应重新读取。文档内容始终视为不可信输入。

搜索匹配文档名称和不超过 256 KiB 的正文，默认最多返回 30 条，每次最多检查 10,000 个文件或运行 10 秒；超限时返回 `truncated`。HTTP 模式的文件链接需要 Bearer token，不能作为公开下载链接。stdio 返回的 `oppen-steward://` 是文件标识，可用 `fetch` 或 `read_file` 读取。

| MCP 工具 | 用途 |
| --- | --- |
| `list_projects` | 查找项目，支持按名称、路径或技能筛选 |
| `refresh_projects` | 开始或继续扫描 |
| `project_overview` | 查看项目入口和 Memory、Attention 的位置 |
| `list_files` | 浏览允许读取的文件 |
| `read_file` | 分块读取文档 |
| `search` / `fetch` | 搜索并读取文档 |
| `get_skill_guide` | 查看两个技能的本机 SKILL.md |
| `list_discussions` / `read_discussion` | 查找和读取讨论，包括编辑所需的版本标识 |
| `create_discussion` / `edit_discussion` | 新建或编辑讨论，由 MCP 更新索引；仅在 `write` 模式下提供 |

</details>

## 保存和继续讨论

想把网页里的讨论留给下次使用，或让本机 Codex 接着阅读，可以在 `.env` 中加入：

```dotenv
OPPEN_DISCUSSION_MODE=write
```

重启服务后，在 ChatGPT 中刷新工具并重新授权。HTTP 授权页会明确列出讨论的读取、新建和编辑权限；原来的只读授权不会自动获得这些权限。如果旧连接一直不显示新权限，可以移除连接，再用原来的 `/mcp` 地址添加一次。Tunnel 用户重启 `tunnel run` 并刷新工具即可，访问权限由本机配置和 Tunnel 认证共同控制。

之后可以直接说：“把这段讨论保存到某某项目”，或“打开之前关于部署的讨论，补上刚才的结论”。GPT 会先读取已有内容再编辑；文件在此期间被别人改过，MCP 会要求重新读取，避免用旧内容覆盖新修改。

| 项目类型 | 保存位置 |
| --- | --- |
| 当前 Steward 项目 | `.oppen-project-steward/Discussion/` |
| Stepwise R v3 项目 | 项目根目录的 `Discussion/` |

文件会命名为 `D-000001__部署方式讨论.md`，后面的编号依次递增。继续同一话题时编辑原文件，MCP 会同步维护 `index.md` 中的链接、简介和更新时间。正文可以自由写，不需要套模板，也不必把讨论登记为正式决定。

这一版支持新建和编辑，不提供删除或重命名。治理索引、Memory、Attention 和其他项目文件保持只读或不可访问。旧版项目需要先在本机升级管理布局，才会开放讨论功能。

讨论正文会发送给 GPT，包括你主动写进去的草稿、代码或数据片段；服务不会读取其中链接的文件。保存本身不会执行代码、修改项目实现，也不会自动提交或推送 Git。讨论是否随你自己的 Git 提交发布，取决于该项目已有的忽略规则，MCP 不会修改这些规则。

只想让 GPT 看已有讨论时，将配置改成 `read`；改回 `off` 可以关闭讨论访问。

<details>
<summary>讨论的编辑、重试和本机文件</summary>

`read_discussion` 返回完整正文及 SHA-256 `revision`；`edit_discussion` 接收完整的新正文、简短简介和 `expected_revision`，保留编号与文件路径。`index` 可以读取，但不能作为编辑目标。追加文字时也先读取原文，再提交包含原文的新正文。

每次新建或编辑需要一个唯一 `request_id`。遇到连接中断时，用同一个编号和相同参数重试，MCP 会继续完成那次写入，或返回已完成的结果。文档和索引分别原子替换；中途失败可能出现“正文已保存、索引尚未更新”，重试会补齐索引。服务用本机锁串行处理多个 MCP 进程的写入。版本校验能发现读取后的修改，但不能锁住不遵守该锁的本机编辑器；请避免在网页保存的同时修改同一文件。

运行目录中的 `discussion.sqlite3` 保存已分配编号、简介和请求记录；未完成请求还会临时保留待写正文，完成后移除。保留运行目录或索引中的编号记录，才能在本机删除文件后继续避免复用旧编号。不要手动删除正在使用的运行状态。

每份正文最多 256 KiB，每个项目最多 1,000 份讨论、10,000 次新建或编辑请求记录，编号最多到 `D-999999`。达到上限会停止新写入；同一请求重试仍可完成。缺失或过期的索引不妨碍列出已有讨论，下一次成功写入会重建索引。讨论只接受平铺的编号 Markdown 文件，链接、特殊文件和配置中排除的路径都不可访问。

</details>

## 授权与口令

**Tunnel 模式**使用 OpenAI 的工作区权限和运行密钥，不会弹出本项目的口令登录页。

**HTTP 模式**会先要求你输入 `.runtime/owner-access.txt` 中的口令，再确认授权。这个口令由首次 `setup` 自动生成，重复运行 `setup` 不会更换口令，也不会让已有授权失效。

普通重启会保留授权。如果更换了公网域名或使用新的运行状态文件夹，需要在 ChatGPT 中重新连接。

开启讨论功能不会扩大旧授权。要读取或编辑讨论，需要在授权页同意新增权限；访问口令仍是原来那一个。

想取消全部授权：

```sh
uv run python run.py revoke-all
```

想换一个新口令：

```sh
uv run python run.py rotate-password
```

更换口令也会撤销已有授权，新口令仍保存在同一个本机文件中。这套授权适合自己使用，不提供多个用户之间的项目隔离。

<details>
<summary>开发者参考：OAuth 协议与浏览器设置</summary>

HTTP 使用 MCP SDK 的授权码流程、S256 PKCE、精确回调匹配，以及固定的 issuer/resource。基础权限是 `governance:read`；讨论读取另需 `discussion:read`，写入再需 `discussion:write`。工具描述及 `_meta.securitySchemes` 声明各自的权限，服务端逐次验证；缺少权限时返回包含 `_meta["mcp/www_authenticate"]` 的错误，供客户端发起新增授权。实现依据见 [OpenAI 官方认证文档](https://developers.openai.com/plugins/build/auth)。服务提供 OAuth Authorization Server Metadata、Protected Resource Metadata，并在授权回调中包含 RFC 9207 `iss`。

授权页检查本机口令、Cookie、CSRF token 和 Origin，禁止 iframe 嵌入。浏览器会话有效 10 分钟，授权码有效 120 秒且只能兑换一次。访问令牌有效 1 小时，刷新令牌有效 30 天；刷新时轮换令牌，旧刷新令牌被重复使用时撤销整条授权链。SQLite 事务保证一次性消费。令牌以 SHA-256 索引保存，刷新只能保留或缩小已授权范围。更换 issuer 或从历史广泛访问升级为治理只读时撤销旧授权，保留本机口令；启用讨论时保留原有治理授权。关闭某种讨论权限后，包含该权限的令牌会被拒绝，需要重新连接。

授权页使用 `Referrer-Policy: same-origin`。改为 `no-referrer` 会让原生表单 POST 的 Origin 变成 `null`，触发来源检查。CSP 的 `form-action` 允许同源提交和已验证的精确回调，避免浏览器阻止表单后的 303 跳转；其他页面默认只允许同源提交。相关规则见 [Fetch 标准](https://fetch.spec.whatwg.org/#append-a-request-origin-header)。

动态注册仅接受 ChatGPT 回调和本机额外配置的精确回调，注册后仍需本人登录同意。支持 public client、client_secret_post 和 client_secret_basic；单进程最多 1,000 个客户端，认证端点每分钟最多 60 次，授权页 POST 每分钟最多 10 次，请求体上限为 1 MiB。

stdio 工具声明 `noauth`，其访问权限依赖本机账户和官方 Tunnel 认证。请勿用无认证的网络转发工具直接公开 stdio。

</details>

## 参与开发

欢迎提交问题和改进。如果要在本机运行测试：

```sh
uv sync --locked --dev
uv run ruff check .
uv run pytest -q
```

测试使用临时项目和测试口令。浏览器测试等详细步骤见 [贡献指南](CONTRIBUTING.md)，安全问题的反馈方式见 [SECURITY.md](SECURITY.md)。

本项目本身也使用 Oppen Project Steward 管理，相关记录只保留在本机，不随仓库发布。

本仓库采用 [MIT License](LICENSE)。外部技能、MCP SDK 和 Tunnel 客户端各自遵循其许可证。
