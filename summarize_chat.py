#!/usr/bin/env python3
"""聊天记录总结工具：把归档 Markdown 全文喂给 GLM-4-Flash 做真正的总结
用法:
  python summarize_chat.py list QQ|WX <会话名关键词>
  python summarize_chat.py run  QQ|WX <会话名关键词> <YYYY-MM 或 all> [YYYY-MM2]
  （默认强制本地脱敏；加 --no-sanitize 可关闭，仅调试用）
安全: 送给 GLM 的文本先经本地 sanitize.py 脱敏，原文不离开本机。
输出: 由 .local/config.json 的 paths.qq_summary_out / wechat_summary_out 指定
"""
import os, sys, json, glob, urllib.request
import sanitize  # 纯本地脱敏,零网络零AI
import local_config as cfg  # 本地配置: 密钥与路径不写死在代码里

SANITIZE_ENABLED = True  # 默认强制脱敏;仅调试时可用 --no-sanitize 关闭

# 全部从本地配置读取（.local/config.json,已被 git 忽略）
# 模型服务: 用当前激活的 provider(云端 API 或本地 Ollama/LM Studio),兼容旧 glm 字段
_P = cfg.provider_at(cfg.active_provider())
KEY = _P.get("key") or ""
API = _P.get("api") or cfg.glm_api()
MODEL = _P.get("model") or cfg.glm_model()
BASE = {
    "QQ": cfg.qq_archive(),
    "WX": cfg.wechat_archive(),
}
OUT = cfg.out_dirs()


def ensure_configured():
    if not API or not MODEL or not any(BASE.values()):
        print(cfg.setup_hint())
        return False
    return True
CHUNK = 90000  # 字符分块（约 60-70K token，留足余量）


def find_conversations(platform, keyword):
    """按关键词找会话目录"""
    root = os.path.join(BASE[platform], "私聊")
    dirs = []
    for d in os.listdir(root):
        if keyword.lower() in d.lower():
            dirs.append(os.path.join(root, d))
    return sorted(dirs)


def month_files(conv_dir):
    return sorted(glob.glob(os.path.join(conv_dir, "20??-??.md")))


def call_glm(messages, temperature=0.3):
    body = json.dumps({"model": MODEL, "messages": messages, "temperature": temperature}).encode()
    headers = {"Content-Type": "application/json"}
    if KEY:  # 本地模型(Ollama/LM Studio)无需密钥
        headers["Authorization"] = f"Bearer {KEY}"
    req = urllib.request.Request(API, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"]


def split_chunks(text, size=CHUNK):
    return [text[i:i + size] for i in range(0, len(text), size)]


def summarize_text(text, scope_desc):
    chunks = split_chunks(text)
    partials = []
    for i, c in enumerate(chunks):
        if len(chunks) > 1:
            print(f"  分块 {i+1}/{len(chunks)} ({len(c)//1000}K字)", flush=True)
        msgs = [
            {"role": "system", "content": (
                "你是聊天记录总结助手。总结用户提供的与他人的聊天记录（'我'代表用户本人）。"
                "用简体中文输出，包含：①关系与互动基调 ②主要话题及要点 ③用户提到的个人生活/计划/情绪 ④对方的近况与观点 ⑤值得回溯的关键事件（带日期）。"
                "只依据记录本身，不要编造；记录无关的内容忽略。")},
            {"role": "user", "content": f"以下是与{scope_desc}的聊天记录（第{i+1}部分）：\n\n{c}\n\n请总结。"},
        ]
        partials.append(call_glm(msgs))
    if len(partials) == 1:
        return partials[0]
    msgs = [
        {"role": "system", "content": "你是聊天记录总结助手。请把多个分段摘要合并成一份完整总结，用简体中文，结构：①关系与互动基调 ②主要话题 ③用户的个人动态 ④对方动态 ⑤关键事件时间线。"},
        {"role": "user", "content": f"以下是与{scope_desc}聊天记录的分段摘要：\n\n" + "\n\n---\n\n".join(partials) + "\n\n请合并为一份总结。"},
    ]
    return call_glm(msgs)


def do_summary(platform, keyword, m_start, m_end=None):
    if not ensure_configured():
        return
    convs = find_conversations(platform, keyword)
    if not convs:
        print(f"未找到匹配 '{keyword}' 的会话"); return
    if len(convs) > 1:
        print("匹配到多个会话：")
        for c in convs: print(" ", os.path.basename(c))
        print("请用更精确的关键词"); return
    conv = convs[0]
    name = os.path.basename(conv)
    files = month_files(conv)
    if m_start != "all":
        files = [f for f in files if m_start <= os.path.basename(f)[:7] <= (m_end or m_start)]
        if not files:
            avail = ", ".join(os.path.basename(f)[:7] for f in month_files(conv))
            print(f"该时段无记录。可用月份: {avail}"); return
    text = ""
    for f in files:
        with open(f, encoding="utf-8") as fh:
            text += f"\n\n## {os.path.basename(f)[:7]}\n\n" + fh.read()
    total_k = len(text) // 1000
    print(f"会话: {name} | {len(files)} 个月文件 | {total_k}K 字", flush=True)
    if total_k > 3000:
        print("超过 3M 字，建议缩小月份范围"); return
    # ★ 本地脱敏: 原文不离开本机,送出去的只有脱敏副本
    if SANITIZE_ENABLED:
        text, sreport = sanitize.sanitize_with_report(text)
        print(f"  [本地脱敏] 替换 {sreport['替换总数']} 处: {sreport['分类统计']}（原件未改动）", flush=True)
    summary = summarize_text(text, name.split("(")[0])
    outdir = os.path.join(OUT[platform], name.split("(")[0])
    os.makedirs(outdir, exist_ok=True)
    span = os.path.basename(files[0])[:7] + ("_" + os.path.basename(files[-1])[:7] if len(files) > 1 else "")
    out = os.path.join(outdir, f"{span}_总结.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# 与{name.split('(')[0]}的聊天总结（{span}）\n\n生成时间: {__import__('time').strftime('%Y-%m-%d %H:%M')}\n来源: {platform} 归档 {len(files)} 个月文件, 约{total_k}K字\n\n---\n\n{summary}\n")
    print(f"✅ 已保存: {out}", flush=True)


def do_list(platform, keyword):
    if not ensure_configured():
        return
    convs = find_conversations(platform, keyword)
    for conv in convs[:10]:
        name = os.path.basename(conv)
        files = month_files(conv)
        sizes = sum(os.path.getsize(f) for f in files) // 1000
        months = [os.path.basename(f)[:7] for f in files]
        print(f"{name} | {len(files)} 个月 ({months[0]}~{months[-1]}) | 共{sizes}K字")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    # 调试开关: --no-sanitize 会关闭脱敏(不推荐),必须在任意位置显式传入
    if "--no-sanitize" in sys.argv:
        SANITIZE_ENABLED = False
        sys.argv.remove("--no-sanitize")
        print("[警告] 已关闭脱敏!原文将直接发送,仅用于调试。", flush=True)
    if sys.argv[1] == "list":
        do_list(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "run":
        do_summary(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] if len(sys.argv) > 5 else None)
