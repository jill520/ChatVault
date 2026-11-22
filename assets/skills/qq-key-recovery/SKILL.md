---
name: qq-key-recovery
description: QQ NT PC 版数据库密钥(passphrase)提取与校验的完整流程。当用户的 ChatVault/聊天备份项目出现"QQ 密钥失效：主库 HMAC 校验失败"时使用。按已知信息优先级排查，禁止直接 attach QQ 进程。
---

# QQ NT 密钥恢复作战手册

目标：拿到 QQ NT PC 版 `nt_msg.db` 的 passphrase（明文字符串，通常含特殊字符），
写入 key 文件（配置 `paths.qq_key_file`），并用 HMAC 校验确认有效。

## 0. 先校验现有 key（最常见情况：key 其实没换）

1. 读配置 `.local/config.json` 的 `paths.qq_key_file` 与 `paths.qq_lib`（指向 nt_msg.db 文件）。
2. 校验脚本（纯本地、只读）：

```python
import sys, os
sys.path.insert(0, r"<BASE>\tools\qq-export-helper\src")
from qqexport import sqlcipher_py as sp
key = open(key_file, "rb").read()
f = sp.get_file(qq_lib_path, 1024, 4096)
print("HMAC 校验:", f.check(key, deep=True))   # True = key 有效
```

校验通过 → 问题在别处（库路径/归档），不要重抓 key。

## 1. 排查优先级（按成本从低到高，禁止跳步）

**第 1 步：找 dump/文本残留。** 历史 key 极可能留在以前的扫描/调试产物里。
- 在程序目录搜 `qq_reg_dump.txt`、`*.txt` dump 文件。
- **关键教训：正则必须匹配全部可打印 ASCII（含 `*#%U@{}~+>` 等特殊字符），
  不能只匹配 `[0-9A-Za-z]`** —— 曾出现过含特殊字符的 key 被字母数字正则漏掉。
  dump 里 R8 寄存器的 base64 解码后可能包含 passphrase。
- 如项目内有 dump 校验脚本，可参考其提取 + 校验逻辑。

**第 2 步：看 GitHub 现成项目**（不要重复造轮子）：
搜 "QQ NT db decrypt"、"qq-win-db-key" 等。qq-export-helper 本身
（tools/qq-export-helper）含 `pe.py` 的 locate_key_v2_function 等 hook 思路。

**第 3 步：都不行才考虑动进程内存，且遵守以下红线（全部是血泪教训）：**
- **禁止 attach / CreateProcess 调试 QQ**——QQ NT 有反调试，attach 后立即自杀退出，
  已验证 spawn/attach/竞速附加三种方式全部失败。
- **禁止全量深度内存扫描**——曾长时间扫描 CPU 无果。只做轻量扫描，
  且先征得用户同意。
- 微信的密钥提取工具链（只读扫描 Weixin.exe 的 Config.Cipher 对象）**不适用 QQ**，
  不要混用。

## 2. 拿到候选 key 后

1. 写入 key 文件：**原文写入，不加换行**（sync_qq.py 按原始字节读）。
2. 用第 0 步脚本校验 HMAC。通过即完成；失败则回到第 1 步换下一个候选。
3. 校验通过后提醒用户：软件的自动同步会立即使用新 key，无需重启。

## 3. 环境事实（已验证过）

- 活跃库通常在：
  `C:\Users\<你的用户名>\OneDrive\文档\Tencent Files\<QQ号>\nt_qq\nt_db\nt_msg.db`
  （注意确认当前库路径；历史归档来源的旧库可能 mtime 停留在过去，校验 key 会误判）。
- salt 在 db 文件头 16 字节。
- QQ NT = SQLCipher 4 变体：PBKDF2-SHA512、AES-256-CBC 无填充、页尾 IV、
  标准页布局；WAL 需手动解密帧 + 重算校验和（sync_qq.py 的 _wal_checksum +
  明文 WAL 回放）。
- 校验算法在 `tools/qq-export-helper/src/qqexport/sqlcipher_py.py`（依赖 cryptography 库）。

## 4. 界面集成边界（诚实原则）

工作台 UI 的 QQ 密钥只提供"手动录入 + HMAC 校验"，**不伪装成一键自动提取**——
因为第 1-3 步本质是 AI 诊断会话，无法固化成确定性按钮。微信密钥可以一键
（只读扫描，见 UI「密钥与索引」页），两者不要混淆。