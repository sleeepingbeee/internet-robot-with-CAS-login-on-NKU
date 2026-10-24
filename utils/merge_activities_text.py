# -*- coding: utf-8 -*-
"""merge_activities_text.py —— Dify「代码执行」节点：把两个抓取节点输出的 text 合并成一段文本。

用途
----
上游两个代码节点（zyfw 志愿系统 cas_core.py / 辅学平台 CAS 内核）各自输出一份形如

    共 N 场活动：
    1. 活动名称
       活动时间：...
       活动地点：...

的 text。本节点把两份 text 合并成**一段**同样格式的 text：

    共 (N1 + N2) 场活动：
    1. 活动名称
       ...
    ...

活动条目按「先第一个节点、后第二个节点」的顺序拼接，序号由本节点统一重排。

Dify 硬约束（改代码时别破坏）
-----------------------------
1. 入口必须是 ``def main(**kwargs) -> dict``；
2. 返回 dict 的键必须和节点里声明的「输出变量」**完全一致** —— 本节点只返回
   ``{"text": ...}``，所以节点里只声明一个输出变量 text（String）。多返回一个键会报
   ``Not all output parameters are validated.``；
3. 模块顶层不能有 print / 自执行 / 文件读写（Dify 会把整段代码内联进自己的 runner，
   任何自执行块都可能导致 ``Output result is missing.``）；
4. 只依赖标准库。

节点输入变量（在 Dify 里声明，再把上游节点的输出映射进来）
----------------------------------------------------------
必填（二选一，推荐同时映射 text）：
    text1  String   第一个抓取节点（zyfw）输出的 text
    text2  String   第二个抓取节点（辅学平台）输出的 text
可选（结构化输入优先于 text 解析，避免二次解析）：
    activities1  Array[Object]   第一个抓取节点的 activities
    activities2  Array[Object]   第二个抓取节点的 activities

另外兼容 text_a/text_b、activities_a/activities_b 等别名；上游没开 include_text 时
只传 activities1 / activities2 也可以。

节点输出变量（只声明这一个）：
    text  String
"""

import json
import re

# ============================================================================
# 配置区
# ============================================================================
# 合并后每场活动的字段顺序：先两个平台共有的字段，再各自的特色字段。
# 该顺序同时适用于「text 解析」和「activities 结构化渲染」两条路径。
FIELD_ORDER = (
    "活动名称",
    "组织机构", "联系人", "联系电话",
    "活动时间", "活动地点",
    "志愿时长", "招募人数",
    "活动类型", "活动学时",
    "链接",
)

# 字段别名：两个内核里同一含义的不同叫法都兜住（如 发起组织/组织机构、
# 联系人电话/联系电话、拟招募人数/招募人数、学时/学分）。
FIELD_ALIASES = {
    "活动名称": ("活动名称", "标题", "name", "title"),
    "组织机构": ("组织机构", "发起组织", "主办单位"),
    "联系人": ("联系人",),
    "联系电话": ("联系电话", "联系人电话"),
    "活动时间": ("活动时间", "举办时间", "活动日期"),
    "活动地点": ("活动地点", "举办地点", "活动场地"),
    "志愿时长": ("志愿时长",),
    "招募人数": ("招募人数", "拟招募人数"),
    "活动类型": ("活动类型", "活动类别"),
    "活动学时": ("活动学时", "实践学时", "学时", "学分"),
    "链接": ("链接", "详情页链接", "url", "href"),
}

# kwargs 里不同命名的别名表（Dify 里变量名随用户习惯而定，多兜几个）
_TEXT1_KEYS = ("text1", "text_a", "a_text", "text_1")
_TEXT2_KEYS = ("text2", "text_b", "b_text", "text_2")
_ACT1_KEYS = ("activities1", "activities_a", "a_activities", "activities_1")
_ACT2_KEYS = ("activities2", "activities_b", "b_activities", "activities_2")

# 行级解析规则
_HEADER_RE = re.compile(r"^共\s*\d+\s*场活动\s*[:：]?\s*$")        # "共 5 场活动："
_ENTRY_RE = re.compile(r"^(\d+)\s*[.、．)）]\s*(.*)$")              # "1. 活动名称"
_NOTE_RE = re.compile(r"(抓取失败|没有抓取到|未抓取到|没有符合|暂无|没有活动|本次没有)")
_EMPTY_VALUE_RE = re.compile(r"^[^:：]{1,30}[:：]\s*$")             # "组织机构：" 这种空值行

UNNAMED_ACTIVITY = "（未命名活动）"
EMPTY_TEXT = "共 0 场活动："


