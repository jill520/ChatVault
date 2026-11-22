#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
archive_parse.py - 归档 Markdown 的统一解析层

归档里存在三种真实行格式（历史产物，不再改归档）：

  A  QQ 主库      - **15:51:14 我**: 打印了
  B  QQ 增量      - **2026-11-18 09:47:20** [昵称(123456789)] 两份
  C  微信         - **15:24:56** [-°c] 已经联系顺丰了 [text]

另外还有两类"伪数据"混在正文里，必须在展示层拦掉：
  - 富文本 protobuf 泄漏：{"align":"center","items":[...   （且多为截断的半截 JSON）
  - 小程序/链接卡片 XML：<?xml version="1.0"?><msg><appmsg>...
  - [未知消息] / 空发送者

本模块只做"把一行变成结构化字段"，不做任何 IO 副作用、不写文件。
所有展示层（搜索卡片、会话气泡、AI 上下文）都只吃这里输出的字段，
禁止再直接吐 Markdown 原始行。
"""
import os
import re
import json

# ---------------------------------------------------------------- 正则

# - **15:51:14 我**: 文本      /  - **2026-11-18 09:47:20** [名] 文本
LINE_RE = re.compile(r"^-\s*\*\*(?:(\d{4}-\d{2}-\d{2})\s+)?(\d{1,2}:\d{2}:\d{2})\s*(.*?)\*\*(.*)$")
DATE_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})")
YM_RE = re.compile(r"(\d{4})-(\d{2})\.md$")
# 文件名里的会话名：昵称(QQ号)_随机后缀  /  QQ号  /  u_uid
SUFFIX_RE = re.compile(r"_[A-Za-z0-9\-_]{6,12}$")
NAME_QQ_RE = re.compile(r"^(.*?)\s*[（(](\d{5,12})[)）]\s*$")
MY_NAME = "我"

# 微信/增量行尾的类型标记，纯噪音，展示前剥掉
TAIL_TAG_RE = re.compile(r"\s*\[(?:text|系统|sticker|pic|reply\+text|引用|图片|语音|视频|文件|位置|链接)\]\s*$", re.I)
# 回复引用： [回复「原文」] / [引用] —— 抽出来单放，气泡里做引用块
REPLY_RE = re.compile(r"^\[回复[「\"']?(.*?)[」\"']?\]\s*")
REPLY_TAIL_RE = re.compile(r"\s*\[reply\+text\]\s*$")

XML_RE = re.compile(r"^\s*(?:<\?xml|<msg>|<appmsg)")
XML_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
JSON_RE = re.compile(r'^\s*\{\s*"')
UNKNOWN_TAG_RE = re.compile(r"^\s*\[未知消息\]\s*$")
SYS_TAIL_RE = re.compile(r"\s*\[系统\]\s*$")
# 纯占位符：[图片] [表情包] [动画表情][动画表情] [语音] ...
MEDIA_ONLY_RE = re.compile(r"^\s*(?:\[[^\[\]]{1,8}\]\s*)+$")

TAG_CN = {"sticker": "表情", "pic": "图片", "text": "", "reply+text": "",
          "img": "图片", "voice": "语音", "video": "视频", "file": "文件"}

MEDIA_TAGS = ("图片", "表情", "动画表情", "语音", "视频", "文件", "位置", "名片",
              "红包", "转账", "链接", "小程序", "音乐", "拍一拍", "撤回")


# ---------------------------------------------------------------- 文本归一

def clean_name(s):
    """目录名/昵称里的换行、制表、连续空白一律压成单空格。"""
    if not s:
        return ""
    s = re.sub(r"[\r\n\t\u200b\u200c\u200d\ufeff]+", " ", str(s))
    s = "".join(ch for ch in s if ch == " " or ord(ch) >= 32)  # 去掉目录名里的控制字符
    # 目录名/昵称里常夹着 <tianhai1012606911@qq.com> 这种邮箱标签，去掉
    s = re.sub(r"<[^<>]{0,60}@[^<>]{0,60}>", "", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def _clip(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------- 发送者解析

class Resolver:
    """把发送者标签翻译成人话：QQ号→昵称、u_uid→昵称、wxid→短号。

    数据源：uid_map.json（uid → {nick, remark, qq}）、group_map.json（群号 → 群名）。
    两个文件由 sync_qq.py 维护，这里只读。
    """

    def __init__(self, base_dir):
        self.uid = {}
        self.uid_prefix = {}
        self.qq2name = {}
        self.groups = {}
        self.my_qq = ""
        self._load(base_dir)

    def _load(self, base_dir):
        try:
            with open(os.path.join(base_dir, "uid_map.json"), encoding="utf-8") as f:
                um = json.load(f)
        except Exception:
            um = {}
        for uid, info in (um or {}).items():
            if not isinstance(info, dict):
                continue
            self.uid[uid] = info
            # 旧格式把发送者截断过（12/16/18/20 字符都出现过），注册前缀以便反查
            for n in (12, 14, 16, 18, 20):
                if len(uid) > n:
                    self.uid_prefix.setdefault(uid[:n], info)
            qq = str(info.get("qq") or "").strip()
            if qq:
                nm = clean_name(info.get("remark") or info.get("nick") or "")
                if nm and qq not in self.qq2name:
                    self.qq2name[qq] = nm
        try:
            with open(os.path.join(base_dir, "group_map.json"), encoding="utf-8") as f:
                gm = json.load(f)
            self.groups = {str(k): clean_name(re.sub(r"<[^<>]{0,10}>", "", str(v)))
                           for k, v in (gm or {}).items()}
        except Exception:
            self.groups = {}
        try:
            import local_config as cfg
            self.my_qq = clean_name(cfg.qq_uin() or "")
        except Exception:
            self.my_qq = ""

    def learn_from_dirs(self, roots):
        """用会话目录名补全映射表缺口。

        `<昵称>(<QQ号>)` 的目录名往往比 uid_map 更全（uid_map 只有聊天出现过的人），
        纯数字目录（318736864）也能靠同群的另一个带名目录补上群名。
        只填缺口，不覆盖 uid_map 里的备注名。
        """
        for root in roots or []:
            for sub, is_group in (("私聊", False), ("群聊", True)):
                base = os.path.join(root, sub)
                try:
                    names = os.listdir(base)
                except OSError:
                    continue
                for d in names:
                    if not os.path.isdir(os.path.join(base, d)):
                        continue
                    m = NAME_QQ_RE.match(SUFFIX_RE.sub("", clean_name(d)))
                    if not m:
                        continue
                    nm = clean_name(m.group(1))
                    qq = m.group(2)
                    if not nm or nm.upper().startswith("QQ") or nm.isdigit():
                        continue  # 名字本身就是 ID，补不上什么
                    if is_group:
                        self.groups.setdefault(qq, nm)
                    else:
                        self.qq2name.setdefault(qq, nm)

    # -- 发送者 ----------------------------------------------------------
    def sender(self, label):
        """返回 (显示名, QQ号, 是否本人)。"""
        raw = clean_name(label)
        if raw == MY_NAME or raw == "我·用户本人":
            return ("我", self.my_qq, True)
        if not raw:
            return ("未知", "", False)
        # u_ 开头的内部 uid
        if raw.startswith("u_"):
            info = self.uid.get(raw) or self.uid_prefix.get(raw)
            if info:
                nm = clean_name(info.get("remark") or info.get("nick") or "")
                qq = str(info.get("qq") or "").strip()
                if qq and self.my_qq and qq == self.my_qq:
                    return ("我", qq, True)
                if nm:
                    return (nm, qq, False)
                if qq:
                    return (self.qq2name.get(qq, "QQ" + qq), qq, False)
            # 映射表里没有、且已被截断的 uid：只留尾号，别把 u_wUQ!sQ2uem 这种原始串摆给用户
            tail = re.sub(r"^u_", "", raw)
            return ("成员·" + tail[-4:], "", False)
        # QQ12345678
        m = re.fullmatch(r"QQ\s*(\d{5,12})", raw)
        if m:
            qq = m.group(1)
            if self.my_qq and qq == self.my_qq:
                return ("我", qq, True)
            return (self.qq2name.get(qq, "QQ" + qq), qq, False)
        # 纯数字 QQ 号
        if re.fullmatch(r"\d{5,12}", raw):
            if self.my_qq and raw == self.my_qq:
                return ("我", raw, True)
            return (self.qq2name.get(raw, "QQ" + raw), raw, False)
        # 昵称(123456)
        m = NAME_QQ_RE.match(raw)
        if m:
            nm = clean_name(m.group(1)) or ("QQ" + m.group(2))
            qq = m.group(2)
            if self.my_qq and qq == self.my_qq:
                return ("我", qq, True)
            return (nm, qq, False)
        # wxid_xxx —— 微信内部号，没有昵称映射，缩短显示
        if raw.startswith("wxid_"):
            return ("微信用户·" + raw[-4:], raw, False)
        # 系统类标签：[文件] [图片] [系统消息] ...
        if raw.startswith("["):
            return ("系统", "", False)
        # 老同步把发送者截断在 24/32 字符，尾部常留下半截 "(20874"，去掉
        raw = re.sub(r"[（(][^（()）]{0,12}$", "", raw).strip()
        return (raw or "未知", "", False)

    # -- 会话 ------------------------------------------------------------
    def conv(self, path, root):
        """从文件路径推出 (kind, conv_key, conv_name, conv_qq)。"""
        try:
            rel = os.path.relpath(path, root)
        except ValueError:
            rel = os.path.basename(path)
        parts = rel.replace("\\", "/").split("/")
        kind = "群聊" if "群聊" in parts else "私聊"
        d = clean_name(parts[-2] if len(parts) >= 2 else parts[0])
        name, qq = self._dir_name(d, kind)
        # 会话键必须是"身份键"而不是"目录键"：同一个人可能有
        #   u_hXc3M_SfidfZwWkQNWNN7w / 胡康(2772411595)_DTPzEHZw / QQ2772411595_kQNWNN7w
        # 三个目录，按目录取键就会在索引里变成三场互不相干的对话。
        # 有 QQ/群号就用它当键，没有才退回目录名。
        conv_key = self.conv_key_of_dir(d, kind, qq)
        return kind, conv_key, name, qq

    def conv_key_of_dir(self, d, kind="私聊", qq=""):
        """目录名 → 会话身份键。有 QQ/群号用号，没有才退回目录名。"""
        if not qq:
            _nm, qq = self._dir_name(clean_name(d), kind)
        if qq:
            return ("群聊/群" if kind == "群聊" else "私聊/QQ") + qq
        return "%s/%s" % (kind, clean_name(d))

    def _dir_name(self, d, kind):
        if not d:
            return ("未知会话", "")
        d = re.sub(r"<[^<>]{0,8}>", "", d).strip() or d  # QQ 富文本群名里的 <$ǿĀD> 装饰
        # u_xxx 目录
        if d.startswith("u_"):
            info = self.uid.get(d) or self.uid_prefix.get(d)
            if info:
                nm = clean_name(info.get("remark") or info.get("nick") or "")
                qq = str(info.get("qq") or "").strip()
                if nm:
                    return (nm, qq)
                if qq:
                    return (self.qq2name.get(qq, "QQ" + qq), qq)
            return (d[:14], "")
        base = SUFFIX_RE.sub("", d)  # 去掉 _DTPzEHZw 这种随机后缀
        m = NAME_QQ_RE.match(base)
        if m:
            nm = clean_name(m.group(1))
            qq = m.group(2)
            if kind == "群聊":
                nm = self.groups.get(qq, nm)
            elif not nm or nm.upper().startswith("QQ") or nm.isdigit():
                nm = self.qq2name.get(qq, nm or ("QQ" + qq))
            return (nm or ("QQ" + qq), qq)
        # QQ1012606911 / QQ1012606911_mqSxuIVQ：私聊目录最常见的一种命名，
        # 老代码只认纯数字和"昵称(QQ)"，于是 118 个私聊会话全都拿不到 QQ 号，
        # 显示成裸 QQ2873393753，也没法和联系人对上。
        m = re.match(r"^QQ\s*(\d{5,12})$", base, re.I)
        if m:
            qq = m.group(1)
            if kind == "群聊":
                return (self.groups.get(qq, "群" + qq), qq)
            return (self.qq2name.get(qq, "QQ" + qq), qq)
        # 临时会话_u_2112371491_12371491 / 临时会话_u_____________2112371491_12371491
        # 同一个人的两种写法（下划线填充不同），QQ 号藏在中间，必须抽出来才能合并
        m = re.match(r"^临时会话.*?(\d{7,12})(?:\D|$)", base)
        if m:
            qq = m.group(1)
            return (self.qq2name.get(qq, "QQ" + qq), qq)
        if re.fullmatch(r"\d{5,12}", base):
            if kind == "群聊":
                return (self.groups.get(base, "群" + base), base)
            return (self.qq2name.get(base, "QQ" + base), base)
        if kind == "群聊":
            return (base, "")
        return (base, "")


# ---------------------------------------------------------------- 行解析

def classify(text):
    """判断消息类型，供展示层决定"折叠/图标/跳过"。"""
    t = (text or "").strip()
    if not t:
        return "empty"
    if UNKNOWN_TAG_RE.match(t):
        return "unknown"
    if JSON_RE.match(t):
        return "json"
    if XML_RE.match(t):
        return "card"
    if SYS_TAIL_RE.search(t):
        return "system"
    if MEDIA_ONLY_RE.match(t):
        return "media"
    if len(t) > 4000:
        return "huge"
    return "text"


def _compact_media(t):
    """[动画表情][动画表情][图片] → 动画表情×2 · 图片"""
    tags = re.findall(r"\[([^\[\]]{1,8})\]", t)
    tags = [TAG_CN.get(x.lower(), x) for x in tags]
    if not tags:
        return t.strip()
    order, count = [], {}
    for x in tags:
        if x not in count:
            count[x] = 0
            order.append(x)
        count[x] += 1
    out = []
    for x in order:
        out.append(x if count[x] == 1 else "%s×%d" % (x, count[x]))
    return " · ".join(out)


CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)


def _xml_field(t, name):
    """取 <name>...</name> 里的文字，顺手剥 CDATA。"""
    m = re.search(r"<%s[^>]*>(.*?)</%s>" % (name, name), t, re.S)
    if not m:
        return ""
    v = CDATA_RE.sub(r"\1", m.group(1))
    v = re.sub(r"<[^>]{0,80}>", "", v)
    return clean_name(v)


def _xml_attr(t, name):
    m = re.search(r'\b%s\s*=\s*"([^"]{0,80})"' % name, t)
    return clean_name(m.group(1)) if m else ""


def summary(text, kind=None):
    """把卡片/富文本压成一句人话，供索引与展示共用。

    QQ 的 JSON 富文本在同步阶段就被截断成 80 字符（源 md 里就是残片），
    再怎么解析也拿不回内容，只能如实标注；微信的 XML 卡片是完整的，
    这里能把图片/文件/链接/转账/位置还原成人看得懂的标签。
    """
    t = (text or "").strip()
    kind = kind or classify(t)
    if kind == "json":
        return "[富文本·原始数据在同步时被截断]"
    if kind != "card":
        return t
    low = t.lower()
    if "<img" in low:
        return "[图片]"
    if "<voicemsg" in low:
        return "[语音]"
    if "<videomsg" in low:
        return "[视频]"
    if "<location" in low:
        lat, lng = _xml_attr(t, "x"), _xml_attr(t, "y")
        poi = _xml_attr(t, "label") or _xml_attr(t, "poiname")
        base = "[位置] " + (poi or "")
        return (base + (" %s,%s" % (lat, lng) if lat and lng else "")).strip()
    if "<emoji" in low or "<face" in low:
        return "[表情]"
    title = _xml_field(t, "title")
    fname = _xml_field(t, "filename") or _xml_field(t, "fileName")
    des = _xml_field(t, "des") or _xml_field(t, "desc")
    if fname:
        return "[文件] " + fname
    if title:
        body = title if not des else ("%s｜%s" % (title, _clip(des, 40)))
        return "[" + body + "]"
    plain = clean_name(re.sub(r"<[^>]{0,200}>", " ", CDATA_RE.sub(r"\1", t)))
    return ("[卡片] " + _clip(plain, 60)).strip() if plain else "[卡片]"


def pretty_text(text, kind=None):
    """把正文整理成给人看的字符串（不删内容，只去噪音）。"""
    t = text or ""
    kind = kind or classify(t)
    # 索引期已经抽成一句人话的（[图片] / [文件] xxx / [转账] ...），直接用，别再折叠一遍
    if kind in ("card", "json") and not re.search(r"[<{]", t):
        return t.strip()
    if kind == "unknown":
        return "[内容未解析]"
    if kind == "json":
        return "[富文本消息·原始数据未解析]"
    if kind == "card":
        m = XML_TITLE_RE.search(t)
        title = clean_name(m.group(1)) if m else ""
        return ("[卡片] " + title) if title else "[卡片/小程序]"
    if kind == "media":
        return _compact_media(t)
    t = REPLY_TAIL_RE.sub("", t)
    while True:
        new = TAIL_TAG_RE.sub("", t)
        if new == t:
            break
        t = new
    return t.strip()


def parse_line(line, cur_date):
    """解析一行，返回 dict 或 None（非消息行）。"""
    m = LINE_RE.match(line)
    if not m:
        return None
    date, hms, mid, rest = m.group(1), m.group(2), m.group(3) or "", m.group(4) or ""
    if mid.strip():
        sender_raw = mid.strip()
        text = rest.lstrip()
        if text.startswith(":"):
            text = text[1:]
        text = text.lstrip(" ")
    else:
        # 格式 B/C：**时间** [发送者] 正文
        s = rest.lstrip()
        if s.startswith("["):
            end = s.find("]")
            if end > 0:
                sender_raw = s[1:end]
                text = s[end + 1:].lstrip()
            else:
                sender_raw, text = "", s
        elif s.startswith(":"):
            sender_raw, text = "", s[1:].lstrip()
        else:
            sender_raw, text = "", s
    hh, mm, ss = (hms.split(":") + ["00", "00"])[:3]
    reply = ""
    rm = REPLY_RE.match(text)
    if rm:
        reply = clean_name(rm.group(1))
        text = text[rm.end():]
    return {
        "date": date or cur_date or "",
        "time": "%02d:%02d:%02d" % (int(hh), int(mm), int(ss)),
        "sender_raw": clean_name(sender_raw),
        "reply": reply,
        "text": text.rstrip(),
        "raw": line,
    }


def parse_file(path, root=None):
    """解析一个归档 md，返回消息列表（续行已合并进上一条）。"""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except Exception:
        return []
    # 微信归档没有 ## 日期 小节，只能拿文件名里的年月补
    file_ym = ""
    m = YM_RE.search(os.path.basename(path))
    if m:
        file_ym = "%s-%s" % (m.group(1), m.group(2))
    cur_date = (file_ym + "-00") if file_ym else ""
    out = []
    for raw in lines:
        s = raw.rstrip()
        if not s.strip():
            continue
        dm = DATE_RE.match(s)
        if dm:
            cur_date = dm.group(1)
            continue
        if s.lstrip().startswith("#"):
            continue
        rec = parse_line(s, cur_date)
        if rec:
            out.append(rec)
        elif out:
            # 续行：报价单、长诗、多链接、XML 多行
            out[-1]["text"] += "\n" + s
    # 有明确日期的文件按时间正序（增量文件是倒序追加的，不排会导致"今天"读反）；
    # 微信那种只有年月、没有日的文件（date 以 -00 结尾）保持原始追加顺序，否则会被时间打乱
    dated = any(m["date"] and not m["date"].endswith("-00") for m in out)
    if dated and len(out) > 1:
        out.sort(key=lambda m: (m["date"], m["time"]))
    return out


def ts_of(date, time):
    """可排序时间戳：YYYYMMDDHHMMSS。微信缺日 => 日补 00。"""
    d = re.sub(r"\D", "", date or "")[:8]
    d = (d + "00000000")[:8]
    t = re.sub(r"\D", "", time or "")[:6]
    t = (t + "000000")[:6]
    try:
        return int(d + t)
    except ValueError:
        return 0


# ---------------------------------------------------------------- FTS 分词

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")


def segment(text):
    """中日韩逐字切、拉丁按词切 —— 让 unicode61 支持中文任意子串检索。"""
    out, buf = [], []
    for ch in text or "":
        if _CJK_RE.match(ch):
            if buf:
                out.append("".join(buf))
                buf = []
            out.append(ch)
        elif ch.isalnum() or ch == "_":
            buf.append(ch.lower())
        else:
            if buf:
                out.append("".join(buf))
                buf = []
    if buf:
        out.append("".join(buf))
    return out


def fts_query(q):
    """关键词 → FTS5 短语查询。返回 None 表示无可用词。"""
    toks = segment(q)
    if not toks:
        return None
    toks = [t.replace('"', "") for t in toks if t]
    if not toks:
        return None
    return '"' + " ".join(toks) + '"'
