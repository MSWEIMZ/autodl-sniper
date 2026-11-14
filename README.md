# autodl-sniper

AutoDL GPU 空闲监控 & 抢卡工具：盯住想要的卡型，空卡一出立即响铃 / 推送 / 自动下单，还能把已关机的实例克隆到任意兼容机器上。

纯 Python 标准库实现，零依赖、单文件后端 + 单文件网页，Windows 双击即用。

![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

<!-- 界面截图：把你的面板截图保存为 docs/screenshot.png 后取消下一行注释 -->
<!-- ![界面预览](docs/screenshot.png) -->

## 功能

**市场监控**
- 卡型多选 + 区域筛选 + 每台至少空闲卡数 + 最高单价过滤
- 每台候选机器二次在线校验，过滤"搜索缓存里有、实际已下线"的幽灵空闲
- 空闲汇总面板：各卡型空闲卡数 / 机器数 / 最低价，实时刷新

**报警通知**
- 响铃（时长可调 / 可持续响到手动关闭）+ 浏览器桌面通知 + GET Webhook（`{msg}` 占位符，兼容 Bark / 企业微信等）
- 报警模式：卡型"从无到有"才报 或 每台新空闲机器都报；冷却时间可调
- 发现空闲可自动打开官网创建页

**自动下单（默认关闭，只报警不扣费）**
- 新建实例：基础镜像 / 私有镜像，按量 / 包日 / 包周 / 包月，数据盘扩容，自定义实例名
- 克隆实例：把已关机源实例克隆到**任意兼容机器**（可不同卡型），自动校验 CPU 架构、芯片厂商、CUDA 上限、数据盘容量，环境随盘带走
- 限定时间段开抢、抢到即停、失败原因中文日志

**关机实例开机监控**
- 关机实例想开回时宿主机要有空卡：可勾选监控任意实例，空出卡即提醒
- 可选"可开机自动开机"（立即计费）

**Token 管理**
- 自动导入：扫描本机浏览器 Local Storage 中 autodl.com 的登录 token，验证有效后自动接回；token 失效后自动监测新登录态
- 备选：书签一键导入 / 控制台一行命令 / 手动粘贴
- 子账号自动适配（`api/v1/sub_user` 改写）；无权限时给出中文提示

## 快速开始

需要 Python 3.8+（无任何第三方依赖）和自己的 AutoDL 账号。

```bash
git clone https://github.com/MSWEIMZ/autodl-sniper.git
cd autodl-sniper
python autodl_monitor.py      # 或 Windows 双击 启动监控.bat / start.bat
```

浏览器打开 <http://127.0.0.1:8917>。

首次使用会自动弹出"连接 AutoDL 账号"引导：

1. 点 **去 AutoDL 登录**，登录你的账号
2. 回到面板点 **⚡ 立即读取浏览器 Token**，工具自动从本机浏览器接回 token
3. 选卡型 → 点 **开始监控**

如果自动读取失败（部分浏览器把 Local Storage 压缩存储），引导里还有书签导入和手动粘贴两种方式。挂机场景推荐用 AutoDL 控制台 **设置 → 开发者 Token** 生成长期有效的 token 直接粘贴。

## 使用说明

| 页面 | 作用 |
| --- | --- |
| 监控面板 | 各卡型空闲概览、通过实时校验的可抢机器列表（去创建 / 克隆）、运行日志 |
| 抢新卡 | 盯哪些卡、什么条件触发、触发后做什么（报警 / 开页面 / 自动下单） |
| 我的实例 | 实例状态与宿主机空闲卡数；☑监控 = 可开机提醒对象；◉克隆源 = 克隆下单用哪台的盘；手动开机 |
| 设置 | 声音 / Webhook / 报警模式；手动粘贴 token |

## 配置说明

所有配置在网页里改即生效并写入 `config.json`（该文件含 token，已被 `.gitignore` 排除）。字段含义见 `config.example.json`，常用项：

| 字段 | 说明 |
| --- | --- |
| `interval` | 轮询间隔秒数，建议 ≥3，过快会被限流（429 自动退避） |
| `gpu_names` / `region_sign_list` | 监控的卡型 / 区域，空数组 = 不限 |
| `req_gpu_amount` / `max_price` | 每台至少空闲卡数 / 最高单价（0 = 不限） |
| `auto_create` | **自动下单开关，默认 false**。开了才会真实扣费 |
| `grab_mode` | `image` 新建镜像实例 / `clone` 克隆已有实例 |
| `grab_window_enable` + `grab_start`/`grab_end` | 只在时间段内开抢，窗口外仅监控 |
| `auto_stop_after_create` | 抢到一台后自动关掉自动下单（防止连抢） |
| `watch_boot` / `watch_boot_uuids` | 关机实例宿主机空卡监控；`null` = 监控全部关机实例 |
| `auto_power_on` | 可开机时自动开机（立即计费，谨慎） |
| `alert_mode` / `alert_cooldown` / `alarm_duration` | 报警策略 |
| `webhook_url` | 通知地址，`{msg}` 会被替换成报警内容 |

## 安全与隐私

- 面板**只监听 127.0.0.1**，不要把端口转发到局域网或公网
- Token 只保存在本机 `config.json`（明文），不会上传到任何第三方；接口下发给前端时永远脱敏
- "自动导入 token"功能会**遍历本机浏览器的 Local Storage leveldb 文件**，只匹配 `autodl.com` 相关记录并校验有效性，相关代码见 `autodl_monitor.py` 中 `_scan_leveldb_tokens`，可自行审计；不想用可完全不用，手动粘贴即可
- 服务端对跨站请求做了 Origin 白名单，其他网站无法跨域调用本地面板
- **Token 泄露处理**：在 AutoDL 控制台重新生成开发者 Token，旧 token 立即失效

## 风险与免责声明

- 本项目是**非官方**工具，与 AutoDL 无任何关联；接口字段来自对官网行为的观察，官方变更可能导致功能失效
- **自动下单、克隆、自动开机会产生真实扣费**。默认 `auto_create=false`、`auto_power_on=false` 时只监控报警，不产生任何费用；开启前请确认计费方式、镜像与数据盘配置无误
- 请合理设置轮询间隔，遵守 AutoDL 服务条款，不要对官网造成访问压力
- 使用本工具产生的费用、账号风险由使用者自行承担；仅供学习交流

## 常见问题

**提示 token 失效 / 弹出连接引导？**
重新登录 autodl.com，工具约 15 秒内自动接回新 token。频繁失效建议换控制台"开发者 Token"。

**子账号能用吗？**
能。请求会自动改写为 `sub_user` 路径；若子账号没有算力市场权限，面板会提示换有权限的账号。

**为什么日志说"过滤 N 台幽灵空闲"？**
官网市场列表有缓存，部分显示空闲的机器实际已下线；工具会逐台做在线校验，只保留确认可创建的。

**抢到了但没停 / 没抢到？**
"抢到即停"成功后自动关闭自动下单；创建结果横幅绿色为成功、红色为失败，日志里有具体原因（余额不足、未实名、机器被抢先等）。

**能开多个窗口 / 多开吗？**
不能也不需要。启动时会探测端口，已有进程在跑会拒绝重复启动。

## 技术说明

```
autodl_monitor.py   # 后端：轮询循环 + AutoDL API 封装 + HTTP 面板服务（纯标准库）
monitor.html        # 前端：监控面板 UI（原生 JS，无构建）
启动监控.bat        # Windows 一键启动（另有 start.bat / start.sh）
config.example.json # 配置模板；运行时生成 config.json（已 gitignore）
```

- 端口固定 `127.0.0.1:8917`；接口为同进程内 `GET/POST /api/*`
- 兼容子账号 token；克隆下单会自动做兼容性检查
- 无数据库，配置即 `config.json`

## License

[MIT](LICENSE)

觉得有用欢迎 ⭐Star；有建议或 bug 欢迎提 Issue。
