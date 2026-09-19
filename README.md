# SJTU校园飞

<img src="assets/campusfly.png" width="128" alt="SJTU校园飞图标">

上海交通大学闵行校区路线编辑桌面应用，使用 Python、PySide6 与腾讯地图。GitHub 仓库名为 **SJTU-CampusFly**。

创作者：**殇霞与夕夏**。

## 功能

- 腾讯校园底图与真实步行查询形成的局部道路图，当前为 370 个节点、433 条边。
- 道路画笔吸附节点，预览并沿已知连接道路续画；保留自由手绘模式。
- 按大致距离规划开放路线；闭合路线支持圈数展开。
- 路线 JSON 保存、载入、撤销、轨迹预览及离线模拟上传。
- 用户 ID、Keepalive、JSESSIONID 和腾讯 Key 填写后自动保存在本机，重新打开时恢复；无需先绘制路线或点击保存配置。
- Keepalive / JSESSIONID 输入区提供交大体育网页链接。
- 采用审核通过的飞翼路线图标，Windows 应用名称为“SJTU校园飞”。

## Windows 安装

在 [Releases](https://github.com/Wangwy-sjtu/SJTU-CampusFly/releases) 下载对应版本的 Windows x64 安装包，运行安装。安装包包含 Python 与 Qt 运行环境，无需另外安装 Python。适用于 Windows 10/11 x64。

安装位置默认是当前用户的 `%LOCALAPPDATA%\Programs\SJTU-CampusFly`，不需要管理员权限。配置与路线保存在 `%LOCALAPPDATA%\SJTU-CampusFly`，升级或卸载程序不会主动删除这些用户文件。

安装包不附带个人 Cookie、用户 ID 或腾讯地图 Key。首次使用，在右侧填写自己的腾讯 Key 并应用；没有 Key 时显示离线坐标网格。默认上传模式为离线模拟。真实接口流程需自行配置账户凭据；接口成功码不代表成绩已入账，请在学校端核实。

## 统计与服务管理（1.1.0 起）

应用在后台联系 `https://998223.xyz/campusfly/api/v1/sync`，发送随机生成的安装编号、应用版本，以及安装、真实提交尝试、学校接口成功码事件。不会向管理服务发送学校账户、Cookie、腾讯 Key、路线、硬件标识或公网 IP。安装编号是本机持久保存的随机值，不是硬件指纹；清除用户数据后会被视为新安装。联网前的安装无法计入，因此后台显示的是已联网登记的安装数量。

管理员可按安装编号或全局暂停服务。收到明确停用状态时，应用显示停用页面并停止尚未提交的任务；无法撤回已经发出的请求。网络故障保留上次有效状态，不会因为超时自动停用；离线时也无法即时收到新的停用或恢复指令。公开源码中的这一机制属于应用服务管理，不能作为防篡改授权系统。

学校接口返回成功码不代表学校成绩已入账，后台分别展示提交尝试与成功码数量。本地模拟不计入提交次数。应用不弹出统计确认窗口；可在“帮助 / 关于”查看说明及安装编号。管理数据库及凭据不位于公共下载目录，统计服务不记录 IP 访问日志；网络连接本身仍会经过域名托管与网络服务商。

服务端部署与管理员使用见 [server/README.md](server/README.md)。

管理员还可发布或撤下应急公告。软件启动联网检查时显示可关闭的纯文本弹窗；同一条公告在每个安装上只提示一次，发布新公告后再提示。已展示的公告编号只保存在本机，不会上报阅读记录。关闭公告不影响正常使用，停用状态由独立的服务开关决定。

## 源码运行

建议 Python 3.11–3.13；本次构建使用 Windows x64 / Python 3.13.5。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe qtui.py
```

也可运行 `run_app.ps1`。源码模式的私人配置保存在项目 `configs/`，路线默认保存在 `routes/`；这两个目录均不提交。

## 测试与构建

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node tests/test_map_runtime.js
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m PyInstaller SJTU-CampusFly.spec --noconfirm
ISCC.exe packaging\installer.iss
```

`ISCC.exe` 来自 [Inno Setup](https://jrsoftware.org/isdl.php)。构建输出分别在 `dist/SJTU-CampusFly/` 和 `installer-output/`。Qt 库保持独立文件，不使用每次启动都解压全部资源的单文件运行方式。

## 目录

| 路径 | 内容 |
| --- | --- |
| `qtui.py` | 桌面界面与线程协调 |
| `src/` | 路线模型、图搜索、采样、配置和接口 |
| `assets/` | 地图页面、帮助、正式图标 |
| `data/` | 应用使用的局部候选路网 |
| `tests/` | Python 与地图 JavaScript 回归测试 |
| `packaging/` | Windows 安装脚本 |
| `docs/` | 构建验证记录 |

## 数据与来源

路网来自腾讯步行接口返回的几何，使用 GCJ-02，保留来源记录。不代表校园所有小路均已覆盖；不可达节点之间不会自动补直线。腾讯地图与道路数据的权利归相应权利人。

本项目从用户提供的 `SJTURunningMan-Stable` 源码包重构而来。原包未附独立 LICENSE，本仓库不替上游授予新的许可；公开可见不等同于授予无限制再分发权。依赖及来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

账户与地图凭据仅保存在本机 `configs/credentials.local.json`，停止输入 0.5 秒、离开输入框或关闭窗口时自动保存。清空字段也会被记住。记录会一直保留到用户修改或删除；学校签发的 Cookie 仍可能过期，届时需重新获取。
