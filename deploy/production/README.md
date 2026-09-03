# iGraph 生产环境 CD 操作手册

## 部署结构

每次向 `main` 推送代码后，GitHub Actions 会先运行现有 CI。CI 全部通过后，
GitHub 托管 runner 构建 5 个以完整 commit SHA 标记的镜像、推送到私有
GHCR，再使用现有可登录服务器的 SSH 密钥部署同一个 SHA。

服务器不会现场构建源码。应用只监听 `127.0.0.1:22108`，现有
`ngrok-igraph.service` 把它发布到：

<https://diffused-flatworm-unskilled.ngrok-free.dev>

部署任务串行执行，健康检查失败时会恢复上一次成功版本。第一次部署没有旧的
iGraph 版本可供回滚。

## 已确认的服务器条件

- SSH：`keyuyang@hdssh01.casdao.com:63125`
- Rootless Docker：正常
- Docker Compose：正常
- 用户级 Docker 和 ngrok 服务：`active`
- Linger：已启用
- 架构：`x86_64`
- GHCR：服务器可达
- 生产 `.env`：已上传，权限为 `600`
- `POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD`：均已设置
- 部署目录：`/home/keyuyang/apps/igraph/releases` 已创建

## 一次性准备

### 1. 更换已经出现在截图中的 ngrok authtoken

当前 ngrok authtoken 曾完整出现在终端截图里。正式上线前应更换：

1. 打开 <https://dashboard.ngrok.com/tunnels/authtokens>。
2. 点击 `New Authtoken`，Description 填 `igraph-server`，复制新 token；
   此时先不要删除旧 token。
3. 登录服务器后执行：

```bash
ssh -p 63125 keyuyang@hdssh01.casdao.com
read -rsp '请粘贴新的 ngrok authtoken，然后按回车：' NGROK_TOKEN
echo
/home/keyuyang/.local/bin/ngrok config add-authtoken "$NGROK_TOKEN"
unset NGROK_TOKEN
chmod 600 /home/keyuyang/.config/ngrok/ngrok.yml
/home/keyuyang/.local/bin/ngrok config check
systemctl --user restart ngrok-igraph.service
systemctl --user is-active ngrok-igraph.service
exit
```

4. 确认公网地址仍能打开，然后删除/吊销旧 credential，只保留
   `igraph-server`。

### 2. 将现有可用 SSH 私钥添加为 GitHub Secret

实测服务器接受的本地密钥是：

```text
/Users/kyy008/.ssh/id_ed25519
```

该密钥没有 passphrase，GitHub Actions 可以非交互式使用。用下面的命令直接
复制私钥内容，不要让它显示在终端里：

```bash
pbcopy < "$HOME/.ssh/id_ed25519"
```

打开 GitHub 上的 iGraph 仓库，依次进入：

`Settings` -> `Secrets and variables` -> `Actions` ->
`New repository secret`

创建：

- Name：`DEPLOY_SSH_KEY`
- Secret：粘贴刚复制的私钥全文

Secret 必须包含 `BEGIN OPENSSH PRIVATE KEY` 和
`END OPENSSH PRIVATE KEY` 两行。不要填 `.pub` 公钥。

这是当前服务器限制下的折中方案：这把密钥也是你的日常 SSH 密钥，因此必须
限制谁能修改 workflow 和向 `main` push。GitHub Actions 不会在日志中打印
该 Secret；工作流只在 `main` push 且 CI 全通过后使用它。

## 第一次发布

添加 `DEPLOY_SSH_KEY` 后，在 Mac 项目根目录执行：

```bash
cd /Users/kyy008/Desktop/Kyy008/Code/GammaLab/iGraph

git diff --check
FRONTEND_BIND_ADDRESS=127.0.0.1 \
FRONTEND_PORT=22108 \
IMAGE_TAG=0000000000000000000000000000000000000000 \
  docker compose \
    -f compose.yaml \
    -f deploy/production/compose.production.yaml \
    config -q

git add \
  .env.example \
  compose.yaml \
  .github/workflows/ci.yml \
  deploy/production
git commit -m "ci: add production continuous deployment"
git push origin main
```

打开 GitHub 仓库的 `Actions` 页面。首次发布应依次完成：

1. `Python tests` 和 `Frontend checks`
2. `Required checks`
3. 5 个 `Publish ... image` 任务
4. `Deploy production`

graph-engine 镜像包含较大的机器学习依赖，第一次构建和拉取会明显更慢。
部署脚本会逐个拉取镜像，并对临时网络断开自动重试 6 次；已完成的镜像层会
继续复用，不会每次都从零开始。GitHub 中不显示逐字节进度属于预期行为。
SSH 每 30 秒发送一次保活包；如果部署命令的 SSH 会话仍被中断，工作流会
自动重连 3 次。服务器端部署锁会阻止两次重连并发修改同一个环境。

## 上线验收

等 `Deploy production` 变绿后执行：

```bash
curl -fsS \
  -H 'ngrok-skip-browser-warning: 1' \
  https://diffused-flatworm-unskilled.ngrok-free.dev/health
```

预期响应：

```json
{"status":"ok","service":"frontend"}
```

浏览器打开公网地址后应显示 iGraph，而不是 nginx 欢迎页。

服务器状态检查：

```bash
ssh -p 63125 keyuyang@hdssh01.casdao.com
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"

docker compose \
  --project-name igraph \
  --env-file /home/keyuyang/apps/igraph/.env \
  -f /home/keyuyang/apps/igraph/current/compose.yaml \
  -f /home/keyuyang/apps/igraph/current/compose.production.yaml \
  ps

cat /home/keyuyang/apps/igraph/.deployed-image-tag
systemctl --user is-active ngrok-igraph.service
curl -fsS http://127.0.0.1:22108/health
exit
```

6 个 Compose 服务必须都在运行；带健康检查的服务必须为 `healthy`；版本
必须是 40 位 commit SHA；ngrok 必须为 `active`。

## 日常发布与故障处理

- 向任意分支 push 都运行 CI。
- 只有 `main` push 且 CI 全通过才会部署。
- 当前 workflow 不监听 `pull_request`。
- `.env`、数据库和项目数据卷一直保留在服务器上。
- GitHub job 使用短期 `GITHUB_TOKEN` 拉取私有镜像，完成后退出 GHCR。
- 不要执行 `docker compose down -v`，`-v` 会删除数据库和应用数据卷。

部署失败时，先展开 GitHub 的 `Deploy production` 日志，再登录服务器执行：

```bash
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
docker compose \
  --project-name igraph \
  --env-file /home/keyuyang/apps/igraph/.env \
  -f /home/keyuyang/apps/igraph/current/compose.yaml \
  -f /home/keyuyang/apps/igraph/current/compose.production.yaml \
  ps
docker compose \
  --project-name igraph \
  --env-file /home/keyuyang/apps/igraph/.env \
  -f /home/keyuyang/apps/igraph/current/compose.yaml \
  -f /home/keyuyang/apps/igraph/current/compose.production.yaml \
  logs --tail=200
```

分享日志时，不要粘贴 `.env`、ngrok authtoken、SSH 私钥或任何模型服务
API 密钥。
