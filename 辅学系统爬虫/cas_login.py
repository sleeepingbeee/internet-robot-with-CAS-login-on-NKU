# -*- coding: utf-8 -*-
"""
================================================================================
 文件名 : cas_login.py
 作用   : 爬虫的【登录部分】—— 按 CAS 登录流程图实现登录并跳转到业务系统
          ★ 登录链路：步骤1（GET CAS 入口，只发 2 个请求就拿到 csrf-token / session）
            → 步骤2（POST 账号密码，密码 AES 加密）
            → 步骤2.5 + 步骤3 + 步骤4 合并（签发 ticket → 用它校验 → 落地业务系统）
            → 步骤4.5（门户页现抓 SSO 入口，中转进 fuxue）
            → 复核（默认 1 个请求判定登录态）
          （步骤5 已实现：爬「可报名活动」表格 + 按活动时间筛选，细节见下方真实链路）
 依赖   : 只使用你机器上【已经安装好】的库，不下载任何新东西
          requests 2.32.4   （已确认安装）
          pycryptodome 3.21（已确认安装，提供 Crypto.Cipher.AES）
          lxml 6.1.1        （已确认安装，步骤5 用 XPath 解析活动表格，不再手写正则）
          其余 hashlib / re / sys / urllib.parse 都是 Python 自带标准库
          ★ 万一你的环境没有 pycryptodome，本文件内置的「纯标准库 AES」会自动顶上，
            功能一模一样，无需安装任何东西。
 说明   : 流程图里写的就是 requests.Session，所以本文件与流程图术语一一对应：
            requests.Session()           → session
            Session.cookies 容器          → session.cookies（requests 自带的 RequestsCookieJar）
            session.get(url)             → session.get(url)
            allow_redirects=True/False   → 同名参数，直接可用
 用法   : 配置区已按「南开大学学工系统 xgb.nankai.edu.cn」填好，
          ★ 账号密码默认从【系统环境变量】读取（不再写进源码）：
              PowerShell : $env:CAS_USERNAME="你的学号"; $env:CAS_PASSWORD="你的密码"
              cmd        : set CAS_USERNAME=你的学号  &  set CAS_PASSWORD=你的密码
              Linux/macOS: export CAS_USERNAME=你的学号 ; export CAS_PASSWORD=你的密码
          然后执行： python cas_login.py
          （也可以把凭据填进配置区的 USERNAME / PASSWORD 作兜底，但推荐保持留空）
================================================================================

  ★ 目标站点真实链路（实盘探测所得，与本文件四个步骤一一对应）：

    步骤1  GET  https://xgb.nankai.edu.cn/login/fslogin
             → 302 https://iam.nankai.edu.cn/api/cas/login?service=https://xgb.nankai.edu.cn/login/fslogin
                 Set-Cookie: PHPSESSID=...               (域 xgb.nankai.edu.cn)
             → 302 https://iam.nankai.edu.cn/login?next=%2Fapi%2Fcas%2Flogin%3Fservice%3D...
                 Set-Cookie: csrf-token=...; session=... (域 iam.nankai.edu.cn)
             ★ 本文件在这里【停住】（只发 2 个请求）：csrf-token / session 是第 2 跳种的，
               后面那个 200 登录页对爬虫毫无用处（实测直接 GET /login 不种任何 Cookie）。
    步骤2  POST https://iam.nankai.edu.cn/api/v1/login      （JSON，password 字段放 AES 密文）
             ★ 该响应【不含 ticket】，它只负责建立 iam 域名下的登录态
    步骤2.5 GET https://iam.nankai.edu.cn/api/cas/login?service=https://xgb.nankai.edu.cn/login/fslogin
             → 302 Location: https://xgb.nankai.edu.cn/login/fslogin?ticket=ST-...   ← ticket 在这里
    步骤3  GET  https://xgb.nankai.edu.cn/login/fslogin?ticket=ST-...
             ★ 直接用步骤2.5 那个 302 的 Location，不再自己拼接（避免两处 service 不一致）
    步骤4  跟随 302 落到业务系统（此时 xgb 的 PHPSESSID 才变成已登录状态）
             ★ 步骤2.5/3/4 在本文件里由 establish_login_state() 一个函数完成
    步骤4.5 GET https://xgb.nankai.edu.cn/home/user/login.html   ← 一站式登录平台门户页
             ★ 在这个门户页的 HTML 里【现抓】目标系统的 SSO 入口（token 每次现生成），再点进去：
               http://fuxue.nankai.edu.cn/Home/Login/orlogin/type/1/id/<学号>/token/<32位随机串>.html
               用门户页地址作 Referer 跟这一跳，fuxue 域的登录态就建立了。
             ★ 已排除的两条错路（实测都进不了 fuxue，别再走）：
               /xszz/Login/index → /login/fslogin.html → CAS → fslogin?ticket= → /home/user/login.html
               （CAS 的 service 始终是 xgb 自己的 /login/fslogin，全程不提 fuxue）
               以及「带上 Referer 直接 GET fuxue 登录页」——fuxue 对任何匿名请求都种 PHPSESSID，
               所以"有 fuxue Cookie"不等于登录成功。
    步骤5  GET https://fuxue.nankai.edu.cn/student/kbmhd/type/{1,2}/p/{N}.html
             ★ 爬取左栏「主题讲座」(type=1) 与「辅学活动」(type=2) 下「可报名活动」的表格，
               自动翻页，筛出【活动开始日期严格晚于今天】的项目，返回字典列表：
               [{"活动名称","活动类型","活动学时","活动时间","活动地点"}, ...]
               测试功能开关 CRAWL_TEST_MODE=True 时另存 CSV，False 时什么都不干。

     ★ ticket 一次性、几十秒过期，所以不能把抓到的 ST-... 写死进配置，必须现场换新的。

  ★ 密码加密：密码不再明文发出，而是按 https://iam.nankai.edu.cn 前端 `aesEncrypt`
    的真实做法加密后再提交（CryptoJS 兼容）：

        明文密码  --AES-256-CBC + PKCS7 填充-->  十六进制密文

    密钥不是随机生成的，而是前端「硬编码推导」出来的：
        key_hex = MD5("9007199254740991")                    // 32 个十六进制字符
        key     = UTF8(key_hex)                              // 32 字节 → AES-256
        iv_hex  = SHA1(UTF8(key_hex)).toString(Hex)          // 40 个十六进制字符
        iv      = UTF8(iv_hex) 的前 16 字节                   // 16 字节

    实测向量（本文件内置自检，启动时会自动跑一遍）：
        AES("demo-1234") = 2b563d38ed5102f50e661c2cc33fec40

    说明：这是前端「混淆」而非真正的安全——密钥写死在 JS 里，任何人都能解密/重放。
    真正防窃听靠 HTTPS，防撞库靠服务端加盐哈希（bcrypt/argon2）。这里只是照葫芦画瓢。
================================================================================
"""

# ============================================================================
# 【导入区】标准库 + 已装的 requests + 已装的 lxml；HTML 解析统一交给 lxml（不再手写正则）
# ============================================================================
import csv                     # 标准库：把爬到的字典列表写成 CSV（步骤5 的测试功能用）
import datetime                # 标准库：取"今天"按日期比较，判断活动时间是否晚于当日
import hashlib                 # 标准库：算 MD5 / SHA1，用来推导 AES 的 key 和 iv
import os                      # 标准库：读系统环境变量；以及拼 CSV / 调试 HTML 的绝对路径
import re                      # 正则：抠 ticket、解析登录页隐藏字段、从 URL 里取页码（HTML 解析已交给 lxml）
import sys                     # 读取控制台编码，防止 Windows cmd（GBK）打印中文时抛 UnicodeEncodeError
import time                    # 标准库：翻页之间 sleep 一下，别把人家服务器打疼
import warnings                # 标准库：屏蔽 requests 版本兼容性警告（见下方 filterwarnings 说明）
from urllib.parse import quote, urljoin, urlparse   # 标准库：补全相对跳转地址、拆解 URL、给敏感值做编码形式脱敏

# 本机实测：requests 2.32.4 自带的版本检查比已装的 urllib3(2.7.0) 旧，import 时就会打印一条
# RequestsDependencyWarning（"urllib3 ... doesn't match a supported version"）。
# 它只是提醒，不影响功能；这里只屏蔽这一条，避免干扰你观察登录流程的输出。
# ★ 必须写在 import requests 之前，否则警告在导入瞬间就已经打出来了，过滤不掉。
warnings.filterwarnings("ignore", message=".*doesn't match a supported version.*")

import requests                # ★ 你已安装的第三方库：负责发请求 + 自动管理 Cookie（= 流程图里的 Session）

# ★ 步骤5 的 HTML 解析用 lxml（本机已装 lxml 6.1.1，无需再下载）。
#   为什么起别名 lxml_html：本文件里 `html` 是到处在用的局部变量名（`html = decode_response(...)`、
#   `def parse_activity_table(html)`），直接 `from lxml import html` 会在这些函数里被局部变量遮蔽。
from lxml import html as lxml_html


# ============================================================================
# 【配置区】★★★ 请按你的目标站点填写，其余代码无需改动 ★★★
# 留空 = 还没配置；main() 会先检查并给出友好提示，不会报一堆看不懂的错。
# ============================================================================

# ---- 步骤1：CAS 登录页地址（GET，用来拿初始会话）----
# ★ 目标站点 = 南开大学学工系统。访问这个地址会自动跟完整个跳转链：
#     https://xgb.nankai.edu.cn/login/fslogin
#        → 302 https://iam.nankai.edu.cn/api/cas/login?service=https://xgb.nankai.edu.cn/login/fslogin
#        → 302 https://iam.nankai.edu.cn/login?next=...
#        → 200 SSO 登录页
#    沿途种下 xgb 的 PHPSESSID 与 iam 的 csrf-token / session —— 下面步骤2 必须带着它们。
CAS_LOGIN_PAGE_URL = "https://xgb.nankai.edu.cn/login/fslogin"

# ---- 步骤2：提交账号密码的接口地址（POST，JSON 体）----
CAS_AUTH_API_URL = "https://iam.nankai.edu.cn/api/v1/login"   # ★ 南开统一身份认证登录接口

# ---- 步骤2.5：CAS 入口地址（GET）★ 唯一真相来源 = CAS_SERVICE_URL ----
# ★ 与通用 CAS 流程的关键差异：/api/v1/login 的响应里【没有 ticket】，
#   它只负责建立 iam 域名下的登录态；ticket 要靠再访问一次 CAS 入口、
#   从 302 的 Location 头里取（Location: https://xgb.nankai.edu.cn/login/fslogin?ticket=ST-...）。
#
# ★★ 同一个入口地址干两件事（已实测）：
#     未登录时 GET 它 → 302 到 iam 登录页，并【种下 csrf-token / session】  ← 步骤1 靠它
#     已登录时 GET 它 → 302 到 CAS_SERVICE_URL?ticket=ST-...                 ← 步骤2.5 靠它
#
# ★★ 为什么不再把 service 写死在两个地方：
#     CAS 严格校验【签发】与【校验】两边的 service 是否逐字符相同。以前
#     CAS_TICKET_URL 里内嵌一份 service、CAS_SERVICE_URL 又写一份，改了一个忘改另一个，
#     就会得到"票据被判无效 → 复核失败"，而报错信息完全不会指向配置。
#     现在 service 只写一次（CAS_SERVICE_URL），完整 URL 由 cas_entry_url() 运行时拼出来。
CAS_TICKET_ENDPOINT = "https://iam.nankai.edu.cn/api/cas/login"   # CAS 入口（不含 service 参数）
CAS_TICKET_URL = ""               # 留空 = 自动拼成 <CAS_TICKET_ENDPOINT>?service=<CAS_SERVICE_URL>（推荐）
                                  # 只有接口格式特殊（service 不在查询串里）时才手工填完整地址

# ---- 步骤3 / 步骤4：CAS 的 service 目标（★ 沿用学工系统，不要改成 fuxue）----
# 它既是步骤3 拼在 ticket 前面的 service 目标，也是步骤4 最终要落地的地址。
# ★ 取值 = 你抓到的那个包【去掉 ?ticket=... 之后】的部分。
#   注意：CAS 严格校验 service 是否与签发时一字不差，所以不要在这里加任何多余参数。
#
# ★★ 目标业务系统是 fuxue.nankai.edu.cn（立公增能辅学平台），为什么这里仍然填 xgb？
#    实测（2026 年本机探测）fuxue 自己【不直接】对接统一认证：
#        GET https://fuxue.nankai.edu.cn/student/kbmhd/type/2/p/1.html
#          → 302 https://fuxue.nankai.edu.cn/login/index.html   ← fuxue 自己的登录页
#        该登录页上"统一身份认证登录"按钮指向的是：
#          https://xgb.nankai.edu.cn/xszz/Login/index
#          → 302 https://xgb.nankai.edu.cn/login/fslogin.html
#          → 302 https://iam.nankai.edu.cn/api/cas/login?service=https://xgb.nankai.edu.cn/login/fslogin
#    也就是说：fuxue 把统一认证【委托给了学工系统 xgb】，CAS 这边登记的 service
#    始终是 https://xgb.nankai.edu.cn/login/fslogin。把它改成 fuxue 的地址 CAS 不会认。
#    所以要进 fuxue，必须是"先登进 xgb，再按浏览器真实足迹走一次中转链"——见下面的步骤4.5。
CAS_SERVICE_URL = "https://xgb.nankai.edu.cn/login/fslogin"

# ---- ★ 步骤4.5：进入业务系统（SSO 中转的最后一跳）----
# ★★ 这一步是打开 fuxue 登录态的【唯一正确姿势】，已由实跑落地页证实：
#
#   CAS 登录成功后，xgb 会把我们送到「一站式登录平台」门户页
#       https://xgb.nankai.edu.cn/home/user/login.html
#   这个门户页里列着一排子系统入口，长这样（真实抓到的 HTML）：
#       <li><a href="http://fuxue.nankai.edu.cn/Home/Login/orlogin/type/1/id/<学号>/token/36c2588f...aa01.html" target="_blank">
#           <p>立公增能辅学平台</p></a></li>
#       <li><a href="https://xgb.nankai.edu.cn/Xszz/Login/orlogin/type/1/id/<学号>/token/8a60031c...a8841.html" target="_blank">
#           <p>本科生管理系统</p></a></li>
#       ……（研究生 / 就业 / 心理健康 / 离校 等同理）
#
#   ★ 两个要害：
#     1) 真正能把 fuxue 登录态建起来的地址是
#            http://fuxue.nankai.edu.cn/Home/Login/orlogin/type/1/id/<学号>/token/<32位随机串>.html
#        它【不在】fuxue 自己的登录页上，也不在 /xszz/Login/index 那条路上
#        （实测那条路 CAS 的 service 参数始终是 xgb 自己的 /login/fslogin，全程不提 fuxue）。
#     2) token 是门户页【每次访问时现生成】的，和学号绑定，
#        所以【绝不能写死】，必须"先访问门户页 → 从 HTML 里把链接抠出来 → 再访问它"。
#
#   因此这一步的实际动作是：
#       ① GET SSO_PORTAL_URL（一站式登录平台）
#       ② 在返回的 HTML 里按 SSO_LINK_KEYWORD 找到目标系统的入口链接
#       ③ GET 那个链接（带上 Referer），拿到 fuxue 域的登录态
#   这一步【不是爬虫】，只是登录流程的最后一跳。
#
# ---- ① 门户页地址（一站式登录平台）----
SSO_PORTAL_URL = "https://xgb.nankai.edu.cn/home/user/login.html"

# ---- ② 目标系统的识别关键字（在门户页里按【链接文字】匹配）----
# 门户页里每一项的文字就是系统名，例如「立公增能辅学平台」，填「辅学」即可命中。
# 填写时会先匹配链接文字，匹配不到再退一步匹配链接地址。留空 = 按 BUSINESS_ENTRY_URLS 走。
SSO_LINK_KEYWORD = "辅学"

# ---- ③ 兜底：如果不想走门户页现抓，就在这里按顺序写死地址 ----
# 正常情况下留空 []，让上面的门户页现抓逻辑生效（token 会过期，写死基本必失败）。
BUSINESS_ENTRY_URLS = []

# ★ 重要提醒：实测 fuxue 在任何匿名请求上都会种 PHPSESSID（连登录页都会），
#   所以"看到 fuxue 域 Cookie"【不等于】登录成功；唯一可信的判据是后面的【复核】——
#   带 cookie 再 GET 目标页面，看它返回 200 而不是 302 回 /login/index.html。

# ---- 调试开关：把步骤4.5 的落地页 HTML 存盘 ----
# 为什么需要它：中转到底断在哪一环，光看状态码不够——把落地页存下来，
# 用记事本/浏览器打开就能看清它是"中转页"（里面有指向 fuxue 的链接/JS）还是"终点页"。
# 存成同目录下的 step4_5_landing.html。确认没问题后改成 False 即可。
DUMP_LANDING_HTML = True

# ============================================================================
# 【爬虫配置区】步骤5：爬取 fuxue 的「可报名活动」表格
# ============================================================================

# ---- ① 爬取入口：左栏「主题讲座」「辅学活动」下的「可报名活动」----
# 由步骤4.5 落地页的左栏 HTML 确认：
#     主题讲座 → /student/kbmhd/type/1.html
#     辅学活动 → /student/kbmhd/type/2.html
# ★ 这里只写【第 1 页】的地址，翻页由程序自己按 /p/N.html 的规则拼。
CRAWL_TARGETS = [
    {"name": "主题讲座", "url": "https://fuxue.nankai.edu.cn/student/kbmhd/type/1.html"},
    {"name": "辅学活动", "url": "https://fuxue.nankai.edu.cn/student/kbmhd/type/2.html"},
]

# ---- ② 总开关：False = 完全不爬（登录流程照跑）----
CRAWL_ENABLED = True

