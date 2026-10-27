# 南开活动报名自动搜集（CAS 登录 + Dify 工作流）

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Dify](https://img.shields.io/badge/Dify-Workflow-1C64F2)](https://dify.ai/)
[![SQLite](https://img.shields.io/badge/Storage-SQLite-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![Feishu](https://img.shields.io/badge/Notify-Feishu-00D6B9)](https://open.feishu.cn/)
[![Last commit](https://img.shields.io/github/last-commit/sleeepingbeee/internet-robot-with-CAS-login-on-NKU)](https://github.com/sleeepingbeee/internet-robot-with-CAS-login-on-NKU/commits/main)

用 **CAS 统一身份认证** 登录南开校内两个活动平台，抓取「未来可报名」的活动，**合并 → 按数据库去重 → 写入 SQLite → 推送飞书**，整条链路由 **Dify 工作流** 定时驱动，不需要常驻服务器。

- 📚 数据源：志愿系统 `zyfw.nankai.edu.cn`、辅学平台 `fuxue.nankai.edu.cn`
- 🤖 运行宿主：Dify「代码执行」节点（Python 3，节点间只传字符串）
- 🗄️ 存储：SQLite（宿主机 `D:\sqlite\dify_sqlite\activities.db`，容器内挂载为 `/dify_sqlite`）
- 🔔 通知：飞书应用消息
- ⏰ 调度：Dify「定时触发器」

![工作流总览](活动报名搜集-whole-workflow.png)

> 上图为 Dify 画布导出的 12 节点总览，下文「工作流拆解」与它一一对应。图里的「下次执行时间 12:00 PM」只是导出瞬间的值，实际触发按画布设定的间隔。

**项目状态**：个人自用，已在本地 Dify（Docker）上长期按小时间隔稳定运行；代码按「可复现」标准整理成仓库，工作流 DSL 可直接导入。仅面向南开校内活动信息汇总，请勿用于批量采集或分发他人信息。

---

## 目录

- [1. 这是什么](#1-这是什么)
- [2. 快速开始](#2-快速开始)
- [3. 仓库结构](#3-仓库结构)
- [4. 工作流拆解](#4-工作流拆解)
- [5. 爬虫内核](#5-爬虫内核)
- [6. 数据库](#6-数据库)
- [7. 本地运行与调试](#7-本地运行与调试)
- [8. 常用命令与 SQL](#8-常用命令与-sql)
- [9. 踩坑清单](#9-踩坑清单)
- [10. 常见问题 FAQ](#10-常见问题-faq)
- [11. 安全与合规](#11-安全与合规)
- [12. 参考文档](#12-参考文档)
- [English TL;DR](#english-tldr)

---

## 1. 这是什么

### 1.1 解决什么问题

南开的活动分散在**两个互不相通**的系统里，报名名额先到先得。人工每天刷两个站、来回对比、还要记得哪些看过了，既费时又容易漏。

本项目把两个站的活动汇总成统一格式，由 Dify 定时触发器按设定间隔（例如每小时）自动跑一轮：

1. 用统一身份认证（CAS / IAM）登录两个系统，密码按前端算法 **AES-256-CBC 加密**后提交，不落明文；
2. 抓列表页 → 逐条请求详情页 → 解析活动字段 → 过滤掉已开始/已结束的活动；
3. 两个平台的结果合并成**一段固定格式的纯文本**；
4. 与 SQLite 里「已经通知过」的活动按名称比对，去掉重复项；
5. 剩下的新活动入库，并通过飞书应用消息推送到个人 / 群。

效果：每天只在飞书里看新活动，不用再登录两个网站。

### 1.2 一条完整链路

```
定时触发器（每小时）
  ├─ 获取当前时间(%Y-%m-%d, Asia/Shanghai)
  ├─ 去除历史活动（SQLite DELETE）→ 查询全部数据（SQLite SELECT）
  ├─ 爬取辅学系统（代码节点）┐
  ├─ 爬取志愿系统（代码节点）┴→ 合并活动 → 根据数据库去重 ─┬→ 计算 SQL → 插入行
  │                                                        └→ 条件分支 → 飞书消息
  └─ 完成
```

### 1.3 设计原则

| 原则 | 做法 |
|---|---|
| 依赖最小 | 代码节点只依赖标准库 + `requests`（Dify 沙箱自带）；本地脚本另用 `lxml`、可选 `pycryptodome` |
| 凭据不进源码 | 账号密码来自环境变量 `CAS_USERNAME` / `CAS_PASSWORD`（Dify 里配置成加密环境变量） |
| 节点间只传字符串 | 除「计算SQL代码」外，所有代码节点输出变量只有 `text`（String），避免 Dify 变量校验报错 |
| 幂等 | 入库用 `INSERT OR IGNORE` + 表级 `UNIQUE(来源系统, 活动名称, 活动时间)`，重复运行不产生脏数据 |
| 失败不阻断 | 爬虫节点 `RAISE_ERRORS=False`，抓取失败返回 `抓取失败：...` 文本；当批 0 条时工作流走条件分支、不推送 |
| 不打扰人 | 只有真正有新活动才发飞书；没有新活动时静默结束 |

---

## 2. 快速开始

### 2.1 前置要求

| 组件 | 要求 / 说明 |
|---|---|
| Dify | 支持「代码执行 / 定时触发器 / 条件分支」节点与插件（画布 DSL 版本 `0.6.0`，建议 1.x 近期版本） |
| 插件 | `langgenius/sqlite` `0.0.7`、`langgenius/feishu_message` `0.0.8`（导入 DSL 时会提示安装） |
| 飞书 | 一个可用的飞书自建应用（拿 `app_id` / `app_secret`，并给接收人发消息的权限） |
| 存储 | 宿主机一个目录（示例 `D:\sqlite\dify_sqlite`）挂载给 Dify 的 `api` / `worker` / `plugin_daemon` 容器 |
| Python（可选） | 本地调试才需要，≥ 3.9 |

### 2.2 三步部署

**第 1 步：导入工作流**

Dify → 「创建应用」→「导入 DSL 文件」→ 选择仓库根目录的 [`活动报名搜集.yml`](活动报名搜集.yml)，按提示安装缺失的 `sqlite`、`feishu_message` 插件。

**第 2 步：配置环境变量与挂载**

在应用「环境变量」里新建两个**加密（Secret）**变量：

```text
CAS_USERNAME   = 你的学号
CAS_PASSWORD   = 你的统一身份认证密码
```

把两个爬虫代码节点的 `username` / `password` 输入分别映射到这两个变量；SQLite 插件凭据的 `database_path` 填**容器内路径** `/dify_sqlite/activities.db`，并在 `docker-compose.yaml` 中给 `api`、`worker`、`plugin_daemon` 都加上同一份挂载：

```yaml
volumes:
  - D:\sqlite\dify_sqlite:/dify_sqlite
```

首次运行前建表（任选其一）：用 `sqlite3.exe` 执行 [6.2 表结构](#62-表结构)，或用 SQLite 插件跑一次建表语句。

**第 3 步：配置飞书并发布**

「发送飞书应用消息」节点里填自建应用凭据、`RECEIVE_ID_TYPE=user_id`、接收人 `user_id`；然后点右上角**「发布」**——只保存不发布的话，定时任务继续跑旧版本。

### 2.3 验证

1. 画布「测试运行」整条工作流，检查「插入行」返回 `rows_inserted`、飞书收到消息；
2. 本地用 sqlite3 查一次库（见 [8. 常用命令与 SQL](#8-常用命令与-sql)）；
3. 等下一个定时周期，确认库里没有重复行、已通知过的活动不再推送。

---

## 3. 仓库结构

```text
internet-robot-with-CAS-login-on-NKU/
├── 活动报名搜集.yml                     # Dify 工作流 DSL 导出（v0.6.0，12 节点，可直接导入）
├── 活动报名搜集-whole-workflow.png      # 工作流画布总览图
├── 志愿系统爬虫/
│   ├── cas_login.py                     # 本地独立脚本：CAS 登录 + 志愿系统抓取，结果打印到 stdout（214 行）
│   └── cas_core.py                      # Dify 代码节点内核：main(**kwargs) -> {"text": ...}（528 行）
├── 辅学系统爬虫/
│   ├── cas_login.py                     # 辅学平台全链路独立脚本：登录→SSO 中转→爬取，可导出 CSV/HTML（1833 行）
│   ├── cas_core.py                      # Dify 代码节点内核（987 行）
│   └── CAS-SSO项目总结.md               # 辅学平台 CAS/SSO 链路的完整技术复盘 + 排障手册（1178 行）
├── utils/                               # Dify 里另外三个「代码执行」节点的源码
│   ├── merge_activities_text.py         # 合并两个爬虫的 text（242 行）
│   ├── filter_activities_by_db.py       # 按数据库已有活动名称去重（229 行）
│   ├── build_insert_sql.py              # 把去重后的 text 拼成一条 INSERT 语句（189 行）
│   └── sql命令.md                       # 常用 SQL / PowerShell 速查
└── README.md
```

两类文件的区别（改逻辑时注意同步）：

| 文件 | 面向对象 | 特点 |
|---|---|---|
| `cas_login.py` | 人 | 带中文过程日志、落盘开关，可直接 `python cas_login.py` 调试 |
| `cas_core.py` | Dify 代码节点 | 无日志、无文件读写、永不抛异常，返回 `{"text": ...}` |

---

## 4. 工作流拆解

> 变量名以画布实际配置为准。凡代码节点，**返回 dict 的键必须与节点里声明的输出变量完全一致**（见[踩坑清单](#9-踩坑清单)）。

### 4.1 节点一览

| # | 节点 | 类型 | 输入 | 输出 / 关键参数 |
|---|---|---|---|---|
| 1 | 定时触发器 | 触发器 | — | 按画布设定的间隔定时拉起 |
| 2 | 获取当前时间 | 时间 / 代码节点 | — | `FORMAT=%Y-%m-%d`，`TIMEZONE=Asia/Shanghai` |
| 3 | 去除历史活动 | SQLite `delete_sql` | 节点 2 的日期 | `DELETE FROM activities WHERE substr(活动时间,1,10) < substr('{{#获取当前时间.text#}}',1,10);` |
| 4 | 查询全部数据 | SQLite `select_sql` | — | `SELECT * FROM activities ORDER BY 活动时间 ASC;`，输出 `text` + `json` |
| 5 | 爬取辅学系统 | 代码执行 | `username` / `password` ← 环境变量 | 输出 `text`（String） |
| 6 | 爬取志愿系统 | 代码执行 | 同上 | 输出 `text`（String） |
| 7 | 合并活动 | 代码执行（`utils/merge_activities_text.py`） | `text1`=节点5，`text2`=节点6 | 输出 `text` |
| 8 | 根据数据库去重 | 代码执行（`utils/filter_activities_by_db.py`） | `text1`=节点7，`text2`=节点4 | 输出 `text` |
| 9 | 计算SQL代码 | 代码执行（`utils/build_insert_sql.py`） | `text`=节点8 | 输出 `sql` / `text` / `count` |
| 10 | 插入行 | SQLite `insert_sql` | `insert_sql`=`{{#节点9.sql#}}` | 返回 `rows_inserted` |
| 11 | 条件分支 | IF/ELSE | 节点8 的 `text` | 等于 `共 0 场活动：` → 结束；否则 → 节点12 |
| 12 | 发送飞书应用消息 | 飞书插件 | `RECEIVE_ID_TYPE=user_id`、`MSG_TYPE=text`、内容=节点8 的 `text` | 推送本批新活动 |

要点：

- 节点 3 的 `DELETE` 放在一切查询之前，保证节点 4 读到的是「今天及以后」的库；
- 节点 3 用**严格小于**（`<`）而不是 `<=`，**当天的活动不会被删**；
- 节点 9→10（入库）与节点 11→12（通知）是两条并行下游，互不阻塞；
- 当天没有新活动时，节点 8 输出 `共 0 场活动：`，条件分支直接吞掉，不打扰人。

### 4.2 SQLite 插件配置

- 插件：`langgenius/sqlite`（仓库导出时为 `0.0.7`）；
- 凭据 `database_path` 必须填**容器内路径**：`/dify_sqlite/activities.db`（不是 Windows 路径）；
- 该路径由 `docker-compose.yaml` 的卷挂载提供，`api` / `worker` / `plugin_daemon` 三个服务都挂 `D:\sqlite\dify_sqlite → /dify_sqlite`；
- 插件一次 `conn.execute()` 只能跑**一条语句**（不能出现第二个分号），所以入库是「一条多行 `INSERT`」而不是逐条插入。

### 4.3 三个代码节点的输出契约

| 文件 | 输入 | 输出 | 备注 |
|---|---|---|---|
| `utils/merge_activities_text.py` | `text1` / `text2`（也兼容 `activities1/activities2` 数组） | `{"text": ...}` | 序号重新从 1 编排 |
| `utils/filter_activities_by_db.py` | `text1` 爬虫文本、`text2` 数据库文本 | `{"text": ...}` | 按活动名称去重；兼容插件 `text`（Python repr）与 `json` 两种返回 |
| `utils/build_insert_sql.py` | `text` | `{"sql","text","count"}` | `INSERT OR IGNORE ... VALUES (...),(...)`；空批次输出 `INSERT ... SELECT NULL WHERE 0;` |

---

## 5. 爬虫内核

### 5.1 志愿系统（zyfw.nankai.edu.cn）

**登录链路**（`志愿系统爬虫/cas_core.py`、`cas_login.py` 相同）：

1. `GET https://zyfw.nankai.edu.cn/Login/index.aspx` 拿会话与登录页参数；
2. `POST https://iam.nankai.edu.cn/api/v1/login`，JSON：`{account, password(AES 密文), account_type: "userid", login_scene: "feilian"}`；
3. 从响应 `data.next.link` 取跳转地址，`GET https://iam.nankai.edu.cn + link + &lang=zh-CN` 落地（登录态建立）。

**密码加密**：复刻 `iam.nankai.edu.cn` 前端的 AES-256-CBC + PKCS7，纯 Python 实现，不依赖 `pycryptodome`：

```text
key = MD5("9007199254740991") 的 32 个十六进制字符（作为 32 字节 UTF-8 字符串）
iv  = SHA1(key) 的十六进制字符串的前 16 字节
自检向量：AES("demo-1234") == "2b563d38ed5102f50e661c2cc33fec40"
```

**抓取**：列表页 `https://zyfw.nankai.edu.cn/Index/huodong/p/{page}.aspx`（最多 15 页，每页 10 条），对每条活动再请求详情页，XPath 取字段：

| 字段 | 来源（详情页） |
|---|---|
| 活动名称 | `//p[@class="hd-title"]` |
| 组织机构 / 联系人 / 联系电话 / 活动时间 / 活动地点 / 志愿时长 / 招募人数 | `//div[@class="xinxi"]/table/tr[n]/td[2]`（n = 1,4,5,6,7,8,9） |

**筛选**：按「活动时间」里 `至` 前面的开始时间判断，**开始日期严格晚于今天**才保留。
**输出**：按开始时间升序，格式见 [5.3](#53-统一文本格式)。

### 5.2 辅学平台（fuxue.nankai.edu.cn）

链路比志愿系统长，完整推导见 [`辅学系统爬虫/CAS-SSO项目总结.md`](辅学系统爬虫/CAS-SSO项目总结.md)，简版：

```text
步骤1   GET  xgb.nankai.edu.cn/login/fslogin   → 302 iam/api/cas/login → 302 iam/login（种 csrf-token / session）
步骤2   POST iam.nankai.edu.cn/api/v1/login    （密码 AES 密文；响应不含 ticket，只建 iam 登录态）
步骤2.5 GET  iam/api/cas/login?service=...     → 302 带 ?ticket=ST-...（ticket 一次性、几十秒过期）
步骤3/4 GET  xgb/login/fslogin?ticket=...      → 跟 302 落地，xgb 域登录态才有效
步骤4.5 GET  xgb/home/user/login.html          → 在门户 HTML 里现抓 SSO 入口 → 进入 fuxue
步骤5   GET  fuxue.nankai.edu.cn/student/kbmhd/type/{1,2}/p/{N}.html   （type=1 主题讲座，type=2 辅学活动）
```

- 密码加密同上（`ENCRYPT_PASSWORD=True`，`AES_KEY_SEED=9007199254740991`，内置自检向量；有 `pycryptodome` 用库、没有用内置纯 Python AES）；
- 爬取字段 5 个：活动名称、活动类型、活动学时、活动时间、活动地点（表头自动映射 + 全角数字归一化）；
- 筛选：`CRAWL_ONLY_FUTURE=True`（活动日期不早于今天），最多翻 `CRAWL_MAX_PAGES` 页；
- 独立脚本 `cas_login.py` 的调试开关：`CRAWL_TEST_MODE=True` 写 `activities.csv`、`DUMP_LANDING_HTML=True` 写 `step4_5_landing.html`，产物落在**脚本同目录**（提交前注意别把 CSV 带进仓库）。

### 5.3 统一文本格式

两个内核输出同一形状，下游节点只认这个格式：

```text
共 3 场活动：
1. 活动名称
   组织机构：...
   活动时间：2026-10-25 08:00 至 2026-10-25 12:00
   活动地点：...
```

- 志愿系统的「活动时间」是区间：`YYYY-MM-DD HH:MM 至 YYYY-MM-DD HH:MM`（**前 10 位就是开始日期**）；
- 辅学平台是单时间点：`YYYY-MM-DD HH:MM`；
- 抓取失败 / 无结果分别输出 `抓取失败：<异常类型>: <异常信息>` 与 `本次没有抓取到符合条件的活动。`。

统一写法让下游的「合并、按库去重、生成 SQL」全部用同一套正则解析，不用为两个平台写两套分支。

### 5.4 为什么能登录（以及为什么不藏密码）

南开 IAM 前端在提交前用固定 seed 派生 key/iv，对密码做 AES-256-CBC 加密后放进 JSON。本项目复刻了这段算法（含内置自检向量，改坏会立刻暴露），但**代理的只是算法，不是凭据**：账号密码始终来自环境变量，源码里 `USERNAME` / `PASSWORD` 保持空字符串，日志也不打印明文。

---

## 6. 数据库

### 6.1 位置

| 位置 | 用途 |
|---|---|
| `D:\sqlite\dify_sqlite\activities.db` | 宿主机路径（用 sqlite3.exe 直接查） |
| `/dify_sqlite/activities.db` | Dify 插件 / 代码节点里的路径（同一文件的容器内挂载） |
| `D:\sqlite\dify_sqlite\activities_backup_*.db` | 运维备份，`activities.db` 损坏时可直接替换 |
| `D:\sqlite\dify_sqlite\test.db` | 与业务无关的调试库，不要混用 |

### 6.2 表结构

```sql
CREATE TABLE activities (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    来源系统  TEXT NOT NULL,          -- 志愿系统 / 辅学系统
    活动名称  TEXT NOT NULL,
    活动时间  TEXT NOT NULL,          -- 志愿：'YYYY-MM-DD HH:MM 至 YYYY-MM-DD HH:MM'；辅学：'YYYY-MM-DD HH:MM'
    活动地点  TEXT,
    组织机构  TEXT,                   -- 仅志愿系统有
    联系人    TEXT,                   -- 仅志愿系统有
    联系电话  TEXT,                   -- 仅志愿系统有
    志愿时长  TEXT,                   -- 仅志愿系统有
    招募人数  TEXT,                   -- 仅志愿系统有
    活动类型  TEXT,                   -- 仅辅学平台有
    活动学时  TEXT,                   -- 仅辅学平台有
    抓取时间  TEXT NOT NULL,          -- 北京时间 'YYYY-MM-DD HH:MM:SS'
    UNIQUE(来源系统, 活动名称, 活动时间)
);
```

### 6.3 约定

- **幂等**：插入统一写 `INSERT OR IGNORE INTO activities (...)`，唯一键冲突时静默跳过，同一活动被重复抓取不会产生重复行；
- **去重维度**：`来源系统 + 活动名称 + 活动时间`，两个系统的同名活动互不影响；
- **时间字段**：`活动时间` 以 `YYYY-MM-DD` 开头，日期比较用字符串比较即可：`substr(活动时间, 1, 10) >= '2026-10-24'`；
- **抓取时间**：Dify 容器是 UTC，代码里显式用 `datetime.now(timezone(timedelta(hours=8)))` 生成北京时间（**不要用 `utcnow()`**，Python 3.12 已标记弃用）；
- **id**：`AUTOINCREMENT`，删除历史行后不会复用旧 id，只作内部行号，业务上不要依赖其连续性；
- **历史清理**：工作流每轮先 `DELETE ... WHERE substr(活动时间,1,10) < 今天` 清掉过期活动，库里长期只保留「今天及以后」的活动。

---

## 7. 本地运行与调试

### 7.1 环境

- Python ≥ 3.9（开发机实测 3.11.9）
- 独立脚本：`requests`、`lxml`；密码加密优先用 `pycryptodome`（缺失时自动切纯标准库 AES）
- Dify 代码节点：只需 `requests`（沙箱自带）；**不能用 `lxml`、不能依赖 `pycryptodome`**，核心里已替换为 `html.parser` 与内置 AES

```powershell
python -m pip install requests lxml pycryptodome
```

### 7.2 凭据

```powershell
# 当前会话（推荐；不要写进源码）
$env:CAS_USERNAME="你的学号"
$env:CAS_PASSWORD="你的密码"
```

Dify 里则在「环境变量」中新建两个**加密（Secret）**变量 `CAS_USERNAME`、`CAS_PASSWORD`，再映射到两个爬虫代码节点的 `username` / `password` 输入。

### 7.3 跑独立脚本

```powershell
cd 志愿系统爬虫 ; python cas_login.py     # 打印「共 N 场活动：...」
cd 辅学系统爬虫 ; python cas_login.py     # CRAWL_TEST_MODE=True 时额外生成 activities.csv 等调试产物
```

### 7.4 调试 Dify 内核（不写文件、不打印）

```powershell
cd 志愿系统爬虫
python -c "import cas_core, json; print(json.dumps(cas_core.main(), ensure_ascii=False))"
```

- `cas_core.main()` 支持的可选输入：`username`、`password`、`crawl_max_pages`、`crawl_delay`、`request_timeout`、`deadline_seconds`、`crawl_only_future`；
- 想看真实失败原因：把文件里的 `RAISE_ERRORS` 改为 `True`，Dify 的 FAIL 面板会直接显示失败阶段（生产环境务必改回 `False`）；
- 整次执行有时间预算（志愿内核默认 20s，Dify 代码节点上限约 30s），超预算时会带着已抓到的数据返回，不会白跑。

### 7.5 在 Dify 里改节点

1. 打开节点，整段粘贴 `cas_core.py` / `utils/*.py` 的内容（Python 3）；
2. 「输出变量」只声明与 `return` 字典一致的键（爬虫/合并/去重节点只声明 `text`；计算 SQL 节点声明 `sql`、`text`、`count`）；
3. 「输入变量」按 [4.1 节点一览](#41-节点一览) 映射上游节点输出；
4. 改完点「测试运行」，再点「发布」——不发布会继续用旧版本跑定时任务。

---

## 8. 常用命令与 SQL

```powershell
# 查询全部数据（单行字段换行显示）
& 'D:\sqlite\sqlite_tools\sqlite3.exe' -cmd ".mode line" 'D:\sqlite\dify_sqlite\activities.db' "SELECT * FROM activities;"

# 看表结构
& 'D:\sqlite\sqlite_tools\sqlite3.exe' 'D:\sqlite\dify_sqlite\activities.db' ".schema activities"

# 备份（改库前先做）
Copy-Item 'D:\sqlite\dify_sqlite\activities.db' "D:\sqlite\dify_sqlite\activities_backup_$(Get-Date -Format yyyyMMdd-HHmmss).db"
```

```sql
-- 全部数据，按开始时间排序
SELECT * FROM activities ORDER BY 活动时间 ASC;

-- 今天及以后（不依赖宿主机时区：UTC 时间 +8 小时）
SELECT * FROM activities WHERE substr(活动时间, 1, 10) >= date('now', '+8 hours');

-- 清除早于某天的历史活动（保留当天）
DELETE FROM activities WHERE substr(活动时间, 1, 10) < '2026-10-24';

-- 手工补一条（注意唯一约束）
INSERT OR IGNORE INTO activities (来源系统, 活动名称, 活动时间, 活动地点, 抓取时间)
VALUES ('志愿系统', '示例活动', '2026-10-30 14:00 至 2026-10-30 17:00', '二主楼', datetime('now', 'localtime'));
```

---

## 9. 踩坑清单

这些都是实际踩过的坑，改代码时别破坏：

1. **`Not all output parameters are validated.`**
   代码节点 `return` 的键必须与节点「输出变量」**逐一对应**。多一个键、少一个键都会报这个错。所以现在所有节点都只返回 `text`（或计算节点固定的三个键）。
2. **`Output result is missing.`**
   Dify 会把整段代码内联进自己的 runner 脚本执行，模块顶层**不能**有 `if __name__ == "__main__"`、`print`、读写文件、`raise SystemExit` 之类的自执行代码，否则 Dify 在打印结果标记前就退出了（现象：0.1 秒、0 token 就失败）。
3. **SQLite 插件一次只能执行一条 SQL**
   插件内部是 `conn.execute(sql)`，写多个分号会报 `You can only execute one statement at a time`。入库所以拼成一条多行 `INSERT`；`select_sql` 必须以 `SELECT` 开头。
4. **容器路径 ≠ Windows 路径**
   插件凭据 `database_path` 要写 `/dify_sqlite/activities.db`；代码节点里也没有这个挂载，读写库只能走 SQLite 插件节点。
5. **时区**
   容器默认 UTC，`datetime.now()` 会慢 8 小时且 `utcnow()` 在 Python 3.12 已弃用；统一用带时区的 `datetime.now(timezone(timedelta(hours=8)))`。
6. **Dify 代码节点里没有 `lxml` / `pycryptodome`**
   内核已把 HTML 解析换成 `html.parser`、AES 换成纯标准库实现；本地脚本仍可用 `lxml`，但不要把 `import lxml` 带进代码节点。
7. **改了代码节点要重新发布**
   测试运行成功 ≠ 定时任务用新代码，画布右上角「发布」才算生效。
8. **数据库文本不是 JSON**
   插件「查询行」的 `text` 输出里 `Data:` 部分是 **Python repr**（单引号、`None`），需要 `ast.literal_eval` 解析；要严格 JSON 就取 `json` 变量。`filter_activities_by_db.py` 两种都兼容。
9. **日期比较别用 `<=`**
   「去除历史活动」必须用严格 `<`，否则把当天的活动一起删掉；爬虫侧「只保留未来」也是「开始时间晚于今天」。
10. **ticket 是一次性的**
    辅学链路的 `ST-` ticket 几十秒后失效，必须在上一步响应里立刻取用；中间加日志、断点都会让它过期。

---

## 10. 常见问题 FAQ

**Q：一定要 Dify 吗？能不能直接跑 Python？**
可以。两个 `cas_login.py` 是完整独立脚本，配合 `utils/*.py` 可以在本地串起来跑；Dify 只是负责调度、持久化（SQLite 插件）和推送（飞书插件）。不过「按库去重 + 入库 + 通知」这套逻辑在 Dify 里已经串好，本地复刻需要自己写少量胶水。

**Q：为什么不把数据库放进仓库？**
库里有联系人姓名、手机号等个人信息，且每天变化。仓库只放代码与 DSL，数据库留在本机。

**Q：抓取频率要控制吗？**
默认串行请求、单轮几十个请求，一小时的调度间隔足够。请遵守学校网络使用规定，不要调大并发或缩短到分钟级。

**Q：登录失效 / 抓到 0 条怎么办？**
先看爬虫节点输出：`抓取失败：<异常>` 说明链路断了（常见是 IAM 页面结构或加密 seed 变更、ticket 过期）；`本次没有抓取到符合条件的活动。` 说明登录成功但暂无未来活动。前者把 `RAISE_ERRORS` 临时改 `True` 复现，后者属正常。

**Q：能加第三个数据源吗？**
可以。按 [5.3 统一文本格式](#53-统一文本格式) 输出即可接入现有「合并 → 去重 → 入库 → 通知」链路；注意合并节点当前只接两个 `text` 输入，需要相应扩展。

---

## 11. 安全与合规

- **凭据**只放环境变量 / Dify 加密环境变量，仓库源码里 `USERNAME`、`PASSWORD` 保持空字符串；
- 数据库里含联系人姓名和手机号（志愿系统字段），属于个人信息，**不要上传公开仓库**，备份文件也要放在本机；
- 登录密码只以 AES 密文形式出现在网络请求里，日志不打印明文、不打印完整密码；
- 本项目仅用于个人校内活动信息汇总，请控制抓取频率（默认串行，单轮不超过几十个请求），遵守学校网络使用与信息安全规定，不要用于批量采集或分发他人信息；
- 如果你 fork 本项目，请自行承担账号与数据合规责任，并在公开分享前清理日志、CSV、数据库等运行产物。

---

## 12. 参考文档

- [`辅学系统爬虫/CAS-SSO项目总结.md`](辅学系统爬虫/CAS-SSO项目总结.md)：辅学平台 CAS/SSO 全链路的抓包分析、数据包详解、密码算法推导、完整踩坑清单与排障手册（1178 行，强烈建议先读）。
- [`utils/sql命令.md`](utils/sql命令.md)：本轮工作流用到的 SQL / 命令速查。
- [`活动报名搜集.yml`](活动报名搜集.yml)：Dify 工作流 DSL（导出为 `kind: app`、`version 0.6.0`，含 12 个节点与依赖插件声明），可直接导入复现。
- 各文件头部 docstring：每个 `cas_core.py` / `utils/*.py` 顶部都写明了 Dify 输入输出契约与部署步骤。
- 工作流画布：[`活动报名搜集-whole-workflow.png`](活动报名搜集-whole-workflow.png)（12 节点总览）。

---

## English TL;DR

A personal automation project for Nankai University campus activities. It logs into two campus platforms (`zyfw.nankai.edu.cn` volunteer system and `fuxue.nankai.edu.cn` tutoring platform) through the university CAS/IAM single sign-on, scrapes upcoming activities, merges and de-duplicates them against a SQLite table, then pushes only the new ones to Feishu — orchestrated as a scheduled Dify workflow.

- **Crawlers**: two pure-Python implementations (stdlib + `requests` only) that reproduce the IAM front-end AES-256-CBC password encryption; each platform ships a human-debuggable script and a Dify-ready core.
- **Workflow**: 12-node Dify DSL (`活动报名搜集.yml`) — trigger, time, cleanup, select, two crawlers, merge, de-duplicate, build SQL, insert, branch, Feishu message.
- **Storage**: SQLite with `UNIQUE(来源系统, 活动名称, 活动时间)` + `INSERT OR IGNORE` for idempotent runs.
- **Credentials**: never in source — `CAS_USERNAME` / `CAS_PASSWORD` environment variables only.
- **Scope**: personal use only; the database contains personal information and is never committed. Use at your own risk and respect your institution's network and data policies.

---

> 本项目为个人学习与校内信息汇总用途，与南开大学官方无关。代码按现状提供，不保证接口长期可用；如遇登录/抓取失败，优先参考 [`辅学系统爬虫/CAS-SSO项目总结.md`](辅学系统爬虫/CAS-SSO项目总结.md) 的排障章节。
