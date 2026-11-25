# OmniFetch

集中管理式的资源发现与下载系统：可插拔爬虫（RSS/站点/论坛/表格）+ 管理层（去重、作品降级链、状态机）+ 可插拔下载器（aria2/qBittorrent/网盘），Web UI 管理。

## 快速开始（本地开发）

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Windows
omnifetch init-db                                 # 初始化 SQLite（含全部唯一约束）
omnifetch run --once                              # 跑一轮调度：抓取→入库→生成任务
omnifetch run                                     # 持续调度
omnifetch serve                                   # Web UI + API，默认只监听 127.0.0.1:8080
pytest                                            # 核心逻辑测试
```

## Web UI

`omnifetch serve` 后访问 `http://localhost:8080`：总览 / 作品（候选链展开与操作）/
任务（状态过滤）/ 失败队列 / 系统状态。数据通过 HTMX 局部轮询刷新，无需前端构建。

认证（可选）：设置环境变量 `OMNIFETCH_TOKEN=你的令牌` 后，UI 会弹出令牌输入框，
API 请求需带 `X-OmniFetch-Token` 头（或 `?token=` / `Authorization: Bearer`）。

### 登录密码（对外部署必开）

设置 `ui_password_hash` 后，所有页面与 HTMX 片段都要求先登录；未登录访问会被
重定向到 `/login`（片段返回 401 + `HX-Redirect`）。会话是签名的 HttpOnly Cookie，
有效期 7 天；修改密码会让所有旧会话立即失效。密码只保存 scrypt 哈希，不存明文。

生成哈希并写入 `config/omnifetch.toml`：

```bash
omnifetch hash-password     # 交互输入两次，输出一行 scrypt$... 哈希
```

```toml
# config/omnifetch.toml
ui_username = "admin"
ui_password_hash = "scrypt$16384$8$1$..."
```

也可用环境变量：`OMNIFETCH_UI_USERNAME`、`OMNIFETCH_UI_PASSWORD_HASH`。容器 / NAS
部署图省事时可以只设 `OMNIFETCH_UI_PASSWORD=明文密码`，启动时在内存里哈希，不写盘。

登录失败有速率限制：同一 IP 5 分钟内失败 5 次后返回 429，每次失败还会附加延迟，
避免被无限尝试。UI 登录与 API 令牌相互独立：`OMNIFETCH_TOKEN` 仍只保护 `/api/*`，
两者都设置时同时生效；页面即使已登录，API 调用仍需令牌。

`serve` 默认只绑定 `127.0.0.1`。需要从局域网或容器外访问时，显式传 `--host 0.0.0.0`
（Docker 镜像的 compose 已这样配置），并**同时设置 UI 登录密码或 `OMNIFETCH_TOKEN`**；
未设置任何认证却绑定非回环地址时，启动日志会输出告警，此时同网段任何人都能修改
插件配置、控制下载。经公网访问请放在 HTTPS 反向代理之后（登录 Cookie 在 https 下
自动加 `Secure`）。

## 配置

TOML 格式，三个位置：

- `config/omnifetch.toml` — 核心设置（调度、并发、令牌、UI 登录等），可复制 `config/omnifetch.example.toml`
- `config/sources.d/*.toml` — 每个爬取源一个文件（`type`/`enabled`/`interval`/`url`…，其余参数原样传给插件）
- `config/downloaders.d/*.toml` — 每个下载器实例一个文件

环境变量 `OMNIFETCH_*` 优先于 TOML（旧前缀 `OMNFETCH_*` 仍兼容）。

RSS 源支持订阅模式：设置 `want_key` 后，该源抓到的资源自动成为该作品的候选。

## Docker

```bash
docker compose up -d                    # 只启动 omnifetch
docker compose --profile downloaders up -d   # 附带 qBittorrent + aria2
```

SQLite 在 named volume `omnifetch-data` 中；镜像可随意升级重建，数据不动。

对外访问（局域网或飞牛 NAS）时至少开一种认证，在 `docker-compose.yml` 的
`environment` 里加：

```yaml
environment:
  OMNIFETCH_UI_PASSWORD: "换成你的登录密码"     # 启动时在内存中哈希；也可用 OMNIFETCH_UI_PASSWORD_HASH
  # OMNIFETCH_TOKEN: "API 令牌（脚本调用 /api 时使用）"
```

宿主机 `./config` 已挂载到容器 `/app/config`，也可以直接执行
`omnifetch hash-password` 后把哈希写进 `config/omnifetch.toml`。
NAS 上把容器 8080 端口映射到宿主机后，浏览器打开 `http://<NAS地址>:8080` 即可看到登录页。
公网访问请配合飞牛的反向代理 / HTTPS，不要直接裸暴露 8080。

## 架构

- `src/omnifetch/core/` — 管理层：模型、去重入库（URL 指纹 + 内容指纹唯一索引）、标题解析（work_key/quality）、want 匹配、任务状态机（原子转移 + 指数退避）、候选排序与降级链
- `src/omnifetch/crawlers/` — 爬虫插件（无状态，只返回 Resource）
- `src/omnifetch/downloaders/` — 下载器插件（submit/poll/cancel，回报 transient/permanent）
- `src/omnifetch/scheduler/` — 触发 / 分发 / 轮询 / 僵尸清扫
- `src/omnifetch/webapi/` — REST API（Web UI 的后端）