# ---- ③ ★ 筛选条件：只保留【活动时间晚于今天】的项目（不包含今天）----
# 判断依据是"活动时间"字段里的【第一个日期】，即开始日期。
# 严格比较：开始日期 > 今天 才保留。改成 False 则全部保留。
CRAWL_ONLY_FUTURE = True

# ---- ④ 翻页：上限 + 每页间隔（秒）----
CRAWL_MAX_PAGES = 15           # 最多翻多少页（防死循环）
CRAWL_DELAY = 0.5              # 每页之间停多久，礼貌一点

# ---- ⑤ ★ 测试功能开关 ----
# True  = 把 step5 返回的字典列表另存为同目录下的 CSV，方便你用 Excel 肉眼核对
# False = 什么都不干（只打印日志）
CRAWL_TEST_MODE = True
CRAWL_CSV_PATH = "activities.csv"

# ---- ⑥ 调试：把每个列表页的原始 HTML 存盘（解析结果不对时打开它排查）----
CRAWL_DUMP_HTML = False
CRAWL_DUMP_DIR = "crawl_html"

# ---- ⑦ 输出字典的字段顺序（同时决定 CSV 表头顺序、日志打印顺序）----
ACTIVITY_FIELDS = ["活动名称", "活动类型", "活动学时", "活动时间", "活动地点"]

# ---- 账号密码：★ 默认从【系统环境变量】读取，不把密码写进源码 ----
# 用法（Windows PowerShell）：
#     $env:CAS_USERNAME = "你的学号"
#     $env:CAS_PASSWORD = "你的密码"
#     python cas_login.py
# 用法（Windows cmd）：
#     set CAS_USERNAME=你的学号
#     set CAS_PASSWORD=你的密码
#     python cas_login.py
# 用法（Linux / macOS）：
#     export CAS_USERNAME=你的学号
#     export CAS_PASSWORD=你的密码
#     python cas_login.py
CREDENTIAL_ENV_USERNAME = "CAS_USERNAME"   # 账号对应的环境变量名（可自定义）
CREDENTIAL_ENV_PASSWORD = "CAS_PASSWORD"   # 密码对应的环境变量名（可自定义）

# ★ 下面两个是【兜底值】：只有在环境变量没设置时才会用到。
#   推荐保持留空 ""，这样密码就不会落到文件里（也不会被误提交到 git）。
USERNAME = ""                    # 例："zhangsan"（南开 IAM 用学号/工号，字段名叫 account）
PASSWORD = ""                    # 例："你的密码"（★ 会自动加密后再发出去；留空则只用环境变量）

# ---- 步骤2 的表单字段名（不同站点可能叫 user / loginName / pwd 等，必须与抓包一致）----
FORM_FIELD_USERNAME = "account"   # ★ 南开 IAM 的 JSON 里账号字段就叫 "account"（不是 username）
FORM_FIELD_PASSWORD = "password"  # 密码字段的 name

# ---- 步骤2 附加负载：本站点必需的额外字段（已实测确定，不要删）----
# 依据：前端登录表单（/portal-static/login_5f57e8985b8fc9f9c2b4.js）提交的是
#       {login_scene, account, password, account_type}
#   其中 login_scene 来自 URL 参数，缺省 "feilian"；
#       account_type 来自登录方式，本站点服务端 /api/login/setting 返回
#       login_orders=["feilian"]、login_enable_ldap=false，实测只有 "userid" 被接受。
# 实测四组对照（假账号，观察服务端返回）：
#   {"account","password"}                                    → 40000 参数错误
#   {"account","password","login_scene":"feilian"}            → 40000 参数错误
#   {"account","password","account_type":"ldap"}              → 40000 参数错误
#   {"login_scene":"feilian","account","password","account_type":"ldap"}
#                                                             → 10110002 当前认证方式不被允许
#   {"login_scene":"feilian","account","password","account_type":"userid"}
#                                                             → 10110001 用户名或密码错误  ★ 到了这一步才说明请求体完全正确
AUTH_API_EXTRA_FORM = {           # 追加进步骤2 请求体的固定字段
    "login_scene": "feilian",     # 登录场景，本站点缺省值
    "account_type": "userid",     # ★ 认证方式：学号/工号（不能用 ldap，本站点 LDAP 未启用）
}
AUTH_API_EXTRA_QUERY = "os=web"    # 前端 request() 会给所有接口统一追加的查询串；留空则不加
AUTH_API_CONTENT_TYPE = "json"    # "form" = 表单提交（application/x-www-form-urlencoded）；
                                  # "json" = 改成 json=payload 提交（requests 会自动设 Content-Type）
                                  # ★ 南开 IAM 用 "json"

# ---- 步骤2 请求头：模仿前端 request() 助手（webpack 模块 255866）----
# 该助手给每个请求都带上：X-Version-Check / X-Fe-Version / Accept-Language / Page-Name
# 以及 content-type 和 csrf-token。少这些头服务端不报错，但保持与浏览器一致更稳妥。
AUTH_API_EXTRA_HEADERS = {        # 合并进步骤2 的请求头
    "X-Version-Check": "1",       # globalInfo.VERSION_CHECK ? "1" : "0"
    "X-Fe-Version": "3.1.9.15606",# globalInfo.VERSION 去掉 "portal-fe-" 前缀
    "Page-Name": "portal",        # 管理端才是 "admin"
    "Accept-Language": "zh-CN,zh;q=0.9",
}
AUTH_API_SEND_CSRF_HEADER = True  # 把步骤1 拿到的 csrf-token cookie 复制成同名请求头（前端 module 618427 的行为）

# ---- 步骤2 返回体里 ticket 的字段名（按优先级依次尝试；支持嵌套一层，如 {"data":{"ticket":".."}}）----
TICKET_JSON_KEYS = ("ticket", "serviceTicket", "TICKET")

# ---- 步骤2 "登录成功"的判定方式：解析 JSON 里的 code 字段，不再做字符串匹配 ----
# ★ 以前是 `'"code":0' in response.text.replace(" ", "")` —— 响应一变格式
#   （多一层嵌套、code 变成字符串 "0"、字段顺序变化）就会静默失效。
AUTH_SUCCESS_JSON_KEY = "code"    # 成功码所在的 JSON 字段名
AUTH_SUCCESS_JSON_CODE = 0        # 服务端约定：0 = 账号密码被认可。改成 None 则不检查

# ---- 请求通用参数 ----
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")  # 伪装成浏览器，很多站点没 UA 会直接拒绝
REQUEST_TIMEOUT = 15              # 单次请求超时（秒）；requests 没有默认超时，必须显式给，否则可能永久卡死
VERIFY_SSL = True                 # True = 校验 https 证书（推荐）；站点自签名证书报错时才改 False

# ---- 判断登录是否成功的依据（步骤4 落地后检查）----
LOGIN_FAIL_KEYWORDS = ("登录失败", "用户名或密码错误", "密码错误", "账号或密码错误",
                       "invalid", "incorrect")            # 页面里出现这些词 = 登录失败
LOGIN_SUCCESS_COOKIE_NAMES = ("CASTGC", "JSESSIONID")      # 出现这些 Cookie = 大概率登录成功（CASTGC = CAS 全局票据）

# ---- 登录态复核：步骤4 之后主动再验一次（本项比"看页面里有没有失败字样"可靠）----
# 原理：把业务入口地址【不带 ticket】再访问一次。
#   已登录 → 服务器认识我们的会话，会直接送到业务页面（或送回业务首页）
#   未登录 → 服务器会把我们 302 踢回登录页
#      · 统一认证的登录页 = iam.nankai.edu.cn/login
#      · fuxue 自己的登录页 = fuxue.nankai.edu.cn/login/index.html
# 这样就能区分"真的登录了"和"只是恰好落在一个叫 login.html 的页面上"。
VERIFY_LOGIN_AFTER_STEP4 = True    # True = 步骤4 之后自动加一次复核；False = 不做复核
# ★ 复核地址填【目标业务系统的真实页面】：只有它能证明"fuxue 也登录成功了"。
#   留空 = 自动用 CAS_SERVICE_URL（那只证明 xgb 登录成功，证明不了 fuxue）。
VERIFY_LOGIN_URL = "https://fuxue.nankai.edu.cn/student/kbmhd/type/1/p/1.html"

# ---- 复核模式：一次请求 还是 三段式逐跳观察 ----
# "single"  = 只发 1 个请求（allow_redirects=True），靠【最终落地的域名】判断登没登录。
#             ★ 推荐：请求数 3 → 1，而且语义更接近"像浏览器一样再访问一次"。
# "verbose" = 老做法：allow_redirects=False，逐跳看 Location，把跳转目标分成
#             issuer / login / other 三类。多花 2 个请求、还会额外签一张新票，
#             但能把"到底是没登录、还是业务系统不认票据"区分开。排查问题时才开。
VERIFY_MODE = "single"

# ---- 调试：是否每次都打印完整 Cookie 容器 ----
# False（推荐）= 只打印【相比上一次新增/变化的】Cookie，日志短且重点清晰
# True         = 每次都把整个 session.cookies 打一遍（信息重复，排查时才需要）
DUMP_VERBOSE_COOKIES = False


# ============================================================================
# 【配置区 · 续】★★★ 密码加密：对应 iam.nankai.edu.cn 的 aesEncrypt ★★★
# 这一段是本文件和"明文版流程图"唯一不同的地方。
# ============================================================================

ENCRYPT_PASSWORD = True           # ★ 总开关。True = 密码 AES 加密后发出；False = 按流程图原样明文发出
                                  #   想对照"加密前 vs 加密后"的差别，把它改成 False 再跑一次即可

AES_KEY_SEED = "9007199254740991"  # 密钥种子：就是 JS 里的 Number.MAX_SAFE_INTEGER.toString()
                                   # 前端用 MD5(它) 得到 key，所以这个值改掉整套密文就变了

AES_KEY_HEX = ""                  # 留空 = 按上面的种子自动推导（= MD5 种子）
                                  # 若你抓包发现站点用的是别的密钥，直接在这里填 32 个十六进制字符
AES_IV_HEX = ""                   # 留空 = 自动推导，取 SHA1(key_hex) 的十六进制字符串的【前 16 个字符】
                                  # 注意：前端是把 40 字符的 SHA1 十六进制串整串当 IV 传进去，
                                  #       而 AES 的 IV 只要 16 字节，所以实际生效的是前 16 个字符
                                  # 留空是推荐做法（与前端行为完全一致）；填了则用你给的前 16 个字符

AES_OUTPUT_ENCODING = "hex"       # 密文编码："hex" = 十六进制（前端就是 hex）；"base64" = Base64

AES_SELF_TEST = True              # True = 启动时先跑一遍已知向量自检，确认加密结果与南开前端一致
                                  #   自检向量：AES("demo-1234") = 2b563d38ed5102f50e661c2cc33fec40
                                  #   自检不影响任何流程；不想看就把这里改 False

ENCRYPT_DEBUG_SHOW_CIPHERTEXT = True  # True = 步骤2 打印一小段密文预览，方便你对着抓包核对
                                      # 注意：永远不打印明文密码


# ============================================================================
# 【工具函数区】与流程图 5 个步骤无关的通用小工具
# ============================================================================

# ---------------------------------------------------------------------------
# 【日志脱敏区】★ 安全相关
#
#   本文件的所有输出最终都经过 safe_print()，而 safe_print() 会先把文本交给
#   redact() 过一遍。也就是说：**不管你在哪里打印什么，密钥、密码、账号、
#   Cookie 值、CAS 票据都不会以明文出现在日志里**。
#
#   为什么要做在"出口"而不是逐个打印点：
#     逐个打码总会漏（新增一行日志就可能漏一次）；做在唯一出口上，是"默认安全"。
# ---------------------------------------------------------------------------

REDACTED = "***"                                                     # 统一的占位符

_REDACT_RULES = (                                                    # 依次作用在每一段输出文本上
    # ① JSON 里的敏感字段：{"ticket":"ST-xxx"} → {"ticket":"***"}
    (re.compile(r'("(?:ticket|serviceTicket|token|access_token|password|pwd|passwd|'
                r'csrf-token|csrf|session|account|username|userName)"\s*:\s*")[^"]*(")', re.I),
     r"\1" + REDACTED + r"\2"),
    # ② URL 查询参数：?ticket=xxx / &token=xxx → ***
    (re.compile(r"([?&](?:ticket|serviceTicket|token|access_token|password|pwd|passwd|"
                r"csrf-token|csrf|session|account|username|code|id)=)[^&#\s\"'<>|]*", re.I),
     r"\1" + REDACTED),
    # ③ 表单体 / 键值对：password=xxx（只要左边不是字母数字下划线就认，中文标点后也算）
    (re.compile(r"(?<![0-9A-Za-z_])("
                r"(?:ticket|serviceTicket|token|access_token|password|pwd|passwd|"
                r"csrf-token|csrf|session|account|username)\s*=\s*)"
                r"[^\s\"'<>|&]*", re.I),
     r"\1" + REDACTED),
    # ④ Cookie / Set-Cookie 的 name=value（属性如 Path/HttpOnly 保留）
    (re.compile(r"\b(PHPSESSID|JSESSIONID|CASTGC|admin-csrf-token|csrf-token|session|cookie)"
                r"\s*=\s*([^;,\s\"'<>|]+)", re.I),
     r"\1=" + REDACTED),
    # ⑤ CAS 票据本体：ST-<时间戳>-<随机串>
    (re.compile(r"\bST-[0-9A-Za-z._\-]+"), "ST-" + REDACTED),
    # ⑥ 门户页 SSO 地址里的路径段：/token/<32位随机串> 与 /id/<学号>
    (re.compile(r"(/token/)[^/\s\"'<>?&#]+", re.I), r"\1" + REDACTED),
    (re.compile(r"(/id/)[^/\s\"'<>?&#]+", re.I), r"\1" + REDACTED),
)


_SECRET_VALUES = set()                                               # 运行期登记的敏感值（账号、密码等）


def register_secret(value):
    """登记一个"运行期才知道的敏感值"，之后它出现在任何输出里都会被替换成 ***。

    为什么需要：账号这类值每次都不一样，没法写进正则；登记后连落地页文本里的
    "欢迎回来<学号>！" 也会被自动打码。
    """
    text = "" if value is None else str(value)                       # 统一成字符串
    if len(text) >= 4:                                               # 太短的值（如 "1"）做全局替换会误伤正常文本
        _SECRET_VALUES.add(text)                                     # 登记原文
        quoted = quote(text, safe="")                                # URL 编码后的形式（账号含中文时用得上）
        if quoted and quoted != text:                                # 编码结果确实不同才登记
            _SECRET_VALUES.add(quoted)                               # 登记编码形式


def redact(text):
    """把任意文本里的敏感值替换成 ***（幂等；本文件所有日志输出都会经过它）。"""
    out = str(text)                                                  # 统一成字符串
    for secret in sorted(_SECRET_VALUES, key=len, reverse=True):     # ① 先替换登记过的敏感值（长串优先，避免部分覆盖）
        out = out.replace(secret, REDACTED)                          # 逐字替换
    for pattern, repl in _REDACT_RULES:                              # ② 再逐条过正则规则
        out = pattern.sub(repl, out)                                 # 命中即替换
    return out                                                       # 返回脱敏后的文本


def mask_secret(value, show=0):
    """把单个敏感值转成「长度可核对、内容不泄漏」的摘要。show>0 时保留前 show 个字符。"""
    text = "" if value is None else str(value)                       # None 当空串处理
    if not text:                                                     # 空值
        return "（空）"                                               # 直说
    if show > 0 and len(text) > show:                                # 允许保留一小截前缀（默认不给）
        return "{}…（已隐藏，共 {} 字符）".format(text[:show], len(text))  # 前缀 + 长度
    return "（已隐藏，共 {} 字符）".format(len(text))                 # 只给长度，不给任何内容


def mask_set_cookie(header_value):
    """Set-Cookie 头脱敏：保留 Cookie 名与 Path/HttpOnly 等属性，值换成 ***。"""
    segments = str(header_value or "").split(";")                    # 按分号拆段
    out = []                                                         # 结果容器
    for index, segment in enumerate(segments):                       # 逐段处理
        if index == 0 and "=" in segment:                            # 第一段是 name=value
            name = segment.split("=", 1)[0].strip()                  # 只留名字
            out.append("{}={}".format(name, REDACTED))               # 值打码
        else:                                                        # 其余是 Path/HttpOnly/Expires 等
            out.append(segment)                                      # 原样保留
    return ";".join(out)                                             # 拼回一个字符串


def _secret_fingerprint(value):
    """给敏感值算一个【不可逆指纹】（SHA-256 前 12 位）+ 字节数。

    用途：既能让你核对"跑两次是不是同一把 key"，又不会把 key 本身写进日志或文档。
    """
    data = b"" if value is None else (                             # 统一成 bytes
        value if isinstance(value, bytes) else str(value).encode("utf-8"))  # str/bytes 都支持
    return "{}（共 {} 字节）".format(hashlib.sha256(data).hexdigest()[:12], len(data))  # 指纹 + 长度


def safe_print(text):
    """跨平台安全打印：① 先做日志脱敏；② 兼顾 Windows cmd 的 GBK 控制台。

    ① 脱敏：所有输出都会先过 redact()，确保日志里不会出现密钥/密码/账号/Cookie/票据。
    ② 编码：编码不进去的字符用 "?" 顶替，保证程序永远不会因为打印而崩。
    """
    text = redact(text)                                              # ★ 唯一出口：先脱敏
    try:                                                             # 正常情况下直接打印
        print(text)                                                  # 输出内容
    except UnicodeEncodeError:                                       # 控制台编码放不下某些字符时
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"  # 取当前控制台编码，取不到用 utf-8
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))  # 替换后再打


def log(step, message):
    """统一打印函数：每行输出都带步骤前缀，方便对着流程图查看执行到哪一步了。"""
    safe_print("[{}] {}".format(step, message))                      # 例如：[步骤1] 已拿到登录页（内部已脱敏）


