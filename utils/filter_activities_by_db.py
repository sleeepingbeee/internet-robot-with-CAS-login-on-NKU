# -*- coding: utf-8 -*-
"""filter_activities_by_db.py —— Dify「代码执行」节点：按数据库已有活动名称过滤爬虫结果。

用途
----
上游两个节点：
  text1  爬虫节点输出的 text，形如
         "共 N 场活动：\n1. 活动名称\n   组织机构：...\n   活动时间：..."
  text2  SQLite 插件「查询行（Select Rows）」节点的输出，两种都认：
         - text 变量："Table: activities\nColumns: ...\nRows: 26\nData: [{'活动名称': ...}, ...]"
           （注意 Data 里是 Python repr，单引号 / None，不是 JSON）
         - json 变量：{"query": ..., "table": ..., "columns": [...], "data": [...], "row_count": 26, "status": "success"}

处理逻辑
--------
按「活动名称」比对（忽略所有空白字符、全角空格、大小写差异）：
数据库里已经存在的整条丢弃，剩下的按**原来的文本格式**重新从 1 编号输出，
仍然是一段字符串。抓虫结果为空 / 数据库为空 / text2 解析失败时都不会丢数据。

Dify 硬约束（改代码时别破坏）
----------------------------
1. 入口必须是 def main(**kwargs) -> dict；
2. 只返回 {"text": ...} 这一个键 → 节点「输出变量」只声明 text（String）。
   多返回一个键会报 "Not all output parameters are validated."；
3. 模块顶层不能有 print / 自执行 / 文件读写，否则可能 "Output result is missing."；
4. 只依赖标准库（json / re / ast）。

节点输入变量（在 Dify 里声明，再把上游输出映射进来）
----------------------------------------------------
    text1  String   爬虫节点输出的 text
    text2  String   「查询行」节点的 text（或 json）

节点输出变量（只声明这一个）：
    text   String
"""

import ast
import json
import re

# ---------------------------------------------------------------------------
# 输入变量别名（Dify 里变量名随习惯而定，多兜几个，取第一个非空的）
# ---------------------------------------------------------------------------
_TEXT1_KEYS = ("text1", "crawler_text", "spider_text", "new_text",
               "text_a", "a_text", "text_1", "爬虫text", "抓取结果")
_TEXT2_KEYS = ("text2", "db_text", "database_text", "existing_text", "db_result",
               "text_b", "b_text", "text_2", "数据库text", "已有数据")

_HEADER_RE = re.compile(r"^共\s*\d+\s*场活动\s*[:：]?\s*$")   # "共 49 场活动："
_ENTRY_RE = re.compile(r"^(\d+)\s*[.、．)）]\s*(.*)$")         # "1. 活动名称"
_NOTE_RE = re.compile(r"(抓取失败|没有抓取到|未抓取到|没有符合|暂无|没有活动|本次没有)")
_DATA_RE = re.compile(r"\bData\s*[:：]\s*", re.S)              # "Data: [...]"

UNNAMED_ACTIVITY = "（未命名活动）"
EMPTY_TEXT = "共 0 场活动："

