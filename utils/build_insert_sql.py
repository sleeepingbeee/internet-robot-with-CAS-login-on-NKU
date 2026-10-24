# -*- coding: utf-8 -*-
"""build_insert_sql.py —— Dify「代码执行」节点：把去重后的活动文本转成一条 INSERT 语句。

上游
----
过滤节点输出的 text（形如 "共 24 场活动：\n1. 活动名称\n   组织机构：...\n   活动时间：..."）。
也兼容直接把 {"text": "..."} 整段当字符串传进来。

下游
----
把 sql 变量喂给 SQLite 插件「插入行（Insert Rows）」节点的 insert_sql 参数：
    INSERT OR IGNORE INTO activities (...) VALUES (...), (...), ...;
为什么用一条多行 INSERT 而不是每行一次：
  - 插件是 conn.execute()，一次只能执行一条语句，不能写多个分号；
  - INSERT OR IGNORE + UNIQUE(来源系统,活动名称,活动时间) 天然幂等，重复跑不会出重复行；
  - 没有活动时输出一条 "INSERT ... SELECT NULL WHERE 0;" 空语句，插件不会报错。

Dify 硬约束
-----------
1. 入口 def main(**kwargs) -> dict；
2. 输出变量声明 sql(String) / text(String) / count(Number) 三个；多一个少一个都会报
   "Not all output parameters are validated."；
3. 顶层不能有 print / 读写文件 / 自执行；
4. 只用标准库。

输入变量：text（String，接过滤节点的 text）
输出变量：sql（String，接插入行节点的 insert_sql）、text（String，给通知用）、count（Number，本批条数）

字段缺失时写 NULL；数据库里没有的字段（例如「链接」）自动丢弃。
"""

import datetime
import json
import re

_COLUMNS = ("来源系统", "活动名称", "活动时间", "活动地点", "组织机构", "联系人",
            "联系电话", "志愿时长", "招募人数", "活动类型", "活动学时", "抓取时间")

_TEXT_KEYS = ("text", "text1", "crawler_text", "filtered_text", "input_text",
              "内容", "活动文本")

_HEADER_RE = re.compile(r"^共\s*\d+\s*场活动\s*[:：]?\s*$")
_ENTRY_RE = re.compile(r"^(\d+)\s*[.、．)）]\s*(.*)$")
_KV_RE = re.compile(r"^([^:：]{1,12})\s*[:：]\s*(.*)$")
_NOTE_RE = re.compile(r"(抓取失败|没有抓取到|未抓取到|没有符合|暂无|没有活动|本次没有)")
_BLANK_CHARS = ("\ufeff", "\u200b", "\u200c", "\u200d", "\u3000", "\xa0")

EMPTY_SQL = 'INSERT OR IGNORE INTO activities ("活动名称") SELECT NULL WHERE 0;'

# 表里 NOT NULL 的列：缺了就不能写，只能跳过（INSERT OR IGNORE 也会整行丢弃）
_REQUIRED = ("来源系统", "活动名称", "活动时间", "抓取时间")


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


def _pick(kwargs, names):
    for name in names:
        if name in kwargs:
            value = kwargs[name]
            if value is not None and value != "":
                return value
    return ""


def _unwrap(raw):
    """模板里可能误传了整个 {"text": "..."}；能解就取里面的 text。"""
    text = _as_text(raw).strip()
    if text.startswith("{") and '"text"' in text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
                return parsed["text"]
        except Exception:
            pass
    return text


def _clean(value):
    text = _as_text(value)
    for ch in _BLANK_CHARS:
        text = text.replace(ch, " ")
    return re.sub(r"\s+", " ", text).strip()


def _parse_blocks(raw):
    """-> [[标题, {字段: 值}, ...], ...]"""
    blocks = []
    current = None
    for line in _unwrap(raw).splitlines():
        if not line.strip():
            continue
        stripped = line.strip()
        if _HEADER_RE.match(stripped):
            continue
        if line[0].isspace():
            if current is None:
                continue
            match = _KV_RE.match(stripped)
            if match is not None:
                key = _clean(match.group(1))
                value = _clean(match.group(2))
                if value:
                    current[1][key] = value
            continue
        entry = _ENTRY_RE.match(stripped)
        if entry is not None:
            current = [_clean(entry.group(2)), {}]
        elif _NOTE_RE.search(stripped):
            # "本次没有抓取到…" / "抓取失败：…" 之类的提示不是活动
            current = None
            continue
        else:
            current = [_clean(stripped), {}]
        blocks.append(current)
    return blocks