# ============================================================================
# 【密码加密区】对应 iam.nankai.edu.cn 前端模块 292684 的 aesEncrypt(e)
#   前端原始代码（已还原，去掉了压缩变量名）：
#     aesEncrypt = function (plain) {
#         const keyHex = CryptoJS.MD5(Number.MAX_SAFE_INTEGER.toString()).toString();
#         const ivFull = CryptoJS.SHA1(CryptoJS.enc.Utf8.parse(keyHex)).toString(CryptoJS.enc.Hex);
#         return CryptoJS.AES.encrypt(plain, CryptoJS.enc.Utf8.parse(keyHex), {
#             iv: CryptoJS.enc.Utf8.parse(ivFull),        // 40 字节，AES 只用前 16 字节
#             mode: CryptoJS.mode.CBC,
#             padding: CryptoJS.pad.Pkcs7,
#         }).ciphertext.toString(CryptoJS.enc.Hex);       // 只取密文，输出 hex
#     };
# 本区把它等价翻译成 Python，并准备了两种后端：
#   后端A：pycryptodome（你机器上已装）—— 快，一行搞定
#   后端B：纯标准库手写 AES —— 没装任何第三方库也能跑，而且能看清 AES 到底在算什么
# ============================================================================

# ---------- 1. 密钥 / IV 推导（与前端逐行对应）----------

def derive_key_iv():
    """按南开 IAM 前端的规则推导 AES 的 key 与 iv，返回 (key字节, iv字节, key十六进制, iv完整十六进制)。

    对应前端那两行：
        keyHex = MD5(seed).toString()                    → 32 个十六进制字符
        ivFull = SHA1(Utf8.parse(keyHex)).toString(Hex)  → 40 个十六进制字符
    """
    # ---- key ----
    if AES_KEY_HEX:                                                  # 配置区手工指定了密钥
        key_hex = AES_KEY_HEX.strip().lower()                        # 去掉空白并统一小写，避免手抖
    else:                                                            # 没指定 → 按前端规则推导
        # hashlib.md5(...).hexdigest() 等价于 CryptoJS.MD5(str).toString()
        # ★ 前端 MD5 的输入是【UTF-8 编码的 seed 字符串】，Python 的 encode("utf-8") 一一对应
        key_hex = hashlib.md5(AES_KEY_SEED.encode("utf-8")).hexdigest()

    # ★ 关键一行：前端用 Utf8.parse(key_hex) 把「32 个字符的十六进制串」当成 32 字节的密钥来用，
    #   而不是把它当十六进制解析成 16 字节。所以这里必须 encode("utf-8")，得到 32 字节 → AES-256。
    key = key_hex.encode("utf-8")

    # ---- iv ----
    if AES_IV_HEX:                                                   # 配置区手工指定了 IV
        iv_hex_full = AES_IV_HEX.strip().lower()                     # 统一小写
    else:                                                            # 没指定 → 按前端规则推导
        # 前端：SHA1(Utf8.parse(keyHex)) 得到 20 字节摘要，再用 .toString(Hex) 变成 40 个字符
        # Python 里等价写法：先 sha1 出 20 字节，再 .hexdigest() 得到 40 个字符
        iv_hex_full = hashlib.sha1(key_hex.encode("utf-8")).hexdigest()

    # 前端又把「40 个字符的十六进制串」整串 Utf8.parse 成 40 字节当 IV 传进去，
    # 而 AES-CBC 只需要 16 字节 IV → CryptoJS 实际只取前 4 个 word（= 前 16 字节）。
    # 所以这里同样只取前 16 个字符。
    iv = iv_hex_full[:16].encode("utf-8")

    register_secret(key_hex)                                         # ★ 登记 key：万一将来哪行代码打了它，也会自动变成 ***
    register_secret(iv_hex_full)                                     # ★ 登记 iv ：同上

    return key, iv, key_hex, iv_hex_full                             # 返回四元组，便于打印和调试


# ---------- 2. PKCS7 填充（CryptoJS 的 pad.Pkcs7）----------

def pkcs7_pad(data, block_size=16):
    """PKCS7 填充：AES 是分组密码，明文长度必须是 16 的整数倍，不够就用"缺几个字节补几"补齐。

    例：明文 6 字节 → 缺 10 字节 → 末尾补 10 个 0x0A；
        明文刚好 16 字节 → 还要再补一整块 16 个 0x10（这是 PKCS7 的规则，必须补）。
    """
    pad_len = block_size - (len(data) % block_size)                  # 要补多少字节：1~16，永远不会是 0
    return data + bytes([pad_len]) * pad_len                         # 返回值 = 原数据 + 重复 pad_len 次的那个字节


# ---------- 3. 后端A：pycryptodome（若已安装则优先使用）----------

try:                                                                 # 试着导入本机已装的 pycryptodome
    from Crypto.Cipher import AES as _PyCryptoAES                    # 只用到 AES 这一个类
    _AES_BACKEND = "pycryptodome"                                    # 记下用的是哪个后端
except Exception:                                                    # 没装 / 装坏了
    _PyCryptoAES = None                                              # 标记不可用，走后面的纯 Python 实现
    _AES_BACKEND = "pure-python"


def _encrypt_with_pycryptodome(plaintext_bytes, key, iv):
    """后端A：用 pycryptodome 做 AES-CBC/PKCS7 加密，返回十六进制密文。"""
    cipher = _PyCryptoAES.new(key, _PyCryptoAES.MODE_CBC, iv)        # 新建 AES-CBC 加密器（key 32 字节 = AES-256）
    padded = pkcs7_pad(plaintext_bytes, 16)                          # 自己填充，省得再 import Crypto.Util.Padding
    return cipher.encrypt(padded).hex()                              # 一次性加密（密码都很短），转成十六进制字符串


# ---------- 4. 后端B：纯标准库手写 AES（保证零第三方依赖也能跑）----------

def _xtime(value):
    """GF(2^8) 上乘 2：左移一位，若最高位溢出就异或 AES 的不可约多项式 0x1B。"""
    value <<= 1                                                      # 乘 2 = 左移 1 位
    if value & 0x100:                                                # 第 9 位溢出了
        value ^= 0x11B                                               # 异或 x^8+x^4+x^3+x+1（0x11B = 0x100 ^ 0x1B）
    return value & 0xFF                                              # 只保留低 8 位


def _gmul(a, b):
    """GF(2^8) 上的乘法（俄罗斯农民乘法），AES 里算 MixColumns 和求逆元都要用。"""
    result = 0                                                       # 累加结果
    while b:                                                         # b 还没被移完
        if b & 1:                                                    # b 的最低位是 1
            result ^= a                                              # 就把当前的 a 异或进结果
        a = _xtime(a)                                                # a 乘 2
        b >>= 1                                                      # b 右移一位
    return result & 0xFF                                             # 结果落在 0~255


def _build_aes_sbox():
    """构造 AES 的 S 盒：S(a) = 仿射变换(a 在 GF(2^8) 中的乘法逆元)，最后异或固定常数 0x63。"""
    # ---- 先用「生成元 3」的指数表/对数表快速求逆元 ----
    exp_table = [0] * 256                                            # exp_table[i] = 3^i
    log_table = [0] * 256                                            # log_table[3^i] = i
    value = 1                                                        # 3^0 = 1
    for i in range(255):                                             # GF(2^8) 的乘法群只有 255 个非零元素
        exp_table[i] = value                                         # 记录 3^i
        log_table[value] = i                                         # 反向记录
        value = _gmul(value, 3)                                      # 下一个幂：乘生成元 3
    inverse = [0] * 256                                              # inverse[x] = x 的乘法逆元
    for x in range(1, 256):                                          # 0 没有逆元，但因为仿射后正好是 0x63，不用特判
        inverse[x] = exp_table[(255 - log_table[x]) % 255]           # x^-1 = 3^(255 - log3(x))
    # ---- 再做仿射变换 ----
    sbox = [0] * 256                                                 # 256 项的 S 盒
    for x in range(256):                                             # 逐个字节算
        b = inverse[x]                                               # 先取逆元
        s = b                                                        # 仿射变换的累加器，先放原值
        for shift in (1, 2, 3, 4):                                   # 再异或 4 个循环左移
            s ^= ((b << shift) | (b >> (8 - shift))) & 0xFF          # 循环左移 shift 位（8 位内）
        sbox[x] = (s ^ 0x63) & 0xFF                                  # 最后异或常数 0x63
    return sbox                                                      # 返回 S 盒（解密用的逆 S 盒这里用不上）


_AES_SBOX = _build_aes_sbox()                                        # 模块导入时构建一次，后面一直复用


def _aes_expand_key(key):
    """AES 密钥扩展：把 32 字节主密钥扩展成 15 组轮密钥（AES-256 共 14 轮 + 1 次初始加轮密钥）。"""
    nk = len(key) // 4                                               # 主密钥字数：32 字节 → 8 个字
    nr = nk + 6                                                      # 轮数：AES-256 → 8 + 6 = 14
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]          # 前 8 个字直接来自主密钥
    rcon = 1                                                         # 轮常量，从 1 开始，每次 xtime
    for i in range(nk, 4 * (nr + 1)):                                # 一直扩展到 4*(14+1) = 60 个字
        temp = list(words[i - 1])                                    # 取上一个字的副本
        if i % nk == 0:                                              # 每 8 个字做一次「轮密钥核心变换」
            temp = [_AES_SBOX[temp[1]], _AES_SBOX[temp[2]],          # RotWord（左循环移 1 字节）+ SubWord
                    _AES_SBOX[temp[3]], _AES_SBOX[temp[0]]]          # 顺序：1,2,3,0
            temp[0] ^= rcon                                          # 第一个字节再异或轮常量
            rcon = _xtime(rcon)                                      # 轮常量乘 2，供下一次使用
        elif nk > 6 and i % nk == 4:                                 # AES-256 特有：中间还要多做一次 SubWord
            temp = [_AES_SBOX[b] for b in temp]                      # 只做 SubWord，不做 RotWord
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])  # 新字 = 「往前 nk 个字」异或 temp
    return [bytes(b for word in words[4 * r:4 * r + 4] for b in word) for r in range(nr + 1)]  # 每 4 字 = 1 组轮密钥


def _aes_encrypt_block(block, round_keys):
    """加密单个 16 字节分组（AES 的核心：SubBytes / ShiftRows / MixColumns / AddRoundKey 反复 14 轮）。"""
    state = list(block)                                              # 状态用一维列表表示：state[4*列 + 行]

    for i in range(16):                                              # ---- 初始 AddRoundKey ----
        state[i] ^= round_keys[0][i]                                 # 按字节异或本轮密钥

    for rnd in range(1, len(round_keys) - 1):                        # ---- 第 1 ~ 倒数第二轮的完整变换 ----
        state = [_AES_SBOX[b] for b in state]                        # ① SubBytes：逐字节过 S 盒（非线性替换）

        shifted = [0] * 16                                           # ② ShiftRows：第 r 行循环左移 r 个字节
        for row in range(4):                                         # 逐行处理
            for col in range(4):                                     # 逐列处理
                shifted[4 * col + row] = state[4 * ((col + row) % 4) + row]  # 行内左循环移位
        state = shifted                                              # 用移位后的状态继续

        for col in range(4):                                         # ③ MixColumns：每一列做 GF(2^8) 矩阵乘法
            s0, s1, s2, s3 = state[4 * col:4 * col + 4]              # 取出这一列的 4 个字节
            t0 = _xtime(s0) ^ (_xtime(s1) ^ s1) ^ s2 ^ s3            # 2*s0 ^ 3*s1 ^ s2 ^ s3
            t1 = s0 ^ _xtime(s1) ^ (_xtime(s2) ^ s2) ^ s3            # s0 ^ 2*s1 ^ 3*s2 ^ s3
            t2 = s0 ^ s1 ^ _xtime(s2) ^ (_xtime(s3) ^ s3)            # s0 ^ s1 ^ 2*s2 ^ 3*s3
            t3 = (_xtime(s0) ^ s0) ^ s1 ^ s2 ^ _xtime(s3)            # 3*s0 ^ s1 ^ s2 ^ 2*s3
            state[4 * col + 0], state[4 * col + 1] = t0, t1          # 写回这一列
            state[4 * col + 2], state[4 * col + 3] = t2, t3          # 写回剩余两个字节

        for i in range(16):                                          # ④ AddRoundKey：再异或本轮密钥
            state[i] ^= round_keys[rnd][i]                           # 对应位置异或

    state = [_AES_SBOX[b] for b in state]                            # ---- 最后一轮：只有 ①②④，没有 MixColumns ----
    shifted = [0] * 16                                               # ShiftRows 的缓冲
    for row in range(4):                                             # 逐行
        for col in range(4):                                         # 逐列
            shifted[4 * col + row] = state[4 * ((col + row) % 4) + row]  # 行内左循环移位
    for i in range(16):                                              # 最后一次 AddRoundKey
        shifted[i] ^= round_keys[-1][i]                              # 异或最后一组轮密钥

    return bytes(shifted)                                            # 输出 16 字节密文分组


def _encrypt_with_pure_python(plaintext_bytes, key, iv):
    """后端B：纯标准库实现的 AES-256-CBC + PKCS7，返回十六进制密文（与 CryptoJS 结果一致）。"""
    round_keys = _aes_expand_key(key)                                # 密钥扩展，只做一次
    padded = pkcs7_pad(plaintext_bytes, 16)                          # PKCS7 填充
    output = bytearray()                                             # 收集密文分组
    previous = iv                                                    # CBC 的「上一个密文分组」，第一块用 IV
    for offset in range(0, len(padded), 16):                         # 逐块加密
        block = padded[offset:offset + 16]                           # 取出这一块明文
        mixed = bytes(a ^ b for a, b in zip(block, previous))        # ★ CBC 的关键：先与上一块密文异或
        previous = _aes_encrypt_block(mixed, round_keys)             # 再送进 AES 单块加密
        output.extend(previous)                                      # 密文块既输出、又留给下一块当异或对象
    return output.hex()                                              # 转成十六进制字符串返回


# ---------- 5. 对外统一入口 ----------

def aes_encrypt(plaintext):
    """★ 本项目唯一对外暴露的加密函数：明文密码进，十六进制密文出。

    等价于南开 IAM 前端的 aesEncrypt()。内部自动选择后端（pycryptodome 优先，否则纯标准库）。
    """
    key, iv, _key_hex, _iv_hex_full = derive_key_iv()                # 每次现算 key/iv（很便宜，且便于配置区随时改）
    # 明文统一按 UTF-8 编码成字节；非 ASCII 密码（比如含中文）也能正确处理
    data = plaintext.encode("utf-8")                                 # 与 CryptoJS 的 Utf8 编码一致
    if _AES_BACKEND == "pycryptodome":                               # 优先用已安装的 pycryptodome
        cipher_hex = _encrypt_with_pycryptodome(data, key, iv)       # 走后端A
    else:                                                            # 环境里没有第三方密码库
        cipher_hex = _encrypt_with_pure_python(data, key, iv)        # 走内置的纯标准库后端
    if AES_OUTPUT_ENCODING.lower() == "base64":                      # 站点要求 Base64 密文时（少数站点这样）
        import base64                                                # 标准库，就近导入
        return base64.b64encode(bytes.fromhex(cipher_hex)).decode("ascii")  # 先还原成字节再 Base64 编码
    return cipher_hex                                                # 默认返回十六进制（与前端 hex 输出一致）


def self_test_crypto():
    """自检：用已知向量验证加密实现是否与南开 IAM 前端完全一致。返回 True/False。"""
    _key, _iv, _key_hex, _iv_hex_full = derive_key_iv()               # 现算一份 key/iv 用于验证（内容不打印）
    expected = "2b563d38ed5102f50e661c2cc33fec40"                     # 已用真实前端 crypto-js 跑出来的结果
    actual = aes_encrypt("demo-1234")                                 # 用同一份明文算一遍（中性测试串，不含任何真实凭据）
    log("加密自检", "后端 = {}（{}）".format(
        _AES_BACKEND,
        "已安装的 pycryptodome" if _AES_BACKEND == "pycryptodome" else "纯标准库手写实现"))  # 告诉用户用的是哪个后端
    log("加密自检", "key 指纹 = {}".format(_secret_fingerprint(_key)))  # ★ 只给不可逆指纹，不打印 key 本身
    log("加密自检", "iv  指纹 = {}".format(_secret_fingerprint(_iv)))   # ★ 只给不可逆指纹，不打印 iv 本身
    log("加密自检", "AES-256-CBC('demo-1234') = {}".format(actual))   # 算出来的密文
    if actual == expected:                                            # 与前端结果一致
        log("加密自检", "[OK] 与 iam.nankai.edu.cn 前端结果完全一致")   # 通过
        return True                                                   # 返回成功
    log("加密自检", "[失败] 与前端结果不一致，期望 {}".format(expected))  # 不一致
    return False                                                      # 返回失败


# ============================================================================
# 【会话工具区】把 requests 的用法包装成流程图里的说法
# ============================================================================

