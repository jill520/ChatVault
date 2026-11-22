#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
prepare_github.py - 生成可安全上传 GitHub 的干净发布副本

工作方式：
  1. 白名单读取核心程序文件（不读取密钥、状态、日志、归档）
  2. 在内存里扫描真实密钥/账号/私人路径；任一命中即中止，不写任何文件
  3. 通过后写入全新发布目录，并补齐 example 配置与 tools 说明

用法:
  python prepare_github.py
  python prepare_github.py --target D:\some\dir
"""
import os
import re
import sys
import json
import argparse

BASE = os.path.dirname(os.path.abspath(__file__))

# 最小发布面：只包含检索/阅读/同步/界面所需源码
WHITELIST = [
    "chatvault.py",
    "chatvault.html",
    "web/app.css",
    "assets/skills/qq-key-recovery/SKILL.md",
    "chatvault_extras.py",
    "chatvault_app.py",
    "chatvault_index.py",
    "archive_parse.py",
    "sanitize.py",
    "summarize_chat.py",
    "local_config.py",
    "conv_exclude.py",
    "group_alias.py",
    "user_alias.py",
    "sync_qq.py",
    "sync_wechat.py",
    "config.example.json",
    "README.md",
    "LICENSE",
    ".gitignore",
    "ChatVault.spec",
    "prepare_github.py",
    "启动工作台.bat",
    "启动窗口版.bat",
    "打包exe.bat",
]

# 结构性敏感模式（不依赖本机配置也能拦住）
BAD_PATTERNS = [
    ("API key", r"\b[0-9a-fA-F]{32}\.[A-Za-z0-9]{16,}\b"),

]

EXTRA_FILES = {
    "group_alias.example.json": "{}\n",
    "user_alias.example.json": "{}\n",
    "tools/README.md": (
        "# 解密工具目录（不随仓库分发）\n\n"
        "同步功能依赖两个本地工具：\n\n"
        "- `tools/qq-export-helper/src`\n"
        "- `tools/wechat4-db-decrypt/src`\n\n"
        "出于合规与隐私考虑，仓库不包含这些工具及配置。需要同步时请自行准备，\n"
        "放到上述路径后重新打包。缺少这些目录时，检索、阅读、联系人、收藏、\n"
        "统计等功能仍可正常使用，只有「同步」页会提示工具缺失。\n"
    ),
}


def collect_secret_values():
    """从本机真实配置里提取必须禁止出现的字面值。"""
    vals = set()
    local = os.path.join(BASE, ".local", "config.json")
    cfg = {}
    if os.path.exists(local):
        try:
            with open(local, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass

    sensitive_keys = {
        "api_key", "qq_archive", "wechat_archive", "qq_summary_out",
        "wechat_summary_out", "qq_key_file", "qq_lib", "qq_log",
        "wechat_log", "qq", "qq_incr", "wechat_incr", "wechat_db_dir",
        "index_db", "trash", "data_root",
    }

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in sensitive_keys and isinstance(v, str) and len(v) >= 6:
                    vals.add(v)
                elif isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(cfg)

    # 本机用户目录（C:\Users\xxx）
    try:
        home = os.path.expanduser("~")
        if home and len(home) >= 8:
            vals.add(home)
    except Exception:
        pass

    # QQ 密钥文件正文
    try:
        from local_config import qq_key_file
        kp = qq_key_file()
        if kp and os.path.exists(kp):
            with open(kp, encoding="utf-8", errors="ignore") as f:
                k = f.read().strip()
            if len(k) >= 8:
                vals.add(k)
    except Exception:
        pass
    return vals


def scan():
    """读取白名单文件并在内存中扫描，返回 (contents, problems)。"""
    contents = {}
    problems = []

    for name in WHITELIST:
        path = os.path.join(BASE, name)
        if not os.path.exists(path):
            problems.append("缺少发布必需文件: " + name)
            continue
        try:
            with open(path, encoding="utf-8") as f:
                contents[name] = f.read()
        except Exception as e:
            problems.append("无法读取 %s: %s" % (name, e))

    secrets = collect_secret_values()
    for name, text in contents.items():
        if name == "prepare_github.py":
            continue  # 自查脚本自身含检测模式，不参与扫描
        for label, pattern in BAD_PATTERNS:
            for m in re.finditer(pattern, text):
                problems.append("%s: 命中[%s] → %s" % (name, label, m.group(0)[:60]))
        for secret in secrets:
            if secret in text:
                problems.append("%s: 命中本机真实配置值 → %s" % (name, secret[:40]))

    # 模板必须为空 key
    example = contents.get("config.example.json", "")
    try:
        ex = json.loads(example) if example else {}
        if ex.get("glm", {}).get("api_key"):
            problems.append("config.example.json 的 glm.api_key 非空，必须留空")
    except Exception as e:
        problems.append("config.example.json 解析失败: %s" % e)

    return contents, problems


def choose_target(base_target):
    """不覆盖、不删除：目标存在则依次换名。"""
    if not os.path.exists(base_target):
        return base_target
    i = 2
    while os.path.exists(base_target + "_" + str(i)):
        i += 1
    return base_target + "_" + str(i)


def main():
    ap = argparse.ArgumentParser(description="生成可上传 GitHub 的干净发布副本")
    default_target = os.path.join(os.path.dirname(BASE), "ChatVault-Release")
    ap.add_argument("--target", default=default_target)
    args = ap.parse_args()

    print("=" * 64)
    print("发布前敏感信息自查")
    print("=" * 64)
    contents, problems = scan()

    if problems:
        print("❌ 自查未通过，已中止，未写入任何文件：")
        for p in problems:
            print("  -", p)
        sys.exit(1)

    print("✅ 自查通过：%d 个文件均无密钥/账号/私人路径" % len(contents))
    for name in WHITELIST:
        print("   ✓", name)

    target = choose_target(args.target)
    os.makedirs(target, exist_ok=True)
    for name, text in contents.items():
        dest = os.path.join(target, *name.split("/"))
        os.makedirs(os.path.dirname(dest) or target, exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    for name, text in EXTRA_FILES.items():
        p = os.path.join(target, *name.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    print()
    print("发布副本已生成:", target)
    print("上传前请再人工确认：该目录内只有上述文件，且 .local/、tools/ 私有源码不在其中。")
    print("提示：config.example.json 是空模板；真实配置只在本机 .local/config.json。")


if __name__ == "__main__":
    main()