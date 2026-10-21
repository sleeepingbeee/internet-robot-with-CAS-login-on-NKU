import requests
import os
from lxml import html
import re
import csv
import json
import hashlib
from datetime import datetime

CAS_LOGIN_PAGE_URL = "https://zyfw.nankai.edu.cn/Login/index.aspx"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/70.0.3538.110 Safari/537.36',
}

USERNAME = os.environ.get('CAS_USERNAME')
PASSWORD = os.environ.get('CAS_PASSWORD')

FIELDS = ("活动名称", "组织机构", "联系人", "联系电话",
          "活动时间", "活动地点", "志愿时长", "招募人数")

# ============================================================================
# ============================================================================
# 密码加密部分
# ============================================================================
# ============================================================================


# ============================================================================
# 0. 固定参数（算法的一部分，不要改；改了密文就和服务端对不上）
# ============================================================================
SEED = "9007199254740991"          # 前端 Number.MAX_SAFE_INTEGER.toString()


# ============================================================================
# 1. 密钥/IV 推导
# ============================================================================
def _key_iv():
    key_hex = hashlib.md5(SEED.encode("utf-8")).hexdigest()          # 32 字符
    key = key_hex.encode("utf-8")                                    # 32 字节 → AES-256
    iv_hex = hashlib.sha1(key_hex.encode("utf-8")).hexdigest()       # 40 字符
    iv = iv_hex[:16].encode("utf-8")                                 # 只用前 16 字节
    return key, iv


# ============================================================================
# 2. AES 内核（GF(2^8) 运算 + S 盒 + 密钥扩展 + 单块加密）
# ============================================================================
def _xtime(a):
    a <<= 1
    return (a ^ 0x11B) & 0xFF if a & 0x100 else a


def _gmul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r & 0xFF


def _sbox():
    exp, log = [0] * 256, [0] * 256
    v = 1
    for i in range(255):
        exp[i], log[v] = v, i
        v = _gmul(v, 3)
    box = [0] * 256
    for x in range(256):
        b = exp[(255 - log[x]) % 255] if x else 0                    # x 的乘法逆元（0 特判）
        s = b
        for sh in (1, 2, 3, 4):
            s ^= ((b << sh) | (b >> (8 - sh))) & 0xFF
        box[x] = (s ^ 0x63) & 0xFF
    return box


_SBOX = _sbox()


def _expand_key(key):
    nk, nr = len(key) // 4, len(key) // 4 + 6                        # AES-256: nk=8, nr=14
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = [_SBOX[t[1]], _SBOX[t[2]], _SBOX[t[3]], _SBOX[t[0]]]  # RotWord + SubWord
            t[0] ^= rcon
            rcon = _xtime(rcon)
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[b] for b in t]                                # AES-256 额外的 SubWord
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return [bytes(b for word in w[4 * r:4 * r + 4] for b in word) for r in range(nr + 1)]


def _encrypt_block(block, rk):
    s = list(block)
    for i in range(16):
        s[i] ^= rk[0][i]
    for rnd in range(1, len(rk) - 1):
        s = [_SBOX[b] for b in s]                                    # SubBytes
        s = [s[4 * ((c + r) % 4) + r] for c in range(4) for r in range(4)]  # ShiftRows
        for c in range(4):                                           # MixColumns
            a0, a1, a2, a3 = s[4 * c:4 * c + 4]
            s[4 * c:4 * c + 4] = [
                _xtime(a0) ^ (_xtime(a1) ^ a1) ^ a2 ^ a3,
                a0 ^ _xtime(a1) ^ (_xtime(a2) ^ a2) ^ a3,
                a0 ^ a1 ^ _xtime(a2) ^ (_xtime(a3) ^ a3),
                (_xtime(a0) ^ a0) ^ a1 ^ a2 ^ _xtime(a3),
            ]
        for i in range(16):
            s[i] ^= rk[rnd][i]                                       # AddRoundKey
    s = [_SBOX[b] for b in s]                                        # 最后一轮没有 MixColumns
    s = [s[4 * ((c + r) % 4) + r] for c in range(4) for r in range(4)]
    return bytes(x ^ y for x, y in zip(s, rk[-1]))


# ============================================================================
# 3. CBC + PKCS7
# ============================================================================
def _pad(data):
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def _cbc_encrypt(data, key, iv):
    rk = _expand_key(key)
    padded = _pad(data)                                              # 只填充一次
    out, prev = bytearray(), iv
    for i in range(0, len(padded), 16):
        prev = _encrypt_block(bytes(a ^ b for a, b in zip(padded[i:i + 16], prev)), rk)
        out += prev
    return bytes(out)


