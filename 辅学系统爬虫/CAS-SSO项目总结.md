# 南开大学 CAS 统一身份认证 + SSO 中转 + 辅学平台活动爬取 —— 项目总结

> 文档版本：2026-10-16
> 对应交付物：[`cas_login.py`](cas_login.py)（2162 行，164235 字节）
> 本文既是**技术复盘**，也是**CAS/SSO 知识科普**，还是**运维排障手册**。所有事实均来自本项目的实盘探测、真实运行日志或源码本身；凡未经验证的地方都会明确标注「未验证」。

---

## 目录

1. [项目概述](#1-项目概述)
2. [涉及的系统与角色](#2-涉及的系统与角色)
3. [CAS / SSO 业务流程讲解](#3-cas--sso-业务流程讲解)
4. [完整调用与跳转顺序](#4-完整调用与跳转顺序)
5. [数据包详解](#5-数据包详解)
6. [密码加密算法](#6-密码加密算法)
7. [代码中的「服务器返回数据存储」逻辑](#7-代码中的服务器返回数据存储逻辑)
8. [爬虫部分](#8-爬虫部分)
9. [踩过的坑（完整清单）](#9-踩过的坑完整清单)
10. [其他重要专业知识](#10-其他重要专业知识)
11. [配置速查表](#11-配置速查表)
12. [故障排查手册](#12-故障排查手册)
13. [附录](#13-附录)

---

## 1. 项目概述

### 1.1 目标

用**一个 Python 文件**完成「登录 → SSO 中转 → 进入辅学平台 → 爬取活动表格」的全链路：

1. 通过南开大学统一身份认证（CAS/IAM）登录，期间**密码必须加密后发送**（对齐前端行为）；
2. 完成 CAS 票据（ticket）的获取、提交与校验；
3. 通过「一站式登录平台」门户页完成到辅学平台 `fuxue.nankai.edu.cn` 的 **SSO 中转**，建立该域的登录态；
4. 爬取辅学平台左栏「主题讲座」「辅学活动」下「可报名活动」的表格（支持翻页），
   筛出**活动时间严格晚于当日**的项目，返回「以字典为元素的列表」；
5. 提供「测试功能」开关：打开时把结果存成 CSV，关闭时什么都不干；
6. 全程输出可读的**中文过程日志**，便于对照抓包排查。

### 1.2 交付物

| 文件 | 说明 |
|---|---|
| `cas_login.py` | 唯一交付物。单文件、标准库 + 两个早已安装的第三方库（`requests` / `lxml`），零新增依赖 |
| `activities.csv` | 测试功能开启时由程序生成的爬取结果（实跑已产出） |
| `crawl_html/` | 可选调试产物，`CRAWL_DUMP_HTML=True` 时保存每页原始 HTML |
| `step4_5_landing.html` | 可选调试产物，`DUMP_LANDING_HTML=True` 时保存 SSO 中转落地页 |

### 1.3 硬性约束

- **不新增第三方依赖**：只用了标准库（`hashlib` / `re` / `csv` / `datetime` / `os` / `sys` / `time` / `warnings` / `urllib.parse`）和早已安装的 `requests`。密码加密优先用 `pycryptodome`（已安装），**万一缺失会自动切到文件内置的「纯标准库 AES」实现**，功能完全一致。步骤5 的 HTML 解析用早已安装的 **`lxml` 6.1.1**（XPath 定位表格，不再手写正则）——注意 `lxml` 是**本次改造新引入的 import**，但它是环境里本来就有的库，不构成"新增下载"。
- **不把凭据写进源码**：账号密码默认从**系统环境变量** `CAS_USERNAME` / `CAS_PASSWORD` 读取，配置区的 `USERNAME` / `PASSWORD` 只作兜底（默认留空）。
- **不写死 ticket / token**：`ST-...` 票据一次性、几十秒过期；门户页的 SSO 入口链接里带**每次访问现生成**的 32 位 token，两者都必须现场获取。
- **不打印明文密码**：日志只输出长度与前缀，从不输出明文。

### 1.4 运行方式

```powershell
# PowerShell（当前会话）
$env:CAS_USERNAME = "你的学号"
$env:CAS_PASSWORD = "你的密码"
python cas_login.py
```

```cmd
:: cmd（当前会话）
set CAS_USERNAME=你的学号
set CAS_PASSWORD=你的密码
python cas_login.py
```

```bash
# Linux / macOS
export CAS_USERNAME=你的学号
export CAS_PASSWORD=你的密码
python cas_login.py
```

> **永久设置**（Windows）：`sysdm.cpl` → 高级 → 环境变量；或用
> `setx CAS_USERNAME "你的学号"`（新开终端才生效；`setx` 写注册表 `HKCU\Environment`，**明文存储**）。

---

## 2. 涉及的系统与角色

| 主机 | 角色 | 技术栈 | 会话 Cookie（域） |
|---|---|---|---|
| `iam.nankai.edu.cn` | **统一身份认证中心（IAM）**，CAS 的实现方 | React SPA（`portal-fe-3.1.9.15606`），22 个 JS chunk 在 `/portal-static/` 下，总约 15.9 MB | `csrf-token`、`session`（HttpOnly/Secure，Max-Age≈1800） |
| `xgb.nankai.edu.cn` | **学工系统**，同时提供「一站式登录平台」门户页 | PHP | `PHPSESSID`、`CASTGC` |
| `fuxue.nankai.edu.cn` | **立公增能辅学平台**（爬取目标） | PHP（标题「立公增能辅学平台」） | `PHPSESSID` |

### 2.1 三者的信任关系

```
   ┌──────────────────────────┐
   │  iam.nankai.edu.cn       │  统一身份认证中心
   │  （CAS Server 的实现）    │  ← 唯一校验密码的地方
   └───────────┬──────────────┘
               │  按 service 参数签发一次性 ST 票据
               │  service 必须与登记值【逐字符完全一致】
               │
   ┌───────────▼──────────────┐
   │  xgb.nankai.edu.cn       │  学工系统 = CAS 注册的 service
   │  /login/fslogin          │  + 一站式登录平台门户页 /home/user/login.html
   └───────────┬──────────────┘
               │  门户页里现抓带 token 的 SSO 入口（orlogin 格式）
               │
   ┌───────────▼──────────────┐
   │  fuxue.nankai.edu.cn     │  业务系统（辅学平台），爬取目标
   └──────────────────────────┘
```

**关键认知**：`xgb.nankai.edu.cn` 是 CAS 白名单里登记的业务系统；`fuxue.nankai.edu.cn` **不是**直接通过 CAS 跳转进入的，而是由 xgb 的门户页充当**中转方**。这一点是本项目最核心的发现（详见 §4.6）。

---

## 3. CAS / SSO 业务流程讲解

### 3.1 CAS 是什么

CAS（Central Authentication Service）是一种**集中式单点登录协议**，核心思想是：

> 业务系统自己不校验密码，而是把用户"赶"到认证中心；认证中心校验通过后，发一张**一次性票据**，用户再拿着票据回业务系统换会话。

好处：密码只暴露给认证中心一个地方；改密码只改一处；一次登录可访问所有登记过的业务系统。

### 3.2 本项目涉及的票据与 Cookie 一览

| 名称 | 全称 / 含义 | 出现位置 | 生命周期 | 谁校验 |
|---|---|---|---|---|
| **ST** | Service Ticket | URL 参数 `?ticket=ST-...`；出现在 POST 响应体或 302 的 `Location` 头 | **一次性**，几十秒 | 业务系统（xgb）向 CAS 校验 |
| **TGT** | Ticket Granting Ticket | 认证中心内部（服务端），对外体现为 `CASTGC` Cookie | 随会话，较长 | CAS Server |
| **CASTGC** | CAS Ticket Granting Cookie | `Set-Cookie`，xgb 域；步骤3/4 写入 | 随会话 | CAS Server |
| **PHPSESSID** | PHP 会话 ID | xgb 和 fuxue 各自域 | 随会话 | 各站点的 PHP |
| **JSESSIONID** | Java Web 会话 ID | 传统 Apereo CAS 部署常见 | 随会话 | 各站点的 Java 容器 |
| **csrf-token** / **session** | IAM 的 CSRF 令牌与会话 | iam 域，步骤1 由 `Set-Cookie` 写入 | ≈30 分钟 | IAM 服务端 |

> **注意**：`JSESSIONID` 在本站点**没有出现**（IAM 是 React SPA + 自研后端，不是 Apereo CAS 的 Java 部署）。代码里的 `LOGIN_SUCCESS_COOKIE_NAMES = ("CASTGC", "JSESSIONID")` 是兼容性写法，本站在 xgb 域实际看到的是 `PHPSESSID` + `CASTGC`。

### 3.3 `service` 参数的精确匹配

CAS 为了防开放重定向，会对 `service` 参数做**登记校验**。本站点的登记值是：

```
https://xgb.nankai.edu.cn/login/fslogin
```

因此：

- `CAS_TICKET_URL` 里的 `service=` 与 `CAS_SERVICE_URL` **必须写完全一样的字符串**（含协议、域名、路径，不能多一个 `/`）；
- **不能**把 `CAS_SERVICE_URL` 改成 `fuxue.nankai.edu.cn` ——CAS 不认它，会拒绝签发；（这条是实测结论，配置区 `cas_login.py:114` 上方有大段注释说明为什么不能改。）

### 3.4 为什么「CAS 登录成功」≠「业务系统登录成功」

这是理解本项目最重要的概念，分三层：

| 层 | 成功标志 | 说明 |
|---|---|---|
| ① 认证中心接受密码 | `POST /api/v1/login` 返回 `code: 0` | 这只说明**密码正确**，此时**还没有任何业务系统登录态**，也没有 ticket |
| ② 拿到 ST 票据 | `GET /api/cas/login?service=...` 返回 302 且 `Location` 里带 `?ticket=ST-...` | 票据是**按当前登录态现场签发**的，本身还没被业务系统消费 |
| ③ 业务系统消费票据 | 302 跟到底，落地业务页，session 里出现业务系统自己的 Cookie | **只有到这一步，业务系统才认你** |

本项目额外的第 ④ 层：业务系统 xgb 登录成功后，**fuxue 仍然没有登录态**——需要再做一次 SSO 中转（§4.6）。实测确认：步骤4 落地在 `https://xgb.nankai.edu.cn/home/user/login.html` 时，直接去请求 fuxue 的业务页面，**依然会被 302 到 fuxue 自己的登录页**。

### 3.5 两种 SSO 中转模式

| 模式 | 触发方式 | 本项目的适用情况 |
|---|---|---|
| **经典 CAS 重定向** | 访问业务页 → 302 到 CAS → 带 `?ticket=` 回到业务页 | fuxue **不适用**（见下方说明） |
| **门户页转发（本项目采用）** | 访问门户页 → **在 HTML 里现抓**带 token 的入口链接 → 点进去 | ✅ fuxue 就是这样进来的 |

fuxue 不适用经典 CAS 的原因（实测）：

- `fuxue.nankai.edu.cn` 任何匿名请求都会 302 到**它自己的** `/login/index.html`，**不会**自动跳 CAS；
- 它的登录页上唯一的学生 SSO 入口是一个**裸链** `<a href="https://xgb.nankai.edu.cn/xszz/Login/index">统一身份认证登录</a>`；
- 而那条 `/xszz/Login/index` 路线**进不了 fuxue**（已证伪，见 §9）。

真正的入口藏在 **xgb 的「一站式登录平台」门户页**里，形如：

```
http://fuxue.nankai.edu.cn/Home/Login/orlogin/type/1/id/<学号>/token/<32位随机串>.html
└──── 注意是 http，不是 https ────┘
```

其中 `id/<学号>` 与 32 位随机 `token` 都是**门户页每次访问时由服务端现生成**的，写死必失效。

---

## 4. 完整调用与跳转顺序

### 4.1 全局流程图

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 步骤1  预访问 CAS 入口（代码只发 2 个请求就停）                            │
│   GET https://xgb.nankai.edu.cn/login/fslogin    allow_redirects=False    │
│     302 → iam /api/cas/login?service=https://xgb.nankai.edu.cn/login/f... │
│   GET 该 302 目标                                allow_redirects=False    │
│     302 → iam /login?next=%2Fapi%2Fcas%2Flogin%3Fservice%3D...            │
│   ★ 停在第 2 跳：收获 csrf-token / session（iam 域），第 3 跳白跑不跟      │
└───────────────────────────────┬──────────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 步骤2  POST 账号密码（密码 = AES 密文，JSON）                             │
│   POST https://iam.nankai.edu.cn/api/v1/login?os=web                      │
│   body {account, password(AES), login_scene:"feilian", account_type:"userid"}│
│   200 {"code":0,...,"data":{"result":"success","next":{"action":"GoToLink",│
│         "link":"/api/cas/login?service=https://xgb.nankai.edu.cn/login/fslogin"}}}│
│   ★ 响应体【不含 ticket】！成功判定 = JSON 里 code == AUTH_SUCCESS_JSON_CODE │
└───────────────────────────────┬──────────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 步骤2.5 ~ 步骤4  现场签发 ST → 校验 → 落地（代码合并为一个函数）           │
│   establish_login_state(session, issued)                                  │
│   ① GET cas_entry_url()                        allow_redirects=False      │
│       302 Location: https://xgb.nankai.edu.cn/login/fslogin?ticket=ST-... │
│       ★ 拦住的唯一目的：读到 Location 拿 ticket + validate_url            │
│   ② GET <那个 Location>（urljoin 补绝对）       allow_redirects=True       │
│       302 → 200 https://xgb.nankai.edu.cn/home/user/login.html            │
│       ★ 这一跳票被消费掉，并写下 CASTGC —— 所以这一跳必须放行             │
│   ★ 到这里 xgb 登录完成，但 fuxue 还没有登录态                            │
└───────────────────────────────┬──────────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 步骤4.5  通过「一站式登录平台」门户页中转进 fuxue（本项目核心）           │
│   ① 复用步骤4 落地页 HTML（同一地址，省 1 个请求）                        │
│   ② 从 HTML 里现抓 <a href="http://fuxue.../Home/Login/orlogin/.../token/...">│
│   ③ GET 该链接，Referer = 门户页地址（逐跳手动跟，自己补 Referer）        │
│   → fuxue 域登录态建立                                                     │
└───────────────────────────────┬──────────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 复核  verify_login_state                                                 │
│   VERIFY_MODE="single"：1 个请求 allow_redirects=True，看最终落地域名      │
│   VERIFY_MODE="verbose"：老做法，allow_redirects=False 逐跳看 Location     │
└───────────────────────────────┬──────────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 步骤5  爬取活动表格                                                       │
│   GET https://fuxue.nankai.edu.cn/student/kbmhd/type/{1,2}/p/{N}.html     │
│   解析 <table class="typetable1"> → 筛选「活动时间晚于当日」→ 字典列表     │
│   测试功能开 → activities.csv；关 → 什么都不干                            │
└──────────────────────────────────────────────────────────────────────────┘
```

### 4.2 逐步详解

下面的"存到哪"一列是用户特别关心的部分，与 §7 呼应。

#### 步骤1 · 预访问 CAS 入口（只发 2 个请求）

| 项 | 内容 |
|---|---|
| 代码 | `step1_open_login_page(session)` — `cas_login.py:913` |
| 方法 | `GET` × 2（`allow_redirects=False`，手动只跟一跳） |
| URL | `https://xgb.nankai.edu.cn/login/fslogin` |
| 请求头 | `User-Agent`（伪装 Chrome 120） |
| 请求体 | 无 |
| 响应链 | `302 → https://iam.nankai.edu.cn/api/cas/login?service=https://xgb.nankai.edu.cn/login/fslogin`<br>`302 → https://iam.nankai.edu.cn/login?next=%2Fapi%2Fcas%2Flogin%3Fservice%3D...`（**代码在这里停住，不再跟第 3 跳**） |
| Set-Cookie | 第 1 跳：`PHPSESSID=...`（域 `xgb.nankai.edu.cn`）<br>第 2 跳：`csrf-token=...; session=...`（域 `iam.nankai.edu.cn`，`Max-Age=1800`，`HttpOnly`/`Secure`） |
| 响应体 | 第 3 跳才是 React SPA 登录页；**爬虫不需要它**，所以没必要去取 |
| **存到哪** | Cookie → **`session.cookies` 自动保存**（无需人工干预）<br>HTML → 只留作日志展示，后续步骤不再依赖 |

> ★ **为什么可以少跟一跳**：实测直接 `GET https://iam.nankai.edu.cn/login` **不种任何 Cookie**；`csrf-token` / `session` 是在第 2 跳 `/api/cas/login` 的响应头里种下的，而第 3 跳只是返回登录页 HTML。所以「手动停在第 2 跳」既拿到了必需 Cookie，又把 3 个请求压到 2 个。
>
> ★ **这一步不能省**。IAM 的 `csrf-token` / `session` 只能靠这次访问拿到；没有它们，步骤2 必然返回 `{"code":40000,...,"message":"参数错误"}`（实测）。

#### 步骤2 · POST 账号密码

| 项 | 内容 |
|---|---|
| 代码 | `step2_post_credentials(session)` — `cas_login.py:959` |
| 方法 | `POST` |
| URL | `https://iam.nankai.edu.cn/api/v1/login?os=web` |
| 请求头 | `Content-Type: application/json`（`requests` 用 `json=` 自动设置）<br>`X-Version-Check: 1`<br>`X-Fe-Version: 3.1.9.15606`<br>`Page-Name: portal`<br>`Accept-Language: zh-CN,zh;q=0.9`<br>`csrf-token: <从 iam 域 cookie 复制>`<br>`Cookie: csrf-token=...; session=...`（自动携带） |
| 请求体 | ```{"account":"<学号>","password":"<64位十六进制密文>","login_scene":"feilian","account_type":"userid"}``` |
| 响应 | `200` `{"code":0,...,"data":{"result":"success","next":{"action":"GoToLink","link":"/api/cas/login?service=https://xgb.nankai.edu.cn/login/fslogin"}}}` |
| **存到哪** | `code` 是否等于 `AUTH_SUCCESS_JSON_CODE`（0）→ 决定失败/继续<br>ticket → **响应体里没有**，转步骤2.5<br>Cookie → `session.cookies` 自动更新<br>返回值 → `{"ticket": None, "validate_url": ""}` 交给步骤2.5 的签发环节 |

**请求体字段的攻克过程**（这部分是实测踩出来的）：

| 请求体 | 服务端响应 | 解读 |
|---|---|---|
| `{account, password}` | `{"code":40000,"action":"alert","message":"参数错误"}` | 缺字段 |
| `+ login_scene:"feilian"` | 40000 参数错误 | 还缺 |
| `+ account_type:"ldap"` | 40000 参数错误 | 字段齐了但值不对（本站 LDAP 未启用） |
| `login_scene:"feilian"` + `account_type:"ldap"` | `10110002 当前认证方式不被允许` | **字段名全对**，只是认证方式不允许 |
| `login_scene:"feilian"` + `account_type:"userid"` | `10110001 用户名或密码错误` | ★ **请求体完全正确**，剩下的只是账号密码 |

> 这套"错误码递进"是排查接口参数的通用手法：**看错误信息从"参数错误"变成"业务错误"，就说明请求格式已经对了**。`step2_post_credentials()` 末尾对 `10110001` / `10110002` / `参数错误` 都做了针对性提示。

#### 步骤2.5 ~ 步骤4 · 现场签发 ST → 校验 → 落地（代码里合并为一个函数）

> 这三步本来就是一条连续的跳转链，代码把它们合进 `establish_login_state(session, issued)`（`cas_login.py:1077`）。
>
> **为什么合并**：
> 1. ticket 与跳转目标不再在三个函数之间传来传去，消除「参数传错 / 漏传」的整类 bug；
> 2. 「签发时的 service」与「校验时的 service」天然是同一个字符串——校验跳直接用签发跳返回的 `Location`，我们一次都不自己拼；
> 3. 三步共用同一个「中止条件」（被弹回登录页），合并后这个判断只写一遍。

##### 三个阶段的分工

| 阶段 | 原来的叫法 | 一句话 | `allow_redirects` |
|---|---|---|---|
| ① 签发 | 步骤2.5 | 用已登录的 iam 会话，换一张一次性的 ST | **`False`**（必须读到 `Location`） |
| ② 校验 | 步骤3 | 把 ST 交给业务系统，让它去 CAS 换回登录态 | `True`（票本来就该在这一跳被用掉） |
| ③ 落地 | 步骤4 | 跟完最后的跳转，落到业务首页 | 同上（自动完成） |

##### 阶段① 签发 ST（原步骤2.5）

| 项 | 内容 |
|---|---|
| 代码 | `establish_login_state()` 第 ① 段 — `cas_login.py:1111-1136`（决定性请求在 `cas_login.py:1118-1119`） |
| 方法 | `GET` |
| URL | `cas_entry_url()` 生成：`https://iam.nankai.edu.cn/api/cas/login?service=https%3A%2F%2Fxgb.nankai.edu.cn%2Flogin%2Ffslogin`<br>（`service` 必须 `quote(safe="")` 整段编码，否则其中的 `?` `&` 会被当成参数分隔符） |
| 关键参数 | `allow_redirects=False` |
| 请求头 | `User-Agent`；`Cookie: csrf-token=...; session=...`（步骤1 种下的，自动携带） |
| 预期响应 | `302`，`Location: https://xgb.nankai.edu.cn/login/fslogin?ticket=ST-<时间戳>-<32位随机串>` |
| 服务端副作用 | **生成并登记**这张 ST，等待被校验 |
| 我们取什么 | `ticket`（正则从 `Location` 抠）、`validate_url`（`urljoin(入口, Location)`） |
| 失败长什么样 | 没登录 / 会话过期 → `Location` 指向 `/login?next=...`，抠不到 `ticket=` → 报「没拿到 ticket」并中止 |

> ★ 这一跳**必须**拦。理由见 §7.6 的核心原理：3xx 响应一旦被自动跟掉，`Location` 就没了，而票只存在于那里。

##### 阶段② 校验 ST（原步骤3）

| 项 | 内容 |
|---|---|
| 代码 | `establish_login_state()` 第 ② 段 — `cas_login.py:1138-1177`（决定性请求在 `cas_login.py:1150-1151`） |
| 方法 | `GET` |
| URL | **就是阶段① 的 `Location`**（`validate_url`），不是自己拼的 |
| 关键参数 | `allow_redirects=True` |
| 请求头 | `User-Agent`；`Cookie` 自动携带 |
| 预期响应 | `302`，`Location: /home/user/login.html`（**相对路径**），同时 `Set-Cookie: CASTGC=...`（域 `xgb.nankai.edu.cn`） |
| 语义 | ★ **CAS 在这里把票标记为「已使用」**——整条链上唯一一次消费票据的地方 |
| 为什么必须 `urljoin` | 相对 `Location` 直接拿去请求会抛 `MissingSchema` |

##### 阶段③ 落地（原步骤4）

| 项 | 内容 |
|---|---|
| 执行者 | 由阶段② 的 `allow_redirects=True` 自动完成：如果 `/home/user/login.html` 还返 302，`requests` 会继续跟 |
| 预期终点 | `200 https://xgb.nankai.edu.cn/home/user/login.html` |
| 副产品 | 这个 HTML 正是步骤4.5 要用的门户页，**直接复用，不再重新 GET** |
| 我们取什么 | `{"status", "final_url", "html"}` → 主流程里叫 `landing` |

##### 完整时序（把三段串起来）

```
①  GET iam/api/cas/login?service=<xgb>          allow_redirects=False
    ← 302 Location: xgb/login/fslogin?ticket=ST-1
    ticket = ST-1 ; validate_url = 上面那个 Location（已补成绝对地址）

②  GET xgb/login/fslogin?ticket=ST-1            allow_redirects=True
    ← 302 Location: /home/user/login.html  +  Set-Cookie: CASTGC=...
    （ST-1 就在这一刻被消费，之后作废）

③  GET xgb/home/user/login.html
    ← 200 门户页 HTML                            ← 最终落地，也是步骤4.5 的输入
```

##### 三步共用的判据

| 检查点 | 通过条件 | 不通过的结论 |
|---|---|---|
| 阶段① 有 `Location` | `issued.headers["Location"]` 非空 | 没登录 / 会话失效 → 中止 |
| 阶段① 能抠出 ticket | 正则命中 `ticket=` | 同上 |
| 阶段②/③ 终点不是认证域 | 最终 `netloc` 不在 `auth_hosts()` 里，或路径不是 `/login` | 被弹回登录页 → `[失败]` |
| 阶段③ 落地页无失败关键词 | `visible_text()` 不含 `LOGIN_FAIL_KEYWORDS` | `[失败]` |

##### 常见失败对照

| 现象 | 原因 |
|---|---|
| 阶段① 的 `Location` 是 `/login?next=...` | 步骤2 其实没登录成功（会话没建立） |
| 阶段① 抠不到 `ticket=` | 同上；或 `service` 与 CAS 登记值不一致（CAS 拒绝签发） |
| 阶段② 又 302 回 `iam.../login?next=...` | 票已被用过 / 已过期 / `service` 不匹配 |
| 阶段③ 落到 `xgb/login/index.html` | 业务系统拒绝票，或这张票不是给它的 |
| 前三步全绿但复核失败 | xgb 登录了、fuxue 还没登录——**这是正常现象**，需要步骤4.5 |

> 三个阶段在日志里分别打 `[步骤2.5]` / `[步骤3]` / `[步骤4]` 前缀，和原流程图一一对得上。

> ★ **`service` 只允许有一处真相来源**：配置区只写 `CAS_SERVICE_URL`，签发地址由它自动拼出来（`cas_entry_url()`，`cas_login.py:835`）。若手工填了 `CAS_TICKET_URL`，`check_config()` 会校验它里面必须含同一个 `service`，否则启动时就报错——因为这个错误在运行期只会表现为「没拿到 ticket」，极难定位。

> ★ **为什么必须拦住 302、票据为什么一次性**，见 §7.6 的完整原理；**浏览器为什么不需要拦**，见 §7.8。

> **注意**：`VERIFY_SSL`、`REQUEST_TIMEOUT` 这类参数虽然不写进 URL，但都必须显式给出——`requests` **默认没有超时**，不给就可能在网络抖动时永久卡死。

#### 步骤4.5 · 通过门户页中转进 fuxue ★ 本项目核心

| 项 | 内容 |
|---|---|
| 代码 | `step4_5_enter_business_system(session, landing=None)` — `cas_login.py:1238`<br>辅助：`find_sso_entry_links()` — `cas_login.py:1207`；`extract_html_redirect()` — `cas_login.py:1179` |
| ① 门户页 | **直接复用步骤4 的落地页 HTML**（同一地址 `SSO_PORTAL_URL`，省 1 个请求）；只有地址对不上时才重新 `GET https://xgb.nankai.edu.cn/home/user/login.html` |
| ② 现抓入口 | 用正则抠出所有 `<a>`，只保留地址里含 **`orlogin`** 的（用 `SSO_LINK_KEYWORD="辅学"` 选中目标） |
| ③ 点进入口 | `GET http://fuxue.nankai.edu.cn/Home/Login/orlogin/type/1/id/<学号>/token/<32位随机串>.html`，**Referer = 门户页地址** |
| **存到哪** | fuxue 域 Cookie → `session.cookies` 自动保存<br>落地地址 + HTML → `dict` 返回，赋给 `enter` |

门户页里抓到的真实 HTML 结构（实测）：

```html
<li>
  <a href="http://fuxue.nankai.edu.cn/Home/Login/orlogin/type/1/id/<学号>/token/<32位随机串>.html"
     target="_blank">
    <div class="img img1"></div>
    <p>立公增能辅学平台</p>
  </a>
</li>
```

**这一步为什么必须"现抓"而不能写死**：

- `token` 是 32 位随机串，**门户页每次访问时服务端现生成**，与学号绑定；
- `id/<学号>` 也随账号变化；
- 用户最初就是「在门户页里手动点这个链接」才进到 fuxue 的——代码只是把这次点击自动化。

#### 复核 · verify_login_state

| 项 | 内容 |
|---|---|
| 代码 | `verify_login_state(session)` — `cas_login.py:1539`；分类器 `classify_auth_redirect()` — `cas_login.py:1448`；单请求判定 `verify_single_request()` — `cas_login.py:1500` |
| 方法 | `GET` × 1（`VERIFY_MODE="single"`，默认）；`verbose` 模式为 3 个请求 |
| URL | `VERIFY_LOGIN_URL = https://fuxue.nankai.edu.cn/student/kbmhd/type/1/p/1.html` |
| 关键参数 | `single`：`allow_redirects=True` 一路跟到底，只看**最终落地**的域名与路径<br>`verbose`：`allow_redirects=False` 逐跳读 `Location`，再用 `classify_auth_redirect()` 分类 |
| 判定 | 最终落在认证域且路径是 `/login` → `[失败]`；可见文字含失败关键词 → `[失败]`；否则 `[成功]` |

`classify_auth_redirect()` 的分类规则（**顺序不能变**）：

| 顺序 | 条件 | 归类 | 说明 |
|---|---|---|---|
| ① | 路径含 `/api/cas` | `issuer` | CAS **签发票据**的接口，不是登录页 |
| ② | 路径含 `/api/` | `other` | ★ 必须在 ③ 之前，否则 `/api/v1/login` 会因以 `/login` 结尾被误判成登录页 |
| ③ | 路径 `rstrip('/')` 后以 `/login` 结尾 | `login` | 真·登录页 |
| ④ | 路径**第一段**是 `login` | `login` | 覆盖 fuxue 的 `/login/index.html`；只认第一段，所以 `/home/user/login.html` 仍是 `other` |
| ⑤ | 主机 == CAS 主机且不含 `/api/` | `login` | 兜底 |
| ⑥ | 其他 | `other` | 通常是业务系统自己的地址 |

#### 步骤5 · 爬取

见 §8。

---

## 5. 数据包详解

### 5.1 步骤2 成功响应

```json
{
  "code": 0,
  "data": {
    "result": "success",
    "next": {
      "action": "GoToLink",
      "link": "/api/cas/login?service=https://xgb.nankai.edu.cn/login/fslogin"
    }
  }
}
```

| 字段 | 含义 |
|---|---|
| `code` | `0` = 成功；`40000` = 参数错误；业务错误码另见下 |
| `data.result` | `"success"` = 认证通过 |
| `data.next.action` | `"GoToLink"` = 服务端告诉前端"接下来去访问这个地址" |
| `data.next.link` | **相对路径**的 CAS 签发地址，正是步骤2.5 要 GET 的那个 |

> **注意**：`next.link` 与 `data.link` 都是**相对路径**。本项目没有直接用它，而是由 `CAS_TICKET_ENDPOINT`（不含 service 的固定入口）+ `CAS_SERVICE_URL` 现场拼出完整签发地址（`cas_entry_url()`，`cas_login.py:835`）——因为 `service` 必须与登记值完全一致，由配置显式控制更安全。若手工填了 `CAS_TICKET_URL`，`check_config()` 会校验它与 `CAS_SERVICE_URL` 一致。

**建议的改进**（未实施）：可以优先用 `next.link` 配合 `urljoin` 推导签发地址，只在缺失时回退到配置拼出来的地址，这样站点改版时更鲁棒。取舍详见 §5.6。

### 5.2 步骤2 各类失败响应

```json
{"code":40000,"action":"alert","message":"参数错误"}
```
→ 请求体缺字段，或缺少 csrf-token 会话（例如跳过了步骤1）。

```json
{"code":10110001, "...": "..."}
```
→ **用户名或密码错误**。这是"好消息"：说明请求格式完全正确。

```json
{"code":10110002, "...": "..."}
```
→ **认证方式不被允许**，检查 `AUTH_API_EXTRA_FORM["account_type"]`（本站只支持 `userid`）。

页面/接口出现 `登录失败`、`用户名或密码错误`、`密码错误`、`账号或密码错误` 等关键词 → 由 `LOGIN_FAIL_KEYWORDS`（`cas_login.py:328`）捕获。

### 5.3 步骤2 请求头溯源（前端源码）

`POST /api/v1/login?os=web` 的请求头不是猜的，是从前端 webpack 模块里逆出来的：

**模块 255866（`request()` 助手）**：

```js
credentials: "include",
headers: {
  ...A(),
  "content-type": "application/json",
  ...getCsrfTokenHeaders()
}
```

**`A()`** 返回：

```js
{
  "X-Version-Check": window.globalInfo.VERSION_CHECK ? "1" : "0",
  "X-Fe-Version":    <globalInfo.VERSION 去掉 "portal-fe-">,   // "3.1.9.15606"
  "Accept-Language": navigator.language,
  "Page-Name":       x ? "admin" : "portal"
}
```

**模块 618427（`getCsrfTokenHeaders()`）**：

```js
return {
  "admin-csrf-token": <cookie admin-csrf-token>,
  "csrf-token":       <cookie csrf-token>
};
```

**查询串**：`request()` 会给所有接口统一追加 `os=web`（本项目用 `AUTH_API_EXTRA_QUERY` 实现）。

> 少了这些头服务端**不报错**，但保持与浏览器一致更稳妥，也能降低被 WAF 拦的概率。

### 5.4 步骤2.5 的响应

```
HTTP/1.1 302 Found
Location: https://xgb.nankai.edu.cn/login/fslogin?ticket=ST-<时间戳>-<32位随机串>
```

- `ST-<时间戳>-<32 位随机串>` 的结构：前面是签发时间（毫秒级），后面是随机串；
- **一次性**：用过即失效；
- **短时效**：几十秒内必须用掉；
- 因此代码里 `extract_ticket()` 会同时支持从**响应体 JSON**（`ticket` / `serviceTicket` / `TICKET`，支持嵌套一层）和**`Location` 响应头**两处提取。

### 5.5 SSO 中转入口链接（步骤4.5）

```
http://fuxue.nankai.edu.cn/Home/Login/orlogin/type/1/id/<学号>/token/<32位随机串>.html
```

| 片段 | 含义 |
|---|---|
| 协议 | **`http`**（门户页给的就是明文 http，不是 https） |
| `/Home/Login/orlogin` | 门户页 SSO 中转的标准格式 |
| `/type/1/` | 入口类型 |
| `/id/<学号>/` | 学号 |
| `/token/<32位>` | **每次现生成的随机令牌**，与学号绑定 |

### 5.6 为什么步骤2 的 POST URL 上没有 `next=%2Fapi%2Fcas%2Flogin%3Fservice%3D...`

一句话结论：

> **`next` 是「登录页（前端路由）」的参数，不是「登录接口」的入参。在 API 流程里，「登录完该去哪」由三样东西承担：①步骤1 里 `?service=` 登记到 iam 会话的上下文；②登录接口**响应体**里的 `data.next.link`；③前端路由自己 `router.push`。这三者都不需要把 `next` 塞进 POST 的 URL。**

#### 实测证据（本机只读探测）

| 请求 | 响应 | 说明了什么 |
|---|---|---|
| `GET /api/cas/login?service=<xgb 地址>` | `302`，`Location: /login?next=%2Fapi%2Fcas%2Flogin%3Fservice%3Dhttps%253A%252F%252Fxgb.nankai.edu.cn%252Flogin%252Ffslogin`，并 `Set-Cookie: csrf-token=...; session=...` | `service` 被**编码进 `next`** 一并交给前端；两个 Cookie 在这一跳种下 |
| `GET /api/cas/login?service=<fuxue 地址>` | 同上结构，只有 `next` 里的 service 不同 | `next` 就是个把 service 原样透传的外壳 |
| `GET /api/cas/login`（不带 service） | `302`，`Location: /login?next=%2Fapi%2Fcas%2Flogin` | 没登记 service，`next` 里也就没有 |
| `GET /login?next=...` | `200`，5661 字节，**`Set-Cookie: None`**；HTML 里出现 `next` 1 次，而 `service` / `cas/login` / `fslogin` 各出现 **0** 次 | 登录页是纯静态 SPA 壳：`next` 只在浏览器地址栏里、由前端 JS 运行时读取，**服务端完全没碰它** |

#### 浏览器里的真实顺序

```
地址栏  /login?next=%2Fapi%2Fcas%2Flogin%3Fservice%3D...
   │  前端 JS 读 window.location.search 里的 next（只在内存里）
   ▼
用户填账号密码 → POST /api/v1/login?os=web      ← URL 上不需要 next
   │  响应体 data.next.link = "/api/cas/login?service=..."
   ▼
前端 router.push(data.next.link)                ← 前端自己决定跳哪
   ▼
浏览器 GET /api/cas/login?service=...
   ← 302 Location: ...?ticket=ST-...
```

#### 为什么把 `next` 塞进 API 的 URL 是错的

1. **不是这个接口的契约**。`POST /api/v1/login` 的入参是凭据（`account` / `password` / `login_scene` / `account_type`），查询串只有前端统一追加的 `os=web`。多带一个 `next` 不但无用，将来若服务端改成「查询串白名单校验」，反而会把请求打挂。
2. **前端在发请求时根本不需要它**。`next` 是「页面级」状态，前端拿到 `data.next.link` 后自己 `router.push`，与登录 POST 是两件事。
3. **它替代不了 Cookie 的作用**。真正让「这次登录属于哪个 service 上下文」生效的，是步骤1 第 2 跳种下的 `csrf-token` / `session`。缺了这个会话，POST 会直接返回 `参数错误`（见 §5.2）——**有没有 `next` 都一样**。

#### 那我们的代码为什么不直接用 `data.next.link`？

`step2_post_credentials()`（`cas_login.py:959`）目前只从响应里取 `ticket` 与 `validate_url`，签发入口由 `cas_entry_url()`（`cas_login.py:835`）用 `CAS_TICKET_ENDPOINT + CAS_SERVICE_URL` 拼出来。

- **好处**：签发地址完全由配置决定、可预测、可校验（`check_config()` 会检查手工填的 `CAS_TICKET_URL` 与 `CAS_SERVICE_URL` 是否一致）。
- **代价**：如果服务端因为某种原因把 `service` 记成了别的值，我们看不见。

> 可选改进（**未实现**）：让 `establish_login_state()` 的签发入口优先取步骤2 响应里的 `data.next.link`，配置拼的作为兜底。这样能 100% 跟随服务端的选择，但会削弱「配置即真相」这条原则。当前代码选择的是后者。

> 与本问题相关的另一面——**为什么浏览器不拦 ticket 也能到站**——见 §7.8。

---

## 6. 密码加密算法

### 6.1 前端原始源码

来自 `https://iam.nankai.edu.cn/portal-static/index_0a037d97_68f2c41350087c1ec359.js` 的 webpack 模块 **292684**：

```js
a.aesEncrypt = function(e) {
  var a = (o = Number.MAX_SAFE_INTEGER.toString(),
           r.default.MD5(o).toString()).toString(),
      _ = r.default.SHA1(r.default.enc.Utf8.parse(a));
  var o;
  return r.default.AES.encrypt(e, r.default.enc.Utf8.parse(a), {
    iv: r.default.enc.Utf8.parse(_.toString(r.default.enc.Hex)),
    mode: r.default.mode.CBC,
    padding: r.default.pad.Pkcs7
  }).ciphertext.toString(r.default.enc.Hex)
}
```

其中 `r = require(544388)` = **crypto-js**；`sha256` 用的是 **js-sha256**（模块 632469，另一个 chunk 文件）。

### 6.2 逐步对照：前端 → Python

| 步骤 | CryptoJS（前端） | Python | 结果 |
|---|---|---|---|
| 种子 | `Number.MAX_SAFE_INTEGER.toString()` | `str(2**53 - 1)` | `<2^53−1 的十进制写法，★ 值已隐去>` |
| 密钥 | `MD5(seed).toString()` | `hashlib.md5(seed.encode()).hexdigest()` | `<32 位十六进制，★ 值已隐去>` |
| 密钥字节 | `enc.Utf8.parse(keyHex)` | `key_hex.encode("utf-8")` | 32 字节 → **AES-256** |
| IV 全文 | `SHA1(enc.Utf8.parse(keyHex)).toString(Hex)` | `hashlib.sha1(key_hex.encode()).hexdigest()` | `<40 字符十六进制，★ 值已隐去>` |
| IV 字节 | `enc.Utf8.parse(ivFull)`（AES 只用前 16 字节） | `iv_full.encode("utf-8")[:16]` | `<IV 全文的前 16 个字符，★ 值已隐去>` |
| 加密 | `AES.encrypt(plain, key, {iv, mode:CBC, padding:Pkcs7})` | `AES.new(key, AES.MODE_CBC, iv)` + PKCS7 填充 | — |
| 输出 | `.ciphertext.toString(enc.Hex)` | `ciphertext.hex()` | 小写十六进制，**无 OpenSSL 头、无 salt** |

### 6.3 推导出的常量（★ 具体数值一律隐去，只保留推导式）

> 本节**不再写出** seed / `key_hex` / `iv` 的字面值。原因：它们等价于一把硬编码对称密钥，
> 写在文档里等于把"能解密/重放所有人登录请求"的能力一起交出去。
> 需要复现时，按下面四行推导式在本地算即可（算法本身是公开的，保密的是那份具体密钥材料）。

```
seed       = Number.MAX_SAFE_INTEGER.toString()          # 十进制写法，具体值见 cas_login.py 的 AES_KEY_SEED
key_hex    = MD5(seed)                                   # 32 个 ASCII 十六进制字符
key        = key_hex.encode("utf-8")                     # 32 字节 → AES-256
iv_hex_40  = SHA1(key_hex.encode("utf-8")).hexdigest()   # 40 个 ASCII 十六进制字符
iv         = iv_hex_40.encode("utf-8")[:16]              # 取前 16 个字符 → 16 字节
mode       = CBC
padding    = PKCS7
output     = 小写十六进制字符串
```

**核对方式（不泄漏内容）**：运行 `python cas_login.py` 时，自检会打印
`key 指纹` / `iv 指纹`（各自 SHA-256 前 12 位 + 字节数）。比对这个指纹就能确认
"本地推导的 key 与线上前端一致"，而无需要把 key 本身写进任何文件。

### 6.4 验证向量（明文均为中性示例，不含任何真实凭据）

| 明文 | 密文 |
|---|---|
| `demo-1234` | `2b563d38ed5102f50e661c2cc33fec40` |
| `123456` | `795ca10dec2207075b09655bf69a0c9f` |
| ``（空串） | `ddb040db3f7e4cb28538d702c1846c5d` |

> **注**：上表三行是用实际 key/iv 算出的密文（相当于"已知明文/密文对"，但**无法反推出 key**）。
> 本文件**不再给出 key/iv 字面值**；拿到 key/iv 后，用 `AES-256-CBC` + `PKCS7` 即可复算校验。
> 源码 `cas_login.py` 的启动自检（`self_test_crypto()`）用的就是第一行 `demo-1234`，那是一条**中性测试串**，不含任何真实凭据。

**验证方法**（关键方法论）：把站点自己的 22 个 JS chunk 全部下载到本地，塞进 Node 的 `vm` 沙箱，伪造 `self.webpackChunkweb.push` 捕获模块，再手写一个迷你 webpack `require` 垫片，**直接执行站点自带的 crypto-js**，然后与本项目的实现逐条比对。这样得到的结论不依赖"我猜对了加密细节"。

### 6.5 双后端实现

`cas_login.py` 里的加密区（`cas_login.py:509`–`752`）提供了两条完全等价的路径：

| 后端 | 触发条件 | 说明 |
|---|---|---|
| `pycryptodome` | `from Crypto.Cipher import AES` 成功 | 优先，性能好 |
| `pure-python` | 上面失败时自动启用 | **文件内置**的 AES-256 实现：GF(2⁸) 对数/反对数表构造 S-box、`_aes_expand_key()`（Nk=8/Nr=14/60 words，含 AES-256 特有的 `i % nk == 4` 额外 SubWord）、`_aes_encrypt_block()`（AddRoundKey/SubBytes/ShiftRows/MixColumns，末轮不做 MixColumns）、`_encrypt_with_pure_python()` 做 CBC 链式 |

**实测**：两条后端 + 一个独立的 pycryptodome 参考实现，对 8 组输入（源码里的自检明文、空串、单字符、16 字节整块、17 字节、中英符号混合、200 个 `x` 等）**8/8 完全一致**，且都命中 §6.4 的验证向量。

### 6.6 自检机制

`AES_SELF_TEST = True` 时，`main()` 会在**发出任何请求之前**先跑 `self_test_crypto()`（`cas_login.py:735`）：加密源码里硬编码的自检明文（**真实口令，本文不写出**），与同一处硬编码的期望密文比对。**不一致就直接退出**，避免用错误的密文去登录导致账号被风控。

### 6.7 ⚠️ 这是「混淆」，不是「安全」

必须讲清楚：

- 密钥/IV **硬编码在前端 JS 里**，任何人打开 DevTools 都能拿到；
- 算法是**对称可逆**的，拿到密钥就能解密，也能伪造密文重放；
- 它**不提供**任何机密性保证——HTTPS 才是真正的传输保密；
- 它**不能**防中间人，也不能防重放。

那它有什么用？**唯一的实际收益**是把明文密码从 DevTools 的 Network 面板、代理日志、WAF 审计日志里抹掉，降低"密码被旁观者顺手记下"的概率。**真正的防护应当来自服务端**：TLS + 服务端加盐哈希（bcrypt / scrypt / Argon2）。

> 顺带一提：`account`（账号）字段是**明文**发送的，前端就没加密它。

---

## 7. 代码中的「服务器返回数据存储」逻辑

这一章是理解"为什么有些数据能自动保存、有些必须手动接住"的关键。

### 7.1 自动保存：`requests.Session.cookies`

`requests.Session` 持有一个 `RequestsCookieJar`，在**每次请求返回时自动解析 `Set-Cookie` 头并写入**，在**下次请求时按域/路径匹配自动带上**。

```python
session = requests.Session()          # cas_login.py:757 build_session()
# 之后每一次 session.get() / session.post() 都会自动读写这个 jar
```

**特性**：

| 特性 | 说明 |
|---|---|
| 按域隔离 | `iam.nankai.edu.cn` 的 cookie 不会发给 `fuxue.nankai.edu.cn` |
| 路径匹配 | 遵守 `Path` 属性 |
| 自动更新 | 同名 cookie 被 `Set-Cookie` 覆盖 |
| 覆盖过期 | 遵守 `Max-Age` / `Expires` |
| 重定向中也会存 | 只要 `Session` 在发请求，**每一跳**的 `Set-Cookie` 都会被收下——这是 `allow_redirects=True` 的隐藏价值 |

**所以这几类数据完全不用管**：`PHPSESSID`、`CASTGC`、`csrf-token`、`session`，以及 fuxue 的登录态 cookie。

代码里 `dump_cookies(session, title)`（`cas_login.py:805`）只是**打印**这个 jar，用于观察，不参与逻辑。

### 7.2 必须手动保存：只在响应里出现一次的瞬时数据

| 数据 | 出处 | 为什么必须手动存 |
|---|---|---|
| **ticket（ST）** | 步骤2.5 的 302 `Location` 头 | 它**不在任何 cookie 里**，只在那一次响应的 `Location` 出现一次；响应对象一结束就没了。必须 `re.search` 抠出来存进 `issued["ticket"]` |
| **`issued["validate_url"]`** | 步骤2.5 的 302 `Location` | 签发与校验两边的 `service` 必须逐字符一致，所以步骤3 直接用服务器给的地址，而不是自己拼字符串 |
| **`landing`** | 步骤2.5~4 的最终地址与 HTML | 汇总日志要打印；`landing["final_url"]` 是观测"到底落在哪"的唯一依据，也是步骤4.5 复用的门户页 HTML |
| **`enter`** | 步骤4.5 的落地结果 | 同上 |
| **`referer`** | 逐跳传递 | `requests` **不会**在自动跳转中补 `Referer`，必须每跳手动传 |
| **`csrf-token`** | 步骤1 的 cookie | 严格说它在 jar 里，但要从 jar **复制到请求头**，这一步是手动的（`cas_login.py:1005-1008`） |
| **门户页的 SSO 入口 URL** | 步骤4.5 的门户页 HTML | 带现生成的 token，必须当次用完 |

**一句话原则**：

> **Cookie 是"会话状态"，由 jar 自动接管；票据和跳转目标是一次性的"消息内容"，必须当场接住存进变量。**

### 7.3 为什么响应对象不能跨步骤用

```python
response = session.get(url)     # response 只在这个函数里有效
return response.text            # 要么把内容 return 出去
```

`requests` 的 `Response` 对象在流式模式下可能持有连接；跨步骤持有它既占资源也容易误用。本项目的做法是：**函数内解析完，只把需要的东西以 `dict` 返回**（例如 `step1_open_login_page()` / `establish_login_state()` / `step4_5_enter_business_system()` 都返回 `{"status": ..., "final_url": ..., "html": ...}` 这类字典）。

### 7.4 三种返回形态的分工

| 形态 | 承担什么 | 例子 |
|---|---|---|
| **临时变量** | 记忆：在同一次运行内传递 | `ticket`、`hop`、`landing`、`enter` |
| **函数返回值** | 传递：跨函数、明确边界 | `step2_post_credentials() -> dict`；`establish_login_state() -> dict`；`step4_5_enter_business_system() -> dict` |
| **日志** | 可观测：给人看，也用于事后排障 | `log("步骤2", "[OK] 已提取 ticket = ...")` |

**关键设计**：**票据本身不落盘、不进日志文件、不写进配置**。日志只打印一次用于确认提取成功，运行结束即随进程消失。

### 7.5 `response.history` 的用法

当 `allow_redirects=True` 时，`requests` 把中间每一跳记在 `response.history` 里，最终响应是 `response` 本身：

```python
response = session.get(url, allow_redirects=True)
for item in response.history:
    print(item.status_code, item.headers.get("Location"))
print("最终落地：", response.url)
```

本项目用它来**可视化票据是在哪一跳被消费的**——`establish_login_state()` 的校验跳与 `step4_5_enter_business_system()` 都打印了完整跳转链，这是排查 SSO 问题最有用的信息。反过来，当需要"亲眼看到 302"时就用 `allow_redirects=False`（签发跳和复核的 verbose 模式都必须如此）。

### 7.6 302 跳转：什么时候自动跟，什么时候必须自己发 GET

**一句话规律**：

> 不需要在中途读数据的跳转 → 让 `requests` 自动跟；
> 中途藏着东西（`ticket` / `Location` / `Set-Cookie` / 需要补 `Referer`）→ 必须 `allow_redirects=False` 自己发 GET。

`requests` 的默认值就是 `allow_redirects=True`，所以**凡是不写这个参数的地方，都是自动跟的**。本项目里只有 4 处主动写 `False`，其余全是自动。

#### 情形一：自动完成（`allow_redirects=True` 或省略）

| 位置 | 代码 | 说明 |
|---|---|---|
| 步骤2.5~4 的②校验跳 | `cas_login.py:1151` | **这一跳就是要让票被消费**：302 → 业务首页 → 写 `CASTGC`，全由 requests 跟完 |
| 步骤4.5 门户页 | `cas_login.py:1283` | 未传参数，走默认 `True`；门户页本身就是最终落地页 |
| 步骤4.5 的 JS/meta 补跟 | `cas_login.py:1375` | 页内前端跳转那几跳交给 requests 自动跟（requests 不执行 JS，所以先自己解析出目标再请求） |
| 登录态复核（single 模式） | `cas_login.py:1513` | 默认 `VERIFY_MODE = "single"`，一次请求跟到底，靠最终落地的域名判断 |
| 步骤5 爬虫 | 页面 GET | 只是取数据，跟完无所谓 |

#### 情形二：必须自己发（`allow_redirects=False`）

| 位置 | 代码 | 为什么必须手动 |
|---|---|---|
| 步骤1 的两跳 | `cas_login.py:935` / `947` | 第 2 跳的响应头里才有 `csrf-token` / `session`；第 3 跳只是登录页 HTML，白跑 |
| 步骤2.5~4 的①签发跳 | `cas_login.py:1119` | **最关键的一条**：ticket 只存在于 302 的 `Location` 响应头里。详见下节「核心原理」 |
| 步骤4.5 手动跟链 | `cas_login.py:1342` | ★ 见下 |
| 登录态复核（verbose 模式） | `cas_login.py:1569` / `1582` / `1602` | 要读 `Location` 才知道被踢去了哪个登录页 |

#### ★ 核心原理：不拦 302，就等于把 ticket 当场用掉

这是 CAS/SSO 里最容易踩、也最不直观的一个坑，值得单独讲清楚。

**前提：票据是"一次性"的。** 业务系统拿 ST 来校验时，CAS 的语义是：

```
GET /api/cas/login?service=<业务地址>&ticket=ST-xxx
  ├─ 票据有效且未用过 → 标记为【已使用】，302 到 <业务地址>
  └─ 票据无效 / 已用过 → 302 回登录页（或错误页）
```

也就是说，**"校验"这个动作本身就带副作用**：它不是"查询这张票对不对"，而是"用掉这张票"。一张 ST 只有一次机会。

**那么 `allow_redirects=True` 会发生什么？**

```
我  们:  GET /api/cas/login?service=...&ticket=ST-xxx        ← 假设我们没拦
        ← 302 Location: https://xgb.../login/fslogin?ticket=ST-xxx
        ↓ requests 在内部立刻自己发了下一跳
requests: GET https://xgb.../login/fslogin?ticket=ST-xxx      ← 票在这里被消费
         ← 302 /home/user/login.html
         ← 200 首页，response 交给我们
```

代码层面看，`response` 是 200，`response.history[0]` 里还能看到那张 `ST-xxx`，好像什么都没丢。**但服务器端那张票已经是"已使用"状态了。** 如果这时再手工拿它发一次校验：

```
GET https://xgb.../login/fslogin?ticket=ST-xxx    ← 第二次使用
  → 302 回登录页（ticket 已失效 / 已使用）
```

结果就是：账面上一路 302 到首页，实际会话没建立，最后「登录态复核」报 `[失败]`，而日志里看不出任何异常——因为**每一次请求都是成功的 HTTP 响应**，只是语义错了。

**为什么 `response.history` 救不了？**

`history` 是"事后可观测"，不是"事后可用"。你能从里面读到那张 `Location`，但读到的是一张**已经作废**的票。可观测性 ≠ 可控性。

**正确姿势：把"能读 3xx"的权利留在自己手里。**

```python
# ① 签发：必须 allow_redirects=False
issued = session.get(cas_entry_url(), timeout=REQUEST_TIMEOUT, allow_redirects=False)   # cas_login.py:1119
location = issued.headers.get("Location")                  # 票只在这里
ticket   = re.search(r"[?&]ticket=([^&#]+)", location).group(1)
validate_url = urljoin(cas_entry_url(), location)          # ★ 相对路径要补绝对

# ② 校验：这时候才 allow_redirects=True —— 这张票本来就该在这一跳被用掉
landed = session.get(validate_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)       # cas_login.py:1151
```

两跳各司其职：**先"接住票"，再"用掉票"**。前者必须拦，后者必须放。

**症状对照表**：

| 现象 | 真实原因 |
|---|---|
| 签发那步 `Location` 是 `None` / 抠不到 ticket | 忘了 `allow_redirects=False`，3xx 响应被 requests 吞掉了 |
| 校验那步 302 回 `iam.../login?next=...` | 用的是一张已经被消费过的票 |
| 校验那步 302 回业务系统的 `/login/index.html` | 票根本没被认（`service` 与签发时不一致），或票已过期 |
| 复核 `[失败]` 但前面每步都是 200 | 会话没建立，但每一步 HTTP 状态都"成功"——最典型的票据误用 |
| 想靠 `response.history` 回捞票据 | 捞到的是已作废的票，救不了 |

> ⚠️ **这条规律有适用边界**：它约束的是「票被消费后你**还要自己再用一次**」的场景。如果只是像浏览器那样「一路自动跟、只看最终落在哪」，不拦 302 反而是**正确且安全**的——因为浏览器本来就是要让票被消费掉。详见 §7.8。

#### 步骤4.5 为什么非要手写循环

这是全项目唯一一个「自己写 `for` 循环跟 302」的地方，原因写在 `cas_login.py:1331-1332`：

> `requests` 自动跟跳转时**不会补 `Referer`**，而真实浏览器每一跳都会带上。

所以这里用 `allow_redirects=False` + 手动循环，自己把上一跳地址塞进下一跳的 `Referer`：

```python
for _hop in range(10):                                       # 最多跟 10 层 302
    hop_headers = dict(headers)                              # 复制基础请求头
    if current_referer:                                      # ★ 有上一跳地址就带上 Referer
        hop_headers["Referer"] = current_referer             # 浏览器就是这么传的
    response = session.get(current_url, timeout=REQUEST_TIMEOUT,
                           allow_redirects=False, headers=hop_headers)
    location = urljoin(current_url, response.headers["Location"]) \
               if response.headers.get("Location") else ""    # ★ 相对路径必须补成绝对地址
    if response.is_redirect is False or not location:        # 不是跳转 = 落地了
        break
    if location in visited:                                  # 已经走过 = 成环
        break
    visited.add(location)
    current_referer = current_url                            # ★ 下一跳的 Referer = 这一跳的地址
    current_url = location                                   # 推进到下一跳
```

手动跟还有个附带好处：**每一跳的 `Set-Cookie` 都能逐个打印出来**（`cas_login.py:1347-1355`）；自动跟时你只看得到最终那一个响应。

#### ⚠️ 两个容易踩的坑

1. **`allow_redirects=False` 返回的是 3xx 响应本身**，`response.url` 仍是你请求的那个地址，**不是你被跳去的地方**。目标地址必须从 `response.headers["Location"]` 读。
2. **`Location` 可能是相对路径**（本项目校验那一跳就返回过 `/home/user/login.html`），必须用 `urljoin()` 补成绝对地址再发下一次 GET；否则会因为缺少协议头而报错（`urllib` 抛 `ValueError: unknown url type`，`requests` 抛 `MissingSchema`）。

> **判断口诀**：先问自己"我需不需要在这一跳的**响应头**里拿东西？"——需要（`Location` / `Set-Cookie` / `Referer` 传递），就 `False` + 自己发；不需要，就交给 `requests` 自动跟。
>
> **再加一句针对票据的**：凡是一次性凭证（ticket / token / 验证码），**产生它的那一跳永远拦，消费它的那一跳永远放**。顺序反了，凭证就白生成了。

### 7.7 请求重试策略：只重试 GET

`build_session()`（`cas_login.py:757`）给 `requests.Session` 挂了一个 `urllib3.Retry`，但**只对 GET 生效**：

```python
retry = Retry(
    total=2, connect=2, read=2, status=2,
    backoff_factor=0.5,                 # 0.5s → 1s，指数退避
    status_forcelist=(502, 503, 504),   # 只在这些网关错误上重试
    allowed_methods=frozenset(("GET",)),# ★ POST 绝不重试
    raise_on_status=False,
)
session.mount("https://", HTTPAdapter(max_retries=retry))
session.mount("http://",  HTTPAdapter(max_retries=retry))
```

**为什么 POST 必须排除**：步骤2 的 POST 携带账号密码和一次性 CSRF 头。如果服务器其实已经处理成功、只是响应在路上丢了，重试就会**再提交一次登录**——轻则被风控标记，重则触发账号锁定；而且 CAS 那边可能已经被消费掉一张票。

这也是一条通用原则：**幂等请求（GET）可以安全重试，非幂等请求（POST / PUT / 带副作用的 GET）不要自动重试**。要重试也得先设计幂等键，而不是靠 `urllib3` 无脑重发。

### 7.8 浏览器为什么不拦 ticket 也能跳到正确页面 ★

这是 §7.6 那套「票会被消费」原理的**反面**，也是最容易把人绕进去的地方。结论先给：

> **浏览器不需要「持有」ticket，它只需要「让 ticket 被消费」。而自动跟 302，恰恰就是在消费它——这正是浏览器想要的。**

#### 四个原因

1. **浏览器的目标是「到站」，不是「拿到票」。** 用户打开 `xgb.nankai.edu.cn/login/fslogin`，期望的结果就是最终看到登录后的首页。中间那张 ST 对浏览器来说是**过路凭证**，用完即弃，没有「下一步还要再用一次」的诉求。所以它一路自动跟：`302 → 302 → 200`，落到哪个页面就是哪个页面。
2. **`Set-Cookie` 由 Cookie 罐自动接管。** 每一跳的 `Set-Cookie`（`csrf-token`、`session`、`CASTGC`、`PHPSESSID`）都会被自动保存、并在下一次请求自动带上。这是浏览器和 `requests.Session()` 都内置的行为，不需要人工干预。
3. **`Referer` 由浏览器自动补。** 这一点恰恰是**我们的代码在步骤4.5 必须手写逐跳循环**的原因（`cas_login.py:1238` 起的函数，详见 §7.6 的「步骤4.5 为什么非要手写循环」）：`requests` 的自动重定向**不会**补 `Referer`，浏览器会。
4. **浏览器不在中途「读」任何东西。** 它不读 `Location`、不解析 ticket、不把它存进变量——因为浏览器没有「保留这张票」这个需求。

#### 那我们的脚本为什么一度需要拦？

两个原因，且都不是「浏览器做不到」：

| 原因 | 说明 |
|---|---|
| **想观测** | 拦下签发跳才能打印 `Location`，让人看到「票长什么样、签发成功没有」。这是排障需求，不是功能需求 |
| **原设计是分步写的** | 流程图把「签发 / 校验 / 落地」拆成三步、且 ticket 要在函数之间传递，所以**必须**把票接住 |

#### 现在的代码其实已经「半浏览器化」

看 `establish_login_state()`（`cas_login.py:1077`）的两段：

```python
# ① 签发跳：仍然拦，因为我们要读 Location
r = session.get(cas_entry_url(), allow_redirects=False)
ticket      = extract_ticket(r)        # 读 Location 拿票（只用于日志/传递）
validate_url = urljoin(...)            # 读 Location 决定下一步请求哪个地址

# ② 校验跳：完全放开，和浏览器一模一样
r = session.get(validate_url, allow_redirects=True)   # ← 票就在这一跳被用掉
```

- ① 拦，是因为**我们要观测、要拿到「下一步请求哪个地址」**；
- ② 放，是因为**票本来就该在这一跳被消费**，跟浏览器同一套动作。

> ★ 把这两点合起来看：**「不拦 302 会把票用掉」不是错误，是事实；只有当你在票被用掉之后还想再用一次时，它才变成错误。** 一次性凭证的正确用法永远是「用一次，然后丢掉」。

#### 一个直接的推论

如果把整条链换成一次 `session.get(cas_entry_url(), allow_redirects=True)`（完全浏览器式），**功能上也能走通**：最终一样落在门户页 `200`，Cookie 一样落地，步骤4.5 照样能拿到 HTML。

代价是**中间每一跳的 `Location` 和逐跳 `Set-Cookie` 全都看不到了**——排障时只知道「最后落在哪」，不知道「哪一跳出了问题」。这正是当前代码选择「① 拦 + ② 放」而不是「整段全自动」的原因：**功能等价，但可观测性不等价**。

> 反过来说，如果哪天真需要「完全无感的一键登录」，把 `establish_login_state()` 整个换成一次自动跟随即可——它是安全的，只是日志会变薄。

---

## 8. 爬虫部分

### 8.1 页面真实结构

辅学平台活动列表页（`/student/kbmhd/type/{1,2}/p/{N}.html`）的结构：

```html
<div class="right_con_top_tex assess_bg">共<span>11</span>个评估</div>

<div class="right_con_table">
  <table class="typetable1">
    <tr class="tr1">                    <!-- ★ 表头行：class="tr1" -->
      <td>活动名称</td><td>活动类型</td><td>活动学时</td><td>活动时间</td><td>活动地点</td>
    </tr>
    <tr>                                <!-- 数据行 -->
      <td>国家安全教育讲座</td><td>主题讲座</td><td>2</td>
      <td>2026-10-18 14:30</td><td>政府学院325</td>
    </tr>
    ...
  </table>
</div>

<div class="pagelist">
  <div>
    <span class="current">1</span>
    <a class="num" href="/student/kbmhd/type/1/p/2.html">2</a>
    <a class="next" href="/student/kbmhd/type/1/p/2.html">&gt;&gt;</a>
  </div>
</div>
```

**左栏入口映射**（从步骤4.5 落地页 HTML 实测）：

| 栏目 | 「可报名活动」 | 「已报名活动」 |
|---|---|---|
| 主题讲座 | `/student/kbmhd/type/1.html` | `/student/huodong/type/1.html` |
| 辅学活动 | `/student/kbmhd/type/2.html` | `/student/huodong/type/2.html` |
| 生涯指导课 | `/student/kbmhd/type/3.html` | `/student/huodong/type/3.html` |

### 8.2 翻页规则

```
第 1 页： https://fuxue.nankai.edu.cn/student/kbmhd/type/1.html
第 N 页： https://fuxue.nankai.edu.cn/student/kbmhd/type/1/p/N.html
```

`activity_page_url(list_url, page)`（`cas_login.py:1805`）负责拼接，且对已经带 `/p/N.html` 的地址**幂等**。

**总页数**：`parse_max_page()`（`cas_login.py:1817`）用 lxml 遍历所有 `//a/@href`，从链接里抠 `/p/(\d+)\.html` 取最大值。

> ★ **重要修正**：单纯靠第 1 页的分页器**会漏页**——真实站点的分页器常常只显示一个"窗口"（如 `1 2 3 … 10`），第 1 页看不到最大页码。所以代码在**每一页**都会重新解析分页器并**抬高上限**（`cas_login.py:1817`）。这是本地 mock 测试发现并修掉的一个真实缺陷。

### 8.3 表格解析（lxml + XPath）

> 本节是**唯一改变外部依赖形态**的地方：爬虫的 HTML 解析从手写正则整体换成 `lxml`（`from lxml import html as lxml_html`）。其余代码（登录、SSO、脱敏、筛选、CSV）一行未动。

解析链一共 4 个函数：

| 函数 | 行号 | 作用 |
|---|---|---|
| `html_root(document)` | `cas_login.py:1652` | 把「HTML 字符串 / bytes」或「已解析的 lxml 元素」统一成根元素；解析失败返回 `None`，不抛异常打断流程 |
| `strip_html_tags(fragment)` | `cas_login.py:1669` | 取纯文本：`fragment_fromstring(fragment, create_parent="div")` → `text_content()` → 合并空白 |
| `parse_activity_table(document)` | `cas_login.py:1685` | XPath 定位表格 → 取直接子行 → 认表头 → 出数据行 |
| `parse_max_page(document, list_url)` | `cas_login.py:1817` | XPath 取 `//a/@href` → 抠 `/p/(\d+)\.html` → 取最大页码 |

`parse_activity_table()` 的 4 步：

1. `//table[contains(concat(" ", normalize-space(@class), " "), " typetable1 ")]` 定位表格，取第一张（用这种写法而不是 `@class='typetable1'`，是为了容忍 `class="typetable1 wide"` 这种多类名）；
2. `./tr | ./tbody/tr` 取**直接子行**（兼容 `<tbody>` 包裹；实在取不到才退化成 `.//tr`）；
3. `class` 里含 `tr1` 的那行当**表头**，其余当数据行，单元格取 `./td | ./th`；
4. 每个单元格交给 `strip_html_tags()`：lxml 自动去标签、还原 `&nbsp;` / `&amp;` / `&#x27;`，再合并空白。

**为什么从正则换成 lxml**（真实动机，不是"更时髦"）：

| 场景 | 正则版 | lxml 版 |
|---|---|---|
| 属性值里带 `>`（如 `title="a>b"`） | `<[^>]*>` 会在这里截断，整行错位 | 正确 |
| 表格被 `<tbody>` 包裹 | 表头定位靠 `<tr[^>]*class=...>` 很脆 | `./tbody/tr` 直接兼容 |
| 嵌套子表格 | `<tr.*?</tr>` 会把**内层表格的行**也抓进来，多出脏数据 | 只取直接子行，不会混入 |
| 实体（`&nbsp;` / `&#x27;`） | 得自己维护一张替换表，漏一个就错 | `text_content()` 自动解码 |
| 大小写混写 / 标签未闭合 | 取决于正则写得多细 | 解析器自动修正 |

**一页只解析一次**：`crawl_activity_list()` 里先 `document = html_root(html)`（`cas_login.py:1652`），再把这个元素同时喂给 `parse_activity_table()` 和 `parse_max_page()`。两个函数都接受「HTML 字符串」或「lxml 元素」——所以单元测试可以直接传字符串，生产路径只解析一次 DOM。

> 副产品仍然成立：`<a class="count" href="...">查看&gt;&gt;</a>` 会被 `text_content()` 还原成纯文本 `查看>>`。

### 8.4 列名映射：别名表 + 位置兜底

`map_activity_columns(headers, width)`（`cas_login.py:1734`）两级策略：

1. **别名匹配**：`ACTIVITY_COLUMN_ALIASES` 给每个字段若干别名（如"活动学时"还可叫"实践学时"/"学时"/"学分"），长别名优先、列不重复占用；
2. **位置兜底**：没认出来的字段，按 `ACTIVITY_FIELDS` 的顺序按下标补齐。

这样站点改表头文字不会直接崩，最差也能靠列序出结果。

### 8.5 时间解析与筛选

真实格式：**`2026-10-18 14:30`**（实测确认，与用户描述一致）。

`extract_activity_date()`（`cas_login.py:1773`）支持：

| 写法 | 解析结果 |
|---|---|
| `2026-10-18 14:30` / `2026-10-18 14:30:00` | `2026-10-18` |
| `2026-10-18 14:30-16:30` | `2026-10-18` |
| `2026-10-18 14:30 ~ 2026-10-18 16:30` | `2026-10-18`（取第一个=开始） |
| `2026-10-18 14:30 至 2026-10-20 16:00` | `2026-10-18` |
| `2026-10-18` / `2026/10/18` / `2026.10.18` / `2026年10月18日` | `2026-10-18` |
| `２０２６年１０月１８日`（全角） | `2026-10-18`（`normalize_fullwidth()` 先转半角） |
| `10月18日`（无年份） | 按**当年**补齐 |
| `每周三 14:30` / `时间待定` | `None`（抠不出） |

`is_activity_future(time_text, today)`（`cas_login.py:1797`）：

- 抠出的**开始日期 > 今天** → `True`（保留）
- **等于或早于今天** → `False`（剔除）
- **抠不出** → `None` → **保留**，但日志打 `[注意] 抠不出日期，按保留处理`，不静默混入

> **语义澄清**：筛选**只比日期，忽略时刻**。因此"今天 23:59"的活动会被剔除（因为不晚于今天），符合"晚于当日（不包含当日）"的要求。字段值本身**保留完整原文**（含时刻），只有比较时才取日期部分。

### 8.6 爬取主循环

`crawl_activity_list(session, target, today)`（`cas_login.py:1856`）：

```
第 1 页 → 解析分页器得总页数 → 解析表格 → 逐行映射成 dict
        → 去重(key = 活动名称 + 活动时间) → 按时间筛选 → 保留
        → 若本页为空则终止；否则 page += 1，sleep(CRAWL_DELAY)
        → 每一页都重新解析分页器并抬高 max_page
        → 受 CRAWL_MAX_PAGES 上限保护，防死循环
```

**登录态保护**：每页请求后检查 `"/login/index.html" in response.url` 或正文含 `统一身份认证登录`，命中就立刻打 `[失败] 已被踢回登录页，登录态失效，停止爬取` 并退出——**绝不把登录页当数据抓**。

### 8.7 测试功能开关

`run_crawl_test_hook(items)`（`cas_login.py:1936`）：

```python
if CRAWL_TEST_MODE is False:
    return          # ★ 什么都不干
```

打开时把结果写成 CSV：

| 细节 | 说明 |
|---|---|
| 编码 | **`utf-8-sig`**（带 BOM）——Excel 打开中文表头不乱码的关键 |
| 换行 | `newline=""`，避免 Windows 下多出空行 |
| 字段顺序 | 固定用 `ACTIVITY_FIELDS`，由 `csv.DictWriter` 保证表头与列顺序一致 |
| 路径 | `CRAWL_CSV_PATH`，相对路径会落到脚本同目录 |

**关闭时"什么都不干"**：不写文件、不创建目录、不做任何 I/O，只在日志里说明"测试功能已关闭（不落盘）"。

### 8.8 实盘验证结果 ✅

2026-10-16 真实运行，`activities.csv` 实际内容：

```csv
活动名称,活动类型,活动学时,活动时间,活动地点
重述“制度”：关于国家治理研究的一点思考,主题讲座,2,2026-10-18 14:30,政府学院325
中国现代国家建构的路径与症结,主题讲座,2,2026-10-17 14:30,325会议室
香港科技大学宣讲会,主题讲座,2,2026-10-19 18:00,范孙楼122教室
南洋理工大学线上宣讲会,辅学活动,2,2026-10-25 19:00,ZOOM会议
【限医学院】运动会开幕式方阵及观众报名,辅学活动,4,2026-10-17 07:30,津南校区文科体育场
物理科学学院参加校运动会活动,辅学活动,6,2026-10-17 06:30,津南校区文科体育场
【体育实践】校运动会开幕式,辅学活动,9,2026-10-17 08:00,津南体育场
```

由此确认：

1. **整条链路（登录 → SSO 中转 → fuxue 登录态 → 爬取）在真实站点上跑通**；
2. **列名映射完全正确**，5 个字段没有串位；
3. **两个入口都产出了数据**（主题讲座 3 条 + 辅学活动 4 条），说明多入口汇总、翻页、去重正常；
4. **筛选语义正确**：运行日 2026-10-16，结果日期为 10-17 / 10-18 / 10-19 / 10-25，**全部严格晚于当日**，没有一条等于当天；
5. **时间原文完整保留**（含 `14:30` 这样的时刻）；
6. **中文字段无乱码**（BOM 生效）。

---

## 9. 踩过的坑（完整清单）

### 9.1 环境与工具类

| # | 现象 | 原因 | 解决 |
|---|---|---|---|
| 1 | Windows 控制台打印 `✅`/`❌` 抛 `UnicodeEncodeError` | cmd 默认 GBK 编码，无法表示 emoji | 写 `safe_print()` 兜底（捕获异常后降级输出），并把 emoji 换成 `[成功]`/`[失败]`/`[注意]` |
| 2 | `ValueError: unknown url type: '/business/home'` | 服务器返回**相对** `Location`，标准库 `urlopen` 无法处理 | 用 `urllib.parse.urljoin(base, location)` 补成绝对 URL（`cas_login.py:490`） |
| 3 | `RequestsDependencyWarning: urllib3 (2.7.0) ... doesn't match a supported version` | urllib3 版本超出 requests 的兼容声明范围 | `warnings.filterwarnings(...)` **必须写在 `import requests` 之前**，写在后面无效 |
| 4 | `web_fetch` 返回 HTTP 200 但**空 body**；`Invoke-WebRequest` 报"基础连接已经关闭"；`curl.exe` 报 `schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS` | 本机 PowerShell/curl 的 TLS 凭据链在沙箱下不可用 | 改用 **Node 的全局 `fetch`/`https`** 完成抓取与探测 |
| 5 | `node -e "..."` 里写 JS 报语法错 | PowerShell 会先解析引号/`$`/分号，把 JS 拆坏 | 把脚本**先写成 `.js` 文件**，再 `node file.js` |
| 6 | `npm install crypto-js` 失败（`EPERM`，写 `npm-cache/_logs` 被拒） | 沙箱对 npm 缓存目录无写权限 | 放弃安装；改为**下载站点自带的 chunk 并在 Node `vm` 里执行**，反而得到了更强的证据 |
| 7 | 写文件报 `file changed since it was read — re-read the file, then retry`，但 `Compare-Object` 显示与备份**字节完全一致** | 文件版本守卫的**误报**（stale-version guard） | 重新 `read` 一次该文件即可清除守卫，然后重试写入 |
| 8 | `Crypto` 能 import 但 `Cryptodome` 不能 | `pycryptodome` 装的是 `Crypto` 命名空间；`pycryptodomex` 才是 `Cryptodome` | 只 `import Crypto`；同时提供纯标准库 AES 兜底，彻底不依赖外部库 |
| 9 | 全角字符（`２０２６年３月１５日`）解析失败 | 正则只认半角数字 | `str.translate()` 把全角数字/符号映射成半角再解析 |

### 9.2 协议与站点行为类

| # | 现象 | 原因 | 解决 |
|---|---|---|---|
| 10 | `POST /api/v1/login` 只返回 `{"code":40000,"message":"参数错误"}` | 请求体缺必需字段（`login_scene` / `account_type`），且缺 csrf 会话 | 从前端源码逆出字段；用"错误码递进"逐字段试（见 §4.2 步骤2） |
| 11 | 跳过步骤1 后必然 `参数错误` | IAM 的 `csrf-token` / `session` 只能靠 `/api/cas/login` 那一跳获取（直接 GET `/login` **不种任何 Cookie**） | 步骤1 保留，且**必须跟到第 2 跳才停**（代码里 `cas_login.py:935` / `947`） |
| 12 | 拿到 `code:0` 却没拿到 ticket | 南开 IAM 的登录接口**不返回 ticket**，票据由 `/api/cas/login?service=` 现场签发 | 新增**步骤2.5**，用 `allow_redirects=False` 拦住 302，从 `Location` 抠 ticket |
| 13 | 步骤3 拿到的 `Location` 是 `/home/user/login.html` | 服务器返回相对路径 | `urljoin` 补绝对（且这里直接用步骤2.5 的 `Location`，不自己拼 `service`） |
| 14 | 复核报 `[失败]` 但其实是误报 | `classify_auth_redirect()` 把 CAS 的**签发接口** `/api/cas/login` 当成登录页（它以 `/login` 结尾） | 分类规则重排：`/api/cas` → `issuer`；`/api/` → `other`，**都必须在 `/login` 结尾规则之前** |
| 15 | 把 `Referer` 当成 SSO 的钥匙 ❌**已证伪** | 当时的假设：浏览器发 Referer 而 `requests` 默认不发，所以补上 Referer 就能进 fuxue | 实测：补上 Referer 后落地页仍是 xgb 自己的门户页，**Referer 不是判据** |
| 16 | `/xszz/Login/index` 这条入口 ❌**已证伪** | 当时以为它会把人送回 fuxue | 实测整条链 `/xszz/Login/index → /login/fslogin.html → CAS → fslogin?ticket= → /home/user/login.html`，**全程不出现 fuxue**；加 `?service=` / `?url=` / `?redirect=` / `?returnUrl=` 全部返回同一个 `Location` |
| 17 | 反复确认「fuxue Cookie 已拿到」却仍没登录 | **fuxue 对任何匿名请求都会种 `PHPSESSID`**（连登录页都种） | 「有 fuxue 域 Cookie」**不等于**登录成功；唯一可信判据是**复核**——请求一个业务页，看最终落在哪（`VERIFY_MODE="single"`）或看 302 的 `Location` 指向哪（`verbose` 模式） |
| 18 | 以为 fuxue 有 CAS 回调端点 | 猜测 | 实测 `/login/cas.html`、`/login/caslogin.html`、`/login/sso.html`、`/index.php/login/index` **全部 404**；`/login/index.html?ticket=ST-fake` 返回 200 登录页（**不消费 ticket**） |
| 19 | 翻页漏数据 | 第 1 页的分页器只显示页码"窗口"（如 `1 2 3 … 10`），只看第 1 页会低估总页数 | **每页都重新解析分页器并抬高上限** |
| 20 | 用户最初抓包的 URL `https://xgb.nankai.edu.cn/login/fslogin?ticket=ST-...` 看起来像"入口" | 它其实是**步骤3**的结果——票据当场消费掉，`ST-...` 几十秒后失效 | 绝不能把抓到的 `ST-...` 写死进配置 |

### 9.3 被明确标记为「死路」的两条路线

```
❌ 路线 A：/xszz/Login/index
   /xszz/Login/index → /login/fslogin.html → iam /api/cas/login?service=<xgb 自己>
   → /login/fslogin?ticket=ST-... → /home/user/login.html
   全程不出现 fuxue 域，也不写 fuxue 域的 Cookie。

❌ 路线 B：补 Referer 后直接 GET fuxue 登录页
   requests 默认不发 Referer 确实是个差异，但补上后落地页依然是
   xgb 自己的门户页 —— Referer 不是 SSO 的判据。

✅ 正确路线：门户页现抓 orlogin 入口（带现生成的 token）
   GET /home/user/login.html → 正则抠出
   http://fuxue.nankai.edu.cn/Home/Login/orlogin/type/1/id/<学号>/token/<32位>.html
   → GET 它（Referer = 门户页）→ fuxue 登录态建立
```

---

## 10. 其他重要专业知识

### 10.1 HTTPS/TLS 与「客户端加密」的边界

- HTTPS 保护的是**传输过程**，防窃听与中间人；它是必需的，不可被客户端加密替代。
- 客户端加密（本项目这种）保护的是**日志/DevTools/代理**里的可见性，**不防中间人**——因为密钥就在页面上。
- 正确姿势：**TLS + 服务端加盐哈希**（bcrypt / scrypt / Argon2）。服务端永远不该存可逆的密码。

### 10.2 CSRF token 的作用与来源

- **作用**：防止第三方站点冒用用户已登录的会话发起请求。
- **原理**：服务端签发一个随机 token，放在 cookie 或页面里；发请求时必须**再以请求头/表单字段回传**一次。攻击者的站点受同源策略限制，**读不到**这个 token。
- **本站点**：步骤1 由 `iam.nankai.edu.cn` 写入 `csrf-token` cookie；前端 `getCsrfTokenHeaders()` 把它**复制成同名请求头**；本项目用 `AUTH_API_SEND_CSRF_HEADER=True` 复现这一行为。

### 10.3 Cookie 属性

| 属性 | 含义 | 本项目观察 |
|---|---|---|
| `Domain` | 可发送该 cookie 的域（含子域） | `iam` 的 cookie 不会发给 `fuxue` |
| `Path` | 生效路径 | `/` |
| `HttpOnly` | JS 不可读（防 XSS 窃取） | iam 的 `session` / `csrf-token` 是 HttpOnly |
| `Secure` | 仅在 HTTPS 下发送 | iam 的 cookie 是 Secure |
| `SameSite` | 跨站发送策略（防 CSRF） | 未逐一记录（**未验证**） |
| `Max-Age` | 存活秒数 | iam 的 `session` 约 1800 秒 |

### 10.4 `requests` 不执行 JavaScript

这是本项目做 SSO 时反复踩到的点：

- `requests` 只做 HTTP，**不跑 JS**、不渲染 DOM；
- 遇到 `200 + <script>location.href='...'</script>` 这种"前端跳转页"，它会**停在中间页**，后面的业务系统永远拿不到登录态；
- 解决：`extract_html_redirect()`（`cas_login.py:1179`）手动正则抠出 meta refresh / `location.href` / `location.replace` / `window.open` 的目标，再补发一次请求。

> 同理，`find_sso_entry_links()` 仍然**用正则抠 HTML**（步骤4.5 的门户页结构简单、正则够用；本次 lxml 改造只动步骤5 的爬虫解析，其余代码按要求保持不动）——所以它只认 `orlogin` 这种稳定特征。

### 10.5 礼貌抓取与反爬

| 措施 | 本项目实现 |
|---|---|
| 请求间隔 | `CRAWL_DELAY = 0.5` 秒/页 |
| 页数上限 | `CRAWL_MAX_PAGES = 15` |
| 超时 | `REQUEST_TIMEOUT = 15` 秒（`requests` 默认**没有超时**，必须显式给） |
| UA 伪装 | `USER_AGENT` 伪装成 Chrome 120 |
| 登录态失效即停 | 检测到被踢回登录页立刻中止，不空转 |
| 不过度请求 | 只抓两个入口的活动列表，不并发、不遍历全站 |

### 10.6 WAF 与日志脱敏

- 请求头与浏览器保持一致（`X-Fe-Version`、`Page-Name`、`Accept-Language`）能降低被 WAF 判定为异常流量的概率；
- **日志在唯一出口统一脱敏**：本文件所有输出最终都经过 `safe_print()`（`cas_login.py:490`），
  而它会先把文本交给 `redact()`（`cas_login.py:447`）。也就是说——**不管在哪一行打印什么**，
  下面这些东西都不会以明文出现在日志里：

  | 类别 | 被保护的形式 | 实现 |
  |---|---|---|
  | 密码 | `password=xxx`、`"password":"xxx"` | 正则规则（`_REDACT_RULES`） |
  | CAS 票据 | `ST-<时间戳>-<随机串>`、`?ticket=…`、`"ticket":"…"` | 正则规则 |
  | Cookie 值 | `PHPSESSID` / `JSESSIONID` / `CASTGC` / `csrf-token` / `session` / `cookie` | 正则规则 + `mask_set_cookie()` |
  | 账号 | `"account":"…"`、`/id/<学号>/`、以及页面文本里的"欢迎回来&lt;学号&gt;！" | `mask_username()`（只给长度 + 不可逆指纹）+ `register_secret()` |
  | SSO token | 门户页直链里的 `/token/<32 位十六进制>/` | 正则规则 |
  | AES `key` / `iv` | 自检只打印 SHA-256 前 12 位的**不可逆指纹** | `_secret_fingerprint()`（`cas_login.py:480`） |

- 只保留"能排错"的信息：响应码、`Location` 跳转链、Cookie **名字**与所属域、字段长度、业务错误码；
- 凭据放环境变量而非源码，避免误提交到版本库。

> **设计要点**：脱敏做在**唯一出口**（`safe_print`）而不是逐个打印点——逐个打码迟早会漏掉新增的那一行日志。

### 10.7 可复现验证的方法论 ★

本项目最有价值的方法论，值得单独记下来：

1. **能拿到"事实源"就绝不去猜**——加密算法不是试出来的，而是把站点自己的 JS chunk 下到本地，在 Node `vm` 里**执行站点自带的 crypto-js**，与自己的实现比对。
2. **本地 mock 服务器做闭环** —— `HTTPServer` 起在 `127.0.0.1`，模拟 CAS 与业务系统的跳转链，可以断言"发了几个请求、顺序对不对、请求体里到底是什么、Cookie 有没有落地"。本项目各阶段的 13/13、14/14、7/7、51/51 测试都是这么做的。
3. **对真实站点只做只读探测** —— 未登录状态下的 GET 请求可以安全地摸清跳转链、参数校验、404 边界。
4. **区分"明确失败"与"无法判断"** —— 代码里 `[成功]`/`[失败]`/`[注意]` 三态，避免把不确定当成结论（第 14 条坑就是这么来的）。
5. **错误码递进法** —— 调接口时把响应从"参数错误"推进到"业务错误"，是证明"请求格式已正确"的最强证据。

---

## 11. 配置速查表

### 11.1 登录与 SSO

| 配置项 | 位置 | 当前值 | 作用 / 改动建议 |
|---|---|---|---|
| `CAS_LOGIN_PAGE_URL` | `cas_login.py:125` | `https://xgb.nankai.edu.cn/login/fslogin` | 步骤1 预访问地址（本文件只跟 2 跳就停） |
| `CAS_AUTH_API_URL` | `cas_login.py:128` | `https://iam.nankai.edu.cn/api/v1/login` | 步骤2 登录接口 |
| `CAS_TICKET_ENDPOINT` | `cas_login.py:144` | `https://iam.nankai.edu.cn/api/cas/login` | 步骤2.5 签发端点（**不含** `service`） |
| `CAS_TICKET_URL` | `cas_login.py:145` | `""` | 留空 = 自动拼成 `CAS_TICKET_ENDPOINT?service=<CAS_SERVICE_URL>`；手工填时 `check_config` 会校验一致性 |
| `CAS_SERVICE_URL` | `cas_login.py:164` | `https://xgb.nankai.edu.cn/login/fslogin` | ★ **service 的唯一真相来源**，签发与校验两边都用它；**不能改成 fuxue** |
| `SSO_PORTAL_URL` | `cas_login.py:193` | `https://xgb.nankai.edu.cn/home/user/login.html` | 步骤4.5 门户页（步骤4 的落地页就是它，HTML 直接复用） |
| `SSO_LINK_KEYWORD` | `cas_login.py:198` | `辅学` | 在门户页里挑哪个子系统的入口；换系统改这里 |
| `BUSINESS_ENTRY_URLS` | `cas_login.py:202` | `[]` | 门户页抓不到时的兜底写死入口（正常应为空） |
| `DUMP_LANDING_HTML` | `cas_login.py:212` | `True` | 存 `step4_5_landing.html` 便于排查 |
| `VERIFY_LOGIN_AFTER_STEP4` | `cas_login.py:339` | `True` | 是否做登录态复核 |
| `VERIFY_LOGIN_URL` | `cas_login.py:342` | `https://fuxue.nankai.edu.cn/student/kbmhd/type/1/p/1.html` | 复核用的业务页 |
| `VERIFY_MODE` | `cas_login.py:350` | `"single"` | 复核模式：`single` = 1 个请求（默认）；`verbose` = 老的三请求逐跳观察 |
| `DUMP_VERBOSE_COOKIES` | `cas_login.py:355` | `False` | `True` 则 `dump_cookies()` 打印全量 Cookie，否则只打新增/变化 |
| `REQUEST_TIMEOUT` | `cas_login.py:324` | `15` | 单次请求超时（秒） |
| `VERIFY_SSL` | `cas_login.py:325` | `True` | 自签名证书站点的临时逃生口 |
| `USER_AGENT` | `cas_login.py:322` | Chrome 120 | 伪装浏览器 |
| `LOGIN_FAIL_KEYWORDS` | `cas_login.py:328` | 中英文失败提示 | 落地页判定用（扫 **可见文字**，不是整页 HTML） |
| `LOGIN_SUCCESS_COOKIE_NAMES` | `cas_login.py:330` | `("CASTGC", "JSESSIONID")` | 粗略成功信号（本站实际是 CASTGC + PHPSESSID） |

### 11.2 步骤2 请求构造

| 配置项 | 位置 | 当前值 | 作用 |
|---|---|---|---|
| `FORM_FIELD_USERNAME` | `cas_login.py:275` | `account` | ★ 南开的账号字段叫 `account`，不是 `username` |
| `FORM_FIELD_PASSWORD` | `cas_login.py:276` | `password` | 密码字段名 |
| `AUTH_API_EXTRA_FORM` | `cas_login.py:292` | `{"login_scene":"feilian","account_type":"userid"}` | ★ 缺这两个必报参数错误 |
| `AUTH_API_EXTRA_QUERY` | `cas_login.py:296` | `os=web` | 前端统一追加的查询串 |
| `AUTH_API_CONTENT_TYPE` | `cas_login.py:297` | `json` | `json` / `form` |
| `AUTH_API_EXTRA_HEADERS` | `cas_login.py:304` | `X-Version-Check` / `X-Fe-Version` / `Page-Name` / `Accept-Language` | 模仿前端 |
| `AUTH_API_SEND_CSRF_HEADER` | `cas_login.py:310` | `True` | 把 cookie 里的 `csrf-token` 复制成请求头 |
| `TICKET_JSON_KEYS` | `cas_login.py:313` | `("ticket","serviceTicket","TICKET")` | 响应体里 ticket 的字段名 |
| `AUTH_SUCCESS_JSON_KEY` | `cas_login.py:318` | `code` | 成功判定的 JSON 字段（原来是 `'"code":0' in response.text` 字符串匹配） |
| `AUTH_SUCCESS_JSON_CODE` | `cas_login.py:319` | `0` | 该字段等于什么才算登录成功 |

### 11.3 密码加密

| 配置项 | 位置 | 当前值 | 作用 |
|---|---|---|---|
| `ENCRYPT_PASSWORD` | `cas_login.py:363` | `True` | ★ 总开关，`False` = 按流程图原样明文发送 |
| `AES_KEY_SEED` | `cas_login.py:366` | `Number.MAX_SAFE_INTEGER.toString()`（★ 字面值见代码，本文件不重复） | 密钥种子 |
| `AES_KEY_HEX` | `cas_login.py:369` | `""` | 留空 = 由种子自动推导 |
| `AES_IV_HEX` | `cas_login.py:371` | `""` | 留空 = 自动推导 |
| `AES_OUTPUT_ENCODING` | `cas_login.py:376` | `hex` | `hex`（前端行为）/ `base64` |
| `AES_SELF_TEST` | `cas_login.py:378` | `True` | 启动时自检，不通过直接退出 |
| `ENCRYPT_DEBUG_SHOW_CIPHERTEXT` | `cas_login.py:382` | `True` | 打印密文前缀便于对照抓包 |

### 11.4 爬虫

| 配置项 | 位置 | 当前值 | 作用 |
|---|---|---|---|
| `CRAWL_TARGETS` | `cas_login.py:223` | 主题讲座 `type/1.html`、辅学活动 `type/2.html` | 只写第 1 页地址，翻页自动 |
| `CRAWL_ENABLED` | `cas_login.py:229` | `True` | `False` = 完全不爬 |
| `CRAWL_ONLY_FUTURE` | `cas_login.py:234` | `True` | ★ 只保留活动时间严格晚于当日 |
| `CRAWL_MAX_PAGES` | `cas_login.py:237` | `15` | 翻页上限（防死循环） |
| `CRAWL_DELAY` | `cas_login.py:238` | `0.5` | 页间延时（秒） |
| `CRAWL_TEST_MODE` | `cas_login.py:243` | `True` | ★ 测试功能：开 = 写 CSV，关 = 什么都不干 |
| `CRAWL_CSV_PATH` | `cas_login.py:244` | `activities.csv` | 输出路径 |
| `CRAWL_DUMP_HTML` | `cas_login.py:247` | `False` | 存每页原始 HTML 便于排查 |
| `CRAWL_DUMP_DIR` | `cas_login.py:248` | `crawl_html` | 存盘目录 |
| `ACTIVITY_FIELDS` | `cas_login.py:251` | 5 个中文字段 | 字段顺序 / CSV 表头 / 日志顺序 |

### 11.5 凭据

| 配置项 | 位置 | 当前值 | 说明 |
|---|---|---|---|
| `CREDENTIAL_ENV_USERNAME` | `cas_login.py:266` | `CAS_USERNAME` | 账号环境变量名 |
| `CREDENTIAL_ENV_PASSWORD` | `cas_login.py:267` | `CAS_PASSWORD` | 密码环境变量名 |
| `USERNAME` | `cas_login.py:271` | `""` | 兜底，推荐留空 |
| `PASSWORD` | `cas_login.py:272` | `""` | 兜底，推荐留空 |

---

## 12. 故障排查手册

### 12.1 快速定位表

| 症状 | 可能原因 | 定位方法 | 处理 |
|---|---|---|---|
| 打印"请先填写以下内容"就退出 | 配置或环境变量缺失 | 看列出的缺失项 | 补环境变量或配置 |
| 加密自检未通过 | AES 常量被人为改动 | 看自检打印的 `key 指纹` / `iv 指纹`（只给不可逆指纹，不打印 key/iv 本身） | 清空 `AES_KEY_HEX` / `AES_IV_HEX` 让它自动推导 |
| 步骤2 返回 `参数错误` | 请求体缺字段 / 缺 csrf 会话 | 确认步骤1 完整跑完（它负责种 `csrf-token` / `session`）；对照抓包看请求体 | 补 `AUTH_API_EXTRA_FORM` |
| 步骤2 返回 `10110001` | **账号或密码错误**（格式是对的） | — | 检查 `CAS_USERNAME` / `CAS_PASSWORD` |
| 步骤2 返回 `10110002` | 认证方式不被允许 | 看 `account_type` | 保持 `userid`（本站 LDAP 未启用） |
| 步骤2 拿不到 ticket | 登录接口不返回 ticket | 看是否走到"步骤2.5"日志 | 正常流程，检查 `CAS_TICKET_URL` |
| 步骤2.5 又被弹回登录页 | 上面的登录没被认可 | 看 302 的 `Location` | 回到步骤2 排查 |
| 步骤3 报 URL 无效 | 相对 `Location` 未补全 | 看日志里的 `Location` | 代码已用 `urljoin`，检查是否被改坏 |
| 复核报 `[失败]` 且 Location 是 fuxue `/login/index.html` | 步骤4.5 没生效 | 看步骤4.5 日志的"锁定入口"和落地页标题 | 确认 `SSO_LINK_KEYWORD` 命中、门户页结构未变 |
| 复核报 `[失败]` 但 Location 是业务页 | 分类器误判 | 看 `classify_auth_redirect` 的归类日志 | 检查规则顺序 |
| 步骤4.5 说"没找到入口" | 门户页改版 / 关键字不对 | 看它列出的所有 `orlogin` 链接 | 改 `SSO_LINK_KEYWORD` |
| 爬到 0 行 | 表格 class 变了 / 被踢回登录页 | 看日志是"没解析出数据行"还是"已被踢回登录页" | 开 `CRAWL_DUMP_HTML=True` 存 HTML 排查 |
| 字段串位 | 表头文字与别名表不匹配 | 看日志里打印的"表头：..." | 往 `ACTIVITY_COLUMN_ALIASES` 加别名 |
| 活动时间没筛掉过期的 | 日期格式不认识 | 看是否有 `[注意] 抠不出日期` | 往 `DATE_PATTERNS` 加格式 |
| CSV 中文乱码 | 编码不对 | 用记事本看是否 BOM | 代码已用 `utf-8-sig` |
| 卡住不动 | 没设超时 | — | `REQUEST_TIMEOUT` 已设为 15 秒 |

### 12.2 日志速查

| 前缀 | 含义 |
|---|---|
| `[配置]` | 凭据来源、账号只给长度 + 不可逆指纹（★ 不打码以外的任何内容） |
| `[步骤1]` ~ `[步骤4.5]` | 各步请求与响应 |
| `[复核]` | 登录态复核结果 |
| `[爬虫]` | 每个入口每页的 GET、解析行数、筛选剔除、保留数 |
| `[测试]` | 测试功能写 CSV 的情况 |
| `[结果]` | 步骤4 的登录判定 |
| `[汇总]` | 最终结论 |
| `[成功]` / `[失败]` / `[注意]` | 三态判定，`[注意]` = 无法判断，不要当成失败 |

---

## 13. 附录

### 13.1 关键常量

| 常量 | 值 |
|---|---|
| AES 密钥种子 | `Number.MAX_SAFE_INTEGER.toString()`（★ 字面值只存在于 `cas_login.py` 的 `AES_KEY_SEED`，本文件不重复） |
| AES `key_hex` | `MD5(seed)` → 32 位十六进制（★ 值已隐去） |
| AES `iv`（前 16 字符） | `SHA1(key_hex)` 十六进制的前 16 个字符（★ 值已隐去） |
| AES 模式 / 填充 / 输出 | CBC / PKCS7 / 小写 hex |
| 自检向量 | `demo-1234` → `2b563d38ed5102f50e661c2cc33fec40`（中性测试串，不含真实凭据） |
| CAS 登记的 service | `https://xgb.nankai.edu.cn/login/fslogin` |
| 门户页 | `https://xgb.nankai.edu.cn/home/user/login.html` |
| fuxue SSO 入口格式 | `http://fuxue.nankai.edu.cn/Home/Login/orlogin/type/1/id/<学号>/token/<32位>.html` |
| 活动列表页 | `https://fuxue.nankai.edu.cn/student/kbmhd/type/{1,2}/p/{N}.html` |
| 表格标识 | `<table class="typetable1">` + `<tr class="tr1">` |

### 13.2 验证证据摘要

| 阶段 | 方式 | 结果 |
|---|---|---|
| 密码加密 | 3 个实现 × 8 组输入比对 + 站点自带 crypto-js | 8/8 一致，命中已知向量 |
| 登录流程 | 本地 mock CAS 服务器 | 14/14 |
| 跳过步骤1 | 本地 mock | 13/13 |
| SSO 中转 / 分类器 | 本地 mock（双 mock 服务器） | 16/16 |
| Referer 方案 | 本地 mock | 7/7（**但真实站点证伪**） |
| 爬虫 | 本地 mock + 真实页 HTML | 51/51 |
| **11 项优化回归** | 本地 mock 单测（临时脚本，跑完即删） | **26/26**：`check_config` service 一致性拦截、`cas_entry_url()` 的 quote、步骤1 只发 2 请求 + 相对 Location 的 `urljoin`、`establish_login_state` 有票 1 请求 / 无票 2 请求、步骤4.5 复用门户页省 1 请求、`verify_single_request()` 的 True/False 判定、`dump_cookies()` 去重、已删函数/开关确实不存在 |
| **整链路** | **真实站点实跑** | ✅ 产出 `activities.csv`，7 条活动，两入口均有数据 |
| 语法 | `python -m py_compile cas_login.py` | 退出码 0 |
| 无凭据启动 | 直接运行 | 自检 OK，仅提示缺账号密码后正常退出（退出码 0） |

### 13.3 `cas_login.py` 结构导航

| 行号 | 内容 |
|---|---|
| 1–85 | 文件头 docstring：真实链路、AES 方案、**日志脱敏**、用法 |
| 86–112 | 【导入区】标准库 + `requests` + **`lxml`**（`warnings.filterwarnings` 必须在 `import requests` **之前**） |
| 113–213 | 【配置区】登录 / SSO（`CAS_LOGIN_PAGE_URL` / `CAS_AUTH_API_URL` / `CAS_TICKET_ENDPOINT` / `CAS_TICKET_URL` / `CAS_SERVICE_URL` / `SSO_PORTAL_URL`…） |
| 214–332 | 爬虫配置区 + 账号密码来源（`CRAWL_*` / `ACTIVITY_*` / `CREDENTIAL_ENV_*`） |
| 333–357 | 复核与 Cookie 打印配置（`VERIFY_LOGIN_AFTER_STEP4` / `VERIFY_LOGIN_URL` / `VERIFY_MODE` / `DUMP_VERBOSE_COOKIES` / 超时 / UA / 失败关键字） |
| 358–384 | 【配置区·续】密码加密（`ENCRYPT_PASSWORD` / `AES_*`） |
| 385–508 | 【工具函数区】+ **日志脱敏区**（`register_secret` / `redact` / `mask_secret` / `mask_set_cookie` / `_secret_fingerprint` / `safe_print` / `log`） |
| 509–752 | 【密码加密区】（`derive_key_iv` / `pkcs7_pad` / pycryptodome 后端 / 纯 Python AES 兜底 / `aes_encrypt` / `self_test_crypto`） |
| 753–908 | 【会话工具区】（`build_session` 757 / `dump_cookies` 805 / `cas_entry_url` 835 / `visible_text` 851 / `extract_ticket` 867） |
| 909–1637 | 【主流程区】 |
| ├ 913 | `step1_open_login_page()` — 手动只跟 2 跳 |
| ├ 959 | `step2_post_credentials()` — POST 登录，JSON `code` 判定 |
| ├ 1077 | `establish_login_state()` — ★ 步骤2.5~4 合并（① 签发拦 302 / ② 校验放行） |
| ├ 1179 | `extract_html_redirect()` |
| ├ 1207 | `find_sso_entry_links()` |
| ├ 1238 | `step4_5_enter_business_system()` — 复用门户页 HTML + 手写循环补 Referer |
| ├ 1448 | `classify_auth_redirect()` |
| ├ 1490 | `auth_hosts()` |
| ├ 1500 | `verify_single_request()` — 复核默认模式 |
| └ 1539 | `verify_login_state()` — 三态结论 |
| 1638–1995 | 【步骤5：爬虫区】（`html_root` / `parse_activity_table` / `parse_max_page` / `decode_response` / `crawl_activity_list` / 测试钩子） |
| 1996–2162 | 【入口区】 |
| ├ 2000 | `mask_username()` |
| ├ 2013 | `resolve_credentials()` |
| ├ 2052 | `check_config()` — 含 service 一致性校验 |
| └ 2084 | `main()` 主流程 |

### 13.4 免责与使用边界

- 本文档与 `cas_login.py` 仅用于**本人账号的自动化登录与数据整理**；
- 密码加密属**混淆**，不构成安全保证；
- 请遵守学校的信息系统使用规定，控制抓取频率（已内置延时与页数上限），**不要用于批量账号或高频抓取**；
- 凭据请放环境变量，不要写进源码或提交到版本库。

---

### 13.5 附录·「把 ticket 拦下来」的全部理由（按代码位置逐条）

「拦 ticket」在代码里没有单独的标志位，它体现为一个动作：**`allow_redirects=False`**。全文件一共 **8 处**手工控制跳转，其中 **3 处**与 ticket 直接相关。下面按位置逐条列出「为什么要拦」，以及**反过来说，什么时候不该拦**。

#### 一、全局前提：拦 302 到底拦的是什么

`requests` 一旦 `allow_redirects=True`，就会把 3xx 链一路跟完，**只把最后一跳的响应交给你**，中间的 `Location`、逐跳 `Set-Cookie`、每一跳的状态码都进了 `response.history`。所以「拦」的真实含义是：

| 拦（`False`）时你能拿到 | 不拦（`True`）时你只能拿到 |
|---|---|
| 这一跳的 `Location`（票据就藏在里面） | 只有最终落点的 `response.url` |
| 这一跳的 `Set-Cookie` | 只能事后从 `response.history` 回捞 |
| 到底停在第几跳、每一跳是谁 | 一次黑箱到站 |
| **对「下一步要请求哪里」的决定权** | 决定权交给 `requests` |

> 一句话：**拦 = 保留选择权与可观测性；不拦 = 把选择权交出去。**

#### 二、3 处与 ticket 直接相关的拦截

| # | 代码位置 | 拦的是哪一跳 | 为什么要拦（理由） | 拦了拿到什么 | 不拦会怎样 |
|---|---|---|---|---|---|
| **1** | `cas_login.py:934-935`（步骤1 第 1 跳） | `xgb/login/fslogin → iam/api/cas/login` | **不是为 ticket，是为「只跟到第 2 跳」**：第 2 跳 `/api/cas/login` 才是种 `csrf-token` / `session` 的那一跳，第 3 跳登录页不种任何 Cookie，纯浪费 | 省 1 个请求，且清楚知道 Cookie 是哪一跳来的 | 多一个无用请求，且无法区分 Cookie 来源 |
| **2** | `cas_login.py:946-947`（步骤1 第 2 跳） | `iam/api/cas/login → iam/login?next=...` | 同上：停在这里就能拿到步骤2 需要的两个 Cookie | 现场只剩 2 个请求 | 一路跟到登录页 HTML，对爬虫零价值 |
| **3** | `cas_login.py:1118-1119`（★ 步骤2.5 签发跳） | `iam/api/cas/login?service=... → xgb/login/fslogin?ticket=ST-...` | **这是最核心的一处**：<br>① 票据**只存在于这一跳的 `Location` 里**，跟掉就再也看不到它的明文；<br>② 读出 `Location` 才能决定「下一步请求哪个地址」（`validate_url`），不拦就只能靠配置自己拼；<br>③ 一旦自动跟，`requests` 会立刻发第②跳让 CAS 校验 → **票当场作废**，之后想再用一次就是重放 | `ticket`（正则可读）、`validate_url`（`urljoin` 补全）、签发状态码、签发时刻现场日志 | 见下面「三、不拦会怎样」 |

#### 三、不拦第 3 处会怎样（也就是「票被消费」的完整链条）

```
拦：GET 签发入口 → 302 Location=...?ticket=ST-1   ← 停住，票在手里，未使用
    然后我们自己决定何时、向谁发第②跳

不拦：GET 签发入口 → 302 → requests 自动 GET xgb/login/fslogin?ticket=ST-1
                          ↑ CAS 在这一跳把 ST-1 标记为已使用
       → 再 302 到业务首页，requests 继续跟 → 200
    结果：登录其实成功了，但我们【看不到 ST-1】，也【不知道落点是怎么来的】
```

在这种「其实已经登录成功」的状态下，若脚本还按原设计拿票据再请求一次：
- 轻则拿不到票（`extract_ticket` 只能从 `history` 回捞到已经作废的 ST-1）；
- 重则用作废票去敲业务系统 → 被 302 回登录页 → 日志显示「登录失败」，而实际上登录早成功了。

**这是最有欺骗性的失败模式**，也是这一处必须拦的最强理由。

#### 四、另外 5 处「拦」——都不是为了 ticket

| # | 代码位置 | 拦的是哪一跳 | 真实理由 | 和 ticket 的关系 |
|---|---|---|---|---|
| 4 | `cas_login.py:1342`（步骤4.5 逐跳循环） | fuxue SSO 入口的 `orlogin/.../token/...` 链 | **`requests` 自动跳转不补 `Referer`，浏览器会**；fuxue 靠 `Referer` 判来源，所以必须自己逐跳发、自己补 `Referer` | **无关**（是 `token` 而非 `ticket`） |
| 5 | `cas_login.py:1375`（步骤4.5 前端跳补救） | 落地页里的 JS/meta 跳转 | 这不是 302，是 HTML 里的前端跳转，`requests` 根本不会跟；必须自己从 HTML 里抠出来再发一跳 | **无关** |
| 6 | `cas_login.py:1569`（复核 verbose 模式·第 1 跳） | `VERIFY_LOGIN_URL` 的第一跳 | 要看服务器把我们往哪 302，才能区分「踢回登录页 / 送去签发接口 / 送去业务页」三种情况 | **间接**：分类结果决定要不要再跟一跳（第 7 处） |
| 7 | `cas_login.py:1582`（复核 verbose·签发跳） | `/api/cas/login` 返回的票据跳 | **同上第 3 处的理由**——要看它给不给票、票指向哪 | ✔ **直接相关**（第二处消费票据风险的现场） |
| 8 | `cas_login.py:1602`（复核 verbose·业务校验跳） | 拿新票敲业务系统 | 只想看业务系统**认不认**这张票（往哪 302），不需要跟完整个落地链 | ✔ **直接相关**（票据在这一跳被业务系统消费） |

> ★ 注意 4、5 两处的 `token` **不是** CAS 的票据，是门户页给 fuxue 的一次性中转令牌，同样一次性、同样不能写死，但和 ST 是两套东西。

#### 五、反过来：什么时候**必须不拦**

| 位置 | 为什么必须放行 |
|---|---|
| `cas_login.py:1151`（步骤3 校验跳） | 票**本来就该在这一跳被消费**；拦着它就永远到不了业务系统 |
| `cas_login.py:1513`（复核 single 模式） | 复核语义就是「像浏览器一样再访问一次」，要的是最终落点 |
| `cas_login.py:1375` 的补跟请求 | 前端跳转的后续链路没有「必须自己读 Location」的需求 |

#### 六、汇总：拦 ticket 的理由清单（可直接背诵）

1. **票据只在 `Location` 里明文出现一次** —— 不拦就永远看不到它。
2. **要自己决定第②跳什么时候发、发给谁** —— 不拦就等于把「消费这张票」的时机交给 `requests`。
3. **票是一次性的** —— 一旦被自动跟掉，再用就是重放，表现为「明明登录成功了却报失败」。
4. **要观测每一跳的状态码 / `Location` / `Set-Cookie`** —— 排障时能一句话定位「哪一跳出了问题」。
5. **校验地址要用服务器给的原地址**（保证签发与校验两边 `service` 逐字符一致），而不是自己拼。
6. **相对 `Location` 必须 `urljoin` 补全**，否则 `requests` 抛 `MissingSchema` —— 这也需要先拿到 `Location`。
7. **（`token` 场景）自动跳转不补 `Referer`**，缺 `Referer` 会被业务系统拒绝。

> 前六条对应「有票 / 要读票 / 要控票」的场景；第七条对应另一个一次性凭证（`token`），**原理相通但对象不同**。

#### 七、与 §7.6 / §7.8 的关系

- §7.6 讲的是**原理**：CAS 的校验语义、为什么自动跟等于当场消费；
- §7.8 讲的是**边界**：浏览器为什么不拦也照样到站（因为它本来就想让票被消费）；
- 本节是**清单**：把这个文件里每一处 `allow_redirects=False` 逐条对号入座，说明它到底属于哪一种理由。

> 口诀：**产生凭证的那一跳永远拦，消费它的那一跳永远放；只有「票还要在别处再用一次」时，拦截才是功能要求，否则它只是排障手段。**

---

*文档结束。*