def build_session():
    """创建 requests 会话（流程图里的 Session）：Cookie 会自动保存在 session.cookies 里并自动携带。

    返回：requests.Session 对象 —— 后续 4 个步骤全都复用它，登录态才不会丢。
    """
    session = requests.Session()                                     # ★ 创建会话，等价于流程图里的 requests.Session()

    session.headers.update({                                         # 给会话设置"公共请求头"，之后每个请求自动带上
        "User-Agent": USER_AGENT,                                    # 伪装浏览器，降低被识别为脚本的概率
        "Accept": "text/html,application/json,*/*",                  # 声明可接受 HTML 和 JSON
        "Accept-Language": "zh-CN,zh;q=0.9",                         # 声明偏好中文页面
        "Connection": "keep-alive",                                  # 复用 TCP 连接，与"会话"语义一致
    })

    session.verify = VERIFY_SSL                                      # 会话级设置：是否校验 https 证书

    # ---- ★ 只给 GET 加自动重试（P1：一次网络抖动不该毁掉整条流程）----
    # ★★ 关键约束：allowed_methods 只放 GET。
    #    登录 POST 绝不能重试 —— 重试等于把同一个密码再提交一遍，
    #    轻则触发失败计数，重则直接锁号。
    #    合并逻辑：urllib3 2.x 用 allowed_methods；旧版用 method_whitelist，这里逐个试。
    retry_kwargs = dict(total=2, connect=2, read=2,                # 最多重试 2 次
                        status=2, backoff_factor=0.5,              # 间隔 0.5s / 1s 递增
                        status_forcelist=(502, 503, 504),          # 只对网关类错误重试
                        raise_on_status=False)                     # 重试用尽也不抛，交给上层判断
    try:                                                           # 优先用新参数名
        from urllib3.util.retry import Retry                      # requests 依赖 urllib3，一定存在
        retry = Retry(allowed_methods=frozenset(("GET",)), **retry_kwargs)
    except TypeError:                                              # 老版本 urllib3 只认 method_whitelist
        from urllib3.util.retry import Retry                      # 再导入一次（上面失败时名字也在）
        retry = Retry(method_whitelist=frozenset(("GET",)), **retry_kwargs)
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)     # 带重试策略的适配器
    session.mount("https://", adapter)                             # https 全走这个适配器
    session.mount("http://", adapter)                              # http 同理

    if not VERIFY_SSL:                                               # 关闭校验时，requests 每次请求都会刷 InsecureRequestWarning
        try:                                                         # urllib3 是 requests 的依赖；仍防御性包一层
            import urllib3                                           # 导入 urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # 关掉这个无害但很吵的警告
        except Exception:                                            # 万一环境异常
            pass                                                     # 忽略，不影响主流程

    return session                                                   # 返回会话对象


_cookie_seen = set()   # 上一次打印过的 (name, domain, value) 快照；只用来算"新增 / 变化"


def dump_cookies(session, title="当前 Cookie 容器（session.cookies）："):
    """打印 session.cookies —— 默认只显示【相比上一次新增或变化】的 Cookie。

    为什么不再每次打全量：整条流程会调用它 6 次，全量打印的信息高度重复，
    真正要看的是"这一步到底新种下了哪个 Cookie"，那才是登录态推进的信号。
    """
    global _cookie_seen                                              # 要更新快照
    cookies = list(session.cookies)                                  # 转成列表，便于判断是否为空
    log("Cookie", title)                                             # 打印小标题
    if not cookies:                                                  # 一个 Cookie 都没有
        safe_print("    （Cookie 容器为空）")                         # 明确提示
        return                                                       # 结束
    known = {(key[0], key[1]) for key in _cookie_seen}               # 上次见过的 (名字, 域)
    rows = []                                                        # 这次要打印的行
    for cookie in cookies:                                           # 逐个遍历
        key = (cookie.name, cookie.domain, cookie.value)             # 唯一标识一个 Cookie
        if DUMP_VERBOSE_COOKIES or key not in _cookie_seen:          # 全量模式，或本步新出现/值变了
            tag = "变化" if (cookie.name, cookie.domain) in known else "新增"
            rows.append((cookie, tag))                               # 记下来
    _cookie_seen = {(c.name, c.domain, c.value) for c in cookies}    # 更新快照
    if not rows:                                                     # 本步没有任何变化
        log("Cookie", "    （本步没有新增/变化的 Cookie；共 {} 个，要看全部把 DUMP_VERBOSE_COOKIES 改 True）".format(
            len(cookies)))                                           # 明确说明
        return                                                       # 结束
    for cookie, tag in rows:                                         # 只打印有变化的行
        safe_print("    {:<12} = {:<40} domain={:<24}[{}]".format(     # 名字 / 打码后的值 / 域 / 标签
            cookie.name, mask_secret(cookie.value), cookie.domain, tag))
    return                                                           # 显式返回，避免误以为有返回值


def cas_entry_url():
    """返回 CAS 入口的完整地址（步骤1 与 步骤2.5 共用同一份推导结果）。

    ★ 单一真相来源：service 只写在 CAS_SERVICE_URL 一处，这里运行时拼出来，
      从根上消除"两处字符串不一致 → CAS 判定 service 不匹配 → 票据无效"。
    优先级：CAS_TICKET_URL（手填的完整地址）> CAS_TICKET_ENDPOINT + service
    """
    if CAS_TICKET_URL:                                               # 手工填了完整地址就用它
        return CAS_TICKET_URL                                        # 直接返回
    if not CAS_TICKET_ENDPOINT or not CAS_SERVICE_URL:               # 缺零件就拼不出来
        return ""                                                    # 交给调用方报错
    separator = "&" if "?" in CAS_TICKET_ENDPOINT else "?"           # 端点已带 ? 就用 & 连接
    return "{}{}service={}".format(                                  # 拼成完整入口地址
        CAS_TICKET_ENDPOINT, separator, quote(CAS_SERVICE_URL, safe=""))  # service 做 URL 编码


def visible_text(response):
    """取页面的【可见文字】（而不是原始 HTML 源码）。

    为什么需要：LOGIN_FAIL_KEYWORDS 是拿关键词判断登录成没成。直接扫 HTML 源码时，
    页面里任何一处 JS 字符串、注释、隐藏模板里含"登录失败"都会造成假阳性。
    用 lxml 取 text_content() 之后只剩用户真正看得见的文字，判断才可信。
    """
    body = decode_response(response)                                 # ★ 统一编码，避免中文乱码
    if not body:                                                     # 空响应
        return ""                                                    # 没有文字
    try:                                                             # lxml 解析失败时不能影响主流程
        return lxml_html.fromstring(body).text_content()             # 只要可见文字
    except Exception:                                                # 解析失败
        return body                                                  # 退回原始文本


def extract_ticket(response):
    """从响应里提取 ticket：优先 JSON，其次正文正则，最后看 Location 头（含整条跳转链）。

    ★ 最后一条现在会扫 response.history：如果调用方开了 allow_redirects=True，
      ticket 就藏在被自动跟掉的某个中间响应的 Location 头里（requests 会把它们
      留在 response.history 中）——这样"自动跟随"和"拿到 ticket"就不再互斥。
    """
    try:                                                             # 大多数 CAS 接口返回 JSON
        data = response.json()                                       # requests 内置解析，比 json.loads(response.text) 省事
    except ValueError:                                               # 响应不是合法 JSON（比如返回的是 HTML 页面）
        data = None                                                  # 标记解析失败，走后面的兜底逻辑

    if isinstance(data, dict):                                       # 顶层是字典：直接找 ticket 字段
        for key in TICKET_JSON_KEYS:                                 # 按配置的候选字段名依次尝试
            if data.get(key):                                        # 命中且值非空
                return str(data[key])                                # 返回 ticket
        for value in data.values():                                  # 再找嵌套一层：{"data": {"ticket": ".."}}
            if isinstance(value, dict):                              # 值本身是字典才可能是嵌套结构
                for key in TICKET_JSON_KEYS:                         # 同样按候选名尝试
                    if value.get(key):                               # 命中
                        return str(value[key])                       # 返回 ticket
    if isinstance(data, str) and data and data.lower() != "success":  # 少数接口直接把 ticket 当纯字符串返回
        return data                                                  # 视为 ticket 返回

    body = decode_response(response)                                 # ★ 按页面声明编码解码，避免中文乱码干扰正则
    match = re.search(r'"(?:ticket|serviceTicket|TICKET)"\s*:\s*"([^"]+)"', body or "")  # 正则兜底：从正文里抠
    if match:                                                        # 抠到了
        return match.group(1)                                        # 返回捕获组

    # ---- 最后一种：ticket 藏在 302 的 Location 头里 ----
    locations = [response.headers.get("Location", "")]                # 先看最终响应自己的 Location
    for hop in getattr(response, "history", None) or []:             # ★ 再看被自动跟掉的每一跳
        locations.append(hop.headers.get("Location", ""))            # 中间响应的 Location 同样保留着
    for location in locations:                                       # 按顺序找第一个带 ticket 的
        match = re.search(r"[?&]ticket=([^&#]+)", location or "")    # 从跳转地址里找 ticket 参数
        if match:                                                    # 找到了
            from urllib.parse import unquote                         # 就近导入，只为这一处使用
            return unquote(match.group(1))                           # URL 解码后返回

    return None                                                      # 四种方式都没找到，返回 None 交给调用方处理


# ============================================================================
# 【主流程区】严格对应流程图的 步骤1 ~ 步骤4.5（步骤5 爬虫区见文件后半部分）
# ============================================================================

def step1_open_login_page(session):
    """步骤1：预访问 CAS 入口，把后续步骤必需的会话 Cookie 拿到手。

    ★ 为什么只发 2 个请求（而不是让 requests 一路跟完 3 跳）：
      本站点 CAS 入口的真实链路是
        第1跳  GET https://xgb.nankai.edu.cn/login/fslogin
                 → 302，种下 xgb 域 PHPSESSID
        第2跳  GET https://iam.nankai.edu.cn/api/cas/login?service=...
                 → 302 ★ 种下 iam 域 csrf-token + session（步骤2 必需）
        第3跳  GET https://iam.nankai.edu.cn/login?next=...
                 → 200，返回 React 登录页，【不种任何 Cookie】

      实测（本机 2026 探测）：直接 GET https://iam.nankai.edu.cn/login 不种任何 Cookie；
      csrf-token / session 是在第 2 跳 /api/cas/login 的响应头里种下的。
      所以第 3 跳对爬虫是纯浪费——步骤2 只用到 Cookie，用不到登录页 HTML。

    返回：dict（status / html / hops）/ None（失败）。
    """
    log("步骤1", "GET CAS 入口（空会话、无负载）：{}".format(CAS_LOGIN_PAGE_URL))  # 打印即将访问的地址
    try:                                                             # 网络类异常统一处理
        # ★ allow_redirects=False：手动控制跟到第几跳，省掉最后一跳无用的登录页
        response = session.get(CAS_LOGIN_PAGE_URL, timeout=REQUEST_TIMEOUT,
                               allow_redirects=False)                 # 第1跳：种 xgb 的 PHPSESSID
    except requests.exceptions.RequestException as error:            # 连不上 / 超时 / 证书错误等
        log("步骤1", "失败：{}".format(error))                        # 打印具体原因
        return None                                                  # 返回 None，主流程中止

    hops = 1                                                         # 已经发了几个请求
    location = response.headers.get("Location", "")                  # 第1跳的跳转目标
    if response.is_redirect and location:                            # 还有下一跳
        next_url = urljoin(response.url, location)                   # ★ 服务器可能给相对地址，必须补全
        log("步骤1", "  HTTP {} -> {}".format(response.status_code, next_url))  # 打印这一跳
        try:                                                         # 网络类异常统一处理
            response = session.get(next_url, timeout=REQUEST_TIMEOUT,
                                   allow_redirects=False)            # 第2跳：★ 种 csrf-token / session
        except requests.exceptions.RequestException as error:        # 请求失败
            log("步骤1", "失败：{}".format(error))                    # 打印原因
            return None                                              # 中止
        hops += 1                                                    # 计数

    log("步骤1", "  最后停在 HTTP {}：{}（共 {} 个请求）".format(          # 确认拿到了 Cookie 来源
        response.status_code, response.url, hops))                   # 状态码 + 地址 + 请求数
    dump_cookies(session, "服务器写入的 Cookie（csrf-token / session 就靠这一步）：")  # 应当能看到两类 Cookie
    return {"status": response.status_code, "html": decode_response(response), "hops": hops}  # 返回现场


def step2_post_credentials(session):
    """步骤2：POST 提交账号密码，拿到 ticket 或"待签发"信号。

    流程图要求：
      请求头：自动携带 Cookie（requests 从 session.cookies 读取）
      请求负载：username=账号 & password=密码（代码手动构造）
      返回体：JSON，里面有 ticket → 必须手动提取存变量

    ★ 本文件的改动点：请求负载里的 password 【不是明文】，而是先过一遍 aes_encrypt() 再放进去。
      也就是说，只有"密码字段的值"变了，流程图的结构、Cookie 流转、ticket 提取全都没变。

    返回：
      {"ticket": "ST-...", "validate_url": "..."}  → 通用 CAS，POST 响应里就带票
      {"ticket": None, "validate_url": ""}         → 本站点：登录成功但票据要另行签发（交给步骤2.5）
      None                                          → 登录失败
    """
    if not USERNAME or not PASSWORD:                                 # 防御性检查：没配账号密码就别发请求
        log("步骤2", "失败：没拿到账号密码 —— 请设置环境变量 {} / {}，或填写配置区 USERNAME / PASSWORD".format(
            CREDENTIAL_ENV_USERNAME, CREDENTIAL_ENV_PASSWORD))        # 明确提示两种提供方式
        return None                                                  # 中止

    # ---- ① 先把密码加密（对应前端 aesEncrypt），得到可以直接发出的密文字符串 ----
    if ENCRYPT_PASSWORD:                                             # 总开关开着
        password_value = aes_encrypt(PASSWORD)                       # ★ 明文进 → 十六进制密文出
        encoding_name = "base64" if AES_OUTPUT_ENCODING.lower() == "base64" else "hex"  # 日志里显示编码名
        if ENCRYPT_DEBUG_SHOW_CIPHERTEXT:                            # 想核对抓包时打印一小段
            log("步骤2", "密码已加密：明文 {} 字符 → {} 密文 {}...（AES-256-CBC/PKCS7，{} 编码）".format(
                len(PASSWORD), len(password_value), password_value[:24], encoding_name))  # 只打印长度和前缀
        else:                                                        # 不想看密文时
            log("步骤2", "密码已加密后发出（{} 编码，长度 {}）".format(encoding_name, len(password_value)))  # 简述
    else:                                                            # 关掉加密 → 退回流程图原始行为
        password_value = PASSWORD                                    # 直接用明文（仅用于对照学习）
        log("步骤2", "[注意] ENCRYPT_PASSWORD=False，本次按流程图原样【明文】发送密码")  # 明确警告

    # ---- ② 手动构造请求负载（流程图原话），密码位置放的是密文 ----
    payload = {                                                      # ★ 手动构造请求负载（流程图原话）
        FORM_FIELD_USERNAME: USERNAME,                               # 账号字段（明文，与前端一致）
        FORM_FIELD_PASSWORD: password_value,                         # 密码字段（★ 密文）
    }
    payload.update(AUTH_API_EXTRA_FORM)                              # 追加配置要求的额外参数（如 service）

    # ---- ③ 组装请求头与最终 URL（对应前端 request() 助手的行为）----
    headers = dict(AUTH_API_EXTRA_HEADERS)                           # 复制一份，避免修改到配置本身
    request_url = CAS_AUTH_API_URL                                   # 默认就用配置里的地址
    if AUTH_API_EXTRA_QUERY:                                         # 前端会给所有接口统一追加 os=web
        request_url += ("&" if "?" in request_url else "?") + AUTH_API_EXTRA_QUERY  # 已带 ? 就用 & 连接
    if AUTH_API_SEND_CSRF_HEADER:                                    # 把 cookie 里的 csrf-token 复制到请求头
        csrf = next((c.value for c in session.cookies if c.name == "csrf-token"), "")  # 从步骤1 建立的会话里取
        if csrf:                                                     # 取到了才加
            headers["csrf-token"] = csrf                             # 前端 getCsrfTokenHeaders() 就是这么干的
    if headers:                                                      # 打印实际用的附加头，便于对照抓包
        log("步骤2", "附加请求头：{}".format(", ".join(sorted(headers.keys()))))  # 只打印头名字，避免刷屏

    log("步骤2", "POST 提交账号密码：{}（Cookie 由 session 自动携带）".format(request_url))  # 打印动作

    try:                                                             # 网络类异常统一处理
        if AUTH_API_CONTENT_TYPE == "json":                          # 站点要求 JSON 提交时
            response = session.post(request_url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)   # json= 自动设 Content-Type
        else:                                                        # 默认走最通用的表单提交
            response = session.post(request_url, data=payload, headers=headers, timeout=REQUEST_TIMEOUT)   # data= 即表单编码
    except requests.exceptions.RequestException as error:            # 请求失败
        log("步骤2", "失败：{}".format(error))                        # 打印原因
        return None                                                  # 中止

    log("步骤2", "HTTP {}，响应体前 200 字符：{}".format(
        response.status_code, decode_response(response)[:200].replace("\n", " ")))  # 打印片段（按声明编码解码）

    # ---- ③ 判定"登录是否被认可"：解析 JSON 的 code 字段（不再做字符串匹配）----
    body = decode_response(response)                                 # ★ 统一编码后的响应文本（中文不乱码）
    try:                                                             # 不是 JSON 就跳过
        data = response.json()                                       # requests 内置解析
    except ValueError:                                               # 返回的是 HTML 之类
        data = None                                                  # 保持 None

    auth_ok = None                                                   # None = 无法判断
    if isinstance(data, dict) and AUTH_SUCCESS_JSON_CODE is not None:  # 响应是 JSON 且配置了成功码
        code = data.get(AUTH_SUCCESS_JSON_KEY)                       # 取出 code 字段
        auth_ok = code == AUTH_SUCCESS_JSON_CODE                     # ★ 严格比较，不再猜字符串形态
        log("步骤2", "响应 JSON：{} = {!r} → {}".format(               # 打印判定依据，方便对照抓包
            AUTH_SUCCESS_JSON_KEY, code,
            "账号密码已被服务端认可" if auth_ok else "登录未被认可"))

    ticket = extract_ticket(response) or ""                          # ★ 手动把 ticket 提取到 Python 变量
    validate_url = ""                                                # POST 直接给出校验地址时收下
    location = response.headers.get("Location", "")                  # 有些 CAS 会直接 302 回业务地址
    if location and "ticket=" in location.lower():                   # 这一跳本身就是校验地址
        validate_url = urljoin(request_url, location)                # 补成绝对地址

    if ticket:                                                       # 通用 CAS：POST 响应里就有票
        log("步骤2", "[OK] 步骤2 响应里已带 ticket = {}".format(ticket))  # 确认票据已存进变量
        dump_cookies(session, "Cookie 容器（步骤2 之后）：")           # 看这一步种下了什么
        return {"ticket": ticket, "validate_url": validate_url}      # 交给"签发/校验"那一步

    # ---- ④ 没票：先把服务端可能给出的失败线索全部打出来 ----
    hints = []                                                       # 收集提示，最后统一打印
    if "10110001" in body:                                           # ★ 本站点：账号或密码不对
        hints.append("10110001 = 用户名或密码错误 —— 说明请求格式【完全正确】，只是账号/密码填错了")
    if "10110002" in body:                                           # ★ 本站点：认证方式不对
        hints.append("10110002 = 认证方式不被允许 —— 请检查 AUTH_API_EXTRA_FORM 里的 account_type")
    if "参数错误" in body:                                           # 请求体缺字段
        hints.append("『参数错误』= 请求体缺字段，对照抓包里 POST 的请求体补进 AUTH_API_EXTRA_FORM")
    if any(word in body for word in LOGIN_FAIL_KEYWORDS):            # 其它失败关键词
        hints.append("响应里出现了失败关键词，请重点核对账号密码")

    if auth_ok is False:                                             # 服务端明确说没通过 → 不必再往下试
        log("步骤2", "失败：登录接口返回 {}={!r}（期望 {}），账号或密码不对".format(
            AUTH_SUCCESS_JSON_KEY, (data or {}).get(AUTH_SUCCESS_JSON_KEY), AUTH_SUCCESS_JSON_CODE))
        for hint in hints:                                           # 逐条打印线索
            log("步骤2", "提示：{}".format(hint))                     # 指向具体排查方向
        return None                                                  # 中止

    for hint in hints:                                               # 判断不了成败时，线索照样打出来
        log("步骤2", "提示：{}".format(hint))                         # 指向具体排查方向
    log("步骤2", "本站点登录接口不直接返回 ticket（这是正常的）→ 交给步骤2.5 去 CAS 入口现场签发")
    dump_cookies(session, "Cookie 容器（步骤2 之后）：")               # 此处 iam 域的 session 应该已经更新
    return {"ticket": None, "validate_url": ""}                      # 交给"签发/校验"那一步


