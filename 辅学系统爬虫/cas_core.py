# -*- coding: utf-8 -*-
"""南开 CAS 登录内核（Dify 代码节点版）。

流程：步骤1 预访问 CAS 入口 → 步骤2 POST 账号密码 → 签发/校验/落地 → 步骤4.5 SSO 中转 → 复核 → 步骤5 爬取。

Dify 用法：整段粘进代码节点，节点声明一个输出变量 activities（类型 array[object]），
返回值固定为 {"activities": [...]}。main() 永不抛异常。

Dify 的两个硬约束（踩过坑，别再改）：
  1. 返回值必须恰好只含节点声明过的键。多一个键 Dify 会报
     "Not all output parameters are validated."（源码里是
     len(parameters_validated) != len(result) 的判定）。
  2. 全文不能出现 if __name__ == "__main__" 之类的自执行代码。Dify 把整段代码
     内联进它自己的 runner 脚本里执行，那段脚本的 __name__ 就是 "__main__"，
     一旦自执行（尤其 raise SystemExit）就会在 Dify 打结果标记之前退出，
     症状是 "Output result is missing."，而且只用 0.1 秒、0 token。

其余接口：
    run()                 -> {"activities": [...]}  核心实现，不打印任何东西。
    collect_activities()  -> list[dict]             失败抛 CasError，想区分错误类型就用它。
    main(**kwargs)        -> dict                   Dify 入口。
账号密码从参数、环境变量 CAS_USERNAME / CAS_PASSWORD 或下面的模块配置读取。
"""

import base64
import datetime
import hashlib
import os
import re
import time
import warnings
from urllib.parse import quote, unquote, urljoin, urlparse

warnings.filterwarnings("ignore", message=".*doesn't match a supported version.*")

import requests
from html.parser import HTMLParser

try:
    from Crypto.Cipher import AES as _PyCryptoAES
except Exception:
    _PyCryptoAES = None

# ---- 目标站点 ----
CAS_LOGIN_PAGE_URL = "https://xgb.nankai.edu.cn/login/fslogin"
CAS_AUTH_API_URL = "https://iam.nankai.edu.cn/api/v1/login"
CAS_TICKET_ENDPOINT = "https://iam.nankai.edu.cn/api/cas/login"
CAS_TICKET_URL = ""
CAS_SERVICE_URL = "https://xgb.nankai.edu.cn/login/fslogin"
SSO_PORTAL_URL = "https://xgb.nankai.edu.cn/home/user/login.html"
SSO_LINK_KEYWORD = "辅学"
BUSINESS_ENTRY_URLS = []

# ---- 步骤2 请求形态 ----
USERNAME = ""
PASSWORD = ""
CREDENTIAL_ENV_USERNAME = "CAS_USERNAME"
CREDENTIAL_ENV_PASSWORD = "CAS_PASSWORD"

# ---- 调试开关 ----
# 排查时改成 True：main() 不再吞异常，Dify 的 FAIL 面板会直接显示失败阶段和原因。
# 正常使用时必须保持 False（否则登录失败会让工作流整体报错）。
RAISE_ERRORS = False
# 内部用：记录当前执行到哪一步，RAISE_ERRORS 打开时把阶段名带进异常信息。
_STAGE = ""
_TRACE = []

# 是否在返回值里额外带一份渲染好的纯文本（给 LLM 节点 / webhook 推送用）。
# 打开后 Dify 代码节点必须多声明一个输出变量 text（String），否则会报
# "Not all output parameters are validated."
INCLUDE_TEXT = True