# ============================================================================
# 小工具
# ============================================================================
def _as_text(value):
    """任意值 -> str（None 变成空串，绝不抛异常）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


def _collapse(value):
    """压缩空白，顺便去掉首尾空白。"""
    return re.sub(r"\s+", " ", _as_text(value)).strip()


def _pick(kwargs, names):
    """按别名顺序从 kwargs 里取第一个非空的参数；都没有返回 None。"""
    for name in names:
        if name in kwargs:
            value = kwargs[name]
            if value is not None and value != "":
                return value
    return None


# ============================================================================
# 路径一：从上游返回的 activities 数组渲染（首选，结构稳定）
# ============================================================================
def _blocks_from_activities(items):
    """list[dict] -> [(标题, [字段行, ...]), ...]。"""
    blocks = []
    if not isinstance(items, list):
        return blocks
    for item in items:
        if not isinstance(item, dict):
            continue
        title = ""
        details = []
        for key in FIELD_ORDER:
            value = ""
            for alias in FIELD_ALIASES.get(key, (key,)):
                candidate = _collapse(item.get(alias))
                if candidate:
                    value = candidate
                    break
            if key == "活动名称":
                title = value
            elif value:
                details.append("%s：%s" % (key, value))
        if not title and not details:
            continue
        blocks.append((title or UNNAMED_ACTIVITY, details))
    return blocks


# ============================================================================
# 路径二：从上游返回的 text 解析（上游没把 activities 传进来时用）
# ============================================================================
def _blocks_from_text(raw):
    """把 "共 N 场活动：1. 名称\\n   字段：值 ..." 解析成 [(标题, [字段行...]), ...]。"""
    blocks = []
    current = None
    for line in _as_text(raw).splitlines():
        if not line.strip():
            continue
        # 1) 汇总行：跳过（总数由本节点重新统计）
        if _HEADER_RE.match(line.strip()):
            continue
        # 2) 缩进行：属于当前活动的 "字段：值"
        if line[0].isspace():
            if current is not None:
                detail = _collapse(line)
                if detail and not _EMPTY_VALUE_RE.match(detail):
                    current[1].append(detail)
            continue
        # 3) 顶格编号行：一场新活动
        entry = _ENTRY_RE.match(line.strip())
        if entry is not None:
            current = [_collapse(entry.group(2)), []]
            blocks.append(current)
            continue
        text = _collapse(line)
        if not text:
            continue
        # 4) 抓取失败 / 无结果之类的提示行：不是活动，跳过
        if _NOTE_RE.search(text):
            current = None
            continue
        # 5) 顶格但没有编号的标题行：也当成一场活动，避免漏数据
        current = [text, []]
        blocks.append(current)
    return blocks


def _source_blocks(kwargs, activity_keys, text_keys):
    """单个上游节点的数据 -> blocks。非空 activities 数组优先，其次 text。"""
    activities = _pick(kwargs, activity_keys)
    if isinstance(activities, list) and activities:
        return _blocks_from_activities(activities)
    if isinstance(activities, str) and activities:
        # Dify 有时会把 Array 变量序列化成 JSON 字符串传进来
        try:
            parsed = json.loads(activities)
        except Exception:
            parsed = None
        if isinstance(parsed, list) and parsed:
            return _blocks_from_activities(parsed)
    raw = _pick(kwargs, text_keys)
    if not isinstance(raw, str):        # 没传 text / 类型不对：视为该来源为空
        return []
    return _blocks_from_text(raw)


# ============================================================================
# 合并渲染
# ============================================================================
def _render(blocks):
    """[(标题, [字段行...]), ...] -> "共 N 场活动：..." 文本。"""
    cleaned = []
    for title, details in blocks or ():
        title = _collapse(title) or UNNAMED_ACTIVITY
        lines = []
        for detail in details or ():
            detail = _collapse(detail)
            if detail and not _EMPTY_VALUE_RE.match(detail):
                lines.append(detail)
        cleaned.append((title, lines))

    if not cleaned:
        return EMPTY_TEXT

    output = ["共 %d 场活动：" % len(cleaned)]
    for index, (title, details) in enumerate(cleaned, 1):
        output.append("%d. %s" % (index, title))
        for detail in details:
            output.append("   %s" % detail)
    return "\n".join(output)


def merge(text1="", text2="", activities1=None, activities2=None):
    """纯函数：合并两份活动数据，返回合并后的文本（供本地测试/复用）。"""
    kwargs = {"text1": text1, "text2": text2,
              "activities1": activities1, "activities2": activities2}
    blocks = []
    blocks.extend(_source_blocks(kwargs, _ACT1_KEYS, _TEXT1_KEYS))
    blocks.extend(_source_blocks(kwargs, _ACT2_KEYS, _TEXT2_KEYS))
    return _render(blocks)


# ============================================================================
# Dify 入口
# ============================================================================
def main(**kwargs):
    """Dify 代码节点入口：永远返回 {"text": "..."}，不抛异常。

    可用输入变量：text1 / text2（String）、activities1 / activities2（Array[Object]）。
    """
    try:
        kwargs = kwargs or {}
        blocks = []
        blocks.extend(_source_blocks(kwargs, _ACT1_KEYS, _TEXT1_KEYS))
        blocks.extend(_source_blocks(kwargs, _ACT2_KEYS, _TEXT2_KEYS))
        text = _render(blocks)
    except Exception:
        text = EMPTY_TEXT
    # 只返回节点里声明过的键，多一个少一个都会让 Dify 报
    # "Not all output parameters are validated."
    return {"text": text}


# ============================================================================
# 部署到 Dify「代码执行」节点：
#   1) 整份文件粘进节点（Python 3，纯标准库，无需 requests）；
#   2) 输出变量只声明一个：text（String）；
#   3) 输入变量（都可选）：text1、text2、activities1、activities2；
#      最省事的接法：text1 = 上游节点1的 text，text2 = 上游节点2的 text；
#   4) 上游节点必须打开 include_text（两个内核默认都是 True），否则改为映射
#      activities1 / activities2。
# ============================================================================
