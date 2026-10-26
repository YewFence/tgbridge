# 容器部署

发布 `ghcr.io/yewfence/tgbridge` 镜像。当前落地的是**本地构建 + compose 启动**这条链路；
GitHub Actions 的镜像发布流水线留待后续接入（见文末「待接入」）。

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

```bash
cp .env.example .env          # 填 BOT_TOKEN / API_ID / API_HASH / ALLOWED_CHATS / RCLONE_REMOTE
# 准备好 rclone.conf（RCLONE_CONFIG_FILE 指向它），或用 .env 覆盖该路径
mise run container:up         # = docker build -t tgbridge:dev . && docker compose up -d
docker compose logs -f tgbridge
mise run container:down       # 停服务并删卷
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
# 或 docker tag tgbridge:dev ghcr.io/<user>/tgbridge:dev && docker push ...
```

VPS 上同样需要 `.env` 与 `rclone.conf`，`docker compose up -d` 即可。

## 待接入

- GitHub Actions 发布流水线：release tag 触发 `docker buildx build --platform linux/amd64,linux/arm64 --push`，
  非 prerelease 追加 `latest`；新增 action 一律 pinact 固定
- `mise.ci.toml` 的 `container:publish` task（多架构 push）与 `container:build:ci`（CI 单架构构建）
- 首次发布后：ghcr 包可见性与仓库关联设置
