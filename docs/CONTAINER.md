# 容器部署

发布 `ghcr.io/yewfence/tgbridge` 镜像。release tag 触发 GitHub Actions 构建并推送多架构镜像，
本地开发则用 compose 启动（见下）。

## 目标与约束

- 版本链路复用仓库现有的两把锁：python / rclone / uv 由 `mise.lock` 锁定，Python 依赖由
  `uv.lock` 锁定，不为镜像新增任何版本来源
- **mise 与 uv 只存在于构建阶段，不进入最终镜像**
- 镜像内装的是项目 wheel（与将来发 PyPI 的产物同形），不是源码目录
- 多架构 `linux/amd64` + `linux/arm64`（`mise.lock` 已锁全部 4 个 linux 平台条目）

## Dockerfile（多阶段）

`Dockerfile` 三段：

| 阶段 | 内容 |
|---|---|
| `tools` | 官方 mise 镜像里 `mise install --locked python uv rclone`，然后用 `mise which` / `mise where` 把三者摊平到固定路径（`/opt/python`、`/usr/local/bin/{uv,rclone}`），后续阶段不依赖 mise 的版本化目录布局 |
| `deps` | 装 `build-essential`（`tgcrypto` 对当前解释器无 wheel，需从 sdist 编译），在最终路径 `/opt/venv` 建 venv，`uv build` 出 wheel，再用 `uv export --locked` + `--require-hashes` 装依赖，最后 `--no-deps` 装上 wheel |
| runtime | `debian:13-slim` + `ca-certificates`，只拷 `/opt/python`、`/opt/venv`、`rclone` 三样；无 mise、无 uv、无编译器 |

关键点：

- **`mise which` 而不是硬编码路径**：rclone 经 aqua 后端安装，二进制在解包后的
  `rclone-v*-linux-*/` 目录里；uv 在 `.mise-bins/` 下。写死路径会在工具升级时静默失效。
- **依赖安装带哈希**：`uv export --locked --no-dev --no-emit-project --format requirements.txt`
  导出锁文件（含 `--hash`），`uv pip install --require-hashes` 校验，锁文件即唯一事实来源。
- **`uv build` 显式产出 wheel**：镜像里装的就是 wheel，而非 `uv sync` 的临时构建，产物形态
  与未来发 PyPI 一致，也让「装进去的到底是什么」可验证。
- 分层顺序保证锁文件不变时源码改动不触发依赖重解析。

## 运行时约定

- 配置全走环境变量：`BOT_TOKEN` / `API_ID` / `API_HASH` / `ALLOWED_CHATS` / `RCLONE_REMOTE` 等
- 以 `nobody`（65534）运行；`/config/rclone` 与 `/tmp/tgbridge` 在镜像内预建为该属主，
  因此**新建命名卷会继承该属主**，不会变成 root 属主
- `RCLONE_CONFIG=/config/rclone/rclone.conf`，`TMP_DIR=/tmp/tgbridge`
- 两个命名卷：`rclone-config` 存 init 容器拷入的 rclone 配置，`tgbridge-tmp` 挂到
  `/tmp/tgbridge` 作为下载暂存。暂存卷落盘而非 tmpfs，下载量不再计入内存占用
- `stop_grace_period: 30s`：`docker stop` 的 SIGTERM 会取消活跃任务（停下载、杀 rclone、清暂存）
- 启动即校验 rclone remote 与 Telegram 连通性：remote 未配置直接 exit 2；连不上 Telegram
  打一行 `startup failed: cannot connect to Telegram` 并 exit 1，交给 `restart` 策略重试

### rclone 配置为什么要过 init 容器

rclone 在刷新 OAuth token 时会**回写 `rclone.conf`**。直接 bind 挂载宿主机明文文件有两个后果：
回写污染宿主机的解密目录，以及容器内写入把文件变成 root 属主。

所以用一次性 init 容器把宿主机文件拷进命名卷，再让业务容器挂载该卷：

```yaml
services:
  rclone-config:
    image: debian:13-slim
    restart: "no"
    volumes:
      - ${RCLONE_CONFIG_FILE:-./rclone.conf}:/input/rclone.conf:ro
      - rclone-config:/out
    entrypoint: ["/bin/sh", "-c"]
    command:
      - install -o 65534 -g 65534 -m 600 /input/rclone.conf /out/rclone.conf
```

`install` 一步同时完成拷贝与属主/权限设定，宿主机文件保持只读输入、不被回写。

## 本地构建与启动

`compose.yaml` 是部署配方（拉取已发布镜像），`compose.dev.yaml` 是本地开发覆盖层
（从当前 checkout 构建）。开发时用 mise task，它已经带上了这两个文件：

