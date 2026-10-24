# -*- coding: utf-8 -*-
"""cas_core.py —— 南开志愿系统（zyfw.nankai.edu.cn）登录 + 活动抓取内核。

本文件专为 Dify「代码执行」节点设计，硬约束（改代码时不要破坏）：

1. 入口必须是 ``def main(**kwargs) -> dict``，Dify 通过 ``main(**inputs_obj)`` 取结果；
2. 返回 dict 的键必须和 Dify 节点里声明的「输出变量」**完全一致**（多一个、少一个都会
   报 ``Not all output parameters are validated.``）；
3. 模块顶层不能有 print / 自执行 / 文件读写 —— Dify 会把整段代码内联进自己的 runner
   并以 ``__name__ == "__main__"`` 执行，任何自执行块都会让 Dify 拿不到 ``<<RESULT>>``，
   报 ``Output result is missing.``；
4. 只依赖标准库 + requests；不能用 lxml（Dify 沙箱没有），也不能依赖 pycryptodome
   （密码加密用本文件内置的纯 Python AES-256-CBC）。

Dify 输出变量（节点里只声明这一个）：
    text  String     纯文本，形状固定：
                     "共 N 场活动：" + 每条 "序号. 活动名称" + 缩进的 "字段：值" 行；
                     抓不到时是 "本次没有抓取到符合条件的活动。" 或 "抓取失败：..."

说明：早期版本同时返回 activities(Array[Object]) + text，现在精简为只返回 text，
节点里也只需声明一个 String 输出变量（多声明/少声明都会触发上面的报错）。
"""

import hashlib
import json
import re
import time
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

try:  # Dify 沙箱一般预装 requests；缺失时退化到空结果而不是让节点直接报错
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    requests = None
    HTTPAdapter = None
    Retry = None


# ============================================================================
# 配置区（Dify 里可以不改代码，用输入变量覆盖；见 main 的 kwargs）
# ============================================================================
USERNAME = ""                 # 留空：优先用 kwargs > 环境变量 CAS_USERNAME
PASSWORD = ""                 # 留空：优先用 kwargs > 环境变量 CAS_PASSWORD

RAISE_ERRORS = False          # 排障时改 True；生产环境保持 False，失败返回空结果

REQUEST_TIMEOUT = 10          # 单个请求超时（秒）
DEADLINE_SECONDS = 20         # 整次执行的时间预算；Dify 代码节点通常 30s 上限
CRAWL_MAX_PAGES = 15           # 最多翻几个列表页（每页 10 条，每条都要再请求详情页）
CRAWL_DELAY = 0.0             # 详情页之间的间隔（秒），0 表示不等待
CRAWL_ONLY_FUTURE = True      # 只保留开始时间在未来的活动
MAX_RETRIES = 3               # 只对 GET 生效；登录 POST 绝不重试

BASE_URL = "https://zyfw.nankai.edu.cn"
IAM_BASE = "https://iam.nankai.edu.cn"
LOGIN_PAGE_URL = BASE_URL + "/Login/index.aspx"
LOGIN_API_URL = IAM_BASE + "/api/v1/login"
LIST_URL_TEMPLATE = BASE_URL + "/Index/huodong/p/{page}.aspx"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 输出字典的字段顺序（与抓取到的 8 个要素一致，另加一个详情页链接）
ACTIVITY_KEYS = ("活动名称", "组织机构", "联系人", "联系电话",
                 "活动时间", "活动地点", "志愿时长", "招募人数")


# ============================================================================
# 1. 密码加密：AES-256-CBC / PKCS7（纯 Python，等价于前端 crypto-js 的结果）
# ============================================================================
AES_SEED = "9007199254740991"     # 前端 Number.MAX_SAFE_INTEGER.toString()，算法的一部分


def _key_iv():
    key_hex = hashlib.md5(AES_SEED.encode("utf-8")).hexdigest()
    key = key_hex.encode("utf-8")
    iv = hashlib.sha1(key_hex.encode("utf-8")).hexdigest()[:16].encode("utf-8")
    return key, iv