FORM_FIELD_USERNAME = "account"
FORM_FIELD_PASSWORD = "password"
AUTH_API_EXTRA_FORM = {"login_scene": "feilian", "account_type": "userid"}
AUTH_API_EXTRA_QUERY = "os=web"
AUTH_API_CONTENT_TYPE = "json"
AUTH_API_EXTRA_HEADERS = {
    "X-Version-Check": "1",
    "X-Fe-Version": "3.1.9.15606",
    "Page-Name": "portal",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
AUTH_API_SEND_CSRF_HEADER = True

# ---- 判定规则 ----
TICKET_JSON_KEYS = ("ticket", "serviceTicket", "TICKET")
AUTH_SUCCESS_JSON_KEY = "code"
AUTH_SUCCESS_JSON_CODE = 0
LOGIN_FAIL_KEYWORDS = ("登录失败", "用户名或密码错误", "密码错误", "账号或密码错误", "invalid", "incorrect")
LOGIN_SUCCESS_COOKIE_NAMES = ("CASTGC", "JSESSIONID")

# ---- 复核 ----
VERIFY_LOGIN_URL = "https://fuxue.nankai.edu.cn/student/kbmhd/type/1/p/1.html"

# ---- 请求通用 ----
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REQUEST_TIMEOUT = 10
VERIFY_SSL = True

# ---- 加密 ----
ENCRYPT_PASSWORD = True
AES_KEY_SEED = "9007199254740991"
AES_KEY_HEX = ""
AES_IV_HEX = ""
AES_OUTPUT_ENCODING = "hex"
AES_SELF_TEST_VECTOR = "2b563d38ed5102f50e661c2cc33fec40"

# ---- 爬虫 ----
CRAWL_ENABLED = True
CRAWL_ONLY_FUTURE = True
CRAWL_MAX_PAGES = 3
CRAWL_DELAY = 0.0
ACTIVITY_FIELDS = ["活动名称", "活动类型", "活动学时", "活动时间", "活动地点"]
ACTIVITY_COLUMN_ALIASES = {
    "活动名称": ("活动名称", "活动标题", "讲座名称", "名称", "标题"),
    "活动类型": ("活动类型", "活动类别", "类型", "类别"),
    "活动学时": ("活动学时", "实践学时", "学时", "学分"),
    "活动时间": ("活动时间", "举办时间", "活动日期", "时间", "日期"),
    "活动地点": ("活动地点", "举办地点", "活动场地", "地点", "场地"),
}
CRAWL_TARGETS = [
    {"name": "主题讲座", "url": "https://fuxue.nankai.edu.cn/student/kbmhd/type/1.html"},
    {"name": "辅学活动", "url": "https://fuxue.nankai.edu.cn/student/kbmhd/type/2.html"},
]


class CasError(Exception):
    def __init__(self, error_type, message):
        super().__init__(message)
        self.error_type = error_type
        self.message = message


# ============================ 密码加密 ============================

def derive_key_iv():
    key_hex = AES_KEY_HEX.strip().lower() if AES_KEY_HEX else hashlib.md5(AES_KEY_SEED.encode("utf-8")).hexdigest()
    key = key_hex.encode("utf-8")
    iv_hex_full = AES_IV_HEX.strip().lower() if AES_IV_HEX else hashlib.sha1(key_hex.encode("utf-8")).hexdigest()
    iv = iv_hex_full[:16].encode("utf-8")
    return key, iv


def pkcs7_pad(data, block_size=16):
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len]) * pad_len


def _xtime(value):
    value <<= 1
    if value & 0x100:
        value ^= 0x11B
    return value & 0xFF


def _gmul(a, b):
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result & 0xFF


def _build_aes_sbox():
    exp_table = [0] * 256
    log_table = [0] * 256
    value = 1
    for i in range(255):
        exp_table[i] = value
        log_table[value] = i
        value = _gmul(value, 3)
    inverse = [0] * 256
    for x in range(1, 256):
        inverse[x] = exp_table[(255 - log_table[x]) % 255]
    sbox = [0] * 256
    for x in range(256):
        b = inverse[x]
        s = b
        for shift in (1, 2, 3, 4):
            s ^= ((b << shift) | (b >> (8 - shift))) & 0xFF
        sbox[x] = (s ^ 0x63) & 0xFF
    return sbox


_AES_SBOX = _build_aes_sbox()


def _aes_expand_key(key):
    nk = len(key) // 4
    nr = nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = [_AES_SBOX[temp[1]], _AES_SBOX[temp[2]], _AES_SBOX[temp[3]], _AES_SBOX[temp[0]]]
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        elif nk > 6 and i % nk == 4:
            temp = [_AES_SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return [bytes(b for word in words[4 * r:4 * r + 4] for b in word) for r in range(nr + 1)]


def _aes_encrypt_block(block, round_keys):
    state = list(block)
    for i in range(16):
        state[i] ^= round_keys[0][i]
    for rnd in range(1, len(round_keys) - 1):
        state = [_AES_SBOX[b] for b in state]
        shifted = [0] * 16
        for row in range(4):
            for col in range(4):
                shifted[4 * col + row] = state[4 * ((col + row) % 4) + row]
        state = shifted
        for col in range(4):
            s0, s1, s2, s3 = state[4 * col:4 * col + 4]
            t0 = _xtime(s0) ^ (_xtime(s1) ^ s1) ^ s2 ^ s3
            t1 = s0 ^ _xtime(s1) ^ (_xtime(s2) ^ s2) ^ s3
            t2 = s0 ^ s1 ^ _xtime(s2) ^ (_xtime(s3) ^ s3)
            t3 = (_xtime(s0) ^ s0) ^ s1 ^ s2 ^ _xtime(s3)
            state[4 * col + 0], state[4 * col + 1] = t0, t1
            state[4 * col + 2], state[4 * col + 3] = t2, t3
        for i in range(16):
            state[i] ^= round_keys[rnd][i]
    state = [_AES_SBOX[b] for b in state]
    shifted = [0] * 16
    for row in range(4):
        for col in range(4):
            shifted[4 * col + row] = state[4 * ((col + row) % 4) + row]
    for i in range(16):
        shifted[i] ^= round_keys[-1][i]
    return bytes(shifted)


def _encrypt_with_pure_python(data, key, iv):
    """纯标准库 AES-256-CBC + PKCS7（沙箱里没有 pycryptodome 时唯一可行方案）。"""
    round_keys = _aes_expand_key(key)
    padded = pkcs7_pad(data, 16)
    output = bytearray()
    previous = iv
    for offset in range(0, len(padded), 16):
        block = padded[offset:offset + 16]
        mixed = bytes(a ^ b for a, b in zip(block, previous))
        previous = _aes_encrypt_block(mixed, round_keys)
        output.extend(previous)
    return output.hex()


_AES_BACKEND = "pycryptodome" if _PyCryptoAES is not None else "pure-python"


def aes_encrypt(plaintext):
    """明文密码 → 十六进制密文，与南开 IAM 前端 aesEncrypt() 结果一致。

    有 pycryptodome 就用它，没有就用纯标准库实现（Dify 沙箱属于后者，
    装不了第三方包，所以这条回退路径是必需的，不要删）。
    """
    key, iv = derive_key_iv()
    data = plaintext.encode("utf-8")
    if _PyCryptoAES is not None:
        cipher = _PyCryptoAES.new(key, _PyCryptoAES.MODE_CBC, iv)
        cipher_hex = cipher.encrypt(pkcs7_pad(data, 16)).hex()
    else:
        cipher_hex = _encrypt_with_pure_python(data, key, iv)
    if AES_OUTPUT_ENCODING.lower() == "base64":
        return base64.b64encode(bytes.fromhex(cipher_hex)).decode("ascii")
    return cipher_hex


def self_test_crypto():
    if aes_encrypt("demo-1234") != AES_SELF_TEST_VECTOR:
        raise CasError("crypto_self_test_failed", "AES 自检未通过：加密结果与 iam.nankai.edu.cn 前端不一致")


# ============================ 会话工具 ============================

def build_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
    })
    session.verify = VERIFY_SSL

    from urllib3.util.retry import Retry

    retry_kwargs = dict(total=2, connect=2, read=2, status=2,
                        backoff_factor=0.5, status_forcelist=(502, 503, 504), raise_on_status=False)
    try:
        retry = Retry(allowed_methods=frozenset(("GET",)), **retry_kwargs)
    except TypeError:
        retry = Retry(method_whitelist=frozenset(("GET",)), **retry_kwargs)
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    if not VERIFY_SSL:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def decode_response(response):
    head = response.content[:2048].lower()
    if b"utf-8" in head or b"utf8" in head:
        response.encoding = "utf-8"
    else:
        response.encoding = response.apparent_encoding or response.encoding
    return response.text


