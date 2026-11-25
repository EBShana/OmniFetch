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

`serve` 默认只绑定 `127.0.0.1`。需要从局域网或容器外访问时，显式传 `--host 0.0.0.0`
（Docker 镜像的 compose 已这样配置），并**同时设置 `OMNIFETCH_TOKEN`**；未设置令牌却
绑定非回环地址时，启动日志会输出告警，此时同网段任何人都能修改插件配置、控制下载。

## 配置

TOML 格式，两个目录：

- `config/sources.d/*.toml` — 每个爬取源一个文件（`type`/`enabled`/`interval`/`url`…，其余参数原样传给插件）
- `config/downloaders.d/*.toml` — 每个下载器实例一个文件

RSS 源支持订阅模式：设置 `want_key` 后，该源抓到的资源自动成为该作品的候选。

## Docker

```bash
docker compose up -d                    # 只启动 omnifetch
docker compose --profile downloaders up -d   # 附带 qBittorrent + aria2
```

SQLite 在 named volume `omnifetch-data` 中；镜像可随意升级重建，数据不动。

## 架构

- `src/omnifetch/core/` — 管理层：模型、去重入库（URL 指纹 + 内容指纹唯一索引）、标题解析（work_key/quality）、want 匹配、任务状态机（原子转移 + 指数退避）、候选排序与降级链
- `src/omnifetch/crawlers/` — 爬虫插件（无状态，只返回 Resource）
- `src/omnifetch/downloaders/` — 下载器插件（submit/poll/cancel，回报 transient/permanent）
- `src/omnifetch/scheduler/` — 触发 / 分发 / 轮询 / 僵尸清扫
- `src/omnifetch/webapi/` — REST API（Web UI 的后端）