def _xtime(a):
    a <<= 1
    return (a ^ 0x11B) & 0xFF if a & 0x100 else a


def _gmul(a, b):
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result & 0xFF


def _build_sbox():
    exp, log = [0] * 256, [0] * 256
    value = 1
    for i in range(255):
        exp[i], log[value] = value, i
        value = _gmul(value, 3)
    box = [0] * 256
    for x in range(256):
        inv = exp[(255 - log[x]) % 255] if x else 0
        s = inv
        for shift in (1, 2, 3, 4):
            s ^= ((inv << shift) | (inv >> (8 - shift))) & 0xFF
        box[x] = (s ^ 0x63) & 0xFF
    return box


_SBOX = _build_sbox()


def _expand_key(key):
    nk = len(key) // 4
    nr = nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = [_SBOX[temp[1]], _SBOX[temp[2]], _SBOX[temp[3]], _SBOX[temp[0]]]
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        elif nk > 6 and i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return [bytes(b for word in words[4 * r:4 * r + 4] for b in word) for r in range(nr + 1)]


def _encrypt_block(block, round_keys):
    state = list(block)
    for i in range(16):
        state[i] ^= round_keys[0][i]
    for rnd in range(1, len(round_keys) - 1):
        state = [_SBOX[b] for b in state]
        state = [state[4 * ((c + r) % 4) + r] for c in range(4) for r in range(4)]
        for c in range(4):
            a0, a1, a2, a3 = state[4 * c:4 * c + 4]
            state[4 * c:4 * c + 4] = [
                _xtime(a0) ^ (_xtime(a1) ^ a1) ^ a2 ^ a3,
                a0 ^ _xtime(a1) ^ (_xtime(a2) ^ a2) ^ a3,
                a0 ^ a1 ^ _xtime(a2) ^ (_xtime(a3) ^ a3),
                (_xtime(a0) ^ a0) ^ a1 ^ a2 ^ _xtime(a3),
            ]
        for i in range(16):
            state[i] ^= round_keys[rnd][i]
    state = [_SBOX[b] for b in state]
    state = [state[4 * ((c + r) % 4) + r] for c in range(4) for r in range(4)]
    return bytes(x ^ y for x, y in zip(state, round_keys[-1]))


def _pad(data):
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def _cbc_encrypt(data, key, iv):
    round_keys = _expand_key(key)
    padded = _pad(data)
    out, prev = bytearray(), iv
    for i in range(0, len(padded), 16):
        prev = _encrypt_block(bytes(a ^ b for a, b in zip(padded[i:i + 16], prev)), round_keys)
        out += prev
    return bytes(out)


def encrypt_password(password):
    """明文密码 -> 十六进制密文；空密码返回空字符串。"""
    if password is None:
        return ""
    data = password if isinstance(password, bytes) else str(password).encode("utf-8")
    if not data:
        return ""
    key, iv = _key_iv()
    return _cbc_encrypt(data, key, iv).hex()


# ============================================================================
# 2. 通用小工具
# ============================================================================
def _clean(text):
    """压缩空白，顺便把 &nbsp; 之类实体还原。"""
    return re.sub(r"\s+", " ", unescape(text or "")).strip()


def _strip_label(text):
    """去掉 "活动时间：" 这类前缀，只留值。

    注意：不能写成 ``^[^:：]*[:：]``，否则纯时间串 "2026-11-01 10:00" 会被截成
    "00"，进而导致日期解析失败、未来活动过滤失效。
    """
    return re.sub(r"^[^\d:：]+[:：]\s*", "", _clean(text))


def _parse_start(text):
    """从 "2026-11-01 10:00 至 2026-11-15 16:00" 中取出开始时间；失败返回 None。"""
    value = _strip_label(text)
    value = re.split(r"至", value)[0].strip()
    match = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", value)
    if not match:
        return None
    try:
        return datetime(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _as_int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value, default):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


