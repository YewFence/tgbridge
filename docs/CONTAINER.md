# 容器发布设计（设想稿）

发布 `ghcr.io/yewfence/tgbridge` 镜像。当前为设计文档，代码落地后按此实施；`mise.toml` 中的 rclone 工具项已先行合入（PoC 与镜像构建共用）。

## 目标与约束

- 镜像发布到 ghcr.io，随 release tag 触发，稳定版同时更新 `latest`
- 版本链路复用仓库现有的两把锁：python / rclone 版本由 `mise.lock` 锁定，Python 依赖由 `uv.lock` 锁定，不为镜像新增任何版本来源
- **mise 与 uv 只存在于构建阶段，不进入最终镜像**（mise 全家桶很大，bot 运行时不需要）
- 多架构 `linux/amd64` + `linux/arm64`（`mise.lock` 已锁全部 4 个 linux 平台条目）

## 依据（已验证的前提）

- mise 安装的 python 是 [python-build-standalone](https://github.com/astral-sh/python-build-standalone) 发行版，安装目录整体拷贝到其他路径后 `bin/python3` 可直接运行（ssl/sqlite3/zlib 扩展均正常）
- rclone 经 `aqua:rclone/rclone` 后端安装，单静态二进制，直接 `COPY` 到 `/usr/local/bin`
- `mise install --locked` 按 `mise.lock` 安装，不查询版本列表（见 mise 文档 [Docker cookbook](https://mise.jdx.dev/mise-cookbook/docker.html)）

## Dockerfile（多阶段）

```dockerfile
# syntax=docker/dockerfile:1

# ---- tools: 官方 mise 镜像里按锁安装 python/uv/rclone ----
FROM ghcr.io/jdx/mise:2026.10.4-debian AS tools
# mise 镜像默认 MISE_DATA_DIR=/mise；改为显式固定，避免依赖镜像内部约定
ENV MISE_DATA_DIR=/mise-data
COPY mise.toml mise.lock ./
# 只装镜像需要的工具；ruff/node/hk 等开发工具不进
RUN mise trust && mise install --locked python uv rclone

# ---- deps: 把 python 拷到最终路径，构建 wheel 并装入 venv ----
FROM tools AS deps
# 通配符吸收版本号，升级 python 无需改 Dockerfile
COPY --from=tools /mise-data/installs/python/3*/ /opt/python/
# venv 在最终路径上新建，无预建 venv 的路径锚定问题
RUN /opt/python/bin/python3 -m venv /opt/venv
COPY pyproject.toml uv.lock ./
RUN uv build --locked --out-dir /dist
COPY src/ src/
RUN uv build --locked --out-dir /dist
RUN UV_PROJECT_ENVIRONMENT=/opt/venv uv pip install --python /opt/venv/bin/python /dist/*.whl

# ---- runtime: 最终镜像不含 mise/uv ----
FROM debian:13-slim
RUN apt-get update \
    && apt-get -y --no-install-recommends install ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deps /opt/python /opt/python
COPY --from=deps /opt/venv /opt/venv
COPY --from=tools /mise-data/installs/rclone/1*/bin/rclone /usr/local/bin/rclone
USER nobody
ENTRYPOINT ["/opt/venv/bin/python", "-m", "tgbridge.entry"]
```

说明：

- **复用 `uv build`**：镜像内安装的是项目 wheel 而非源码目录，wheel 同时是未来发 PyPI 的产物形态；两次 `uv build` 中第一次利用 Docker 层缓存锁依赖元数据，源码变更时不重跑依赖解析
- mise 镜像 tag（`2026.10.4`）随 `actions:update` / renovate 更新，必要时可升级为 digest 固定（`ghcr.io/jdx/mise@sha256:<digest>`）
- `ENTRYPOINT` 指向 `tgbridge.entry`，与 DESIGN.md 的入口模块对齐，代码落地时校正

## 发布流水线

沿用仓库模板（thin workflow）的结构：workflow 只做编排，实际命令在 mise task。

**`.github/workflows/release.yml` 新增 job**（形状参考模板 go-cli 的 container-image capability）：

- `publish-container`：`needs: [version, release]`，`if: should_release == 'true'`
- permissions: `contents: read, packages: write`
- 步骤：checkout（`checkout_ref`）→ mise-action（`install_args: docker` 或 docker buildx 由 runner 自带）→ `GITHUB_TOKEN` 登录 ghcr → `mise run container:publish`

**`mise.ci.toml` 新增 task**：

- `container:publish`：`docker buildx build --platform linux/amd64,linux/arm64 --push`，tag 取 `RELEASE_TAG`；非 prerelease 追加 `latest`
- `container:build`：本地单架构构建（`ko.local` 等价的 `container:publish --dry-run` 形态，供本地 smoke）

**新增 Action**（`docker/build-push-action`、`docker/login-action`、`docker/setup-buildx-action` 等）一律 pinact 固定。

## 运行时约定

- 配置全走环境变量：`BOT_TOKEN` / `API_ID` / `API_HASH` / `ALLOWED_CHATS` / `RCLONE_REMOTE` 等（DESIGN.md 配置表）
- rclone 配置经 volume 挂载：`-v ~/.config/rclone:/config/rclone`，容器内 `RCLONE_CONFIG=/config/rclone/rclone.conf`（或 `XDG_CONFIG_HOME`）
- 下载暂存 `TMP_DIR` 默认 `/tmp/tgbridge`，如需持久卷挂载可覆盖

## compose.yaml 示例（发布后补）

```yaml
services:
  tgbridge:
    image: ghcr.io/yewfence/tgbridge:latest
    restart: unless-stopped
    environment:
      BOT_TOKEN: ${BOT_TOKEN}
      API_ID: ${API_ID}
      API_HASH: ${API_HASH}
      ALLOWED_CHATS: ${ALLOWED_CHATS}
      RCLONE_REMOTE: gdrive:media
    volumes:
      - ./rclone:/config/rclone:ro
    tmpfs:
      - /tmp/tgbridge
```

## 实施清单（代码落地后）

- [ ] `Dockerfile` + `.dockerignore`（排除 `.env*`、`**/*.local.toml`、docs、`.git` 等）
- [ ] 本地 smoke：`mise run container:build` 后跑通「启动 → 收到转发 → GD 出现文件」最小链路
- [ ] `mise.ci.toml` 两个 task + release.yml job + pinact 固定新 action
- [ ] 首次发布后：ghcr 包可见性 / 仓库关联设置
- [ ] README / docs 补「Container Image」一节（参考模板 README-section 文案）