def establish_login_state(session, issued):
    """步骤2.5 + 步骤3 + 步骤4 合并：签发 service 票据 → 校验 → 落地业务系统。

    ★★ 这个函数就是"为什么不拦截 302 会让 ticket 当场被用掉"的答案 ★★

    service ticket（ST）是【一次性】的。CAS 的判定链只有两跳：
        ① GET <CAS入口>?service=<业务地址>   → 302 到 <业务地址>?ticket=ST-xxx    ← 签发票据
        ② GET <业务地址>?ticket=ST-xxx       → 302 到业务首页                     ← 校验并【消费】票据

    第②跳一旦发生，这张 ST 就作废了。所以"拦不拦 302"本质上是在决定【由谁来完成第②跳】：

        拦（allow_redirects=False）→ 我们自己发第②跳。票据还在手里，能手动拼 URL、
                                     能逐跳看 Location/Set-Cookie，出错停在哪一跳一目了然。
        不拦（allow_redirects=True）→ requests 替我们发第②跳。票据当场被消费；
                                     此时若再用同一张票请求一次，CAS 会按重放/无效票据处理，
                                     通常直接 302 回登录页 —— 于是日志显示"登录失败"，
                                     而实际上登录早就成功了。这是最有欺骗性的一种失败。

    因此本函数【先拦后放】：
        签发那一跳必须 allow_redirects=False —— 必须读到 Location 才能拿到 ST；
        校验这一跳则 allow_redirects=True —— 这张票本来就要在这一跳被消费掉，
                                            再拦着它就永远到不了业务系统。

    ★ 顺带解决另一个坑：校验地址【直接用服务器 302 给的 Location】，不再自己拼
      "<CAS_SERVICE_URL>?ticket=..."。CAS 要求签发与校验两边的 service 逐字符相同，
      用服务器给的原地址就天然一致；自己拼则依赖两处配置不写错。

    参数 issued：步骤2 的返回 {"ticket": str|None, "validate_url": str}
    返回：落地结果 {"status","final_url","html"}；失败返回 None。
    """
    ticket = (issued or {}).get("ticket") or ""                      # 步骤2 有没有直接给票
    validate_url = (issued or {}).get("validate_url") or ""          # 步骤2 有没有直接给校验地址

    # ================= ① 签发：手里没票，就去 CAS 入口拿一张 =================
    if not ticket and not validate_url:                              # 通用流程：POST 响应里没有 ticket
        entry = cas_entry_url()                                      # 入口地址由 CAS_SERVICE_URL 推导
        if not entry:                                                # 配置不全
            log("步骤2.5", "失败：CAS_TICKET_ENDPOINT / CAS_SERVICE_URL 没配置，无法签发 ticket")
            return None                                              # 中止
        log("步骤2.5", "登录接口未返回 ticket，改为 GET CAS 入口签发：{}".format(entry))
        try:                                                         # 网络类异常统一处理
            probe = session.get(entry, timeout=REQUEST_TIMEOUT,      # ★ 这一跳必须拦住
                                allow_redirects=False)               #   自动跟掉就再也读不到 Location
        except requests.exceptions.RequestException as error:        # 请求失败
            log("步骤2.5", "GET CAS 入口失败：{}".format(error))       # 打印原因
            return None                                              # 中止
        location = probe.headers.get("Location", "")                  # ★ ticket 就藏在这里
        log("步骤2.5", "HTTP {}，Location = {}".format(                 # 打印现场，便于对照
            probe.status_code, location or "(没有 Location)"))
        ticket = extract_ticket(probe) or ""                          # 从 Location 里抠 ticket
        if location:                                                  # 服务器给了目标地址
            validate_url = urljoin(entry, location)                   # ★ 直接用它，不再自己拼
        if not ticket:                                                # 没抠到票
            if "login" in location.lower():                           # 被弹回登录页 = 登录态没建立
                log("步骤2.5", "失败：签发入口又把你弹回登录页，说明步骤2 的登录请求没被服务端认可")
                log("步骤2.5", "      请优先核对账号密码，以及 AUTH_API_EXTRA_FORM 里的 account_type")
            else:                                                     # 其他原因
                log("步骤2.5", "失败：Location 里没有 ticket 参数（{}）".format(location or "无 Location"))
            return None                                               # 中止
        log("步骤2.5", "[OK] 已拿到 ticket = {}".format(ticket))        # 确认票据已存进变量
        dump_cookies(session, "Cookie 容器（签发之后）：")              # 看这一步又种下了什么

    # ================= ② 校验 + 落地：这里才允许自动跟 302 =================
    if not validate_url:                                             # 只有票、没有现成地址（通用 CAS 的形态）
        separator = "&" if "?" in CAS_SERVICE_URL else "?"            # 业务地址可能已带 ?xxx=yyy
        validate_url = "{}{}ticket={}".format(                        # ★ 兜底才手动拼
            CAS_SERVICE_URL, separator, quote(ticket, safe=""))       # ticket 必须 URL 编码
        log("步骤3", "响应里只有 ticket、没有现成地址 → 手动拼校验地址：{}".format(validate_url))
    else:                                                            # 服务器给了完整地址
        log("步骤3", "校验地址直接采用服务器 302 给出的 Location（不再自己拼，避免 service 不一致）")

    log("步骤3", "GET 校验地址（★ 允许自动跟随：票据本来就要在这一跳被消费）：{}".format(validate_url))
    try:                                                             # 网络类异常统一处理
        response = session.get(validate_url, timeout=REQUEST_TIMEOUT,  # 发校验请求
                               allow_redirects=True)                 # ★ 这里放行，一路跟到业务系统
    except requests.exceptions.RequestException as error:            # 请求失败
        log("步骤3", "失败：{}".format(error))                        # 打印原因
        return None                                                  # 中止

    for item in response.history:                                    # 逐跳打印：自动跟随也能事后看历史
        set_cookie = item.headers.get("Set-Cookie", "")              # 这一跳种下的 Cookie
        log("步骤3", "  跳转经历：HTTP {}{} -> {}".format(             # 状态码 + Cookie + 下一跳
            item.status_code,
            "，Set-Cookie: " + mask_set_cookie(set_cookie) if set_cookie else "",
            item.headers.get("Location", "")))
    log("步骤4", "HTTP {}，最终落地地址：{}".format(response.status_code, response.url))
    dump_cookies(session, "Cookie 容器（业务系统登录 Cookie 已写入）：")

    page_text = visible_text(response)                               # ★ 只看可见文字，避免被 JS 字符串误伤
    failed = any(word in page_text for word in LOGIN_FAIL_KEYWORDS)  # 页面出现失败关键词 = 登录失败
    cookie_names = [cookie.name for cookie in session.cookies]        # 当前所有 Cookie 名
    has_login_cookie = any(name in cookie_names for name in LOGIN_SUCCESS_COOKIE_NAMES)  # 是否出现登录态 Cookie
    if failed:                                                        # 命中失败关键词
        log("结果", "[失败] 页面里出现了失败提示，请检查账号密码或字段名")
    elif has_login_cookie:                                            # 有登录态 Cookie
        log("结果", "[成功] 已在 session.cookies 中看到 {}".format(cookie_names))
    else:                                                             # 既没失败提示也没常见登录 Cookie
        log("结果", "[注意] 没看到失败提示，也没看到常见登录 Cookie，请人工核对页面内容")
    return {"status": response.status_code, "final_url": response.url,  # 返回落地结果
            "html": decode_response(response)}                        # ★ 统一编码后的 HTML（步骤4.5 会复用）


def extract_html_redirect(html, base_url):
    """从 HTML 里挖出「前端跳转」目标（meta refresh / location.href / location.replace）。

    为什么需要：
        有些 SSO 中转不是 302，而是返回一个 HTTP 200 的 HTML，再靠 JS 或 meta 跳一次。
        requests 不会执行 JS，如果不手动把目标抠出来再请求一次，就会停在中间页上，
        后面的业务系统永远拿不到登录态。

    返回：绝对 URL；找不到时返回 None。
    """
    if not html:                                                     # 空页面
        return None                                                  # 没有可跳的目标
    patterns = (                                                     # 常见的几种前端跳转写法
        r'<meta[^>]+http-equiv\s*=\s*["\']?refresh["\']?[^>]*content\s*=\s*["\'][^"\']*url\s*=\s*([^"\'>\s]+)',
        r'location\s*\.\s*(?:href|replace)\s*\(\s*["\']([^"\']+)["\']',
        r'location\s*\.\s*(?:href|replace)\s*=\s*["\']([^"\']+)["\']',
        r'window\s*\.\s*open\s*\(\s*["\']([^"\']+)["\']',
    )
    for pattern in patterns:                                         # 逐个模式找
        match = re.search(pattern, html, re.IGNORECASE)               # 找第一个命中
        if match is None:                                            # 这个模式没命中
            continue                                                 # 试下一个
        target = match.group(1).strip()                              # 抠出来的目标
        if target and not target.lower().startswith(("javascript:", "#")):  # 排除无效目标
            return urljoin(base_url, target)                         # 补成绝对 URL 返回
    return None                                                      # 都没有


def find_sso_entry_links(html, base_url):
    """从「一站式登录平台」门户页里，把各个子系统的 SSO 入口链接全部抠出来。

    门户页里每个系统的结构都是（实测抓到的真实 HTML）：
        <li><a href="http://fuxue.nankai.edu.cn/Home/Login/orlogin/type/1/id/<学号>/token/36c2...aa01.html" target="_blank">
            <div class="img img1"></div>
            <p>立公增能辅学平台</p>
        </a></li>

    返回 [(绝对URL, 链接文字), ...]，按页面出现顺序排列。

    ★ 为什么必须现抓：URL 里的 token 是门户页【每次访问时由服务端现生成】的，
      32 位随机串，和学号绑定，写死必失效。
    """
    results = []                                                     # [(URL, 文字)]
    for match in re.finditer(                                        # 逐个 <a> 标签找
        r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        html, re.IGNORECASE | re.DOTALL
    ):
        href = match.group(1).strip()                                # 链接地址
        if not href or href.startswith(("javascript:", "#")):        # 跳过 JS 假链接
            continue                                                 # 下一个
        # 只要"登录入口"型的链接：地址里带 orlogin（门户页的 SSO 中转格式）
        if "orlogin" not in href.lower():                            # 不是 SSO 入口
            continue                                                 # 下一个
        text = re.sub(r"<[^>]+>", "", match.group(2))                 # 去掉包在里面的标签
        text = re.sub(r"\s+", "", text)                              # 去掉空白
        results.append((urljoin(base_url, href), text))              # 补成绝对地址
    return results                                                   # 按出现顺序返回


