#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sanitize.py - 纯本地聊天记录脱敏工具

设计原则（不可妥协）:
  1. 绝对本地: 只 import Python 标准库 re/sys/argparse/collections,
     没有任何网络调用、没有 AI、没有文件上传。可断网运行。
  2. 确定性: 同样输入永远同样输出,可逐行审计,无随机、无状态。
  3. 保守: 只有校验算法能确认的高置信度规则才替换;
     地址/微信号/QQ号等易误报的只统计报告,不改动。
  4. 保形: 替换后保留格式骨架(如 138****5678),
     让 AI 仍能理解"这是个手机号/卡号",不破坏总结语义。

对外接口:
  sanitize_text(text)                -> str            纯函数,保守模式
  analyze(text)                      -> dict           只报告不修改
  sanitize_with_report(text)         -> (str, dict)    两者都要

命令行:
  python sanitize.py check  <file>              只看报告,不动文件
  python sanitize.py dryrun <file> [行数]        打印替换前后对照(不写文件)
  python sanitize.py clean  <file> -o <out>      写脱敏副本(原件绝不改动)

注意: 本脚本从不删除、覆盖任何原始文件;clean 模式只写一个新的副本。
"""
import re
import sys
import argparse
from collections import Counter

# ---------------------------------------------------------------- 校验算法

def luhn_ok(digits):
    """Luhn 校验: 银行卡/信用卡号的通用校验位算法,能砍掉绝大多数数字误报。"""
    if not digits.isdigit() or len(digits) < 13:
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def idcard_ok(s):
    """中国大陆身份证 18 位校验: 出生日期合法性 + MOD 11-2 校验位。"""
    s = s.upper()
    if len(s) != 18 or not re.fullmatch(r"\d{17}[\dX]", s):
        return False
    year, month, day = int(s[6:10]), int(s[10:12]), int(s[12:14])
    if not (1900 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31):
        return False
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    codes = "10X98765432"
    total = sum(int(s[i]) * weights[i] for i in range(17))
    return codes[total % 11] == s[17]


# ---------------------------------------------------------------- 正则规则

PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
IDCARD_RE = re.compile(
    r"(?<![0-9Xx])([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?![0-9Xx])"
)
BANK_CAND_RE = re.compile(r"(?<!\d)(\d{16,19})(?!\d)")
# 带分隔符的卡号: 4位一组、3-5组、空格或横线分隔(总长16-19位)
BANK_SPLIT_RE = re.compile(r"(?<![\d])((?:[0-9]{4}[ -]){2,4}[0-9]{3,4})(?![\d])")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
SECRET_RE = re.compile(r"(?i)(密码|验证码|口令|password|passwd|pin)\s*[是为:：]{0,3}\s*(\d{4,8})(?!\d)")

# 只报告不替换
ADDR_RE = re.compile(r"[\u4e00-\u9fa5]{2,6}(?:省|市|区|县)[\u4e00-\u9fa5\d]{0,16}(?:路|街|号|室|栋|单元)\d{0,6}")
WXID_RE = re.compile(r"(?i)(wxid_[A-Za-z0-9_\-]{5,20})")
QQ_RE = re.compile(r"(?<!\d)([1-9]\d{4,10})(?!\d)")

PRIORITY = {"email": 10, "idcard": 9, "bank": 8, "phone": 7, "secret": 6}

# ---------------------------------------------------------------- 掩码

def _mask(cat, s):
    if cat == "phone":
        return s[:3] + "****" + s[-4:]
    if cat == "bank":
        return "**** **** **** " + s[-4:]
    if cat == "idcard":
        return s[0] + "*" * (len(s) - 2) + s[-1]
    if cat == "email":
        user, domain = s.split("@", 1)
        return (user[0] if user else "") + "***@" + domain
    if cat == "secret":
        return "*" * 6
    return "[已脱敏]"


# ---------------------------------------------------------------- 核心

def _collect(text):
    """收集所有高置信度命中,解决重叠后返回不重叠的替换列表。"""
    hits = []

    def add(start, end, cat, value):
        hits.append((start, end, cat, value))

    for m in EMAIL_RE.finditer(text):
        add(m.start(), m.end(), "email", m.group(0))

    for m in IDCARD_RE.finditer(text):
        if idcard_ok(m.group(1)):
            add(m.start(), m.end(), "idcard", m.group(1))

    for m in BANK_CAND_RE.finditer(text):
        d = m.group(1)
        if luhn_ok(d):
            add(m.start(), m.end(), "bank", d)

    for m in BANK_SPLIT_RE.finditer(text):
        raw = m.group(1)
        d = re.sub(r"[^0-9]", "", raw)
        if 16 <= len(d) <= 19 and luhn_ok(d):
            add(m.start(), m.end(), "bank", raw)

    for m in PHONE_RE.finditer(text):
        add(m.start(), m.end(), "phone", m.group(1))

    for m in SECRET_RE.finditer(text):
        s, e = m.span(2)
        add(s, e, "secret", m.group(2))

    # 重叠解决: 优先级高 > 长度长 > 位置靠前
    hits.sort(key=lambda h: (-PRIORITY[h[2]], -(h[1] - h[0]), h[0]))
    chosen = []
    occupied = []
    for h in hits:
        s, e, cat, val = h
        if any(s < oe and os < e for os, oe in occupied):
            continue
        chosen.append(h)
        occupied.append((s, e))

    chosen.sort(key=lambda h: h[0])
    return chosen


def sanitize_with_report(text, mode="conservative"):
    """脱敏并返回 (脱敏后文本, 报告)。mode 目前只有 conservative。"""
    chosen = _collect(text)
    items = []
    pieces = []
    pos = 0
    for s, e, cat, val in chosen:
        pieces.append(text[pos:s])
        masked = _mask(cat, val)
        pieces.append(masked)
        items.append({"类别": cat, "原文": val, "替换为": masked})
        pos = e
    pieces.append(text[pos:])
    new_text = "".join(pieces)

    counts = Counter(i["类别"] for i in items)
    report = {
        "替换总数": len(items),
        "分类统计": dict(counts),
        "替换明细": items,
        "仅报告未替换": {
            "地址候选": len(ADDR_RE.findall(text)),
            "微信号候选": len(WXID_RE.findall(text)),
            "QQ号候选": len(QQ_RE.findall(text)),
        },
    }
    return new_text, report


def sanitize_text(text):
    """对外主接口: 纯函数,输入原文,输出脱敏文本。"""
    return sanitize_with_report(text)[0]


def analyze(text):
    """只报告不脱敏,用于评估某份文本的敏感信息分布。"""
    return sanitize_with_report(text)[1]


# ---------------------------------------------------------------- CLI

def _cmd_check(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    _, report = sanitize_with_report(text)
    print("=== 敏感信息报告(仅统计,未改动文件) ===")
    print("替换总数:", report["替换总数"])
    print("分类统计:", report["分类统计"])
    print("仅报告未替换:", report["仅报告未替换"])
    print("\n前 20 条替换示例:")
    for i in report["替换明细"][:20]:
        print("  [%s] %s -> %s" % (i["类别"], i["原文"], i["替换为"]))


def _cmd_dryrun(path, limit):
    with open(path, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    new_text, report = sanitize_with_report(text)
    print("=== 脱敏前后对照(不写文件) ===")
    print("替换总数:", report["替换总数"], "|", report["分类统计"])
    print("-" * 60)
    shown = 0
    for i in report["替换明细"]:
        if shown >= limit:
            break
        print("  [%s] %s  ->  %s" % (i["类别"], i["原文"], i["替换为"]))
        shown += 1
    print("-" * 60)
    print("脱敏后文本前 400 字:")
    print(new_text[:400])


def _cmd_clean(path, out):
    with open(path, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    new_text, report = sanitize_with_report(text)
    with open(out, "w", encoding="utf-8") as f:
        f.write(new_text)
    print("已写脱敏副本:", out)
    print("替换总数:", report["替换总数"], "|", report["分类统计"])
    print("原件未改动:", path)


def main():
    p = argparse.ArgumentParser(description="纯本地聊天记录脱敏工具")
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("check", help="只看报告")
    c.add_argument("file")
    d = sub.add_parser("dryrun", help="打印前后对照")
    d.add_argument("file")
    d.add_argument("limit", nargs="?", type=int, default=30)
    cl = sub.add_parser("clean", help="写脱敏副本")
    cl.add_argument("file")
    cl.add_argument("-o", "--out", required=True)
    args = p.parse_args()
    if args.cmd == "check":
        _cmd_check(args.file)
    elif args.cmd == "dryrun":
        _cmd_dryrun(args.file, args.limit)
    elif args.cmd == "clean":
        _cmd_clean(args.file, args.out)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
