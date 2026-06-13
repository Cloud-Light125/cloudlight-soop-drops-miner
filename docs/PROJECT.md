# SOOP Drops Miner — 项目文档

> 版本：v1.0.1 · by www5329  
> 本文档描述项目架构、模块职责、数据流与实现细节，供开发者阅读与维护。  
> 用户使用说明见 [README.md](../README.md)。

---

## 目录

1. [项目概述](#1-项目概述)
2. [与上游仓库的关系](#2-与上游仓库的关系)
3. [系统架构](#3-系统架构)
4. [目录结构](#4-目录结构)
5. [模块说明](#5-模块说明)
6. [数据模型](#6-数据模型)
7. [核心业务流程](#7-核心业务流程)
8. [外部 API 与协议](#8-外部-api-与协议)
9. [直播间选择策略](#9-直播间选择策略)
10. [任务类型与状态判定](#10-任务类型与状态判定)
11. [多账号并行架构](#11-多账号并行架构)
12. [图形界面设计](#12-图形界面设计)
13. [认证与本地存储](#13-认证与本地存储)
14. [配置与定时参数](#14-配置与定时参数)
15. [入口与运行方式](#15-入口与运行方式)
16. [构建与发布](#16-构建与发布)
17. [开发辅助工具 soop_tools](#17-开发辅助工具-soop_tools)
18. [扩展与维护建议](#18-扩展与维护建议)
19. [免责声明](#19-免责声明)

---

## 1. 项目概述

**SOOP Drops Miner** 是针对 [SOOP Live](https://www.sooplive.com)（原 AfreecaTV 国际版）掉宝（Drops）活动的第三方挂机工具。

### 设计目标

- 在**不下载实际音视频流**的前提下，模拟官方播放器行为，使账号观看时长计入掉宝任务进度。
- 支持**多账号并行**挂机，统一图形界面管理与奖励背包汇总。
- 区分**固定型 / 抽奖型 / 随机型**三种掉宝，智能或手动选台，并明确 **同一时间只能挂一个直播间** 的限制。
- 启动时**先进房再拉任务**（`channel_first`），适配「进房后 mission 才出现任务」的官方行为。
- 网络异常时**自动恢复 HTTP 会话**，避免单次 gather 超时导致整账号退出。

### 技术栈

| 类别 | 选型 |
|------|------|
| 语言 | Python 3.10+ |
| 异步 HTTP | aiohttp |
| WebSocket | websockets |
| GUI | tkinter（标准库） |
| 打包 | PyInstaller（单文件 exe） |

### 依赖

```
aiohttp>=3.9.0
websockets>=12.0
yarl>=1.9.0
```

---

## 2. 与上游仓库的关系

本仓库根目录 `TwitchDropsMiner-master` 源自开源项目 [Twitch Drops Miner](https://github.com/DevilXD/TwitchDropsMiner)（Twitch 掉宝挂机）。

`soop_miner/` 是**独立子项目**，面向 SOOP Live 平台：

- 拥有独立的模块、API 对接与 GUI，**不依赖** Twitch 相关代码运行。
- 可单独开源、单独打包为 `SOOP_Drops_Miner.exe`。
- 上级目录 `soop_tools/` 为本地逆向/抓包辅助脚本，**不纳入 Git 仓库**，不参与运行时。

---

## 3. 系统架构

### 3.1 逻辑分层

```
┌─────────────────────────────────────────────────────────────┐
│  表现层   gui.py          Tkinter 多账号界面、进度、背包      │
├─────────────────────────────────────────────────────────────┤
│  编排层   multi_miner.py  多账号 SoopMiner 生命周期管理       │
├─────────────────────────────────────────────────────────────┤
│  核心层   miner.py        挂机主循环、状态机、任务/背包轮询   │
├─────────────────────────────────────────────────────────────┤
│  协议层   center.py       Bridge WebSocket 进房               │
│           watch.py        CSTATUS 观看心跳 (gather)          │
│           drops.py        Drops REST API 封装                │
│           channel.py      直播间发现与选择策略                │
│           auth.py         登录与会话持久化                    │
├─────────────────────────────────────────────────────────────┤
│  基础层   models.py       领域数据模型                        │
│           constants.py    URL、间隔、路径等常量                 │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 运行时线程模型

| 线程 | 职责 |
|------|------|
| **主线程** | Tkinter 事件循环，UI 渲染与用户交互 |
| **Miner 线程** | 独立 `asyncio` 事件循环，运行 `MultiMinerManager` 与所有 `SoopMiner` |
| **GUI 后台线程** | 添加账号、拉取背包、刷新直播间列表、智能选台预览等短时异步任务 |

GUI 通过 `root.after(0, ...)` 将 `MinerState` 回调切回主线程更新界面，避免跨线程直接操作 Tk 组件。

### 3.3 端到端数据流

```
登录/加载 cookies
       │
       ▼
ensure_drops_ready() ──► 自动开启 Drops 开关 + 预热 /mission、/event
       │
       ├─ get_drops_event_list.php ──► DropEvent 活动目录（/event）
       └─ get_drops_mission_list.php ──► Mission 已参与任务（/mission）
       │
       ▼
pick_channel(mode=smart|manual|owesports) ──► LiveChannel
       │
       ├─ channel_first：先进房 ──► 再拉 mission ──► 再 sync 选台
       │
       ├──────────────────────────────┐
       ▼                              ▼
BridgeSession.connect()      WatchHeartbeat.send()
  player_live_api                  exlogcollector gather
  wss://bridge.sooplive.com        (每 5 秒，带 HTTP 超时)
       │
       ▼
_poll_loop (60s 任务 / 120s 背包)
       │
       ├─► view_time 进度更新（按任务分轨检测停滞）
       ├─► 网络异常 → _recover_network() 重建 session
       └─► 自动 claim_item + 背包同步
       │
       ▼
MinerState ──on_state──► GUI 账号树 / 单类型进度面板 / 日志
```

---

## 4. 目录结构

```
soop_miner/
├── entry.py              # PyInstaller 与开发统一入口
├── __main__.py           # CLI/GUI 参数路由
├── __init__.py
├── constants.py          # 全局常量
├── models.py             # 数据模型
├── auth.py               # 登录、cookies、多账号目录
├── drops.py              # Drops API 客户端
├── channel.py            # 直播间策略与搜索
├── center.py             # Bridge WebSocket
├── watch.py              # 观看心跳
├── stream.py             # HLS 探测（实验性，主流程未使用）
├── miner.py              # 单账号挂机核心
├── multi_miner.py        # 多账号管理器
├── gui.py                # 图形界面
├── single_instance.py    # Windows 单实例
├── requirements.txt
├── README.md             # 用户使用说明
├── docs/
│   └── PROJECT.md        # 本文档
├── build.spec            # PyInstaller 配置
├── build.bat             # Windows 打包脚本
├── run.bat               # 开发启动脚本
├── init_git.bat
├── .gitignore
│
├── accounts/             # 运行时生成（.gitignore，勿提交）
│   └── <userid>/
│       └── cookies.json
├── cookies.json          # 旧版单账号（.gitignore）
└── .disclaimer_accepted  # 首次免责确认（.gitignore）

上级目录（开发/打包时，多数不在 Git 仓库内）：
├── dist/SOOP_Drops_Miner.exe   # .gitignore
├── build/                      # .gitignore
└── soop_tools/                 # 开发辅助脚本，本地保留，勿提交
```

### 4.1 版本控制与隐私边界

开源仓库**只跟踪** `soop_miner/` 内的正式源码、文档与打包配置。以下内容必须在 `.gitignore` 中排除，且不得 push：

| 类别 | 路径示例 | 说明 |
|------|----------|------|
| 账号会话 | `accounts/`、`cookies.json` | 含 AuthTicket 等，属个人隐私 |
| 运行时标记 | `.disclaimer_accepted` | 本地 UI 状态 |
| 抓包产物 | `*.har`、`*_probe/`、`*_dump.json` | 可能含 Cookie 与 API 原始响应 |
| 构建输出 | `build/`、`dist/`、`*.exe` | 可本地重新生成 |
| 开发辅助 | `soop_tools/` | 逆向/探测脚本，非发布物 |

`init_git.bat` 在创建初始提交前会检查暂存区是否误含上述路径，若命中则中止提交。

---

## 5. 模块说明

### 5.1 `entry.py`

打包 exe 与 `python -m soop_miner` 的共用入口。无命令行参数时自动追加 `--gui`，再调用 `__main__.main()`。

### 5.2 `__main__.py`

| 参数 | 说明 |
|------|------|
| `--gui` | 启动图形界面（默认） |
| `--cli` | 命令行模式 |
| `--userid` / `--password` | CLI 登录或补充账号 |
| `-v` / `--verbose` | DEBUG 日志（CLI） |

规则：`use_gui = args.gui or not args.cli`；仅当显式 `--cli` 且提供账号时使用 CLI。

### 5.3 `constants.py`

集中管理 URL、轮询间隔、HTTP 超时、应用元数据、免责文案、`DATA_DIR` 解析逻辑。

`DATA_DIR` 规则：

- **打包后**：exe 所在目录
- **源码运行**：`soop_miner/` 目录

关键 URL：`DROPS_MISSION_URL`（/mission）、`DROPS_EVENT_URL`（/event）。

### 5.4 `models.py`

领域模型与日期解析，详见 [第 6 节](#6-数据模型)。

### 5.5 `auth.py`

| 函数 | 作用 |
|------|------|
| `login(userid, password)` | POST 登录，校验 `AuthTicket` / `BbsTicket` |
| `save_cookies` / `load_cookies` | 单账号读写 |
| `load_all_cookies()` | 加载 `accounts/` 下全部账号 |
| `list_accounts()` | 返回已保存账号 ID 列表 |
| `remove_account(userid)` | 删除账号目录 |
| `migrate_legacy_cookies()` | `cookies.json` → `accounts/<uid>/` |
| `apply_cookies(session)` | 注入 aiohttp CookieJar（多域名） |
| `cookie_header(cookies)` | 拼 HTTP `Cookie` 头 |
| `userid_from_cookies(cookies)` | 从 `BbsTicket` / `UserTicket` 解析 uid |

### 5.6 `drops.py` — `DropsClient`

| 方法 | API | 说明 |
|------|-----|------|
| `get_missions()` | `GET get_drops_mission_list.php` | 已参与任务 → `list[Mission]` |
| `get_events()` / `get_progress_events()` | `POST get_drops_event_list.php` | 活动目录 → `list[DropEvent]` |
| `get_inventory()` | `POST get_drops_list.php` | 分页背包 |
| `get_item_detail()` | `POST get_drops_use_info.php` | 物品详情 / 兑换码 |
| `claim_item()` | 同上 | 领取可领物品 |
| `ensure_drops_ready()` | 开关 + GET /mission、/event | 自动开启 Drops 并预热会话 |
| `empty_missions_hint()` | — | mission 为空时的用户提示文案 |
| `progress_events_summary()` | — | 活动页进行中活动摘要 |

认证失败：`HTTP 401` 或 `result == -1` → `RuntimeError("Drops API 认证失败")`。

### 5.7 `channel.py`

| 函数/类 | 作用 |
|---------|------|
| `ChannelConfig` | 选台策略：`mode`（smart / manual / owesports）、`priority_mission_id`、`hang_without_missions` |
| `PRIORITY_MISSION_AUTO` / `ONE_STREAM_NOTICE` | 优先任务常量与「同一时间只能挂一台」说明文案 |
| `parse_stream_input()` | 解析 URL、bjid/bno、纯数字 ID |
| `fetch_live_channel()` | `player_live_api` 查询单频道是否开播 |
| `fetch_hashtag_drops_channels()` | `#드롭스` 标签搜索（sch.sooplive.com） |
| `fetch_live_drops_channels()` | 标签直播 + 任务官方频道补探测 |
| `pick_channel()` | 按 smart / manual / owesports **互斥**策略选台 |
| `_pick_for_active_missions()` | 智能选台：按优先任务匹配固定 → 抽奖 → 随机 |
| `_pick_manual_channel()` / `_pick_owesports_channel()` | 手动 / 仅官方台 |
| `is_category_fixed_mission()` | 分类固定型（无 broadIdList，需挂对应分类 #드롭스） |
| `fixed_category_channels()` | 为分类固定型在标签直播中找匹配频道 |
| `mission_progresses_on_channel()` | 判断某任务是否能在当前台累计 |
| `missions_for_channel()` | 当前台可累计的任务列表 |
| `manual_channel_mismatch_warnings()` | 手动选台分类不匹配时的警告（不阻止挂机） |
| `format_channel_preview()` | GUI「预计进入：…」预览文案 |
| `filter_missions_by_priority()` / `mission_pick_label()` | 优先任务过滤与下拉展示 |
| `channel_drops_type_tags()` / `format_channel_drops_label()` | 直播间列表 [固定/抽奖/随机] 标签 |
| `fixed_column_summary()` / `fixed_column_warning()` | 固定型状态摘要与红色警告 |

### 5.8 `center.py` — `BridgeSession`

SOOP 掉宝进度依赖 **Bridge WebSocket 进房会话**（非仅 HTTP 心跳）。

连接流程：

1. `POST player_live_api.php?bjid=` 获取 `GWIP/GWPT/CTIP/CTPT/BNO` 等
2. 连接 `wss://bridge.sooplive.com/Websocket/{bjid}`，子协议 `bridge`
3. 发送 `INIT_GW` → 等待 `FLASH_LOGIN`、`CERTTICKETEX`
4. 发送 `INIT_BROAD` → 等待 `JOINCH_COMMON`、`GETCHINFOEX`
5. 每 **20 秒** 发送 `{"SVC":"KEEPALIVE"}`

对外暴露：`broad_no`、`center_ip`、`center_port`（供 `WatchHeartbeat` 使用）、`is_connected`。

### 5.9 `watch.py` — `WatchHeartbeat`

模拟 `play.sooplive.com` 播放器的 **CSTATUS** 遥测：

- `POST https://exlogcollector.sooplive.com/gather`
- 字段以 `SEP = "\x06"` 分隔编码（`encode_a()`）
- MD5 签名字段：`sv|ns|ver|tm|ht|cs|uid|a|afreeca`
- `send()` 使用 `HTTP_CONNECT_TIMEOUT` / `HTTP_TOTAL_TIMEOUT`，避免无限挂起
- `switch_channel()` 在换台或 bridge 返回新 center 地址时重置计数与时间戳

### 5.10 `stream.py`

`head_latest_segment()` 解析 HLS m3u8 并对最新分片发 HEAD 请求。**当前 `miner.py` 未引用**，属实验/备用模块。

### 5.11 `miner.py` — `SoopMiner`

单账号挂机核心，主要方法：

| 方法 | 说明 |
|------|------|
| `run()` | 启动：`channel_first` 刷新任务 → 解析频道 → 并发心跳与轮询 |
| `_heartbeat_loop()` | 每 `HEARTBEAT_INTERVAL` 确保 bridge 并发送 gather；失败触发 `_recover_network` |
| `_poll_loop()` | 每 60s 刷新任务，每 120s 刷新背包并尝试领取 |
| `_refresh_missions(channel_first=…)` | 拉任务、换台、按任务分轨停滞检测 |
| `_sync_channel()` | `pick_channel()` + 手动模式 mismatch 警告 |
| `_recover_network()` | 关闭 bridge/session，重建 aiohttp，冷却 `NETWORK_RECOVER_COOLDOWN` |
| `_log_channel_missions()` | 记录本台可累计任务及需换台任务 + `ONE_STREAM_NOTICE` |
| `_ensure_bridge()` | 建立/维护 BridgeSession |
| `_try_claim()` | 自动领取 `can_claim` 物品 |
| `get_state()` / `_emit_state()` | 构造 `MinerState` 并回调 GUI |

**进度停滞检测**：按 `drops_idx` 分轨，连续 3 次任务轮询（约 3 分钟）`view_time` 无变化时输出警告，并区分抽奖/分类固定型的分类不匹配提示。

### 5.12 `multi_miner.py` — `MultiMinerManager`

| 方法 | 说明 |
|------|------|
| `start_all(cookies_map)` | 为每个账号 `start_account()` |
| `start_account(cookies)` | 创建 `SoopMiner`，`asyncio.create_task(_run_miner)` |
| `stop_all()` / `stop_account()` | 设置各 miner 的 `_stop` 事件 |
| `wait()` / `shutdown()` | 等待任务结束并关闭 session |

所有账号共享同一个 `ChannelConfig`（同一套直播间策略）。

### 5.13 `gui.py` — `SoopGui`

Tkinter 界面，职责包括：

- 账号 CRUD、全部开始/停止
- **三模式选台**：智能 / 手动 / 仅 owesports；智能模式优先任务下拉与「预计进入」预览
- **单类型任务进度**：根据当前/预计直播间只展示固定型、抽奖型或随机型之一
- 直播间列表 `[固定/抽奖/随机]` 类型标签
- 手动选台 mismatch 提示；开始前写日志 ⚠
- 日志面板、奖励背包汇总
- 异步任务调度与 `MinerState` 同步

`run_gui()` 在 Windows 下会先调用 `single_instance.ensure_single_instance_or_exit()`，避免重复双击 exe 多开。

### 5.14 `single_instance.py`

Windows 专用单实例控制：命名 Mutex + 按窗口标题查找已有 Tk 窗口并 `SetForegroundWindow`。非 Windows 平台直接放行。

---

## 6. 数据模型

### 6.1 `DropItem` — 任务档位

| 字段 | 含义 |
|------|------|
| `item_name` | 奖励名称 |
| `give_term` | 达标观看分钟数 |
| `view_time` | 当前累计分钟 |
| `percent` | API 返回进度百分比 |
| `mission_success` | 该档位是否已完成/中奖 |

属性 `term_label`：将分钟格式化为 `1h` / `45m` 等。

### 6.2 `LiveChannel` — 直播间

| 字段 | 含义 |
|------|------|
| `user_id` | 主播 ID（bjid） |
| `user_nick` | 显示昵称 |
| `broad_no` | 当前广播编号 |
| `on_air` | 是否在线 |
| `category_no` / `category_names` | 分类（抽奖型 / 分类固定型匹配用） |
| `has_drops` | 是否带掉宝标签 |

### 6.3 `Mission` — 掉宝任务（/mission 已参与）

| 字段 | API 键 | 说明 |
|------|--------|------|
| `drops_idx` | `dropsIdx` | 任务 ID |
| `give_con` | `giveCon` | `term`=固定型，`draw`=抽奖型，`none`=随机型 |
| `filter` | `filter` | `progress` / `completed` 等 |
| `live` | `live` | 是否处于可累计状态 |
| `start_date` / `end_date` | 日期字符串 | 活动时间窗 |
| `channels` | `broadIdList` | 关联官方频道（可为空 → 分类固定型） |
| `category_no` / `category_name` | 分类字段 | 抽奖型 / 分类固定型匹配用 |
| `items` | `itemList` | 档位列表 |

**类型属性：** `is_fixed` / `is_lottery` / `is_random` / `type_label` / `type_short`

**状态属性：**

```python
is_event_active   # filter == "progress" and live
is_event_ended    # not is_event_active
is_not_yet_open   # 已结束标记但 now < end_date
is_truly_ended    # now >= end_date 或无有效截止日期
```

`active_item()`：返回第一个 `mission_success=False` 的档位。

### 6.4 `DropEvent` — 活动目录（/event）

| 字段 | 说明 |
|------|------|
| `drops_idx` / `title` | 活动 ID 与标题 |
| `give_con` | 同 Mission，含 `none` 随机型 |
| `dup_flag` | 是否每人限一次 |
| `live` / `filter` | 进行中状态 |

用于 GUI 直播间类型推断与 mission 为空时的活动页摘要；**不替代** Mission 的进度档位。

### 6.5 `InventoryItem` — 背包物品

| 字段 | 说明 |
|------|------|
| `item_code_idx` | 物品索引 |
| `claimed` | `useFlag == "Y"` |
| `redeem_code` | 兑换码（detail API 填充） |
| `can_claim` | `not claimed` |

### 6.6 `MinerState` — 运行时快照（`miner.py`）

GUI 与日志使用的单账号状态：`uid`、`running`、`status`、当前频道、`bridge_connected`、`missions`、`inventory`、`available_channels`。

---

## 7. 核心业务流程

### 7.1 启动阶段（`SoopMiner.run()`）

```
1. ensure_drops_ready()（若尚未执行）
2. 记录选台模式标签（智能 / 手动 / 仅 owesports）
3. _refresh_missions(channel_first=True)
   ├─ 先进房（hang_without_missions）
   ├─ get_missions()
   ├─ 再次 pick_channel() 按任务精调
   └─ 无频道 → status="等待直播间"
4. _refresh_inventory() 首次拉背包
5. asyncio.gather(_heartbeat_loop(), _poll_loop())
```

### 7.2 心跳循环

```
while not stopped:
    if 有当前频道:
        if bridge 未连接 → _ensure_bridge()
        try: WatchHeartbeat.send()
        except: _recover_network("心跳")
    await sleep(HEARTBEAT_INTERVAL)  # 5s
```

### 7.3 轮询循环

```
while not stopped:
    每 60s → _refresh_missions()（异常 → _recover_network）
    每 120s → _try_claim() + _refresh_inventory()
    内层每 5s 检查停止信号
```

### 7.4 换台逻辑（`_sync_channel`）

- `user_id` 变化：关闭旧 bridge，新建 `WatchHeartbeat`，打可累计 / 需换台日志；**manual 模式**额外打 `manual_channel_mismatch_warnings`
- 仅 `broad_no` 变化：`switch_channel`
- bridge 连接成功后，用 `CTIP/CTPT` 更新心跳 center 地址

### 7.5 状态机（`_status_text()`）

| 状态 | 条件 |
|------|------|
| 已停止 | `_stop` 已设置 |
| 空闲 | 未 running |
| 等待直播间 | 无 `_current` |
| 连接中 / 重连中 | bridge 未就绪或网络恢复中 |
| 未开放掉宝 | 固定型均非 active 且存在 `is_not_yet_open` |
| 挂机中·固定型已结束 | 固定型结束但抽奖型仍 active |
| 活动已结束 | 相关任务均已结束 |
| 挂机中 | 正常运行 |
| 进房失败 | bridge 连接异常 |

---

## 8. 外部 API 与协议

### 8.1 HTTP 接口一览

| 用途 | URL | 方法 | 模块 |
|------|-----|------|------|
| 登录 | `login.sooplive.co.kr/app/LoginAction.php` | POST | auth |
| 任务列表 | `drops.sooplive.com/api/get_drops_mission_list.php` | GET | drops |
| 活动列表 | `drops.sooplive.com/api/get_drops_event_list.php` | POST | drops |
| 背包列表 | `drops.sooplive.com/api/get_drops_list.php` | POST | drops |
| 领取/详情 | `drops.sooplive.com/api/get_drops_use_info.php` | POST | drops |
| 频道信息 | `live.sooplive.co.kr/afreeca/player_live_api.php` | POST | channel, center |
| 标签搜索 session | `sch.sooplive.com/api.php?m=stopWord` | GET | channel |
| 标签直播搜索 | `sch.sooplive.com/api.php?m=liveSearch` | GET | channel |
| 观看心跳 | `exlogcollector.sooplive.com/gather` | POST | watch |
| 流分配（未接入主流程） | `livestream-manager.sooplive.com/broad_stream_assign.html` | GET | center |

### 8.2 标签搜索参数要点

`fetch_hashtag_drops_channels()` 流程：

1. `m=stopWord` 获取 `sessionKey`
2. `m=liveSearch`，`szKeyword=드롭스`，`location=drops`，`isHashSearch=1`
3. 解析 `REAL_BROAD`，映射 `broad_cate_no`、`category_tags`、`is_drops`

与官网 [#드롭스 直播搜索](https://www.sooplive.com/search?hash=hashtag&tagname=%EB%93%9C%EB%A1%AD%EC%8A%A4&hashtype=live&stype=hash&acttype=live&location=drops) 对齐。

### 8.3 Bridge WebSocket 消息（摘要）

| SVC | 方向 | 说明 |
|-----|------|------|
| `INIT_GW` | 发送 | 携带 gate/center/broadno/cookie/guid |
| `FLASH_LOGIN` / `CERTTICKETEX` | 接收 | 网关握手 |
| `INIT_BROAD` | 发送 | 进房 |
| `JOINCH_COMMON` / `GETCHINFOEX` | 接收 | 进房成功 |
| `KEEPALIVE` | 发送 | 每 20s 保活 |

### 8.4 观看心跳 payload（摘要）

`WatchHeartbeat` 构造类 CSTATUS 字段，包含 `bid`、`bno`、`cno`（分类）、`uniq_key`、`guid`、buffer 计数、center IP/端口等，POST 至 gather 端点。Referer 为 `play.sooplive.com/{bjid}/{broad_no}`。

---

## 9. 直播间选择策略

### 9.1 `ChannelConfig`

```python
@dataclass
class ChannelConfig:
    mode: str = "smart"              # smart | manual | owesports
    manual_input: str = ""           # 手动：URL / ID / 下拉选中 bjid/bno
    preferred_bjid: str = "owesports"
    priority_mission_id: str = "auto"  # auto 或 mission.drops_idx
    hang_without_missions: bool = True # 无 mission 也先进 #드롭스 房
```

### 9.2 `pick_channel()` 三种模式（互斥）

| mode | 行为 |
|------|------|
| **smart** | `_pick_for_active_missions()`：按 `priority_mission_id` 过滤；`auto` 时顺序 固定 → 抽奖 → 随机；匹配分类 / 官方台。仍无台且 `hang_without_missions` → 标签列表首个在线 |
| **manual** | 解析 `manual_input` 或列表首项；**不**做任务智能覆盖；分类不匹配仅 `manual_channel_mismatch_warnings()` |
| **owesports** | 仅 `fetch_live_channel(preferred_bjid)`；未开播返回 None，**不**自动切 hashtag |

### 9.3 智能选台 `_pick_for_active_missions`

1. `filter_missions_by_priority()` 得到目标任务集
2. 指定优先任务时：只为该任务 `_pick_mission_channel()`
3. `auto` 时：依次对 active 的 fixed / lottery / random 任务尝试选台
4. 固定型：官方 `broadIdList` 在线 → 否则 `fixed_category_channels()`（分类固定型）
5. 抽奖型：标签直播中 `channel_matches_mission_category()`
6. 随机型：官方台或分类匹配

### 9.4 固定型子类型

| 子类型 | 特征 | 选台依据 |
|--------|------|----------|
| **官方固定型** | `broadIdList` 非空 | 官方频道 ID 在线（如 owesports） |
| **分类固定型** | `broadIdList` 空，有 `category_no/name` | 对应游戏分类的 `#드롭스` 直播（如 이터널 리턴） |

### 9.5 同一时间一台

常量 `ONE_STREAM_NOTICE` 说明：仅与当前直播间分类/官方台匹配的任务会累计。GUI 智能预览会列出**本台不会累计**的其他任务；miner 日志在换台时输出「需换台或调整优先任务」。

---

## 10. 任务类型与状态判定

### 10.1 类型划分

| 类型 | `give_con` | 发放方式 | 常见展示页 |
|------|------------|----------|------------|
| 固定型 | `term` | 观看达标后自动发放 | mission |
| 抽奖型 | `draw` | 观看达标后参与抽奖 | mission |
| 随机型 | `none` | 观看期间随机发放 | 多在 event |

### 10.2 Mission vs Event

- **Event**（`get_drops_event_list.php`）：活动总览，含尚未加入 mission 的活动。
- **Mission**（`get_drops_mission_list.php`）：已参与、有 `itemList` 进度。需进 Drops 直播间后由服务端加入。

### 10.3 活动生命周期

```
官网 API: filter + live + 日期
         │
         ├─ filter=="progress" && live → 进行中 (is_event_active)
         │
         └─ 否则 → is_event_ended
                ├─ now < end_date → is_not_yet_open
                └─ now >= end_date → is_truly_ended
```

### 10.4 GUI 进度展示

- **单列、单类型**：`_missions_for_current_display()` 根据当前/预计直播间或优先任务，只渲染固定 / 抽奖 / 随机之一。
- **标题**：`任务进度 · {类型}（选中账号）`。
- **警告**：当前类型为固定型时使用 `fixed_column_warning()`；手动选台用 `manual_channel_mismatch_warnings()`；等待直播间时提示无法累计。

档位状态文案（`_item_status()`）区分固定型与抽奖型/随机型的进行中、已完成、未开放等。

---

## 11. 多账号并行架构

```
MultiMinerManager
    │
    ├── SoopMiner(uid=A)  ──► 独立 session / bridge / heartbeat
    ├── SoopMiner(uid=B)  ──► 独立 session / bridge / heartbeat
    └── SoopMiner(uid=C)  ──► ...
              │
              └─ on_state(MinerState) ──► GUI 合并展示
```

| 维度 | 是否共享 |
|------|----------|
| `ChannelConfig`（直播间策略） | ✅ 共享 |
| cookies / session | ❌ 每账号独立 |
| 任务进度 `view_time` | ❌ 每账号独立 |
| 背包数据 | ❌ 分账号存储，GUI 汇总展示 |

CLI 模式（`run_miner()`）同样通过 `MultiMinerManager` 启动 `load_all_cookies()` 中的全部账号，并注册 SIGINT/SIGTERM 优雅停止。

---

## 12. 图形界面设计

### 12.1 窗口与布局

- 默认尺寸：**1080 × 920**，最小 **960 × 760**
- 顶部：应用名 + 版本 + 账号统计
- 底部：免责说明按钮 + `by www5329` + 版本
- Notebook 两页：**多账号挂机** / **奖励背包**

### 12.2 「多账号挂机」页结构

| 区域 | 内容 |
|------|------|
| 操作条 | 大号「全部开始」「全部停止」+ 状态提示 |
| 账号列表 | 添加/删除、Treeview（状态/进度/直播间） |
| 直播间 | `ONE_STREAM_NOTICE` 说明；智能 / 手动 / 仅 owesports；优先任务 + 预计进入预览；手动列表与链接 |
| 任务进度 | `PanedWindow` 上为**单类型**进度列，下为日志 |
| 日志 | `ScrolledText`，挂载 `SoopDropsMiner` logger |

挂机运行中，直播间列表每 **2 分钟**自动静默刷新（`_schedule_running_channel_refresh`）。

### 12.3 进度面板逻辑

```python
_effective_channel_for_progress()
  → 挂机中：MinerState 当前频道
  → 智能：_smart_preview_channel（异步 pick_channel 结果）
  → 手动：_local_manual_channel()
  → owesports：cached 中的 owesports

missions_for_channel() → 过滤出当前类型 → 渲染 mission 块
```

### 12.4 线程与异步边界

- **禁止**在 Miner 线程直接操作 Tk
- 所有 UI 更新经 `root.after(0, callback)` 派发到主线程
- 短时网络请求在独立 daemon 线程跑临时 `asyncio` 循环

### 12.5 首次运行

检测 `.disclaimer_accepted` 不存在时弹出免责说明模态框，用户同意后方可使用。

---

## 13. 认证与本地存储

### 13.1 关键 Cookie 字段

`AuthTicket`、`BbsTicket`、`UserTicket`、`RDB` 及 `_au*` 系列等，完整列表见 `auth.COOKIE_NAMES`。

### 13.2 存储路径

| 路径 | 说明 |
|------|------|
| `accounts/<userid>/cookies.json` | 多账号主存储 |
| `cookies.json` | 旧版兼容，自动迁移后仍可读 |
| `.disclaimer_accepted` | 免责确认标记 |

### 13.3 安全注意

- `.gitignore` 已排除 `accounts/`、`cookies.json`、`*.har`、抓包目录及 `soop_tools/`
- 提交前执行 `git status`，确认无 Cookie 或 HAR 文件进入暂存区
- `build.bat` 打包后用 `findstr` 检查 exe 是否误含 `AuthTicket` 等敏感字符串
- **切勿**将含 cookies 的目录与 exe 一并分发
- 若 Cookie 曾误提交 Git，除从历史中移除外，应在平台侧使旧会话失效（改密或重新登录）

---

## 14. 配置与定时参数

定义于 `constants.py`：

| 常量 | 值 | 用途 |
|------|-----|------|
| `HEARTBEAT_INTERVAL` | 5.0 s | gather 心跳间隔 |
| `MISSION_POLL_INTERVAL` | 60.0 s | 任务刷新 |
| `INVENTORY_POLL_INTERVAL` | 120.0 s | 背包刷新 + 自动领取 |
| `HTTP_CONNECT_TIMEOUT` | 15.0 s | aiohttp / gather 连接超时 |
| `HTTP_TOTAL_TIMEOUT` | 30.0 s | aiohttp / gather 总超时 |
| `NETWORK_RECOVER_COOLDOWN` | 30.0 s | 网络恢复最小间隔 |
| `DEFAULT_CHANNEL_BJID` | `owesports` | 默认官方频道 |
| `DROPS_HASHTAG` | `드롭스` | 标签搜索关键词 |

硬编码间隔（未放入 constants）：

| 位置 | 值 | 用途 |
|------|-----|------|
| `center.py` | 20 s | Bridge KEEPALIVE |
| `miner.py` `_poll_loop` | 5 s | 轮询循环内层 tick |
| `gui.py` | 120 s | 挂机中频道列表自动刷新 |
| `channel.py` | 3 页 × 30 条 | 标签搜索分页上限 |

---

## 15. 入口与运行方式

### 15.1 开发环境

```bat
# 在 soop_miner 目录
run.bat

# 或在上级目录
python -m soop_miner --gui
```

### 15.2 命令行模式

```bat
python -m soop_miner --cli --userid 账号 --password 密码 -v
```

将加载已保存的全部账号并并行挂机；若提供 `--userid/--password` 会先登录并保存。

### 15.3 打包 exe

```bat
cd soop_miner
build.bat
# 输出: ../dist/SOOP_Drops_Miner.exe
```

exe 无控制台窗口（`console=False`），默认启动 GUI。

**单实例（Windows）**：GUI 启动前通过命名 Mutex 检测是否已有实例；若已运行，则将现有窗口恢复并置前，新进程立即退出（`single_instance.py`）。

---

## 16. 构建与发布

### 16.1 `build.spec` 要点

- 入口：`soop_miner/entry.py`
- 工作路径：上级仓库根目录（`pathex`）
- 单文件模式，`excludes=["cookies"]`
- 显式 `hiddenimports` 包含 aiohttp、websockets 子模块

### 16.2 发布检查清单

- [ ] 版本号更新 `constants.VERSION`
- [ ] `git status` 无 `accounts/`、`cookies.json`、`*.har`、`soop_tools/`
- [ ] 本地 `accounts/` 未打入 exe
- [ ] `build.bat` 安全检查通过
- [ ] README 与 Release 说明含免责条款
- [ ] 发布包不含 Cookie、HAR 或开发辅助脚本

---

## 17. 开发辅助工具（本地，不纳入 Git）

上级目录 `soop_tools/` 为**开发者本地工具链**，用于 API 发现与逻辑验证：

- **不参与** `soop_miner` 运行时
- **不提交** 到开源仓库（已在 `.gitignore` 中忽略；与主仓库同级时请勿 `git add`）
- 输出目录（如 `soop_capture_output/`、`*_probe/`）常含 Cookie 或原始 API 响应，同样不得入库

典型脚本用途（仅供维护者本地参考）：

| 脚本 | 用途 |
|------|------|
| `soop_capture.py` | 抓取 Drops API 响应 |
| `analyze_har.py` | 分析 HAR 中的 gather 心跳 |
| `probe_hashtag*.py` | 发现 sch 搜索 API（已沉淀至 `channel.py`） |
| `diag_warnings.py` | GUI 警告逻辑诊断 |

研究结论已融入 `constants.py`、`watch.py`、`channel.py`、`drops.py`。公开文档与 Release **无需附带** 这些脚本。

---

## 18. 扩展与维护建议

### 18.1 常见改动入口

| 需求 | 建议修改 |
|------|----------|
| 新增 Drops API | `drops.py` + `models.py` |
| 调整选台策略 | `channel.py` `pick_channel()`、`_pick_for_active_missions()` |
| 修改心跳格式 | `watch.py`（参考 `soop_tools/analyze_har.py`） |
| Bridge 协议变化 | `center.py` |
| UI 文案/布局 | `gui.py` |
| 轮询频率 / 超时 | `constants.py` |

### 18.2 调试建议

- CLI 加 `-v` 观察单账号完整日志
- 关注 `进度已 N 分钟无变化` 警告 → 检查 bridge、分类是否匹配
- 关注 `需换台或调整优先任务` → 多任务单台限制
- GUI 日志面板级别为 INFO，模块 logger 名：`SoopDropsMiner`

### 18.3 已知限制

- 未实现真实 HLS 拉流（`stream.py` 未接入），依赖 bridge + gather 模拟观看。
- **同一时间只能挂一个直播间**；多任务需换台或改优先任务，无法并行累计。
- 所有账号共用同一 `ChannelConfig`，暂不支持每账号独立选台策略。
- 随机型进度依赖 mission 是否已加入及分类匹配，部分活动长期仅出现在 event 页。
- 平台 API 可能随时变更，维护者可在本地 `soop_tools/` 重新抓包验证（勿将抓包文件提交 Git）。

---

## 19. 免责声明

本软件为第三方辅助工具，与 SOOP Live / AfreecaTV 官方无任何关联，亦未获官方授权或认可。

使用本软件可能违反平台服务条款。账号受限、封号、奖励失效等风险由使用者自行承担。本软件按「现状」提供，不对挂机成功率、掉宝进度、奖励领取结果作任何保证。

完整条款见程序内「免责说明」及 `constants.DISCLAIMER_TEXT`。

---

*文档随代码演进更新；若与源码不一致，以源码为准。*