def step4_5_enter_business_system(session, landing=None):
    """步骤4.5：进入业务系统（SSO 中转的最后一跳），进入 fuxue.nankai.edu.cn。

    ★ 这一步【不是爬虫】，只是登录流程的最后一跳，不抓取任何业务数据。

    实际动作（已由实跑 + 落地页 HTML 证实）：
        ① 拿到「一站式登录平台」门户页 SSO_PORTAL_URL 的 HTML
           —— ★ 优先复用步骤4 的落地响应（两者通常是同一个页面），省 1 个请求
        ② 在 HTML 里按 SSO_LINK_KEYWORD 找到目标系统的 SSO 入口链接
        ③ GET 那个链接，拿到 fuxue 域的登录态

    ★ 真正的入口长这样（token 每次现生成，绝不能写死）：
        http://fuxue.nankai.edu.cn/Home/Login/orlogin/type/1/id/<学号>/token/<32位随机串>.html
      它【不在】fuxue 自己的登录页上，也不在 /xszz/Login/index 那条路上
      （实测那条路 CAS 的 service 参数始终是 xgb 自己的 /login/fslogin，全程不提 fuxue）。

    ★ 本函数额外做两件事：
        ① 手动跟 302 链，每一跳都补 Referer（requests 自动跟跳转时不会补）；
        ② 自动跟随页面里的 meta refresh / JS location 跳转（requests 不执行 JS）。

    ★ 注意：fuxue 在任何匿名请求上都会种 PHPSESSID（连登录页都会），
      所以"有 fuxue 域 Cookie"【不等于】登录成功，唯一可信的判据是后面的【复核】。

    参数：landing = 步骤4 的返回值（含 final_url / html），可为 None。
    返回：最后一跳的落地结果 dict；未配置或失败时返回 None（不影响主流程继续复核）。
    """
    entry_urls = list(BUSINESS_ENTRY_URLS)                           # 兜底用的写死地址

    # ---- 第 0 步：拿到门户页 HTML，把目标系统的 SSO 入口【现抓】出来 ----
    if SSO_PORTAL_URL:                                               # 配了门户页就走推荐路线
        portal_html = ""                                             # 门户页 HTML
        portal_url = SSO_PORTAL_URL                                  # 门户页地址
        # ★ 省请求：步骤4 的落地页往往就是门户页，那份 HTML 直接拿来用，不必再 GET 一次
        if landing and landing.get("html"):                          # 步骤4 有落地响应可复用
            land = urlparse(landing.get("final_url") or "")          # 步骤4 最终地址
            want = urlparse(SSO_PORTAL_URL)                          # 门户页地址
            if land.netloc and land.netloc == want.netloc and \
               land.path.rstrip("/") == want.path.rstrip("/"):       # 同一个页面（忽略末尾斜杠）
                portal_html = landing["html"]                        # ★ 直接复用步骤4 的 HTML
                portal_url = landing.get("final_url") or SSO_PORTAL_URL  # 真实地址同样复用
                log("步骤4.5", "第 0 步：复用步骤4 已经抓到的门户页 HTML（省 1 个请求）：{}".format(portal_url))
        if not portal_html:                                          # 不能复用才真去抓
            log("步骤4.5", "第 0 步：GET 一站式登录平台门户页，现抓目标系统的 SSO 入口")
            log("步骤4.5", "        {}".format(SSO_PORTAL_URL))
            try:                                                     # 网络类异常统一处理
                portal = session.get(SSO_PORTAL_URL, timeout=REQUEST_TIMEOUT)  # 访问门户页
            except requests.exceptions.RequestException as error:    # 失败
                log("步骤4.5", "门户页访问失败：{}".format(error))       # 打印原因
                return None                                          # 不中断主流程，交给复核判定
            portal_html = decode_response(portal)                    # ★ 统一编码，避免中文乱码
            portal_url = portal.url                                  # 跟随后的真实地址
            log("步骤4.5", "  HTTP {}，{} 字符".format(portal.status_code, len(portal_html)))
        title = re.search(r"<title[^>]*>(.*?)</title>", portal_html, re.IGNORECASE | re.DOTALL)  # 页面标题
        log("步骤4.5", "  标题：{}".format(title.group(1).strip() if title else "（无）"))  # 确认拿到的是哪个页面
        links = find_sso_entry_links(portal_html, portal_url)        # ★ 抠出所有子系统入口
        log("步骤4.5", "  门户页里共发现 {} 个子系统 SSO 入口：".format(len(links)))
        for url, text in links:                                      # 全部列出来，便于人工核对
            log("步骤4.5", "    - {}{}".format(text or "（无文字）", "  ->  {}".format(url)))
        picked = None                                                # 按关键字挑中的那个
        if SSO_LINK_KEYWORD:                                         # 先按链接文字匹配
            for url, text in links:                                  # 逐条比对
                if SSO_LINK_KEYWORD in text:                         # 命中文字
                    picked = (url, text)                             # 记下
                    break                                            # 结束
            if picked is None:                                       # 文字没命中就退一步比地址
                for url, text in links:                              # 逐条比对
                    if SSO_LINK_KEYWORD.lower() in url.lower():      # 命中地址
                        picked = (url, text)                         # 记下
                        break                                        # 结束
        if picked is not None:                                       # 找到了目标系统入口
            log("步骤4.5", "  ★ 锁定目标入口：{} -> {}".format(picked[1], picked[0]))
            entry_urls = [picked[0]]                                 # ★ 只用现抓到的这个地址
        else:                                                        # 没找到
            log("步骤4.5", "[注意] 门户页里没找到包含「{}」的入口，退回 BUSINESS_ENTRY_URLS".format(SSO_LINK_KEYWORD))
            log("步骤4.5", "        提示：把 SSO_LINK_KEYWORD 改成上面某一条的文字即可")

    if len(entry_urls) == 0:                                         # 没门户页也没兜底地址
        log("步骤4.5", "门户页里没找到目标入口，且 BUSINESS_ENTRY_URLS 为空 → 跳过这一步")
        log("步骤4.5", "        本次只登录 {}".format(urlparse(CAS_SERVICE_URL).netloc))
        return None                                                  # 跳过

    response = None                                                  # 最后一跳的响应
    referer = portal_url if SSO_PORTAL_URL else ""                   # ★ 入口的 Referer = 门户页地址（浏览器就是这么点的）
    total = len(entry_urls)                                          # 一共几跳

    for index, entry in enumerate(entry_urls, 1):                    # 按浏览器真实顺序逐跳访问
        log("步骤4.5", "第 {}/{} 跳：GET {}{}".format(                     # 说明这一跳干什么
            index, total, entry,
            "（Referer = {}）".format(referer) if referer else "（无 Referer）"
        ))
        headers = {"Referer": referer} if referer else {}            # ★ 带上上一跳地址作为 Referer

        # ---- 手动跟 302 链：每一跳都补上 Referer，和浏览器的行为一致 ----
        # ★ 为什么不用 allow_redirects=True：requests 自动跟跳转时【不会】补 Referer，
        #   而真实浏览器每一跳都会带上。xgb 的 SSO 恰恰靠 Referer 判断来源，所以必须手动跟。
        current_url = entry                                          # 当前要访问的地址
        current_referer = referer                                    # 当前这一跳该带的 Referer（上一跳地址）
        response = None                                              # 最后一跳的响应
        visited = {entry}                                            # 走过的地址，防死循环
        for _hop in range(10):                                       # 最多跟 10 层 302
            hop_headers = dict(headers)                              # 复制基础请求头
            if current_referer:                                      # ★ 有上一跳地址就带上 Referer
                hop_headers["Referer"] = current_referer             # 浏览器就是这么传的
            try:                                                     # 网络类异常统一处理
                response = session.get(current_url, timeout=REQUEST_TIMEOUT, allow_redirects=False, headers=hop_headers)  # ★ 不自动跟，自己看 Location
            except requests.exceptions.RequestException as error:    # 请求失败
                log("步骤4.5", "  失败：{}".format(error))             # 打印原因
                return None                                          # 不中断主流程，交给后续复核判定
            try:                                                     # 取这一跳种下的 Cookie（可能有多个）
                set_cookies = response.raw.headers.getlist("Set-Cookie")  # urllib3 的取法
            except Exception:                                        # 取不到就算了
                set_cookies = []                                     # 置空
            location = urljoin(current_url, response.headers["Location"]) if response.headers.get("Location") else ""  # 下一跳地址
            log("步骤4.5", "  HTTP {}{}{}".format(                    # 把这一跳的关键信息打出来
                response.status_code,
                "，Set-Cookie: " + " | ".join(c.split(";")[0] for c in set_cookies) if set_cookies else "",
                " -> {}".format(location) if location else ""
            ))
            if response.is_redirect is False or not location:        # 不是跳转 = 落地了
                break                                                # 结束
            if location in visited:                                  # 已经走过 = 成环
                log("步骤4.5", "  检测到跳转成环，停止")               # 提示
                break                                                # 结束
            visited.add(location)                                    # 记下来
            current_referer = current_url                            # ★ 下一跳的 Referer = 这一跳的地址
            current_url = location                                   # 推进到下一跳

        # ---- 若落地页是 200 但内含前端跳转（JS/meta），手动补跟 ----
        for _hop in range(5):                                        # 最多补跟 5 层
            if response is None or response.status_code != 200:      # 不是 200 就没有 HTML 可解析
                break                                                # 结束补跳循环
            target = extract_html_redirect(decode_response(response), response.url)  # ★ 找页面里的前端跳转（统一编码）
            if not target or target in visited:                       # 没有目标，或目标已经走过
                break                                                # 结束补跳循环
            log("步骤4.5", "  页面内含【前端跳转】（JS/meta），手动补跟一跳 -> {}".format(target))  # 说明在干嘛
            visited.add(target)                                      # 记下来
            try:                                                     # 网络类异常统一处理
                response = session.get(target, timeout=REQUEST_TIMEOUT, allow_redirects=True, headers={"Referer": response.url})  # 跟着跳
            except requests.exceptions.RequestException as error:    # 请求失败
                log("步骤4.5", "  补跟失败：{}".format(error))         # 打印原因
                break                                                # 结束
            log("步骤4.5", "  HTTP {}，当前地址：{}".format(response.status_code, response.url))  # 状态码 + 当前 URL

        fuxue_now = [c for c in session.cookies if "fuxue" in (c.domain or "")]  # 此刻 fuxue 域 Cookie
        log("步骤4.5", "  此刻 fuxue 域 Cookie：{}".format(                    # 名字列出来
            ", ".join(sorted({c.name for c in fuxue_now})) or "（无）"
        ))
        log("步骤4.5", "  落地页标题：{}".format(                              # 标题最能说明这是哪一页
            (re.search(r"<title[^>]*>(.*?)</title>", decode_response(response), re.IGNORECASE | re.DOTALL) or [None, "（无）"])[1].strip()
        ))
        referer = response.url or entry                              # ★ 下一跳带上这一跳的地址

    log("步骤4.5", "HTTP {}，最终落地地址：{}".format(response.status_code, response.url))  # 状态码 + 最终 URL
    dump_cookies(session, "Cookie 容器（业务系统 fuxue 的登录 Cookie 应已写入）：")  # 这里应能看到 fuxue 域的 Cookie

    # ---- 落地后自检 ----
    landed_host = urlparse(response.url).netloc                      # 最终落在哪个域
    if landed_host.startswith("fuxue"):                              # 落在 fuxue
        # ★ 注意：fuxue 在任何匿名请求上都会种 PHPSESSID（连登录页都会），
        #   所以"有 fuxue 域 Cookie"不等于登录成功，唯一可信的判据是后面的【复核】。
        fuxue_cookies = [c for c in session.cookies if "fuxue" in (c.domain or "")]  # 筛 fuxue 域的 Cookie
        log("步骤4.5", "已取得 fuxue 域 Cookie：{}".format(                 # 列出来
            ", ".join(sorted({c.name for c in fuxue_cookies})) or "（无）"
        ))
        if "/login/" in urlparse(response.url).path:                  # 但还停在 fuxue 的登录页
            log("步骤4.5", "[注意] 落地页仍然是 fuxue 的【登录页】（{}）—— SSO 很可能没生效".format(
                urlparse(response.url).path
            ))
            log("步骤4.5", "  说明：fuxue 对匿名访问也会种 PHPSESSID，所以有 Cookie 不代表已登录")
    else:                                                            # 没落在 fuxue
        log("步骤4.5", "[注意] 最终落在 {}，不是 fuxue —— 这次中转没进 fuxue".format(landed_host or "（未知）"))

    # ---- 把落地页里所有的链接/表单/JS 地址列出来，看它到底有没有指向 fuxue 的出口 ----
    body = decode_response(response)                                 # ★ 统一编码后的落地页 HTML
    landed_links = sorted(set(re.findall(                            # 抓 href / src / action 三类属性
        r'''(?:href|src|action)\s*=\s*["']([^"'\s]+)''', body, re.IGNORECASE
    )))
    js_links = sorted(set(re.findall(                                # 再抓 JS 里的跳转目标
        r'''(?:location\s*\.\s*(?:href|replace\s*\(\s*)?|location\s*=\s*)["']([^"'\s]+)''',
        body, re.IGNORECASE
    )))
    all_links = sorted(set(landed_links) | set(js_links) | set(sorted(set(re.findall(  # 再兜底：页面里出现的任何完整 URL
        r'''https?://[^\s"'\\)<>]+''', body, re.IGNORECASE
    )))))
    log("步骤4.5", "  落地页里共有 {} 个链接/跳转目标，其中提到 fuxue / xszz / 辅学 的：".format(len(all_links)))
    interesting = [                                       # 只挑和这次问题相关的
        lk for lk in all_links
        if any(k in lk.lower() for k in ("fuxue", "xszz", "%e8%be%85%E5%AD%A6", "辅学"))
    ]
    for lk in interesting[:15]:                                      # 最多列 15 条
        log("步骤4.5", "    ★ {}".format(lk))                          # 可能就是真正的出口
    if len(interesting) == 0:                                        # 一条都没有
        log("步骤4.5", "    （没有）—— 说明这个落地页本身不是中转页")
    for lk in [l for l in all_links if l not in interesting][:15]:   # 其余链接也列几条，便于人工判断
        log("步骤4.5", "      {}".format(lk))                          # 普通链接

    # ---- 把落地页 HTML 存盘，人工打开就能看清是"中转页"还是"终点页" ----
    if DUMP_LANDING_HTML:                                            # 配置区开关
        try:                                                         # 写文件失败不影响主流程
            dump_path = os.path.join(                                # 存到脚本同目录
                os.path.dirname(os.path.abspath(__file__)), "step4_5_landing.html"
            )
            with open(dump_path, "w", encoding="utf-8") as handle:   # 落盘
                handle.write(body)                                   # 原样保存
            log("步骤4.5", "  落地页 HTML 已保存到：{}".format(dump_path))
        except OSError as error:                                     # 写不了就算了
            log("步骤4.5", "  落地页 HTML 保存失败：{}".format(error))
    return {"status": response.status_code, "final_url": response.url, "html": body}  # 返回落地结果


def classify_auth_redirect(url):
    """把跳转目标分成三类，返回 'issuer' / 'login' / 'other'。

    'issuer' = CAS【签发票据】的接口，例如 https://iam.nankai.edu.cn/api/cas/login?service=...
               —— 它的路径同样以 /login 结尾、主机同样是统一认证，长得像登录页，
                  但真实作用是"按当前登录态现场签发 ST 票据"：
                      会话已登录 → 302 到业务地址并带上 ?ticket=ST-...
                      会话未登录 → 302 回真正的登录页 /login
               所以【必须比登录页那条规则先判断】，否则会被误判成"被踢回登录页"。
    'login'  = 真正的统一认证登录页（/login、/portal/login 之类）
    'other'  = 其他，通常就是业务系统自己的地址
    """
    if not url:                                                      # 没有跳转目标
        return "other"                                               # 归为其他
    parts = urlparse(url)                                            # 拆解 URL
    path = parts.path.lower()                                        # 路径部分
    if "/api/cas" in path:                                           # ★ 先判签发接口，避免被下面的 /login 规则误伤
        return "issuer"                                              # 签发票据的接口
    # /api/ 下的其它接口也都不是"登录页"。
    # ★ 必须放在"以 /login 结尾"规则之前：否则 /api/v1/login（登录接口本身）
    #   会因为路径以 /login 结尾而被误判成登录页。
    if "/api/" in path:                                              # 其它 API 接口
        return "other"                                               # 不是登录页
    if path.rstrip("/").endswith("/login"):                          # 路径本身就是 /login（含 /portal/login）
        return "login"                                               # 真·登录页
    segments = [seg for seg in path.split("/") if seg]                # 路径分段
    # ★ 路径的【第一段】就是 login（如 /login、/login/index.html、/login/doindex.html）。
    #   这条专门覆盖"业务系统自己的登录页"——fuxue.nankai.edu.cn 被踢回来时就是
    #   /login/index.html，光看"以 /login 结尾"是认不出来的。
    #   只认第一段，所以业务页面 /home/user/login.html（第一段是 home）不会被误判。
    if segments and segments[0] == "login":                          # 第一段是 login
        return "login"                                               # 真·登录页
    auth_host = urlparse(CAS_AUTH_API_URL).netloc                    # 统一认证的主机名
    # ★ 兜底：跳到统一认证主机、且不是 /api/ 接口的页面，一律按登录页处理
    #   （覆盖 /login、/portal/login、/#/login 这类写法）
    #   注意不要去匹配"路径里含 login"，业务系统的 /home/user/login.html 就长这样，
    #   它属于业务系统自己的页面，不是统一认证的登录页。
    if auth_host and parts.netloc == auth_host and "/api/" not in path:
        return "login"                                               # 真·登录页
    return "other"                                                   # 其他


def auth_hosts():
    """收集"认证域"的主机名，用来判断"是不是被弹回统一认证了"。"""
    hosts = set()                                                    # 认证域集合
    for url in (CAS_LOGIN_PAGE_URL, CAS_AUTH_API_URL, CAS_TICKET_ENDPOINT, CAS_TICKET_URL):
        netloc = urlparse(url or "").netloc.lower()                  # 取出主机名
        if netloc:                                                   # 非空才算
            hosts.add(netloc)                                        # 收进集合
    return hosts                                                     # 返回


def verify_single_request(session, target):
    """★ 单请求模式的登录态复核：一路跟完跳转，只看【最终落在哪个域】。

    为什么一个请求就够：
      "被踢回登录页"这个信号的本质，是跳转链的【终点】落在认证域名上。
      allow_redirects=True 之后 requests 会把每一跳都留在 response.history 里，
      所以终点看 response.url、过程看 history 就够了，不需要我们自己逐跳读 Location。
      代价：看不到"统一认证有效、但业务系统不认这张票"这种中间态
            —— 需要那种精度时把 VERIFY_MODE 改成 "verbose"。

    返回：True（已登录）/ False（未登录）/ None（请求失败，无法判断）
    """
    try:                                                             # 网络异常统一处理
        response = session.get(target, timeout=REQUEST_TIMEOUT, allow_redirects=True)  # 一路跟到底
    except requests.exceptions.RequestException as error:            # 请求失败
        log("复核", "请求失败，无法复核：{}".format(error))            # 打印原因
        return None                                                  # 无法判断

    history = getattr(response, "history", None) or []               # 跳转链
    log("复核", "HTTP {}（共 {} 跳），最终落在：{}".format(response.status_code, len(history), response.url))
    for hop in history:                                              # 逐跳回放，保留可观测性
        log("复核", "  {} -> {}".format(hop.status_code, hop.headers.get("Location", "")))  # 状态码 + 目标

    final = urlparse(response.url)                                   # 终点
    final_host = final.netloc.lower()                                # 终点域名
    target_host = urlparse(target).netloc.lower()                    # 复核目标域名
    if final_host in auth_hosts() and "/login" in final.path:        # 终点是认证系统的登录页
        log("复核", "[失败] 最终被送回了统一认证登录页（{}）→ 登录态没有建立".format(final_host))
        return False                                                 # 明确未登录
    if final_host and final_host != target_host:                     # 落在别的域（业务侧又转了一手）
        log("复核", "[注意] 最终落在另一个域：{}".format(final_host))    # 说明跳转链更长
    text = visible_text(response)                                    # ★ 只看可见文字，避免 JS 字符串假阳性
    if any(word in text for word in LOGIN_FAIL_KEYWORDS):            # 落点页面写着登录失败
        log("复核", "[失败] 落地页里出现了登录失败提示")
        return False                                                 # 失败
    log("复核", "[成功] 落地页没有再要求登录（{}）→ 登录态已建立".format(response.url))
    return True                                                      # 已登录