def _guess_system(fields, title):
    """根据字段组合判断来源系统。"""
    if fields.get("活动类型") or fields.get("活动学时"):
        return "辅学系统"
    link = fields.get("链接", "")
    if fields.get("组织机构") or fields.get("志愿时长") or "zyfw" in link:
        return "志愿系统"
    return "未知系统"


def _grab_time():
    """抓取时间：Asia/Shanghai（UTC+8），用带时区对象计算，容器时区不影响。"""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    return now.strftime("%Y-%m-%d %H:%M:%S")


def _sql_literal(value):
    if value is None or value == "":
        return "NULL"
    return "'" + _as_text(value).replace("'", "''") + "'"


def build_rows(raw_text, grab_time=""):
    """文本 -> ([{列: 值}], 跳过条数)。缺 NOT NULL 字段的条目只能跳过。"""
    grab_time = _clean(grab_time) or _grab_time()
    rows = []
    skipped = 0
    seen = set()
    blocks = _parse_blocks(raw_text)
    # 过滤节点已经去过重，这里再按「来源系统+名称+时间」兜一次，防止同批重复
    for title, fields in blocks:
        name = _clean(title)
        if not name:
            continue
        row = {column: None for column in _COLUMNS}
        row["来源系统"] = _guess_system(fields, name)
        row["活动名称"] = name
        for column in ("活动时间", "活动地点", "组织机构", "联系人", "联系电话",
                       "志愿时长", "招募人数", "活动类型", "活动学时"):
            row[column] = fields.get(column) or None
        row["抓取时间"] = grab_time
        # 活动时间 / 活动名称 等都是 NOT NULL，缺了就写不进去
        if any(not row.get(column) for column in _REQUIRED):
            skipped += 1
            continue
        key = (row["来源系统"], row["活动名称"], row["活动时间"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows, skipped


def build_sql(rows):
    """-> 一条 INSERT OR IGNORE 语句；rows 为空时返回空操作语句。"""
    if not rows:
        return EMPTY_SQL
    column_sql = ", ".join('"%s"' % column for column in _COLUMNS)
    value_sql = ",\n".join(
        "(" + ", ".join(_sql_literal(row.get(column)) for column in _COLUMNS) + ")"
        for row in rows
    )
    return 'INSERT OR IGNORE INTO activities (%s) VALUES\n%s;' % (column_sql, value_sql)


def build_summary(rows, skipped=0):
    total = len(rows)
    if total == 0:
        base = "共 0 条待写入：数据库里都已有，无需插入。"
    else:
        counter = {}
        for row in rows:
            system = row["来源系统"]
            counter[system] = counter.get(system, 0) + 1
        detail = " / ".join("%s %d 条" % (key, value) for key, value in counter.items())
        base = "共 %d 条待写入（%s）。" % (total, detail)
    if skipped:
        base += "另有 %d 条缺少「活动时间」等必填字段，已跳过。" % skipped
    return base


def main(**kwargs):
    """Dify 代码节点入口。"""
    text = ""
    sql = EMPTY_SQL
    count = 0
    try:
        kwargs = kwargs or {}
        text = _as_text(_pick(kwargs, _TEXT_KEYS))
        rows, skipped = build_rows(text, _pick(kwargs, ("grab_time", "now_time", "当前时间")))
        sql = build_sql(rows)
        count = len(rows)
        summary = build_summary(rows, skipped)
    except Exception as err:  # 任何异常都不让工作流挂掉
        sql = EMPTY_SQL
        count = 0
        summary = "生成插入 SQL 失败：%s: %s" % (type(err).__name__, err)
    return {"sql": sql, "text": summary, "count": count}