def visible_text(response):
    body = decode_response(response)
    if not body:
        return ""
    return html_to_text(body)


def cas_entry_url():
    if CAS_TICKET_URL:
        return CAS_TICKET_URL
    if not CAS_TICKET_ENDPOINT or not CAS_SERVICE_URL:
        return ""
    separator = "&" if "?" in CAS_TICKET_ENDPOINT else "?"
    return "{}{}service={}".format(CAS_TICKET_ENDPOINT, separator, quote(CAS_SERVICE_URL, safe=""))


def extract_ticket(response):
    data = None
    try:
        data = response.json()
    except Exception:
        data = None

    if isinstance(data, dict):
        for key in TICKET_JSON_KEYS:
            if data.get(key):
                return str(data[key])
        for value in data.values():
            if isinstance(value, dict):
                for key in TICKET_JSON_KEYS:
                    if value.get(key):
                        return str(value[key])
    if isinstance(data, str) and data and data.lower() != "success":
        return data

    body = decode_response(response)
    match = re.search(r'"(?:ticket|serviceTicket|TICKET)"\s*:\s*"([^"]+)"', body or "")
    if match:
        return match.group(1)

    locations = [response.headers.get("Location", "")]
    for hop in getattr(response, "history", None) or []:
        locations.append(hop.headers.get("Location", ""))
    for location in locations:
        match = re.search(r"[?&]ticket=([^&#]+)", location or "")
        if match:
            return unquote(match.group(1))
    return None


def auth_hosts():
    hosts = set()
    for url in (CAS_LOGIN_PAGE_URL, CAS_AUTH_API_URL, CAS_TICKET_ENDPOINT, CAS_TICKET_URL):
        netloc = urlparse(url or "").netloc.lower()
        if netloc:
            hosts.add(netloc)
    return hosts


def classify_auth_redirect(url):
    if not url:
        return "other"
    parts = urlparse(url)
    path = parts.path.lower()
    if "/api/cas" in path:
        return "issuer"
    if "/api/" in path:
        return "other"
    if path.rstrip("/").endswith("/login"):
        return "login"
    segments = [seg for seg in path.split("/") if seg]
    if segments and segments[0] == "login":
        return "login"
    auth_host = urlparse(CAS_AUTH_API_URL).netloc
    if auth_host and parts.netloc == auth_host and "/api/" not in path:
        return "login"
    return "other"


def _get(session, url, step, **kwargs):
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    try:
        return session.get(url, **kwargs)
    except requests.exceptions.RequestException as error:
        raise CasError("network", "{}：请求失败 {} -> {}".format(step, url, error))


# ============================ 主流程 ============================

def step1_open_login_page(session):
    response = _get(session, CAS_LOGIN_PAGE_URL, "step1", allow_redirects=False)
    location = response.headers.get("Location", "")
    if response.is_redirect and location:
        response = _get(session, urljoin(response.url, location), "step1", allow_redirects=False)
    if response.status_code >= 400:
        raise CasError("http_error", "step1：CAS 入口返回 HTTP {}".format(response.status_code))
    return {"status": response.status_code, "url": response.url}


