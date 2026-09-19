# CampusFly 管理服务

这是 CampusFly 桌面客户端使用的匿名同步与远程禁用服务。它只使用 Python 标准库和 SQLite，默认绑定 `127.0.0.1:8791`。请求处理器关闭 `http.server` 默认访问日志，不收集、保存或输出 IP 地址；Caddy/Nginx 反向代理也应关闭此路由的访问日志。

## API 合同

`POST /campusfly/api/v1/sync` 接收 JSON 对象：

```json
{
  "installation_id": "4d0f8a2e-8d4a-4c24-9f27-8d7a0c6a2c5e",
  "token": "64 个十六进制字符的客户端随机密钥",
  "events": [
    {"id": "c4b3b8e8-8e8c-4d93-a9f7-7f2a5f0ce3b5", "kind": "install", "occurred_at": "2026-09-19T12:00:00Z"}
  ],
  "version": "1.1.0"
}
```

`installation_id` 和事件 `id` 都是 UUID；`token` 是客户端首次生成并长期保存的 32 字节随机值的 64 位十六进制编码。服务器首次看到安装时自动建行，只保存 token 的 SHA-256 哈希；之后每次同步必须提供同一个 token。token 错误返回 `401`，不会更新版本、状态或事件。

事件 `kind` 只能是 `install`、`submission_attempt` 或 `submission_success`。客户端只有在学校接口返回成功代码 `0` 后才发送 `submission_success`。同一安装的同一事件 UUID 只写入一次；重复请求仍会在 `acknowledged` 中确认该 ID。服务器响应固定为（没有活动公告时 `announcement` 为 `null`）：

```json
{"disabled": false, "acknowledged": ["c4b3b8e8-8e8c-4d93-a9f7-7f2a5f0ce3b5"], "announcement": null}
```

有活动公告时，`announcement` 为 `{ "id": "公告 UUID", "title": "纯文本标题", "body": "纯文本正文" }`。公告标题最多 80 字，正文最多 2000 字；每次发布（包括编辑）都会生成新的公告 UUID。客户端应以纯文本节点显示这些字段，不把内容当作 HTML、脚本或可点击链接。

请求体最多 64 KiB，每批最多 100 个事件；UUID、版本、带时区的 ISO-8601 时间、事件种类和 token 都会校验。未知字段不保存。安装数量按安装 UUID 去重，尝试和成功码事件分别统计。

## 管理页

`GET /campusfly/admin` 使用 HTTP Basic Auth。管理员用户名和密码只从 `CAMPUSFLY_ADMIN_USER`、`CAMPUSFLY_ADMIN_PASSWORD` 读取，源码和页面都不包含凭据。管理页通过 HTTPS 反向代理提供；写操作要求 `Origin` 精确等于 `CAMPUSFLY_ORIGIN`，并要求管理页生成的一次性 CSRF nonce。页面提供全局禁用/恢复、每个安装的本地禁用/恢复按钮，以及公告发布/编辑/撤回表单。

全局禁用优先级最高，会让所有安装的同步响应 `disabled: true`。单个安装的“禁用”只影响该安装；“恢复”清除它的本地禁用标记，不能绕过全局禁用。该开关只影响客户端是否继续工作，不提供命令执行、更新或任意远程控制。

公告发布后会持久化在私有 SQLite 数据库中，直到管理员撤回；撤回后新的 sync 响应返回 `announcement: null`。公告内容按纯文本转义到管理页，页面 CSP 禁止脚本、外部资源和表单跨域提交。

页面使用内联 CSS、无外部资源，并且只展示安装 UUID、版本、时间及匿名计数。列表最多显示最近 500 个安装。

## 环境变量与启动

生产环境至少设置以下变量。`CAMPUSFLY_DB` 应位于静态网站根目录之外，并限制为服务用户可读写。

```sh
export CAMPUSFLY_DB=/home/ethanwwy/.local/share/campusfly-management/management.sqlite3
export CAMPUSFLY_ADMIN_USER='从私有环境注入'
export CAMPUSFLY_ADMIN_PASSWORD='从私有环境注入'
export CAMPUSFLY_ORIGIN=https://998223.xyz
python3 -m server.app
```

可选的 `CAMPUSFLY_PORT` 默认是 `8791`；`CAMPUSFLY_BIND` 只允许 `127.0.0.1`、`localhost` 或 `::1`。不要把该服务直接绑定到公网地址。

### systemd 用户服务示例

下面的部署使用独立的服务目录 `/home/ethanwwy/projects/campusfly-service`，状态库位于静态根目录之外。将 `server/app.py` 部署为该服务目录中的 `app.py`（或把 `ExecStart` 改为 `server/app.py`）。环境文件只由服务用户读取，权限设为 `0600`，其中包含管理员凭据，不要提交到仓库。

```ini
[Unit]
Description=CampusFly anonymous sync service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/ethanwwy/projects/campusfly-service
EnvironmentFile=/home/ethanwwy/.local/share/campusfly-management/service.env
ExecStart=/usr/bin/python3 /home/ethanwwy/projects/campusfly-service/app.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/ethanwwy/.local/share/campusfly-management

[Install]
WantedBy=default.target
```

```sh
mkdir -p /home/ethanwwy/.config/systemd/user
mkdir -p /home/ethanwwy/.local/share/campusfly-management
chmod 700 /home/ethanwwy/.local/share/campusfly-management
chmod 600 /home/ethanwwy/.local/share/campusfly-management/service.env
systemctl --user daemon-reload
systemctl --user enable --now campusfly-management.service
```

将 unit 保存为 `~/.config/systemd/user/campusfly-management.service`，并按需执行 `loginctl enable-linger ethanwwy` 让用户服务在无交互登录时继续运行。若使用 `ExecStart=/usr/bin/python3 /home/ethanwwy/projects/campusfly-service/app.py`，该文件应是本服务的 `server/app.py` 部署副本。

反向代理只转发 `/campusfly/api/v1/sync` 和 `/campusfly/admin` 到 `127.0.0.1:8791`，静态安装包仍由静态站点服务。代理必须使用 HTTPS；管理 POST 要保留浏览器的 `Origin: https://998223.xyz`。不要为此服务启用包含请求地址的访问日志。

## 测试

在 `release/SJTU-CampusFly` 下运行：

```sh
python3 -m unittest discover -s server -p 'test_*.py' -v
```

测试覆盖安装注册和 SQLite 重启后的持久性、事件幂等、错误 token、输入限制、Basic Auth、精确 Origin、CSRF nonce、全局/单安装禁用状态、公告发布/编辑/撤回与纯文本转义，以及静默请求日志。
