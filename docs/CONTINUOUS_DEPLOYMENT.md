# main 持续部署

`.github/workflows/ci.yml` 在推送 `main` 和 `dev` 时运行后端功能回归、前端构建以及 Docker 配置检查。
只有 `main` 的推送（包括合并）且所有必需 CI 作业成功，才会执行 `deploy`。
手动运行 CI 和推送 `dev` 均不会部署。main 的运行不会因新提交而中途取消，部署另有串行锁。

## GitHub 配置

仓库 Actions Secrets：

- `IGRAPH_CD_SSH_KEY`：服务器登录私钥，只能通过加密 Secret 配置，禁止提交到仓库。
- `IGRAPH_CD_KNOWN_HOSTS`：预先核验的服务器主机公钥，部署启用严格主机校验。

仓库 Actions Variables：`IGRAPH_CD_HOST`、`IGRAPH_CD_PORT`、`IGRAPH_CD_USER`。
现有服务器集中管理 SSH 公钥，因此使用已有登录密钥；以后管理员允许独立部署公钥时，
可以为 `deploy/cd/receive.py` 配置 forced command 和 `restrict`，然后轮换上述 Secret。

接收脚本安装在 `~/.local/bin/igraph-cd-entry`。部署用户运行 rootless Docker，
不需要 sudo。安装或更新接收脚本后可以用 `python3 ~/.local/bin/igraph-cd-entry check` 验证。

## 部署过程

1. GitHub 导出通过 CI 的准确提交，SSH 将源码归档传到服务器；不依赖服务器访问 GitHub。
2. 接收脚本校验提交 ID、归档大小和路径，在 `~/apps/igraph-deploy/incoming-<SHA>` 解压。
3. 保留 `~/apps/igraph/.env`、`host-config`、Compose 项目名 `igraph-kyy`、网络和已有数据卷。
   复用服务器离线依赖，生成以提交 SHA 标记的新镜像；构建期间旧容器继续运行。
4. 保存旧源码、环境配置和 PostgreSQL dump 到 `~/apps/igraph-deploy/backups/`。
5. 切换应用容器，等待健康检查，并验证前端首页及后端、运行时、图服务健康接口。
6. 成功后同步源码到既有 systemd 工作目录，并记录
   `~/apps/igraph/host-config/deployed-main-commit.txt` 和 `~/apps/igraph-deploy/current-commit.txt`。

构建失败不会替换运行中的容器。切换或健康检查失败时自动恢复旧源码和镜像并将 CD 标为失败。
镜像回退不自动回滚数据库迁移；修改数据库结构时必须保证向后兼容，必要时人工恢复对应备份。
部署切换会短暂重启应用服务，长时间运行的任务应避开发布时间。

备份包含敏感数据，只在服务器用户目录保存；按磁盘使用情况人工清理已确认不再需要的备份和旧镜像。
不要执行 `docker compose down -v`，也不要删除现有 `igraph-kyy` 数据卷。

## 检查

在 GitHub Actions 中查看 CI 的 `Deploy main to server` 作业；失败日志区分测试、构建和部署步骤。
服务器上运行：

```bash
cat ~/apps/igraph/host-config/deployed-main-commit.txt
cd ~/apps/igraph
docker compose -p igraph-kyy -f compose.yaml -f host-config/compose.host.yaml ps
curl --fail http://127.0.0.1:22108/
```

验证归档上传时，可先执行 `bash -n deploy/cd/deploy.sh` 和 Python 语法检查；
首次上线必须以实际 GitHub Actions 的 CI 和部署成功、服务器提交标记一致作为完成依据。