def step2_post_credentials(session, username, password):
    password_value = aes_encrypt(password) if ENCRYPT_PASSWORD else password

    payload = {
        FORM_FIELD_USERNAME: username,
        FORM_FIELD_PASSWORD: password_value,
    }
    payload.update(AUTH_API_EXTRA_FORM)

    headers = dict(AUTH_API_EXTRA_HEADERS)
    request_url = CAS_AUTH_API_URL
    if AUTH_API_EXTRA_QUERY:
        request_url += ("&" if "?" in request_url else "?") + AUTH_API_EXTRA_QUERY
    if AUTH_API_SEND_CSRF_HEADER:
        csrf = next((c.value for c in session.cookies if c.name == "csrf-token"), "")
        if csrf:
            headers["csrf-token"] = csrf

    try:
        if AUTH_API_CONTENT_TYPE == "json":
            response = session.post(request_url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        else:
            response = session.post(request_url, data=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as error:
        raise CasError("network", "step2：POST 登录接口失败 -> {}".format(error))

    body = decode_response(response)
    try:
        data = response.json()
    except ValueError:
        data = None

    code_checked = isinstance(data, dict) and AUTH_SUCCESS_JSON_KEY in data
    auth_ok = None
    if code_checked and AUTH_SUCCESS_JSON_CODE is not None:
        code = data.get(AUTH_SUCCESS_JSON_KEY)
        auth_ok = code == AUTH_SUCCESS_JSON_CODE
    # 判据：有票 → 成功；JSON 里明确给了 code 但不是成功码 → 失败；其余（本站点接口不返回 code）→ 交给步骤2.5 现场签发

    ticket = extract_ticket(response) or ""
    validate_url = ""
    location = response.headers.get("Location", "")
    if location and "ticket=" in location.lower():
        validate_url = urljoin(request_url, location)

    if ticket:
        return {"ticket": ticket, "validate_url": validate_url}

    hints = []
    if "10110001" in body:
        hints.append("10110001 = 用户名或密码错误（请求格式正确，账号或密码不对）")
    if "10110002" in body:
        hints.append("10110002 = 认证方式不被允许（检查 AUTH_API_EXTRA_FORM 的 account_type）")
    if "参数错误" in body:
        hints.append("『参数错误』= 请求体缺字段（对照抓包补进 AUTH_API_EXTRA_FORM）")
    if any(word in body for word in LOGIN_FAIL_KEYWORDS):
        hints.append("响应里出现了失败关键词")

    if auth_ok is False:
        message = "step2：登录未被服务端认可（{}={!r}，期望 {!r}）".format(
            AUTH_SUCCESS_JSON_KEY, (data or {}).get(AUTH_SUCCESS_JSON_KEY), AUTH_SUCCESS_JSON_CODE)
        if hints:
            message += "；" + "；".join(hints)
        raise CasError("login_rejected", message)

    # 无票且无明确成功码：属于本站正常路径（票由步骤2.5 现场签发），不中止；仅当出现失败线索时才报错
    if auth_ok is None and hints:
        raise CasError("login_unconfirmed", "step2：无法确认登录结果；" + "；".join(hints))

    return {"ticket": None, "validate_url": ""}


def establish_login_state(session, issued):
    ticket = (issued or {}).get("ticket") or ""
    validate_url = (issued or {}).get("validate_url") or ""

    # ① 签发：必须 allow_redirects=False，否则 ticket 会被 requests 当场跟掉（票一次性，自动跟 = 立刻消费）
    if not ticket and not validate_url:
        entry = cas_entry_url()
        if not entry:
            raise CasError("config_error", "CAS_TICKET_ENDPOINT / CAS_SERVICE_URL 未配置，无法签发 ticket")
        probe = _get(session, entry, "step2.5", allow_redirects=False)
        location = probe.headers.get("Location", "")
        ticket = extract_ticket(probe) or ""
        if ticket and location:
            # 校验地址以服务器给的 Location 为准（保证签发/校验两边 service 逐字符一致）
            validate_url = urljoin(probe.url or entry, location)
        if not ticket:
            if "login" in location.lower():
                raise CasError("no_ticket", "签发入口把请求弹回了登录页，步骤2 的登录态没有建立")
            raise CasError("no_ticket", "签发入口的 Location 里没有 ticket（{}）".format(location or "无 Location"))

    # ② 校验 + 落地：必须 allow_redirects=True，票据本来就要在这一跳被消费
    if not validate_url:
        separator = "&" if "?" in CAS_SERVICE_URL else "?"
        validate_url = "{}{}ticket={}".format(CAS_SERVICE_URL, separator, quote(ticket, safe=""))
    response = _get(session, validate_url, "step3", allow_redirects=True)

    cookie_names = [cookie.name for cookie in session.cookies]
    has_login_cookie = any(name in cookie_names for name in LOGIN_SUCCESS_COOKIE_NAMES)
    if not has_login_cookie and any(word in visible_text(response) for word in LOGIN_FAIL_KEYWORDS):
        raise CasError("login_failed", "业务系统落地页出现登录失败提示")

    return {
        "status": response.status_code,
        "final_url": response.url,
        "html": decode_response(response),
        "ticket": ticket,
        "cookie_names": cookie_names,
    }


def extract_html_redirect(html, base_url):
    if not html:
        return None
    patterns = (
        r'<meta[^>]+http-equiv\s*=\s*["\']?refresh["\']?[^>]*content\s*=\s*["\'][^"\']*url\s*=\s*([^"\'>\s]+)',
        r'location\s*\.\s*(?:href|replace)\s*\(\s*["\']([^"\']+)["\']',
        r'location\s*\.\s*(?:href|replace)\s*=\s*["\']([^"\']+)["\']',
        r'window\s*\.\s*open\s*\(\s*["\']([^"\']+)["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match is None:
            continue
        target = match.group(1).strip()
        if target and not target.lower().startswith(("javascript:", "#")):
            return urljoin(base_url, target)
    return None


def find_sso_entry_links(html, base_url):
    results = []
    for match in re.finditer(r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL):
        href = match.group(1).strip()
        if not href or href.startswith(("javascript:", "#")):
            continue
        if "orlogin" not in href.lower():
            continue
        text = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", match.group(2)))
        results.append((urljoin(base_url, href), text))
    return results


def step4_5_enter_business_system(session, landing=None):
    entry_urls = list(BUSINESS_ENTRY_URLS)
    portal_url = SSO_PORTAL_URL

    if SSO_PORTAL_URL:
        portal_html = ""
        if landing and landing.get("html"):
            land = urlparse(landing.get("final_url") or "")
            want = urlparse(SSO_PORTAL_URL)
            if land.netloc and land.netloc == want.netloc and land.path.rstrip("/") == want.path.rstrip("/"):
                portal_html = landing["html"]
                portal_url = landing.get("final_url") or SSO_PORTAL_URL
        if not portal_html:
            portal = _get(session, SSO_PORTAL_URL, "step4.5")
            portal_html = decode_response(portal)
            portal_url = portal.url

        links = find_sso_entry_links(portal_html, portal_url)
        picked = None
        if SSO_LINK_KEYWORD:
            for url, text in links:
                if SSO_LINK_KEYWORD in text:
                    picked = (url, text)
                    break
            if picked is None:
                for url, text in links:
                    if SSO_LINK_KEYWORD.lower() in url.lower():
                        picked = (url, text)
                        break
        if picked is not None:
            entry_urls = [picked[0]]
        elif not entry_urls:
            raise CasError("sso_entry_not_found",
                           "门户页里没找到包含「{}」的 SSO 入口，且 BUSINESS_ENTRY_URLS 为空".format(SSO_LINK_KEYWORD))

    if not entry_urls:
        return None

    response = None
    referer = portal_url if SSO_PORTAL_URL else ""

    for entry in entry_urls:
        headers = {"Referer": referer} if referer else {}
        current_url = entry
        current_referer = referer
        response = None
        visited = {entry}
        for _hop in range(10):
            hop_headers = dict(headers)
            if current_referer:
                hop_headers["Referer"] = current_referer
            response = _get(session, current_url, "step4.5", allow_redirects=False, headers=hop_headers)
            location = urljoin(current_url, response.headers["Location"]) if response.headers.get("Location") else ""
            if response.is_redirect is False or not location:
                break
            if location in visited:
                raise CasError("redirect_loop", "step4.5：SSO 中转跳转成环（{}）".format(location))
            visited.add(location)
            current_referer = current_url
            current_url = location

        for _hop in range(5):
            if response is None or response.status_code != 200:
                break
            target = extract_html_redirect(decode_response(response), response.url)
            if not target or target in visited:
                break
            visited.add(target)
            response = _get(session, target, "step4.5", allow_redirects=True, headers={"Referer": response.url})

        referer = response.url or entry

    return {
        "status": response.status_code,
        "final_url": response.url,
        "html": decode_response(response),
        "cookie_names": [cookie.name for cookie in session.cookies],
    }


def verify_single_request(session, target):
    try:
        response = session.get(target, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except requests.exceptions.RequestException:
        return None

    final = urlparse(response.url)
    final_host = final.netloc.lower()
    if final_host in auth_hosts() and "/login" in final.path:
        return False
    if any(word in visible_text(response) for word in LOGIN_FAIL_KEYWORDS):
        return False
    return True


def verify_login_state(session):
    return verify_single_request(session, VERIFY_LOGIN_URL or CAS_SERVICE_URL)


# ============================ 步骤5 爬虫 ============================

_BLOCK_TAGS = frozenset((
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
))
_SKIP_TEXT_TAGS = frozenset(("script", "style", "noscript", "template"))
_VOID_TAGS = frozenset((
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
))


def html_to_text(document):
    """把 HTML 转成可见文字：剥离 script/style 与全部标签，块级标签当空白处理。"""
    if document is None:
        return ""
    if isinstance(document, bytes):
        try:
            document = document.decode("utf-8", "replace")
        except Exception:
            document = str(document)
    text = str(document)
    if "<" not in text:
        return re.sub(r"\s+", " ", text).strip()

    class _TextExtractor(HTMLParser):
        def __init__(self):
            HTMLParser.__init__(self, convert_charrefs=True)
            self.parts = []
            self.depth = 0

        def handle_starttag(self, tag, attrs):
            if tag in _SKIP_TEXT_TAGS:
                self.depth += 1
                return
            if self.depth:
                return
            if tag in _VOID_TAGS:
                if tag == "br":
                    self.parts.append("\n")
                return
            if tag in _BLOCK_TAGS:
                self.parts.append("\n")

        def handle_startendtag(self, tag, attrs):
            if self.depth:
                return
            if tag == "br":
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in _SKIP_TEXT_TAGS:
                if self.depth:
                    self.depth -= 1
                return
            if self.depth:
                return
            if tag in _BLOCK_TAGS:
                self.parts.append("\n")

        def handle_data(self, data):
            if not self.depth:
                self.parts.append(data)

    extractor = _TextExtractor()
    try:
        extractor.feed(text)
        extractor.close()
        raw = "".join(extractor.parts)
    except Exception:
        raw = re.sub(r"<[^>]*>", " ", text)
    return re.sub(r"\s+", " ", raw).strip()


class _TableCollector(HTMLParser):
    """收集第一个 class 含 typetable1 的表格；行记为 {"class": str, "cells": [str]}。"""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.depth = 0
        self.target_depth = 0
        self.rows = []
        self.row = None
        self.cell = None
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        mapping = dict(attrs)
        if self.depth == 0:
            if tag == "table":
                classes = mapping.get("class", "")
                if "typetable1" in classes.split():
                    self.depth = 1
                    self.target_depth = 1
                else:
                    self.depth = 0
                    self.target_depth = 0
            return
        if tag == "table":
            self.depth += 1
            return
        if tag == "tr":
            self.row = {"class": mapping.get("class", ""), "cells": []}
            self.rows.append(self.row)
            return
        if tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if self.done:
            return
        if tag == "table":
            if self.depth > 0:
                self.depth -= 1
                if self.depth == 0:
                    self.done = True
            return
        if tag == "tr":
            self.row = None
            self.cell = None
            return
        if tag in ("td", "th"):
            if self.cell is not None and self.row is not None:
                self.row["cells"].append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None

    def handle_data(self, data):
        if self.done:
            return
        if self.cell is not None:
            self.cell.append(data)


def parse_activity_table(document):
    """从页面里找第一个 class 含 typetable1 的表格，返回 (表头, 数据行)。"""
    if document is None:
        return [], []
    if isinstance(document, bytes):
        try:
            document = document.decode("utf-8", "replace")
        except Exception:
            document = str(document)
    text = str(document)
    collector = _TableCollector()
    try:
        collector.feed(text)
        collector.close()
    except Exception:
        return [], []
    if not collector.rows:
        return [], []

    headers = []
    for tr in collector.rows:
        if "tr1" in tr["class"].split():
            headers = list(tr["cells"])
            break

    rows = []
    for tr in collector.rows:
        cells = list(tr["cells"])
        if not cells:
            continue
        if "tr1" in tr["class"].split():
            continue
        if headers and cells == headers:
            continue
        if all(cell == "" for cell in cells):
            continue
        rows.append(cells)
    return headers, rows


def map_activity_columns(headers, width):
    index = {}
    used = set()
    for field in ACTIVITY_FIELDS:
        for head_index, head in enumerate(headers):
            if head_index in used:
                continue
            for alias in ACTIVITY_COLUMN_ALIASES.get(field, ()):
                if alias and alias in head:
                    index[field] = head_index
                    used.add(head_index)
                    break
            if field in index:
                break
    for fallback, field in enumerate(ACTIVITY_FIELDS):
        if field not in index and fallback < width:
            index[field] = fallback
    return index


def normalize_fullwidth(text):
    table = {ord(c): ord(h) for c, h in zip("０１２３４５６７８９", "0123456789")}
    table.update({ord("－"): ord("-"), ord("／"): ord("/"), ord("．"): ord(".")})
    return (text or "").translate(table)


DATE_PATTERNS = (
    re.compile(r"(\d{4})\s*[-/年\.]\s*(\d{1,2})\s*[-/月\.]\s*(\d{1,2})\s*日?"),
    re.compile(r"(?<![\d:])(\d{1,2})\s*[-/月\.]\s*(\d{1,2})\s*日?(?![\d:])"),
)


def extract_activity_date(text):
    text = normalize_fullwidth(text)
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        groups = match.groups()
        if len(groups) == 3:
            year, month, day = (int(g) for g in groups)
        else:
            year = datetime.date.today().year
            month, day = int(groups[0]), int(groups[1])
        try:
            return datetime.date(year, month, day)
        except ValueError:
            continue
    return None


def is_activity_future(time_text, today):
    activity_date = extract_activity_date(time_text)
    if activity_date is None:
        return None
    return activity_date > today


def activity_page_url(list_url, page):
    root = re.sub(r"/p/\d+\.html?$", "", list_url.rstrip("/"), flags=re.IGNORECASE)
    if root.lower().endswith(".html"):
        root = root[:-5]
    return "{}/p/{}.html".format(root, page)


def parse_max_page(document):
    if document is None:
        return 1
    if isinstance(document, bytes):
        try:
            document = document.decode("utf-8", "replace")
        except Exception:
            document = str(document)
    pages = [1]
    for match in re.finditer(r"""href\s*=\s*["']([^"']*)["']""", str(document), re.IGNORECASE):
        page_match = re.search(r"/p/(\d+)\.html?", match.group(1), re.IGNORECASE)
        if page_match is not None:
            pages.append(int(page_match.group(1)))
    return max(pages)


def crawl_activity_list(session, target, today):
    name = target.get("name", "")
    list_url = target.get("url", "")
    kept = []
    seen = set()
    page = 1
    max_page = 1

    while page <= max_page and page <= CRAWL_MAX_PAGES:
        url = activity_page_url(list_url, page)
        response = _get(session, url, "crawl")
        if "/login/index.html" in response.url or "统一身份认证登录" in response.text:
            raise CasError("session_expired", "爬虫：已被踢回登录页，登录态失效（{}）".format(url))
        if response.status_code != 200:
            break
        html = decode_response(response)
        headers, rows = parse_activity_table(html)
        page_max = parse_max_page(html)
        if page_max > max_page:
            max_page = page_max

        index = map_activity_columns(headers, max((len(r) for r in rows), default=0))
        for cells in rows:
            item = {}
            for field in ACTIVITY_FIELDS:
                column = index.get(field)
                item[field] = cells[column] if (column is not None and column < len(cells)) else ""
            if not item["活动名称"]:
                continue
            key = (item["活动名称"], item["活动时间"])
            if key in seen:
                continue
            seen.add(key)
            if CRAWL_ONLY_FUTURE and is_activity_future(item["活动时间"], today) is False:
                continue
            kept.append(item)

        if not rows:
            break
        page += 1
        if page <= max_page and CRAWL_DELAY > 0:
            time.sleep(CRAWL_DELAY)
    return kept


def step5_crawl_business_data(session):
    if CRAWL_ENABLED is False:
        return []
    today = datetime.date.today()
    results = []
    for target in CRAWL_TARGETS:
        results.extend(crawl_activity_list(session, target, today))
    return results


# ============================ 入口 ============================

def resolve_credentials(username=None, password=None):
    name = CREDENTIAL_ENV_USERNAME
    pwd = CREDENTIAL_ENV_PASSWORD
    final_username = username if username else (os.environ.get(name, "").strip() or USERNAME)
    final_password = password if password else (os.environ.get(pwd, "") or PASSWORD)
    if not final_username or not final_password:
        raise CasError(
            "missing_credentials",
            "缺少账号或密码（可传入参数，或设置环境变量 {} / {}）".format(name, pwd),
        )
    return final_username, final_password


def check_config():
    missing = []
    if not CAS_LOGIN_PAGE_URL:
        missing.append("CAS_LOGIN_PAGE_URL")
    if not CAS_AUTH_API_URL:
        missing.append("CAS_AUTH_API_URL")
    if not CAS_SERVICE_URL:
        missing.append("CAS_SERVICE_URL")
    if not CAS_TICKET_ENDPOINT and not CAS_TICKET_URL:
        missing.append("CAS_TICKET_ENDPOINT")
    if CAS_TICKET_URL and CAS_SERVICE_URL and CAS_SERVICE_URL not in CAS_TICKET_URL:
        missing.append("CAS_TICKET_URL(service 与 CAS_SERVICE_URL 不一致)")
    if AES_KEY_HEX and len(AES_KEY_HEX.strip()) != 32:
        missing.append("AES_KEY_HEX(必须 32 个字符)")
    if AES_IV_HEX and len(AES_IV_HEX.strip()) < 16:
        missing.append("AES_IV_HEX(至少 16 个字符)")
    if missing:
        raise CasError("config_error", "配置缺失或不合法：" + ", ".join(missing))


def collect_activities(username=None, password=None):
    """执行登录并返回活动列表（list[dict]）；任何失败都抛 CasError。"""
    global _STAGE
    _STAGE = "配置检查"
    _TRACE.clear()
    check_config()

    if AES_SELF_TEST_VECTOR:
        _STAGE = "AES 自检"
        self_test_crypto()

    _STAGE = "凭据解析"
    username, password = resolve_credentials(username, password)

    _STAGE = "建立会话"
    session = build_session()
    _STAGE = "步骤1 预访问 CAS 入口"
    page = step1_open_login_page(session)
    _TRACE.append("步骤1 状态=%s 跳数=%s" % (page.get("status"), len(page.get("hops") or [])))
    _STAGE = "步骤2 提交账号密码"
    issued = step2_post_credentials(session, username, password)
    _TRACE.append("步骤2 拿到ticket=%s" % ("有" if issued.get("ticket") else "无"))
    _STAGE = "步骤2.5~4 签发并校验 ticket"
    landing = establish_login_state(session, issued)
    _TRACE.append("校验跳最终URL=%s" % landing.get("final_url"))

    _STAGE = "步骤4.5 SSO 中转"
    enter = step4_5_enter_business_system(session, landing)
    if enter is not None and enter.get("final_url"):
        landing = enter
    _TRACE.append("步骤4.5 最终URL=%s" % (enter or {}).get("final_url"))

    _STAGE = "登录态复核"
    verified = verify_login_state(session)
    _TRACE.append("登录态复核=%s" % verified)
    if verified is False:
        raise CasError("verify_failed", "登录态复核未通过：目标页面把请求送回了登录页")
    if not CRAWL_ENABLED:
        _TRACE.append("爬取被 CRAWL_ENABLED 关闭")
        return []

    _STAGE = "步骤5 爬取活动列表"
    items = step5_crawl_business_data(session)
    _TRACE.append("抓到 %d 条活动" % len(items))
    return items


def run(username=None, password=None):
    """核心实现：返回 {"activities": [...]}。

    RAISE_ERRORS 为 False 时永不抛异常（失败返回空列表）；
    为 True 时把原始异常抛给调用方，便于在 Dify 面板里看原因。
    """
    try:
        activities = collect_activities(username=username, password=password)
    except Exception as exc:
        if RAISE_ERRORS:
            raise CasError(
                "debug",
                "[{}] {}: {}".format(_STAGE or "未知阶段", type(exc).__name__, exc),
            ) from None
        activities = []
    if not isinstance(activities, list):
        activities = []
    # 调试模式：流程走完却一条都没抓到，把每步轨迹抛出去（否则面板上只显示 []）
    if RAISE_ERRORS and not activities:
        raise CasError("empty_result", "登录流程走完但结果为 0 条 → " + " | ".join(_TRACE))
    return {"activities": activities}


def render_activities_text(activities):
    """把活动列表渲染成纯文本，供 LLM 节点或 webhook 推送直接使用。

    输出是字符串（Dify 里声明为 String 类型的输出变量），每场活动三到四行，
    形状稳定、不依赖 LLM 的格式化能力。
    """
    items = [item for item in (activities or []) if isinstance(item, dict)]
    if not items:
        return "本次没有抓取到符合条件的活动。"
    lines = ["共 {} 场活动：".format(len(items))]
    for index, item in enumerate(items, 1):
        title = str(item.get("活动名称", "") or "").strip() or "（未命名活动）"
        lines.append("{}. {}".format(index, title))
        for label in ("活动时间", "活动地点", "活动类型", "活动学时"):
            value = str(item.get(label, "") or "").strip()
            if value:
                lines.append("   {}：{}".format(label, value))
    return "\n".join(lines)


def apply_kwargs(kwargs):
    """把 Dify 节点传来的参数覆盖到模块配置上。

    支持的键：username / password / crawl_max_pages / crawl_delay /
    request_timeout / crawl_only_future / activity_fields / crawl_targets。
    传空值等于“用默认值”。
    """
    if not isinstance(kwargs, dict):
        return
    username = kwargs.get("username")
    if username:
        globals()["USERNAME"] = str(username)
    password = kwargs.get("password")
    if password:
        globals()["PASSWORD"] = str(password)
    for key, caster in (
        ("crawl_max_pages", int),
        ("request_timeout", int),
        ("crawl_delay", float),
    ):
        value = kwargs.get(key)
        if value not in (None, ""):
            try:
                globals()[key.upper()] = caster(value)
            except (TypeError, ValueError):
                pass
    if kwargs.get("crawl_only_future") is not None:
        globals()["CRAWL_ONLY_FUTURE"] = bool(kwargs.get("crawl_only_future"))
    fields = kwargs.get("activity_fields")
    if isinstance(fields, list) and fields:
        globals()["ACTIVITY_FIELDS"] = [str(item) for item in fields]
    targets = kwargs.get("crawl_targets")
    if isinstance(targets, list) and targets:
        cleaned = [
            {"name": str(item.get("name", "")), "url": str(item.get("url", ""))}
            for item in targets
            if isinstance(item, dict) and item.get("url")
        ]
        if cleaned:
            globals()["CRAWL_TARGETS"] = cleaned


def main(**kwargs):
    """Dify 代码节点入口：必须返回 dict，键名与节点里声明的输出变量一致。

    输入全部可选（username / password / crawl_max_pages / ... ），
    任何时候都返回 {"activities": [...]}（INCLUDE_TEXT 打开时多一个 "text"），
    不会抛异常。
    """
    try:
        apply_kwargs(kwargs)
    except Exception:
        pass
    try:
        result = run()
    except Exception:
        if RAISE_ERRORS:
            raise
        result = {"activities": []}
    if not isinstance(result, dict):
        result = {"activities": []}
    if not isinstance(result.get("activities"), list):
        result = {"activities": []}
    # 只返回节点里声明过的键：多一个少一个都会让 Dify 报
    # "Not all output parameters are validated."
    activities = result["activities"]
    payload = {"activities": activities}
    if INCLUDE_TEXT:
        payload["text"] = render_activities_text(activities)
    return payload

