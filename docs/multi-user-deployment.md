# 多用户工作区

前端入口默认显示登录页面，支持注册、登录、退出和修改密码。用户名为 3–32 位英文字母、数字、点、下划线或短横线，密码为 10–128 个字符。注册成功后自动进入个人工作区。

## 数据与身份

- 账号、密码摘要、可撤销的登录会话存储在持久卷 `/data/backend/auth.sqlite3`。密码使用独立随机盐和 PBKDF2-SHA256；登录凭证只写入 HttpOnly、SameSite Cookie。HTTPS 下自动使用 Secure Cookie。
- 登录有效期为 14 天，退出立即撤销当前会话；修改密码撤销所有旧会话，并为当前浏览器签发新会话。
- 项目、数据集、上传源文件、构图结果、聊天记录和模型设置按服务端用户 ID 隔离。客户端传入的 `X-User-ID` 不作为身份依据。
- 新用户不会继承部署者的模型密钥；使用 AI 对话或 AI 构图前，请在个人设置里填写自己的服务连接。显式 CSV 主外键构图不依赖模型密钥。
- 原工作区的数据仍属于 `IGRAPH_ADMIN_USER_ID`（默认 `admin`）。公开注册不能取得这个身份；没有“首个注册用户自动成为管理员”的规则。
- 原工作区构图连接保留在 `/data/configuration/construction-model.json`；个人连接保存在 `/data/configuration/users/` 下。请同时备份 `igraph-data` 和 PostgreSQL 持久卷，不要执行 `docker compose down -v`。

## 原工作区首次登录

部署后在服务器生成一个仅部署者可读的临时密码文件（至少 10 字符），复制到 Backend 容器，然后执行：

```sh
docker compose exec backend python -m backend.auth --username admin --password-file /tmp/owner-password
```

成功后移除容器中的临时文件。此命令只能首次创建原工作区登录；重复执行不会覆盖密码。登录后可通过顶栏“修改密码”更换初始密码。

## 部署配置

Compose 显式启用 `BACKEND_AUTH_ENABLED=true`，认证数据库位于数据持久卷。仅 Frontend 对外发布端口，Backend、Agent Runtime、Graph Engine 仍走内部网络及服务密钥认证。

如果经过可信反向代理，配置 `BACKEND_AUTH_TRUSTED_PROXIES` 为实际代理网段，并由代理覆盖 `X-Real-IP`、`X-Forwarded-Proto`。`BACKEND_AUTH_ALLOWED_ORIGINS` 只接受明确来源，不使用通配符。注册和登录有持久化频率限制。

公网入口可以将 Frontend 映射到 `21120:80`。使用明文 HTTP 时密码与会话在网络上没有 TLS 加密；正式公开使用建议在入口启用 HTTPS。

## 验证

服务器测试覆盖登录持久化、退出撤销、修改密码、重复注册、CSRF、跨账户访问以及模型密钥隔离。升级后应使用两个独立浏览器会话验证上传、构图、刷新和服务重启后的恢复。