def verify_login_state(session):
    """登录态复核（步骤4 之后的加分项）：不带 ticket 再访问一次业务入口，看会不会被踢回统一认证。

    为什么要这一步：
        步骤4 的落地页地址是 https://xgb.nankai.edu.cn/home/user/login.html，
        光看"HTTP 200 + 地址里带 login"无法判断到底登没登上。
        所以再发一个请求做"反问"，按跳转目标分三种情况处理：
            被 302 踢回真正的登录页（/login）        → 没登录
            被 302 送到 CAS 签发接口（/api/cas/login）→ ★ 再跟一跳，看它给不给票据
            被 302 送到业务系统自己的页面            → 已登录
            直接 200 返回业务页面                    → 已登录

    ★ 特别注意第②种：/api/cas/login 虽然路径以 /login 结尾，但它【不是登录页】，
      而是"给已登录会话签发票据"的接口。早期版本用"路径含 login"一刀切，
      把它误判成"被踢回登录页"从而误报失败，这里已修正。

    返回：True（已登录）/ False（未登录）/ None（没做复核或请求失败，无法判断）
    """
    if VERIFY_LOGIN_AFTER_STEP4 is False:                            # 用户在配置区关掉了复核
        return None                                                  # 什么都不做
    target = VERIFY_LOGIN_URL or CAS_SERVICE_URL                     # 复核地址：没单独填就用业务入口
    log("复核", "不带 ticket 再访问一次业务入口，确认登录态：{}".format(target))  # 说明这一步在干嘛

    if VERIFY_MODE != "verbose":                                     # ★ 默认单请求模式
        return verify_single_request(session, target)                # 不会再签发多余的票据


    try:                                                             # 网络异常统一处理
        # ★ 关键：allow_redirects=False。我们要亲眼看到服务器往哪里 302，
        #    如果自动跟随了跳转，就看不到"被踢回登录页"这个信号了。
        response = session.get(target, timeout=REQUEST_TIMEOUT, allow_redirects=False)  # 发复核请求
    except requests.exceptions.RequestException as error:            # 请求失败
        log("复核", "请求失败，无法复核：{}".format(error))            # 打印原因
        return None                                                  # 无法判断

    location = response.headers.get("Location", "")                  # 服务器给的跳转目标（如果有）
    log("复核", "HTTP {}{}".format(response.status_code, "，Location = {}".format(location) if location else ""))
    kind = classify_auth_redirect(location)                          # ★ 分类：issuer / login / other

    # ---- 情况①：跳到了 CAS 签发票据的接口 → 再跟一跳，看它到底给不给票据 ----
    if kind == "issuer":                                             # 这不是登录页，而是发票据的接口
        log("复核", "跳转目标是 CAS【签发票据】接口（不是登录页），再跟一跳看它返回什么")
        try:                                                         # 网络异常统一处理
            issued = session.get(location, timeout=REQUEST_TIMEOUT, allow_redirects=False)  # 跟着问一次
        except requests.exceptions.RequestException as error:        # 请求失败
            log("复核", "请求失败，无法复核：{}".format(error))        # 打印原因
            return None                                              # 无法判断
        issued_location = issued.headers.get("Location", "")         # 这一跳的目标
        log("复核", "HTTP {}{}".format(issued.status_code, "，Location = {}".format(issued_location) if issued_location else ""))
        if "ticket=" not in issued_location:                         # 没给票据
            if classify_auth_redirect(issued_location) == "login":   # 被弹回真正的登录页
                log("复核", "[失败] 签发接口把请求弹回了统一认证登录页 → 登录态没有建立")
                return False                                         # 未登录
            log("复核", "[注意] 签发接口既没给票据也没回登录页，无法据此判断登录态")  # 少见情况
            return None                                              # 无法判断
        log("复核", "[成功] 签发接口给出了新票据 → 统一认证登录态有效")  # 会话仍然有效

        # ---- 端到端再走一遍：拿这张【新票据】重新访问业务系统，看业务侧到底认不认 ----
        # 为什么要多这一跳：新票据只能证明"统一认证认识我们"，
        # 证明不了"业务系统会接受这张票据"。业务系统票据校验失败时同样会把我们打回 CAS，
        # 所以必须真的用票据敲一次业务系统，才算端到端验证。
        log("复核", "用这张新票据再访问一次业务系统，确认业务侧是否认账")
        try:                                                         # 网络异常统一处理
            landed = session.get(issued_location, timeout=REQUEST_TIMEOUT, allow_redirects=False)  # 带新票据敲业务系统
        except requests.exceptions.RequestException as error:        # 请求失败
            log("复核", "请求失败，无法复核：{}".format(error))        # 打印原因
            return None                                              # 无法判断
        landed_location = landed.headers.get("Location", "")         # 业务系统往哪跳
        log("复核", "HTTP {}{}".format(landed.status_code, "，Location = {}".format(landed_location) if landed_location else ""))
        landed_kind = classify_auth_redirect(landed_location)        # 再分类一次
        if landed_kind == "issuer":                                  # 又被打回签发接口 = 死循环
            log("复核", "[失败] 业务系统又把我们送回 CAS 签发接口 → 业务侧没有认这张票据")
            return False                                             # 票据不被业务系统接受
        if landed_kind == "login":                                   # 被打回登录页
            log("复核", "[失败] 业务系统把新票据退回了登录页 → 业务侧校验没通过")
            return False                                             # 未登录
        log("复核", "[成功] 业务系统接受了新票据（HTTP {}）→ 端到端登录成功".format(landed.status_code))
        return True                                                  # 已登录

    # ---- 情况②：被踢回真正的登录页 ----
    if kind == "login":                                              # 确认是登录页
        log("复核", "[失败] 服务器把我们踢回了登录页（{}）→ 登录态没有建立".format(
            urljoin(target, location).split("?")[0]))                # ★ 打印完整地址，便于区分是 iam 还是 fuxue 自己的登录页
        return False                                                 # 明确未登录

    # ---- 情况③：跳到了业务系统自己的地址 ----
    if response.status_code in (301, 302, 303, 307, 308):            # 有跳转但不是回登录页
        log("复核", "[成功] 跳转到业务系统自己的页面（{}）→ 登录态已建立".format(location))
        return True                                                  # 已登录
    if response.status_code == 200:                                  # 直接返回页面
        if any(word in response.text for word in LOGIN_FAIL_KEYWORDS):  # 但页面里写着登录失败
            log("复核", "[失败] 业务页面里出现了登录失败提示")
            return False                                             # 失败
        log("复核", "[成功] 业务入口没有再要求登录，直接返回了业务页面 → 登录态已建立")
        return True                                                  # 已登录
    log("复核", "[注意] HTTP {}，无法判断登录态".format(response.status_code))  # 其他状态码
    return None                                                      # 无法判断


# ============================================================================
# 【步骤5：爬虫区】爬取 fuxue 的「可报名活动」表格，并按活动时间筛选
# ============================================================================

# 表头文字 → 字段名的别名表。站点换名字时，往对应元组里加词即可。
ACTIVITY_COLUMN_ALIASES = {
    "活动名称": ("活动名称", "活动标题", "讲座名称", "名称", "标题"),
    "活动类型": ("活动类型", "活动类别", "类型", "类别"),
    "活动学时": ("活动学时", "实践学时", "学时", "学分"),
    "活动时间": ("活动时间", "举办时间", "活动日期", "时间", "日期"),
    "活动地点": ("活动地点", "举办地点", "活动场地", "地点", "场地"),
}


def html_root(document):
    """把「HTML 字符串 / bytes」或「已经解析好的 lxml 元素」统一成 lxml 的根元素。

    为什么要有这一层：解析一页 HTML 要花几毫秒，`parse_activity_table()` 和 `parse_max_page()`
    用的是同一份页面，传 lxml 元素进来就能只解析一次；也方便单元测试直接塞字符串。
    解析失败返回 None，调用方按"页面上没东西"处理，不抛异常打断整条流程。
    """
    if document is None:                                             # 没给内容
        return None                                                  # 直说没有
    if isinstance(document, (str, bytes)):                           # 还是原始 HTML
        try:
            return lxml_html.fromstring(document)                    # ★ lxml 解析（自动修闭合标签、还原实体）
        except Exception:                                            # 空串 / 残缺得太厉害
            return None                                              # 当作解析不出
    return document                                                  # 已经是 lxml 元素，直接复用


def strip_html_tags(fragment):
    """把一段 HTML（字符串，或已经解析好的 lxml 元素）变成纯文本：去标签、还原实体、合并空白。

    ★ 为什么不再用 `<[^>]*>` 这种正则：属性值里出现 `>`（如 title="a>b"）会把标签截错，
      而且 &nbsp; / &#x27; 这类实体还得自己维护替换表；lxml 一次性都做对。
    """
    if isinstance(fragment, (str, bytes)):                           # 传进来的是 HTML 片段
        try:
            fragment = lxml_html.fragment_fromstring(fragment, create_parent="div")  # ★ 用 div 包一层，多根/纯文本片段都不丢
        except Exception:                                            # 空串 / 解析不出来
            return re.sub(r"\s+", " ", str(fragment)).strip()         # 退回"只合并空白"
    if fragment is None:                                             # 什么都没有
        return ""                                                    # 返回空串
    return re.sub(r"\s+", " ", fragment.text_content() or "").strip()  # text_content() 会把后代文本全拼起来


def parse_activity_table(document):
    """解析活动列表页，返回 (表头列表, 数据行列表)；每个数据行是"单元格文本"的列表。

    依据步骤4.5 落地页的真实结构：
        <table class="typetable1">
          <tr class="tr1"><td>活动名称</td><td>活动类型</td>...</tr>   ← 表头行
          <tr><td>xxx</td><td>yyy</td>...</tr>                        ← 数据行
        </table>

    ★ 用 lxml 的 XPath 定位，而不是正则：
        - 能正确处理 <tbody> 包裹、属性值里带 ">"、大小写混写、标签没闭合等真实网页的脏数据；
        - 只取【这张表的直接行】，不会把嵌套子表格的行混进来（正则的 `<tr.*?</tr>` 会混）。
    """
    root = html_root(document)                                       # 统一成 lxml 根（字符串/元素都接受）
    if root is None:                                                 # 解析不出
        return [], []                                                # 返回空
    tables = root.xpath(                                             # 定位目标表格：class 里含 typetable1 的那个
        '//table[contains(concat(" ", normalize-space(@class), " "), " typetable1 ")]'
    )
    if not tables:                                                   # 页面上没有目标表格
        return [], []                                                # 返回空
    table = tables[0]                                                # 有多张就取第一张

    rows_elements = table.xpath('./tr | ./tbody/tr')                 # ★ 只取直接子行（兼容 <tbody> 包裹）
    if not rows_elements:                                            # 有的站点把 tr 包在别的容器里
        rows_elements = table.xpath('.//tr')                         # 退一步：取所有后代行

    headers = []                                                     # 表头
    for tr in rows_elements:                                         # 找表头行（特征：class 含 tr1）
        if "tr1" not in tr.get("class", "").split():                 # 不是表头行
            continue                                                 # 看下一行
        headers = [strip_html_tags(td) for td in tr.xpath('./td | ./th')]  # 逐个单元格取文本
        break                                                        # 只认第一行表头

    rows = []                                                        # 数据行
    for tr in rows_elements:                                         # 逐行
        cells = [strip_html_tags(td) for td in tr.xpath('./td | ./th')]  # 取这一行的所有单元格
        if not cells:                                                # 空行
            continue                                                 # 跳过
        if "tr1" in tr.get("class", "").split():                     # 表头行本身
            continue                                                 # 跳过
        if headers and cells == headers:                             # 长得跟表头一样的行
            continue                                                 # 跳过
        if all(cell == "" for cell in cells):                        # 整行全空
            continue                                                 # 跳过
        rows.append(cells)                                           # 收下这一行
    return headers, rows                                             # 返回


def map_activity_columns(headers, width):
    """把表头文字映射成 {字段名: 列下标}；表头认不出来时按默认顺序兜底。

    先按别名整体匹配（"活动名称" 这种长别名优先于 "名称"），
    匹配不全的字段再按 ACTIVITY_FIELDS 的顺序补位，保证任何页面结构下都能出结果。
    """
    index = {}                                                       # {字段名: 列下标}
    used = set()                                                     # 已占用的列，避免两个字面段抢同一列
    for field in ACTIVITY_FIELDS:                                    # 逐字段找它的列
        for head_index, head in enumerate(headers):                  # 逐列表头
            if head_index in used:                                   # 这一列已经被别的字段占了
                continue                                             # 看下一列
            for alias in ACTIVITY_COLUMN_ALIASES.get(field, ()):     # 逐个别名比对
                if alias and alias in head:                          # 表头里含有这个别名
                    index[field] = head_index                        # 记下列号
                    used.add(head_index)                             # 标记占用
                    break                                            # 这个字段搞定了
            if field in index:                                       # 已经找到
                break                                                # 跳出
    for fallback, field in enumerate(ACTIVITY_FIELDS):               # 没认出来的字段按顺序兜底
        if field not in index and fallback < width:                  # 位置还在表宽之内
            index[field] = fallback                                  # 直接按下标取
    return index                                                     # 返回映射


def normalize_fullwidth(text):
    """把全角数字/符号换成半角，避免"２０２６年３月１５日"这种写法抠不出日期。"""
    table = {ord(c): ord(h) for c, h in zip("０１２３４５６７８９", "0123456789")}  # 全角数字
    table.update({ord("－"): ord("-"), ord("／"): ord("/"), ord("．"): ord(".")})   # 全角符号
    return (text or "").translate(table)                              # 逐个字符替换


# 日期格式：先试"带年份"的，再试"只有月日"的（后者按当年补齐）
DATE_PATTERNS = (
    re.compile(r"(\d{4})\s*[-/年\.]\s*(\d{1,2})\s*[-/月\.]\s*(\d{1,2})\s*日?"),
    re.compile(r"(?<![\d:])(\d{1,2})\s*[-/月\.]\s*(\d{1,2})\s*日?(?![\d:])"),
)


def extract_activity_date(text):
    """从"活动时间"文本里抠出【开始日期】，返回 datetime.date；抠不到返回 None。

    支持 2026-03-15 / 2026/3/15 / 2026.03.15 / 2026年3月15日 / 3月15日（按当年算）。
    时间区间（如"2026-03-15 至 2026-03-20"）只取【第一个】日期，即开始时间。
    """
    text = normalize_fullwidth(text)                                 # 先做全角转半角
    for pattern in DATE_PATTERNS:                                    # 逐个格式尝试
        match = pattern.search(text)                                 # 找第一个匹配
        if match is None:                                            # 这种格式没匹配上
            continue                                                 # 换下一种
        groups = match.groups()                                      # 取出数字
        if len(groups) == 3:                                         # 带年份
            year, month, day = (int(g) for g in groups)              # 直接用
        else:                                                        # 只有月日
            year = datetime.date.today().year                        # 补当年
            month, day = int(groups[0]), int(groups[1])              # 月、日
        try:
            return datetime.date(year, month, day)                   # 造出日期对象
        except ValueError:                                           # 例如 2 月 30 日
            continue                                                 # 换下一种格式
    return None                                                      # 都没抠到


def is_activity_future(time_text, today):
    """活动开始日期是否【严格晚于】今天。返回 True / False / None（抠不出日期）。"""
    activity_date = extract_activity_date(time_text)                 # 抠开始日期
    if activity_date is None:                                        # 抠不出来
        return None                                                  # 无法判断
    return activity_date > today                                     # 严格晚于今天


def activity_page_url(list_url, page):
    """把"第 1 页地址"换成"第 page 页地址"。

        https://fuxue.nankai.edu.cn/student/kbmhd/type/1.html
        → https://fuxue.nankai.edu.cn/student/kbmhd/type/1/p/2.html
    """
    root = re.sub(r"/p/\d+\.html?$", "", list_url.rstrip("/"), flags=re.IGNORECASE)  # 先去掉可能已存在的 /p/N.html
    if root.lower().endswith(".html"):                               # 去掉 .html，腾出插 /p/N 的位置
        root = root[:-5]                                             # 截掉
    return "{}/p/{}.html".format(root, page)                         # 拼出这一页的地址


def parse_max_page(document, list_url=""):
    """从分页器里找出最大页码；找不到就返回 1（只有一页）。

    ★ 用 lxml 遍历 <a href>，不再写正则去扫整页源码：
      正则版会把页面里任何一处 `href="/p/3.html"` 都算进去（比如侧边栏、面包屑），
      lxml 版同样只看链接，但能顺带做规范化，且不会误伤 JS 字符串里的假 href。
    分页链接的形态认 `/p/<数字>.html(?)`；其他写法（如 `?page=3`）靠 CRAWL_MAX_PAGES 兜底。
    """
    root = html_root(document)                                       # 统一成 lxml 根（字符串/元素都接受）
    if root is None:                                                 # 解析不出
        return 1                                                     # 当作只有一页
    pages = [1]                                                      # 至少有一页
    for href in root.xpath('//a/@href'):                             # 页面上所有链接的 href
        match = re.search(r"/p/(\d+)\.html?", href or "", re.IGNORECASE)  # 形如 /p/3.html
        if match is not None:                                        # 是分页链接
            pages.append(int(match.group(1)))                        # 收下页码
    return max(pages)                                                # 最大页码 = 总页数


def save_crawl_html(name, page, html):
    """调试用：把某个列表页的原始 HTML 存到 CRAWL_DUMP_DIR 下。"""
    directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), CRAWL_DUMP_DIR)  # 存盘目录
    os.makedirs(directory, exist_ok=True)                            # 不存在就建
    path = os.path.join(directory, "{}-p{}.html".format(name, page))  # 文件名带上系统名和页码
    with open(path, "w", encoding="utf-8") as file_object:           # 写文件
        file_object.write(html)                                      # 写原始 HTML
    log("爬虫", "已存原始 HTML：{}".format(path))                     # 告知路径


def decode_response(response):
    """按页面自己声明的编码解码，避免中文表格变乱码。"""
    head = response.content[:2048].lower()                           # 只看页面头部
    if b"utf-8" in head or b"utf8" in head:                          # 声明了 UTF-8
        response.encoding = "utf-8"                                  # 强制用 UTF-8
    else:                                                            # 其他编码
        response.encoding = response.apparent_encoding or response.encoding  # 让 requests 猜
    return response.text                                             # 返回解码后的文本


