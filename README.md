# SJTU校园飞

<img src="assets/campusfly.png" width="128" alt="SJTU校园飞图标">

上海交通大学闵行校区路线编辑桌面应用，使用 Python、PySide6 与腾讯地图。GitHub 仓库名为 **SJTU-CampusFly**。

## 功能

- 腾讯校园底图与真实步行查询形成的局部道路图，当前为 370 个节点、433 条边。
- 道路画笔吸附节点，预览并沿已知连接道路续画；保留自由手绘模式。
- 按大致距离规划开放路线；闭合路线支持圈数展开。
- 路线 JSON 保存、载入、撤销、轨迹预览及离线模拟上传。
- Keepalive / JSESSIONID 输入区提供交大体育网页链接。
- 采用审核通过的飞翼路线图标，Windows 应用名称为“SJTU校园飞”。

## Windows 安装

在 [Releases](https://github.com/Wangwy-sjtu/SJTU-CampusFly/releases) 下载 `SJTU-CampusFly-1.0.0-Setup-x64.exe`，运行安装。安装包包含 Python 与 Qt 运行环境，无需另外安装 Python。适用于 Windows 10/11 x64。

安装位置默认是当前用户的 `%LOCALAPPDATA%\Programs\SJTU-CampusFly`，不需要管理员权限。配置与路线保存在 `%LOCALAPPDATA%\SJTU-CampusFly`，升级或卸载程序不会主动删除这些用户文件。

安装包不附带个人 Cookie、用户 ID 或腾讯地图 Key。首次使用，在右侧填写自己的腾讯 Key 并应用；没有 Key 时显示离线坐标网格。默认上传模式为离线模拟。真实接口流程需自行配置账户凭据，其服务端接受结果尚未验证。

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
