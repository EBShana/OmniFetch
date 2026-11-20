# OmniFetch

集中管理式的资源发现与下载系统：可插拔爬虫（RSS/站点/论坛/表格）+ 管理层（去重、作品降级链、状态机）+ 可插拔下载器（aria2/qBittorrent/网盘），Web UI 管理。

## 快速开始（本地开发）

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Windows
omnifetch init-db                                 # 初始化 SQLite（含全部唯一约束）
omnifetch run --once                              # 跑一轮调度：抓取→入库→生成任务
omnifetch run                                     # 持续调度
omnifetch serve                                   # Web API，默认 :8080
pytest                                            # 核心逻辑测试
```

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
