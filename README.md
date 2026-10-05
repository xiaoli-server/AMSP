# MC Server Panel (Termux 版)

> 面向初学者的 Minecraft 服务器图形化管理面板，在手机 Termux 上即可运行。
> 无需懂命令行、无需手动编辑配置文件，浏览器点点点就能创建、启动、管理你的 MC 服务器。

**由 [xiaoli Studio](https://xiaolistudio.com.cn) 出品** · 官网：[xiaolistudio.com.cn](https://xiaolistudio.com.cn)

---

## ✨ 功能特性

- **10 种服务端核心一键接入**：自动获取版本列表与官方下载链接，下载后自动创建实例
- **图形化新建实例向导**：选核心 → 选版本 → 填参数 → 自动生成 `eula.txt` / `server.properties` / 启动脚本
- **网页控制台**：实时滚动日志（stdout + stderr 双管道，不卡死）、在线发送服务器指令
- **一键启停**：启动 / 停止都带明确弹窗反馈；停止会等待服务器保存世界并真正退出
- **实例管理**：列表页实时显示内存 / 磁盘占用，支持一键删除实例（含存档文件夹，带二次确认）
- **资源市场**：内置 Modrinth 搜索，下载 Mod / 数据包 / 插件 带实时进度
- **Forge 自动安装**：下载后自动执行 `--installServer` 并生成启动脚本
- **稳定性**：数据库自动迁移（旧版本自动补字段）、全局异常统一返回 JSON、后台下载不阻塞

## 🧱 支持的服务器核心

| 核心 | 类型 | 说明 |
|---|---|---|
| Paper | 服务端 | 原版优化，插件兼容，社区最主流 |
| Folia | 服务端 | Paper 的分区多线程分支 |
| Purpur | 服务端 | Paper 增强，更多自定义选项 |
| Spigot | 服务端 | 经典原版插件服务端 |
| Fabric | 模组服务端 | 轻量模组加载器 |
| Forge | 模组服务端 | 经典模组加载器（自动安装） |
| Velocity | 代理端 | 新一代代理，性能强 |
| Waterfall | 代理端 | BungeeCord 的社区分支 |
| BungeeCord | 代理端 | 经典群组服代理 |
| Travertine | 代理端 | 支持 1.7–1.12 旧版本的 Waterfall 分支 |

## 📦 快速开始（Termux）

### 方式一：一键安装脚本（推荐）

在官网 [xiaolistudio.com.cn](https://xiaolistudio.com.cn) 下载 **AMSP.sh**，然后：

```bash
bash AMSP.sh
```

脚本会自动完成：安装依赖（Python / OpenJDK 等）→ 拉取代码 → 启动面板，全程无需手动操作。

### 方式二：手动安装

```bash
# 1. 安装依赖
pkg update -y && pkg upgrade -y
pkg install -y python openjdk-17
pip install flask flask-cors requests cachetools

# 2. 获取代码
git clone https://github.com/<你的用户名>/<仓库名>.git
cd <仓库名>

# 3. 启动面板
python3 app.py
```

浏览器打开 **http://127.0.0.1:5000** 即可开始使用。

> 手机自带浏览器直接访问即可；同一 Wi-Fi 下电脑访问 `http://<手机IP>:5000` 也可以（手机需与电脑同网段）。

## 🎮 使用说明

1. **新建实例**：点击「新建实例」→ 选择核心 → 选择版本 → 填写实例名 / 端口 / 内存 → 创建
   - 核心下载与解压自动完成，Forge 会自动执行安装，耐心等进度提示即可
2. **启动**：回到实例列表 → 进入控制台 → 点「启动」
   - 日志开始滚动即启动成功；首次启动生成世界需要一点时间，看到 `Done` 即可进服
3. **停止**：点「停止」→ 等待弹窗提示「实例已停止」（服务器保存世界需要几秒，属正常）
4. **删除**：列表页点红色「删除」→ 确认后记录与文件夹一并清除（不可恢复，慎用）
5. **下载资源**：资源市场页搜索 Modrinth 内容，点击下载，进度条实时显示

## 📁 项目结构

```
.
├── app.py                  # 主程序（单文件，包含全部后端逻辑）
├── templates/
│   ├── index.html          # 实例列表页（含删除）
│   ├── create_instance.html# 新建实例向导
│   ├── instance.html       # 实例控制台（日志/命令）
│   ├── market.html         # 资源市场
│   ├── dp_download.html    # 数据包下载
│   └── plugin_download.html# 插件下载
├── papermc_fetch.py        # 独立下载脚本（可选，纯标准库）
└── mc_server/              # 实例目录（运行后自动生成，可删）
```

## ❓ 常见问题（FAQ）

**Q：控制台没有日志 / 实例显示运行中但页面空白？**
A：通常是因为面板重启过，旧的 Java 进程成了"孤儿进程"（日志线程已断开）。执行 `pkill -f java` 杀掉旧进程，然后在面板里重新点「启动」即可。

**Q：启动时报 `no such column: pid`？**
A：旧版本的数据库缺字段。新版会自动迁移补上，只需重启面板（启动时会打印 `[迁移] instances 表已补上 pid 列`），无需手动操作。

**Q：提示端口被占用 / 启动失败？**
A：`pkill -f java` 清掉残留进程后重试。

**Q：一定要装 Java 吗？**
A：是的。运行服务器需要 OpenJDK：MC 1.17+ 用 `openjdk-17`；老版本（1.12 以下）建议 `openjdk-8` / `openjdk-11`。

**Q：Forge 创建实例很慢？**
A：Forge 是安装器，首次需下载依赖库（几百 MB），耐心等待进度条即可，超时（>10 分钟）会明确报错。

**Q：这个面板能公网访问吗？**
A：默认只监听本机。如需公网，请自行配置反向代理 / 内网穿透，并务必修改 `API_TOKEN`（代码中的 `MySecretMCAPI2026` 为演示值）。

## 🔗 数据源

| 数据 | 来源 |
|---|---|
| Paper 系核心 | fill.papermc.io API v3 |
| Purpur | api.purpurmc.org |
| Spigot | getbukkit.org |
| Fabric | meta.fabricmc.net |
| Forge | files.minecraftforge.net / maven.minecraftforge.net |
| BungeeCord | ci.md-5.net (Jenkins) |
| Mod / 插件 | Modrinth API v2 |

## ⚠️ 免责声明

- 本项目仅用于学习交流与个人使用；请遵守 Minecraft EULA 与各服务端的使用条款
- 面板内下载的资源版权归原作者所有，请在合法范围内使用
- 公网部署安全风险自负

## 📄 License

MIT
