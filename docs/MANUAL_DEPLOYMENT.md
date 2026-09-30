# main 手动部署

GitHub Actions CI 工作流及其中的部署作业已删除。以下脚本供人工部署指定提交，
执行前应完成相应的[本地测试](TESTING.md)。部署使用已有服务器 SSH 登录密钥，
私钥保存在本机，主机公钥须提前核验，禁止将凭据提交到仓库。

## 服务器配置

接收脚本安装在 `~/.local/bin/igraph-cd-entry`。部署用户运行 rootless Docker，
不需要 sudo。安装或更新接收脚本后可以用 `python3 ~/.local/bin/igraph-cd-entry check` 验证。

## 部署过程

1. 在本地导出已验证的准确提交，SSH 将源码归档传到服务器；不依赖服务器访问 GitHub。
2. 接收脚本校验提交 ID、归档大小和路径，在 `~/apps/igraph-deploy/incoming-<SHA>` 解压。
3. 保留 `~/apps/igraph/.env`、`host-config`、Compose 项目名 `igraph-kyy`、网络和已有数据卷。
   复用服务器离线依赖，生成以提交 SHA 标记的新镜像；构建期间旧容器继续运行。
4. 保存旧源码、环境配置和 PostgreSQL dump 到 `~/apps/igraph-deploy/backups/`。
5. 切换应用容器，等待健康检查，并验证前端首页及后端、运行时、图服务健康接口。
6. 成功后同步源码到既有 systemd 工作目录，并记录
   `~/apps/igraph/host-config/deployed-main-commit.txt` 和 `~/apps/igraph-deploy/current-commit.txt`。

构建失败不会替换运行中的容器。切换或健康检查失败时自动恢复旧源码和镜像并返回非零退出码。
镜像回退不自动回滚数据库迁移；修改数据库结构时必须保证向后兼容，必要时人工恢复对应备份。
部署切换会短暂重启应用服务，长时间运行的任务应避开发布时间。

备份包含敏感数据，只在服务器用户目录保存；按磁盘使用情况人工清理已确认不再需要的备份和旧镜像。
不要执行 `docker compose down -v`，也不要删除现有 `igraph-kyy` 数据卷。

## 检查

部署完成后，核对脚本退出码和部署日志，并在服务器上运行：

```bash
cat ~/apps/igraph/host-config/deployed-main-commit.txt
cd ~/apps/igraph
docker compose -p igraph-kyy -f compose.yaml -f host-config/compose.host.yaml ps
curl --fail http://127.0.0.1:22108/
```

验证归档上传时，可先执行 `bash -n deploy/cd/deploy.sh` 和 Python 语法检查；
上线以部署脚本成功、服务健康及服务器提交标记与目标提交一致作为完成依据。