# ============================================================================
# 3. HTML 解析（标准库实现，等价于原来 lxml 的 XPath）
# ============================================================================
class _DetailParser(HTMLParser):
    """解析活动详情页：标题 + div.xinxi 表格里的 "标签/值" 行。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.rows = []
        self._title_parts = []
        self._in_title = False
        self._xinxi_depth = 0
        self._table_depth = 0
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "div":
            if self._xinxi_depth:
                self._xinxi_depth += 1
            elif "xinxi" in classes:
                self._xinxi_depth = 1
        elif tag == "p" and "hd-title" in classes:
            self._in_title = True
        elif self._xinxi_depth:
            if tag == "table":
                self._table_depth += 1
            elif tag == "tr" and self._table_depth == 1:
                self._row = []
            elif tag in ("td", "th") and self._table_depth == 1:
                self._cell = []

    def handle_endtag(self, tag):
        if tag == "div" and self._xinxi_depth:
            self._xinxi_depth -= 1
            if not self._xinxi_depth:
                self._table_depth = 0
        elif tag == "p" and self._in_title:
            self._in_title = False
            self.title = _clean("".join(self._title_parts))
        elif self._xinxi_depth:
            if tag in ("td", "th") and self._table_depth == 1 and self._cell is not None:
                if self._row is not None:
                    self._row.append(_clean("".join(self._cell)))
                self._cell = None
            elif tag == "tr" and self._table_depth == 1 and self._row is not None:
                self.rows.append(self._row)
                self._row = None
            elif tag == "table":
                self._table_depth = max(0, self._table_depth - 1)

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)
        if self._cell is not None:
            self._cell.append(data)


class _ListParser(HTMLParser):
    """解析列表页：每个 div.huodong 里的详情链接 + 活动时间。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self._item = None
        self._div_depth = 0
        self._first_href = None
        self._tit_href = None
        self._time_buf = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "div":
            if self._item is None:
                if "huodong" in classes:
                    self._item = {}
                    self._div_depth = 1
                    self._first_href = self._tit_href = self._time_buf = None
            else:
                self._div_depth += 1
            return
        if self._item is None:
            return
        if tag == "a":
            href = (attrs.get("href") or "").strip()
            if href:
                if self._first_href is None:
                    self._first_href = href
                if "tit" in classes:
                    self._tit_href = href
        elif tag == "p" and "time" in classes:
            self._time_buf = []

    def handle_endtag(self, tag):
        if self._item is None:
            return
        if tag == "p" and self._time_buf is not None:
            self._item["time"] = _clean("".join(self._time_buf))
            self._time_buf = None
        elif tag == "div":
            self._div_depth -= 1
            if self._div_depth <= 0:
                href = self._tit_href or self._first_href
                if href:
                    self.items.append({"href": href, "time": self._item.get("time", "")})
                self._item = None

    def handle_data(self, data):
        if self._time_buf is not None:
            self._time_buf.append(data)


# 详情页表格里的标签并不统一（发起组织/组织机构、联系人电话/联系电话、
# 拟招募人数/招募人数），这里做别名映射，输出统一用 ACTIVITY_KEYS 里的名字。
_LABEL_ALIASES = {
    "组织机构": ("发起组织", "组织机构", "主办单位"),
    "联系人": ("联系人",),
    "联系电话": ("联系人电话", "联系电话"),
    "活动时间": ("活动时间",),
    "活动地点": ("活动地点",),
    "志愿时长": ("志愿时长",),
    "招募人数": ("拟招募人数", "招募人数"),
}


def _parse_detail(html_text):
    parser = _DetailParser()
    try:
        parser.feed(html_text or "")
        parser.close()
    except Exception:
        pass
    row_map = {}
    for row in parser.rows:
        if len(row) >= 2 and row[0]:
            row_map[row[0].rstrip("：:").strip()] = row[1]
    activity = {"活动名称": parser.title or ""}
    for key in ACTIVITY_KEYS[1:]:
        value = ""
        for alias in _LABEL_ALIASES.get(key, (key,)):
            if row_map.get(alias):
                value = row_map[alias]
                break
        activity[key] = value
    return activity


def _parse_list(html_text):
    parser = _ListParser()
    try:
        parser.feed(html_text or "")
        parser.close()
    except Exception:
        pass
    return parser.items