```bash
cp .env.example .env          # 填 BOT_TOKEN / API_ID / API_HASH / ALLOWED_CHATS / RCLONE_REMOTE
# 准备好 rclone.conf（RCLONE_CONFIG_FILE 指向它），或用 .env 覆盖该路径
mise run container:up         # = docker build -t tgbridge:dev . && docker compose -f compose.yaml -f compose.dev.yaml up -d
docker compose -f compose.yaml -f compose.dev.yaml logs -f tgbridge
mise run container:down       # 停服务并删卷
```

部署到 VPS 时只用基础文件，它拉取已发布镜像而不构建：

```bash
docker compose up -d          # 拉取 ghcr.io/yewfence/tgbridge:latest
TGBRIDGE_IMAGE=ghcr.io/yewfence/tgbridge:0.1.0 docker compose up -d   # 固定版本
```

若构建期需要走代理（例如国内网络拉 python-build-standalone / aqua 资产）：

```bash
docker build --network=host \
  --build-arg HTTP_PROXY=http://127.0.0.1:7897 \
  --build-arg HTTPS_PROXY=http://127.0.0.1:7897 \
  --build-arg NO_PROXY=localhost,127.0.0.1 \
  -t tgbridge:dev .
```

`--network=host` 让构建容器能访问宿主机上的 `127.0.0.1` 代理；`HTTP_PROXY` / `HTTPS_PROXY`
是 Docker 预定义的 build arg，无需在 Dockerfile 里声明。注意用不带 MITM 凭据的普通代理端口，
否则 TLS 证书校验会失败。

## 推送到 VPS 验证

镜像不依赖构建机架构之外的东西，直接 save/load 或经 registry 中转即可：

```bash
docker save tgbridge:dev | gzip | ssh vps 'gunzip | docker load'
# 或 docker tag tgbridge:dev ghcr.io/yewfence/tgbridge:dev && docker push ...
```

VPS 上同样需要 `.env` 与 `rclone.conf`，`docker compose up -d` 即可。

## 发布流水线

镜像由 `.github/workflows/release.yml` 的 `container` job 发布，走 Docker 官方的 reusable
workflow [`docker/github-builder`](https://github.com/docker/github-builder)：

| 环节 | 行为 |
|---|---|
| 触发 | 与 GitHub Release 同一时机：push `v*` tag、release PR 合并、或手动 `workflow_dispatch` |
| 镜像名 | `ghcr.io/yewfence/tgbridge`（ghcr 要求仓库名小写） |
| tag | 版本号（如 `0.1.0`）；稳定版额外打 `latest`，**预发布版本不打 `latest`** |
| 架构 | `linux/amd64` + `linux/arm64` |
| 凭据 | `GITHUB_TOKEN` 登录 ghcr，需要 `packages: write` 权限 |

### 原生多架构，无 QEMU

`docker/github-builder` 按平台把构建拆成矩阵，并映射到对应的 GitHub 托管 runner：

```text
linux/amd64 -> ubuntu-26.04
linux/arm64 -> ubuntu-26.04-arm
```

每个平台在原生 arm64/amd64 runner 上编译，不走 QEMU 模拟。构建按 digest 推送，
再由 reusable workflow 内部的 `finalize` job 自动合并成多平台 manifest 并打 tag——
不需要手写 digest 传递或 manifest 合并步骤。

`tgcrypto` 在当前解释器上没有 wheel，arm64 上同样从 sdist 编译，所以 `deps` 阶段在两个架构上
都要装 `build-essential`。

### 版本号是单一来源

`pyproject.toml` 是项目版本的唯一事实来源：

- `prepare-release` workflow 用 `mise run release:version:sync` 把版本写成 release tag 的版本号
  （`uv version` 同时更新 `pyproject.toml` 和 `uv.lock`），并把这两个文件一起提交进 release PR
- `src/tgbridge/__init__.py` 从已安装的 dist 元数据读版本（`importlib.metadata`），
  因此镜像里 `/status` 报的就是该 release 的版本，不会停留在旧值

### 构建 commit 必须与 tag 一致

镜像构建的源码来自 git context，固定在 `GITHUB_SHA` 上。手动 `workflow_dispatch` 时
`GITHUB_SHA` 是所选分支的 HEAD，而不是 release tag 指向的 commit——那样产出的镜像会挂着
release 的版本号却装着更新的代码。

因此 `container-tags` job 会比对 `GITHUB_SHA` 与 release commit：不一致就跳过镜像发布，
并在 job summary 里写明原因。push tag 和 release PR 合并两条路径天然一致，不受影响。

### 首次发布后

ghcr 包默认与仓库关联但可能不是公开的，需要在包的设置里把可见性调整为需要的状态。