# 比对时忽略的不可见 / 空白字符
_BLANK_CHARS = ("\ufeff", "\u200b", "\u200c", "\u200d", "\u3000", "\xa0")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _as_text(value):
    """任意值 -> str（None 变空串，绝不抛异常）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


def _collapse(value):
    """多行/连续空白压成一个空格并去首尾空白。"""
    return re.sub(r"\s+", " ", _as_text(value)).strip()


def _name_key(value):
    """活动名称归一化：去掉所有空白（含全角空格、BOM/零宽），统一大小写。"""
    text = _as_text(value)
    for ch in _BLANK_CHARS:
        text = text.replace(ch, " ")
    return re.sub(r"\s+", "", text).strip().casefold()


def _pick(kwargs, names):
    """按别名顺序取第一个非空参数；都没有返回空串。"""
    for name in names:
        if name in kwargs:
            value = kwargs[name]
            if value is not None and value != "":
                return value
    return ""


# ---------------------------------------------------------------------------
# 1. 解析爬虫文本 -> [(标题, [字段行, ...]), ...]
# ---------------------------------------------------------------------------
def _parse_crawler_text(raw):
    blocks = []
    current = None
    for line in _as_text(raw).splitlines():
        if not line.strip():
            continue
        stripped = line.strip()
        # 汇总行："共 N 场活动：" 跳过（总数由本节点重新统计）
        if _HEADER_RE.match(stripped):
            continue
        # 缩进行：当前活动的 "字段：值"
        if line[0].isspace():
            if current is not None and stripped:
                current[1].append(stripped)
            continue
        # 顶格编号行：一场新活动
        entry = _ENTRY_RE.match(stripped)
        if entry is not None:
            current = [entry.group(2).strip(), []]
            blocks.append(current)
            continue
        # "抓取失败：…" / "本次没有抓取到…" 之类的提示行：不是活动，跳过
        if _NOTE_RE.search(stripped):
            current = None
            continue
        # 顶格但没编号：也当成一场活动，避免漏数据
        current = [stripped, []]
        blocks.append(current)
    return blocks


# ---------------------------------------------------------------------------
# 2. 渲染回原来的文本格式
# ---------------------------------------------------------------------------
def _render(blocks):
    cleaned = []
    for title, details in blocks:
        title = _collapse(title) or UNNAMED_ACTIVITY
        lines = [d for d in (_collapse(line) for line in details) if d]
        cleaned.append((title, lines))
    if not cleaned:
        return EMPTY_TEXT
    output = ["共 %d 场活动：" % len(cleaned)]
    for index, (title, lines) in enumerate(cleaned, 1):
        output.append("%d. %s" % (index, title))
        for line in lines:
            output.append("   %s" % line)
    return "\n".join(output)


# ---------------------------------------------------------------------------
# 3. 解析数据库文本 -> list[dict]
# ---------------------------------------------------------------------------
def _rows_from_obj(obj):
    """把 json / literal_eval 的结果统一成「列表」；认不出来返回 None。"""
    if isinstance(obj, dict):
        for key in ("data", "rows", "records", "data_list"):
            value = obj.get(key)
            if isinstance(value, list):
                return value
        if any(key in obj for key in ("活动名称", "名称", "name", "title")):
            return [obj]
        return None
    if isinstance(obj, list):
        return obj
    return None


def _parse_db_rows(db_text):
    text = _as_text(db_text).strip()
    if not text:
        return None
    # a) 插件 json 变量 / 直接是 JSON 数组
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    rows = _rows_from_obj(parsed) if parsed is not None else None
    if rows is not None:
        return rows
    # b) 插件 text 变量："... Data: [{'活动名称': ...}]"（Python repr）
    match = _DATA_RE.search(text)
    if match is not None:
        payload = text[match.end():].strip()
        try:
            parsed = ast.literal_eval(payload)
        except Exception:
            parsed = None
        rows = _rows_from_obj(parsed)
        if rows is not None:
            return rows
    # c) 整段就是 Python repr 的列表
    try:
        parsed = ast.literal_eval(text)
    except Exception:
        parsed = None
    return _rows_from_obj(parsed)


def _db_name_keys(db_text):
    """数据库里已有的活动名称集合；无法解析时返回 None（表示「不知道」，不过滤）。"""
    rows = _parse_db_rows(db_text)
    if rows is None:
        return None
    keys = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for column in ("活动名称", "名称", "name", "title"):
            if column in row:
                key = _name_key(row.get(column))
                if key:
                    keys.add(key)
                break
    return keys


# ---------------------------------------------------------------------------
# 4. 纯函数：过滤 + 渲染
# ---------------------------------------------------------------------------
def filter_activities(crawler_text="", db_text=""):
    """爬虫文本里去掉数据库已有活动，返回同样格式的字符串。"""
    blocks = _parse_crawler_text(crawler_text)
    if not blocks:
        # 上游是 "本次没有抓取到…" / "抓取失败：…" 之类的提示：原样返回
        return _as_text(crawler_text).strip() or EMPTY_TEXT
    db_keys = _db_name_keys(db_text)
    if db_keys is None:
        # 没拿到数据库数据 / 解析失败：不过滤，保证不丢活动
        return _render(blocks)
    kept = [block for block in blocks if _name_key(block[0]) not in db_keys]
    return _render(kept)


# ---------------------------------------------------------------------------
# 5. Dify 入口
# ---------------------------------------------------------------------------
def main(**kwargs):
    """Dify 代码节点入口：永远返回 {"text": "..."}，不抛异常。"""
    crawler_text = ""
    text = EMPTY_TEXT
    try:
        kwargs = kwargs or {}
        crawler_text = _as_text(_pick(kwargs, _TEXT1_KEYS))
        db_text = _as_text(_pick(kwargs, _TEXT2_KEYS))
        text = filter_activities(crawler_text, db_text)
    except Exception:
        # 兜底：至少把爬虫原文还回去，绝不因为过滤失败把活动弄丢
        text = crawler_text.strip() or EMPTY_TEXT
    # 只返回节点里声明过的键，多一个少一个都会让 Dify 报
    # "Not all output parameters are validated."
    return {"text": text}


# ---------------------------------------------------------------------------
# 部署到 Dify「代码执行」节点：
#   1) 整份文件粘进节点（Python 3，纯标准库）；
#   2) 输出变量只声明一个：text（String）；
#   3) 输入变量声明 text1、text2（String），分别映射爬虫节点.text
#      和「查询行」节点的 text（或 json）；
#   4) 返回空时是 "共 0 场活动："，下游可直接判断或照常推送。
# ---------------------------------------------------------------------------