def crawl_activity_list(session, target, today):
    """爬一个入口（含翻页），返回保留下来的活动字典列表。

    翻页策略：第 1 页解析分页器得到总页数，然后 1 → N 顺次抓；
    同时设 CRAWL_MAX_PAGES 上限，任何一页拿不到数据就停。
    """
    name = target.get("name", "未命名")                               # 入口名，只用于日志
    list_url = target.get("url", "")                                 # 第 1 页地址
    kept = []                                                        # 保留的活动
    seen = set()                                                     # 去重键，防止跨页重复
    page = 1                                                         # 从第 1 页开始
    max_page = 1                                                     # 总页数，抓完第 1 页才知道

    while page <= max_page and page <= CRAWL_MAX_PAGES:              # 逐页抓
        url = activity_page_url(list_url, page)                      # 本页地址
        log("爬虫", "[{}] 第 {} 页 GET {}".format(name, page, url))    # 打日志
        try:                                                         # 网络异常统一处理
            response = session.get(url, timeout=REQUEST_TIMEOUT)     # ★ 复用已登录的 session
        except requests.exceptions.RequestException as error:        # 请求失败
            log("爬虫", "[{}] 第 {} 页 请求失败：{}".format(name, page, error))
            break                                                    # 停止翻页
        if "/login/index.html" in response.url or "统一身份认证登录" in response.text:  # 被弹回登录页
            log("爬虫", "[{}] [失败] 已被踢回登录页，登录态失效，停止爬取".format(name))
            break                                                    # 登录态没了，再抓也没意义
        if response.status_code != 200:                              # 非 200
            log("爬虫", "[{}] 第 {} 页 HTTP {}，停止翻页".format(name, page, response.status_code))
            break                                                    # 停止翻页
        html = decode_response(response)                             # 解码
        if CRAWL_DUMP_HTML:                                          # 开了调试存盘
            save_crawl_html(name, page, html)                        # 存下来

        document = html_root(html)                                   # ★ 一页只让 lxml 解析一次，下面两个解析函数共用
        headers, rows = parse_activity_table(document)               # ★ 解析表格
        page_max = parse_max_page(document, list_url)                # 每页都看一眼分页器
        if page_max > max_page:                                      # ★ 第 1 页的分页器有时只显示“窗口”（如 1 2 3 …）
            max_page = page_max                                      #   后面的页会露出更大的页码，所以上限要能抬高
        if page == 1:                                                # 第 1 页
            log("爬虫", "[{}] 共 {} 页；表头：{}".format(
                name, max_page, " / ".join(headers) if headers else "（没识别到 class=tr1 表头行，按默认列序解析）"
            ))
        log("爬虫", "[{}] 第 {} 页 HTTP 200，解析出 {} 行".format(name, page, len(rows)))

        if not rows and page == 1:                                   # 第 1 页就一行都没有
            log("爬虫", "[{}] [注意] 第 1 页没解析出任何数据行；若确认页面有表格，"
                        "可把 CRAWL_DUMP_HTML 改成 True 存下 HTML 排查".format(name))

        index = map_activity_columns(headers, max((len(r) for r in rows), default=0))  # 列映射
        page_kept = 0                                                # 本页保留条数
        for cells in rows:                                           # 逐行
            item = {}                                                # 一条活动
            for field in ACTIVITY_FIELDS:                            # 逐字段取值
                column = index.get(field)                            # 对应列号
                item[field] = cells[column] if (column is not None and column < len(cells)) else ""  # 取值或空
            if not item["活动名称"]:                                   # 没有名字的行不算数据行
                continue                                             # 跳过
            key = (item["活动名称"], item["活动时间"])                 # 去重键
            if key in seen:                                          # 重复（翻页重叠等）
                continue                                             # 跳过
            seen.add(key)                                            # 记下

            if CRAWL_ONLY_FUTURE:                                    # 需要按时间筛选
                verdict = is_activity_future(item["活动时间"], today)  # 判断是否晚于今天
                if verdict is False:                                 # 不晚于今天
                    log("爬虫", "[{}] [筛选] 剔除（未晚于 {}）：{} | {}".format(name, today, item["活动名称"], item["活动时间"]))
                    continue                                         # 丢掉
                if verdict is None:                                  # 抠不出日期
                    log("爬虫", "[{}] [筛选] [注意] 抠不出日期，按保留处理：{} | {}".format(name, item["活动名称"], item["活动时间"]))
            kept.append(item)                                        # 收下
            page_kept += 1                                           # 计数
        log("爬虫", "[{}] 第 {} 页 保留 {} 条（累计 {} 条）".format(name, page, page_kept, len(kept)))

        if not rows:                                                 # 空页说明已经到头
            break                                                    # 提前结束
        page += 1                                                    # 下一页
        if page <= max_page:                                         # 还有下一页
            time.sleep(CRAWL_DELAY)                                  # 歇一下再抓

    return kept                                                      # 返回该入口的结果


def run_crawl_test_hook(items):
    """★ 测试功能：打开时把结果字典列表存成 CSV；关闭时【什么都不干】。"""
    if CRAWL_TEST_MODE is False:                                     # 关闭状态
        return                                                       # 什么都不干
    if not items:                                                    # 没有数据
        log("测试", "[测试功能] 结果为空，不写 CSV")                    # 告知
        return                                                       # 结束
    path = CRAWL_CSV_PATH                                            # 配置里的路径
    if not os.path.isabs(path):                                      # 相对路径
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)  # 放到脚本同目录
    with open(path, "w", newline="", encoding="utf-8-sig") as file_object:  # ★ utf-8-sig 让 Excel 不乱码
        writer = csv.DictWriter(file_object, fieldnames=ACTIVITY_FIELDS)  # 按固定字段顺序写
        writer.writeheader()                                         # 写表头
        for item in items:                                           # 逐条写
            writer.writerow({field: item.get(field, "") for field in ACTIVITY_FIELDS})
    log("测试", "[测试功能] 已把 {} 条结果写入 CSV：{}".format(len(items), path))


def print_activity_results(items):
    """把结果列表按行打印出来，便于直接看。"""
    safe_print("-" * 78)                                             # 分隔线
    log("结果", "共 {} 条活动（条件：{}）".format(
        len(items), "活动时间晚于今天" if CRAWL_ONLY_FUTURE else "不筛选"
    ))
    if not items:                                                    # 空结果
        safe_print("  （没有符合条件的活动）")                          # 友好提示
    for order, item in enumerate(items, 1):                          # 逐条打印
        safe_print("  {:>3}. {}".format(                             # 序号 + 各字段
            order, " | ".join(item.get(field, "") for field in ACTIVITY_FIELDS)
        ))
    safe_print("-" * 78)                                             # 分隔线


def step5_crawl_business_data(session):
    """步骤5：爬取 fuxue 的「可报名活动」表格，筛选出活动时间晚于今天的项目。

    返回：以字典为元素的列表，形如
        [{"活动名称": "", "活动类型": "", "活动学时": "", "活动时间": "", "活动地点": ""}, ...]

    ★ 复用步骤1~4.5 建立好的 session（登录 Cookie 都在 session.cookies 里），不重新登录。
    ★ 是否落盘由「测试功能」的开关决定：CRAWL_TEST_MODE=True 存 CSV，False 什么都不干。
    """
    if CRAWL_ENABLED is False:                                       # 总开关关着
        log("爬虫", "CRAWL_ENABLED=False，跳过步骤5")                   # 告知
        return []                                                    # 返回空列表
    today = datetime.date.today()                                    # 今天，用于筛选
    log("爬虫", "开始爬取，共 {} 个入口；筛选：{}".format(
        len(CRAWL_TARGETS),
        "只保留活动时间晚于 {}".format(today.isoformat()) if CRAWL_ONLY_FUTURE else "不筛选"
    ))
    results = []                                                     # 汇总结果
    for target in CRAWL_TARGETS:                                     # 依次爬每个入口
        items = crawl_activity_list(session, target, today)          # 爬一个入口
        log("爬虫", "[{}] 最终保留 {} 条".format(target.get("name", "未命名"), len(items)))
        results.extend(items)                                        # 并入总结果
    log("爬虫", "全部入口合计 {} 条".format(len(results)))              # 汇总日志
    run_crawl_test_hook(results)                                     # ★ 测试功能（开=写 CSV，关=什么都不干）
    return results                                                   # 返回字典列表


# ============================================================================
# 【入口区】把所有步骤串起来，对应流程图从上到下的箭头走向
# ============================================================================

def mask_username(username):
    """账号脱敏：日志里【不出现任何可识别的账号信息】，只给长度 + 不可逆指纹。

    说明：以前是"留头 2 位留尾 2 位"（如 20***01），但学号只有 6~12 位，
    露出 4 位等于泄漏大半，所以改成完全不露内容，只保留"能核对是否是同一个账号"的指纹。
    """
    text = str(username or "")                                       # 兜底成字符串
    if not text:                                                     # 空账号
        return "（未设置）"                                           # 直说
    return "***（已隐藏，共 {} 字符；指纹 {}）".format(
        len(text), hashlib.sha256(text.encode("utf-8")).hexdigest()[:8])  # 长度 + 指纹


def resolve_credentials():
    """★ 从系统环境变量读取账号密码；环境变量缺失时才回退到配置区的 USERNAME / PASSWORD。

    返回 (username, password, user_source, pass_source)：
        user_source / pass_source 是"给人看的来源说明"，用来在日志里告知到底从哪取的，
        但绝不包含密码明文。

    为什么优先环境变量：
        1. 密码不落盘，不会被误提交进 git / 不会被截图带出去；
        2. 换账号只要改环境变量，不用动代码；
        3. 也可用于 CI：把凭据放进 secret，运行时注入环境变量即可。
    """
    global USERNAME, PASSWORD                                        # 要把解析结果写回全局变量供步骤2 使用

    env_username = os.environ.get(CREDENTIAL_ENV_USERNAME, "").strip()   # 账号：去掉首尾空格（表单也是 trim 后提交）
    env_password = os.environ.get(CREDENTIAL_ENV_PASSWORD, "")       # 密码：★ 不做 strip，原样使用（首尾字符可能就是密码的一部分）

    if env_username:                                                 # 环境变量里有账号
        USERNAME = env_username                                      # 用它覆盖配置区
        username_source = "环境变量 {}".format(CREDENTIAL_ENV_USERNAME)  # 记录来源
    elif USERNAME:                                                   # 环境变量没有，但配置区填了
        username_source = "配置区 USERNAME（未设置环境变量）"          # 记录来源
    else:                                                            # 两边都没有
        username_source = "未提供"                                    # 记录来源

    if env_password:                                                 # 环境变量里有密码
        PASSWORD = env_password                                      # 用它覆盖配置区
        password_source = "环境变量 {}".format(CREDENTIAL_ENV_PASSWORD)  # 记录来源
    elif PASSWORD:                                                   # 环境变量没有，但配置区填了
        password_source = "配置区 PASSWORD（未设置环境变量）"          # 记录来源
    else:                                                            # 两边都没有
        password_source = "未提供"                                    # 记录来源

    register_secret(USERNAME)                                        # ★ 登记账号：之后任何输出里出现它都会被替换成 ***
    register_secret(PASSWORD)                                        # ★ 登记密码：双保险（正常路径本就从不打印密码）

    return USERNAME, PASSWORD, username_source, password_source      # 返回解析结果与来源


def check_config():
    """开跑前检查配置区，避免用户看到一堆难懂的报错。返回缺失项清单（空 = 配置齐了）。

    ★ 账号密码在这里检查的是 resolve_credentials() 解析后的结果，
      所以既可以用环境变量提供，也可以填在配置区。
    ★ service 现在只有 CAS_SERVICE_URL 一处真相来源，ticket 入口由它自动拼出来。
      如果手工填了 CAS_TICKET_URL，就反过来校验里面必须含同一个 service ——
      因为 CAS 要求"签发票据时用的 service"与"拿 ticket 去校验时的 service"【逐字符相同】，
      两边不一致时服务器只会表现为"没拿到 ticket"，日志里根本看不出是配置问题。
    """
    missing = []                                                     # 收集缺失项
    if not CAS_LOGIN_PAGE_URL:                                       # 步骤1 必填
        missing.append("CAS_LOGIN_PAGE_URL（步骤1 CAS 入口地址）")      # 记录
    if not CAS_AUTH_API_URL:                                         # 步骤2 必填
        missing.append("CAS_AUTH_API_URL（步骤2 账号密码接口）")        # 记录
    if not CAS_SERVICE_URL:                                          # 步骤2.5/3/4 必填
        missing.append("CAS_SERVICE_URL（业务系统地址，同时是 CAS 的 service 参数）")  # 记录
    if not CAS_TICKET_ENDPOINT and not CAS_TICKET_URL:               # 步骤2.5 的签发入口
        missing.append("CAS_TICKET_ENDPOINT（签发票据的接口地址）")      # 记录
    if CAS_TICKET_URL and CAS_SERVICE_URL and CAS_SERVICE_URL not in CAS_TICKET_URL:  # 两处 service 不一致
        missing.append("CAS_TICKET_URL 里的 service 与 CAS_SERVICE_URL 不一致"
                       "（★ 必须逐字符相同；建议把 CAS_TICKET_URL 留空，让程序按 CAS_SERVICE_URL 自动拼）")  # 记录
    if not USERNAME or not PASSWORD:                                 # 登录凭据必填（环境变量或配置区，二者取其一）
        missing.append("账号密码：环境变量 {} / {}（或配置区 USERNAME / PASSWORD）".format(
            CREDENTIAL_ENV_USERNAME, CREDENTIAL_ENV_PASSWORD))        # 记录，并告诉用户两种提供方式
    if AES_KEY_HEX and len(AES_KEY_HEX.strip()) != 32:               # 手工填的密钥必须是 32 个字符（= 32 字节 = AES-256）
        missing.append("AES_KEY_HEX（必须恰好 32 个十六进制字符，也就是 32 字节）")  # 记录
    if AES_IV_HEX and len(AES_IV_HEX.strip()) < 16:                  # 手工填的 IV 至少要 16 个字符
        missing.append("AES_IV_HEX（至少 16 个字符，AES-CBC 只用前 16 字节）")     # 记录
    return missing                                                   # 返回清单


def main():
    """主流程：步骤1 → 步骤2 → （步骤2.5 + 步骤3 + 步骤4 合一的 establish_login_state）→ 步骤4.5 → 复核 → 步骤5。

    步骤2.5/3/4 合并的原因：三者本来就是一条"签发 → 校验 → 落地"的连续跳转链，
    拆成三个函数时票据与跳转目标要在函数之间传来传去，反而更容易出现
    "签发时 service 与校验时 service 不一致"这类难查的问题。
    步骤5 = 复用登录好的 session 爬取 fuxue 的「可报名活动」表格，筛出活动时间晚于当日的项目。
    """
    # ★ 第一步：解析账号密码 —— 优先从【系统环境变量】读，环境变量没设置才用配置区的兜底值
    username, password, username_source, password_source = resolve_credentials()  # 解析凭据（不打印明文密码）

    if AES_SELF_TEST:                                                # 配置区要求自检就先自检
        safe_print("=" * 78)                                         # 分隔线
        if self_test_crypto() is False:                              # 加密结果和南开前端对不上
            safe_print("加密自检未通过：请检查配置区的 AES_KEY_SEED / AES_KEY_HEX / AES_IV_HEX")  # 提示排查方向
            return                                                   # 直接退出，避免发出错误密文把账号锁掉
        safe_print("=" * 78)                                         # 分隔线

    missing = check_config()                                         # 先检查配置
    if missing:                                                      # 有缺失
        safe_print("请先在文件顶部【配置区】填写以下内容：")             # 友好提示
        for item in missing:                                         # 逐条列出
            safe_print("  - {}".format(item))                        # 打印缺失项
        return                                                       # 直接退出，不发任何请求

    safe_print("=" * 78)                                             # 分隔线，让输出更好读
    safe_print("CAS 登录流程开始（步骤1 → 登录 → 票据签发与校验 → SSO 中转 → 业务系统 fuxue → 爬取活动）")  # 标题
    safe_print("密码发送方式：{}".format("AES-256-CBC 加密后发送" if ENCRYPT_PASSWORD else "★ 明文发送（对照用）"))  # 说明加密状态
    log("配置", "账号来源：{}（账号 = {}）".format(username_source, mask_username(username)))  # 告知账号从哪来（已打码）
    log("配置", "密码来源：{}（明文 {} 字符，内容不打印）".format(password_source, len(password)))  # 告知密码从哪来，绝不打印内容
    log("配置", "登录态复核模式：{}".format("单请求（默认）" if VERIFY_MODE != "verbose" else "三段式（verbose）"))
    safe_print("=" * 78)                                             # 分隔线

    session = build_session()                                        # ★ 建会话：这一步之后所有请求都复用它

    page = step1_open_login_page(session)                            # 步骤1：预访问 CAS 入口，种下 csrf-token / session
    if page is None:                                                 # 步骤1 失败
        return                                                       # 中止

    issued = step2_post_credentials(session)                         # 步骤2：POST 账号密码（密码已加密），拿 ticket / 待签发信号
    if issued is None:                                               # 步骤2 失败
        return                                                       # 中止

    landing = establish_login_state(session, issued)                 # 步骤2.5 + 步骤3 + 步骤4：签发 → 校验 → 落地
    if landing is None:                                              # 登录状态没建立
        return                                                       # 中止

    enter = step4_5_enter_business_system(session, landing)          # ★ 步骤4.5：SSO 中转进入 fuxue（复用步骤4 的门户页 HTML）
    if enter is not None:                                            # 成功走完中转
        landing = enter                                              # 汇总里显示 fuxue 的落地地址更有意义

    verified = verify_login_state(session)                           # ★ 复核：不带 ticket 再问一次业务系统，确认真的登录上了

    safe_print("-" * 78)                                             # 分隔线
    log("汇总", "登录流程已执行完毕（步骤1 ~ 步骤4.5），登录态全在 session.cookies 里，可直接交给步骤5 复用")  # 汇总说明
    log("汇总", "最终落地地址：{}".format(landing["final_url"]))        # 打印最终地址
    if verified is True:                                             # 复核确认已登录
        log("汇总", "[成功] 登录态复核通过：该 session 已可直接用于步骤5 抓取数据")  # 给出明确结论
    elif verified is False:                                          # 复核发现未登录
        log("汇总", "[失败] 登录态复核未通过：请检查账号密码，或用抓包对照步骤2 的请求体")  # 给出排查方向
    else:                                                            # 没复核或没法判断
        log("汇总", "[注意] 未能自动判定登录态，请人工核对最终落地页内容")  # 中性提示
    safe_print("-" * 78)                                             # 分隔线

    # ★ 步骤5：爬取活动表格。复核【明确失败】时不硬闯，否则只会抓到一页登录页
    if verified is False and CRAWL_ENABLED:                          # 登录态复核明确失败
        log("步骤5", "[已跳过] 登录态复核未通过，不再发起爬取请求；请先让登录跑通")  # 说明原因
        return                                                       # 结束主流程
    items = step5_crawl_business_data(session)                       # ★ 步骤5：只复用登录好的 session
    print_activity_results(items)                                    # 把结果列表打印出来
    log("汇总", "步骤5 完成：共返回 {} 条活动字典；测试功能 {}".format(
        len(items),
        "已开启（结果已存 CSV：{}）".format(CRAWL_CSV_PATH) if CRAWL_TEST_MODE else "已关闭（不落盘）"
    ))
    safe_print("-" * 78)                                             # 分隔线


if __name__ == "__main__":                                           # 直接运行本文件时才执行（被 import 时不会自动跑）
    main()                                                           # 启动主流程