# ============================================================================
# 4. ★ 内核入口
# ============================================================================
def encrypt_password(password):
    """输入明文密码，输出十六进制密文。

    >>> encrypt_password("demo-1234")
    '2b563d38ed5102f50e661c2cc33fec40'
    """
    if password is None:
        return ""
    data = password if isinstance(password, bytes) else str(password).encode("utf-8")
    if not data:
        return ""
    key, iv = _key_iv()
    return _cbc_encrypt(data, key, iv).hex()
# ============================================================================
# ============================================================================
# 加密部分结束
# ============================================================================
# ============================================================================



def is_time_range_later_than_today(time_str: str) -> bool:
    text = re.sub(r'\s+', ' ', (time_str or '')).strip()
    parts = re.split(r'至', text)
    if len(parts) != 2:
        return False                      # 或 raise，按你的策略
    start_dt = datetime.strptime(parts[0].strip(), "%Y-%m-%d %H:%M")
    return start_dt.date() > datetime.now().date()


def get_login_page(session):
    response = session.get(CAS_LOGIN_PAGE_URL, headers=HEADERS, allow_redirects=True)
    return response

def post_login(session, login_url, username, encrypted_password):
    payload = {
        'account': username,
        'password': encrypted_password,
        'account_type': 'userid',
        'login_scene':'feilian',
    }
    response = session.post(login_url, json=payload, headers=HEADERS, allow_redirects=False)
    data = json.loads(response.text)
    link = data["data"]["next"]["link"]
    return link

def get_system_page(session, system_url):
    session.get(system_url, headers=HEADERS, allow_redirects=True)
    return

def get_list_page(session):

    info_list = []

    for i in range(1,16):
        LIST_URL = f"https://zyfw.nankai.edu.cn/Index/huodong/p/{i}.aspx"
        response = session.get(LIST_URL, headers=HEADERS)
        main_tree = html.fromstring(response.text)
        
        activities = main_tree.xpath('//div[@class="huodong"]')

        for activity in activities:

            activity_list = []

            activity_page_URL = "https://zyfw.nankai.edu.cn" + str(activity.xpath('.//a/@href')[0])
            activity_page = session.get(activity_page_URL, headers=HEADERS)
            activity_tree = html.fromstring(activity_page.text)

            activity_name = activity_tree.xpath('//p[@class="hd-title"]')[0].text_content().strip()
            activation_organization = activity_tree.xpath('//div[@class="xinxi"]/table/tr[1]/td[2]')[0].text_content().strip()
            contact_person = activity_tree.xpath('//div[@class="xinxi"]/table/tr[4]/td[2]')[0].text_content().strip()
            contact_phone = activity_tree.xpath('//div[@class="xinxi"]/table/tr[5]/td[2]')[0].text_content().strip()
            activity_time = activity_tree.xpath('//div[@class="xinxi"]/table/tr[6]/td[2]')[0].text_content().strip()
            activity_location = activity_tree.xpath('//div[@class="xinxi"]/table/tr[7]/td[2]')[0].text_content().strip()
            activity_period = activity_tree.xpath('//div[@class="xinxi"]/table/tr[8]/td[2]')[0].text_content().strip()
            recruitment_number = activity_tree.xpath('//div[@class="xinxi"]/table/tr[9]/td[2]')[0].text_content().strip()

            if is_time_range_later_than_today(activity_time):
                activity_list.append(activity_name)
                activity_list.append(activation_organization)
                activity_list.append(contact_person)
                activity_list.append(contact_phone)
                activity_list.append(activity_time)
                activity_list.append(activity_location)
                activity_list.append(activity_period)
                activity_list.append(recruitment_number)

                info_list.append(activity_list)
    return info_list

def _start_time(time_str):
    """从 "2026-11-01 10:00 至 2026-11-15 16:00" 中取出开始时间，用于排序。"""
    match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2})', (time_str or '').strip())
    if match:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M")
    return datetime.max

def format_activities(info_list):
    """所有抓到的要素统一输出成 “标签：内容” 的形式。"""
    info_list = sorted(info_list, key=lambda item: _start_time(item[4]))
    lines = [f"共 {len(info_list)} 场活动："]
    for index, item in enumerate(info_list, 1):
        lines.append(f"{index}. {FIELDS[0]}：{item[0]}")
        for label, value in zip(FIELDS[1:], item[1:]):
            lines.append(f"   {label}：{value}")
    return "\n".join(lines)

def main():
    session = requests.Session()
    get_login_page(session)
    LOGIN_URL = "https://iam.nankai.edu.cn/api/v1/login?os=web"
    ENCRYPTED_PASSWORD = encrypt_password(PASSWORD)
    link = post_login(session, LOGIN_URL, USERNAME, ENCRYPTED_PASSWORD)
    SYSTEM_URL = "https://iam.nankai.edu.cn" + link + "&lang=zh-CN"
    get_system_page(session, SYSTEM_URL)
    info_list = get_list_page(session)
    print(format_activities(info_list))


if __name__ == '__main__':
    main()