def _max_page(html_text):
    pages = re.findall(r"/Index/huodong/p/(\d+)\.aspx", html_text or "", flags=re.I)
    return max((int(page) for page in pages), default=1)


# ============================================================================
# 4. 网络层
# ============================================================================
def _build_session():
    if requests is None:
        raise RuntimeError("当前环境缺少 requests 库，无法发起网络请求")
    session = requests.Session()
    if HTTPAdapter is not None and Retry is not None:
        retry = Retry(total=MAX_RETRIES, backoff_factor=0.3,
                      status_forcelist=(502, 503, 504),
                      allowed_methods=frozenset(("GET",)))   # 登录 POST 绝不重试
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    return session


def _timeout_left(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("已用完时间预算")
    return left


def _get(session, url, deadline, allow_redirects=True):
    left = _timeout_left(deadline)
    response = session.get(url, headers=HEADERS, allow_redirects=allow_redirects,
                           timeout=(min(5.0, left), min(REQUEST_TIMEOUT, left)))
    response.encoding = "utf-8"          # 站点声明 charset=utf-8，显式指定防乱码
    if response.status_code >= 400:
        raise RuntimeError("HTTP %s: %s" % (response.status_code, url))
    return response


def _resolve_login_link(link):
    """把登录接口返回的 next.link 变成可访问的绝对地址。"""
    if not link:
        return None
    link = link.strip()
    if link.startswith("http://") or link.startswith("https://"):
        url = link
    else:
        url = urljoin(IAM_BASE, link)
    if "lang=" not in url:
        url += ("&" if "?" in url else "?") + "lang=zh-CN"
    return url


def _login(session, username, password, deadline):
    """登录并建立业务系统会话；失败返回 False（列表/详情页本身是公开的，可继续抓）。"""
    _get(session, LOGIN_PAGE_URL, deadline)          # 拿 csrf-token / session / PHPSESSID
    payload = {
        "account": username,
        "password": encrypt_password(password),
        "account_type": "userid",
        "login_scene": "feilian",
    }
    headers = dict(HEADERS)
    headers["Referer"] = IAM_BASE + "/login"
    csrf = session.cookies.get("csrf-token")
    if csrf:
        headers["csrf-token"] = csrf
    login_url = "%s?os=web&t=%d" % (LOGIN_API_URL, int(time.time() * 1000))
    left = _timeout_left(deadline)
    response = session.post(login_url, json=payload, headers=headers,
                            allow_redirects=False,
                            timeout=(min(5.0, left), min(REQUEST_TIMEOUT, left)))
    response.encoding = "utf-8"
    if response.status_code >= 400:
        return False
    try:
        data = json.loads(response.text)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    link = ((data.get("data") or {}).get("next") or {}).get("link")
    target = _resolve_login_link(link)
    if not target:
        return False
    _get(session, target, deadline, allow_redirects=True)
    return True


# ============================================================================
# 5. 抓取
# ============================================================================
def _wanted(activity, only_future):
    if not only_future:
        return True
    start = _parse_start(activity.get("活动时间", ""))
    return start is None or start > datetime.now()


def _crawl(session, max_pages, only_future, delay, deadline):
    activities = []
    seen = set()

    first = _get(session, LIST_URL_TEMPLATE.format(page=1), deadline)
    first_html = first.text
    total_pages = min(max_pages, _max_page(first_html))
    total_pages = max(total_pages, 1)

    for page in range(1, total_pages + 1):
        try:
            if page == 1:
                html_text = first_html
            else:
                html_text = _get(session, LIST_URL_TEMPLATE.format(page=page), deadline).text
        except TimeoutError:
            break

        for item in _parse_list(html_text):
            if time.monotonic() > deadline:
                return activities
            detail_url = urljoin(BASE_URL, item["href"])
            if detail_url in seen:
                continue
            seen.add(detail_url)
            # 列表卡片上已有时间，先粗筛一遍，省掉过期活动的详情请求
            if only_future:
                start = _parse_start(item.get("time", ""))
                if start is not None and start <= datetime.now():
                    continue
            try:
                detail_html = _get(session, detail_url, deadline).text
                activity = _parse_detail(detail_html)
            except TimeoutError:
                return activities
            except Exception:
                continue                      # 单条详情失败不影响整体
            activity["链接"] = detail_url
            if _wanted(activity, only_future):
                activities.append(activity)
            if delay > 0:
                time.sleep(min(delay, max(0.0, deadline - time.monotonic())))

    activities.sort(key=lambda item: _parse_start(item.get("活动时间", "")) or datetime.max)
    return activities


# ============================================================================
# 6. 文本渲染（给 LLM 节点当 context，必须是字符串）
# ============================================================================
def _render_text(activities):
    if not activities:
        return "本次没有抓取到符合条件的活动。"
    lines = ["共 %d 场活动：" % len(activities)]
    for index, activity in enumerate(activities, 1):
        lines.append("%d. %s" % (index, activity.get("活动名称", "")))
        for key in ACTIVITY_KEYS[1:]:
            lines.append("   %s：%s" % (key, activity.get(key, "")))
        if activity.get("链接"):
            lines.append("   链接：%s" % activity["链接"])
    return "\n".join(lines)


# ============================================================================
# 7. Dify 入口
# ============================================================================
def _resolve_credentials(kwargs):
    username = kwargs.get("username") or USERNAME
    password = kwargs.get("password") or PASSWORD
    return str(username or "").strip(), str(password or "").strip()


def main(**kwargs):
    """Dify 代码节点入口：永远返回 {"text": "..."}（只有一个 String 输出）。

    可用输入变量（都可省略）：
        username / password           账号密码（string）
        crawl_max_pages               最多翻几页（number，默认 1）
        crawl_delay                   详情页间隔秒数（number，默认 0）
        request_timeout               单请求超时（number）
        deadline_seconds              整体时间预算（number）
        crawl_only_future             是否只看未来（boolean）
    """
    activities = []
    text = ""
    try:
        if requests is None:
            raise RuntimeError("当前环境缺少 requests 库，无法发起网络请求")
        request_timeout = _as_int(kwargs.get("request_timeout"), REQUEST_TIMEOUT)
        globals()["REQUEST_TIMEOUT"] = max(1, request_timeout)
        deadline = time.monotonic() + max(1, _as_int(kwargs.get("deadline_seconds"),
                                                     DEADLINE_SECONDS))
        max_pages = max(1, _as_int(kwargs.get("crawl_max_pages"), CRAWL_MAX_PAGES))
        delay = max(0.0, _as_float(kwargs.get("crawl_delay"), CRAWL_DELAY))
        only_future = _as_bool(kwargs.get("crawl_only_future"), CRAWL_ONLY_FUTURE)
        username, password = _resolve_credentials(kwargs)

        session = _build_session()
        if username and password:
            try:
                _login(session, username, password, deadline)
            except Exception:
                pass                       # 登录失败仍可抓公开内容（电话显示"登录后可看"）
        activities = _crawl(session, max_pages, only_future, delay, deadline)
        text = _render_text(activities)
    except Exception as exc:
        if RAISE_ERRORS:
            raise
        text = "抓取失败：%s: %s" % (type(exc).__name__, exc)
    # 只返回节点里声明过的键，多一个少一个都会让 Dify 报
    # "Not all output parameters are validated."
    return {"text": text}


# ============================================================================
# 部署到 Dify「代码执行」节点：
#   1) 整份文件粘贴进节点（Python 3，已预装 requests 即可，无需其他依赖）；
#   2) 输出变量只声明一个：text（String）；
#      多声明/少声明都会触发 "Not all output parameters are validated"；
#   3) 输入变量（均可选）：username、password、crawl_max_pages、crawl_delay、
#      request_timeout、deadline_seconds、crawl_only_future；
#   4) 节点超时要 ≥ 30s：本内核默认在 20s 预算内返回，超预算会带着已抓到的数据退出。
#
# 本地自测（不写文件）：
#   python -c "import cas_core,json;print(json.dumps(cas_core.main(),ensure_ascii=False))"
# ============================================================================
