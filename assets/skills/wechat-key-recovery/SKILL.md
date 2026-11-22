---
name: wechat-key-recovery
description: 微信 4.x PC 版数据库密钥（enc_key）提取与校验的完整流程。当 ChatVault 的「一键抓取微信密钥」失败（微信未登录、版本更新、权限不足、数据目录换盘、多账号选错）时使用。全程只读，不修改微信任何文件。
---

# 微信 4.x 密钥恢复手册

目标：拿到微信 `db_storage` 里每个数据库的 32 字节 `enc_key`，写入 `all_keys.json`，
并用 SQLCipher 第 1 页 HMAC 校验确认有效。

密钥等于聊天记录的钥匙。**不要发到网上、群里、截图里，也不要用任何在线服务解密。**

## 0. 先确认前提（大部分失败都在这里）

按顺序核对，不要跳步：

1. **微信已登录**：窗口应是主界面，不是登录/扫码页。
   窗口大约 300×390 就是还停在登录页，先让用户扫码。
2. **微信在运行**：任务管理器里能看到 `Weixin.exe` 主进程，通常占几百 MB 内存。
3. **数据目录找对**：
   - 读 `%APPDATA%\Tencent\xwechat\config\*.ini`，文件内容就是用户选的数据根
     （例如 `D:\`、`D:\微信`）。这是用户改过盘之后最可靠的来源。
   - 实际库在 `<数据根>\xwechat_files\<wxid>_<后缀>\db_storage`。
   - 一台电脑可能有多个账号目录，选**最近有写入**的那个（看 `message/*.db` 的修改时间）。
   - 默认 `Documents\xwechat_files` 只是回退；用户改过位置就以 ini 为准。
4. **程序位置不用猜**：读注册表 `HKCU\Software\Tencent\Weixin` 的 `InstallPath`；
   找不到再试 `C:\Program Files\Tencent\Weixin`、`C:\Program Files (x86)\Tencent\Weixin`。
5. **权限要够**：微信以管理员运行时，读取内存的工具也要以管理员运行，
   否则会报「拒绝访问 / 无法读取微信进程」。

## 1. 用随包工具先试（只读）

工具在 `<程序目录>\tools\wechat4-db-decrypt\src`。

```powershell
cd "<程序目录>\tools\wechat4-db-decrypt"
$env:PYTHONPATH = "$PWD\src"
python -m wxexport.keys.hunt --db-dir "<数据根>\xwechat_files\<账号>\db_storage" --deep
```

- `--deep` 会多试 passphrase/PBKDF2，慢一点但成功率高。
- 成功会输出 `匹配 N/N 个数据库，已写入 ...all_keys.json`。
- 失败时看三种统计，判断卡在哪：
  - `Config.Cipher: ... 锚点 0`：微信没登录，或者版本结构变了。
  - 锚点有、`节点 0`：内存布局和当前工具 profile 不匹配，通常是微信升级。
  - `passphrase 候选: N` 但 `验证 0`：候选不对，或数据库不属于当前账号。

## 2. 工具不支持当前版本时

**不要 attach / 调试微信进程**（有反调试和风控风险），也不要修改微信数据库。
按成本从低到高处理：

1. **先换账号目录**：多账号时确认 ini 里的数据根和当前登录账号一致。
2. **找历史密钥**：程序目录、`<数据根>\密钥\`、旧备份里的 `all_keys.json`
   或 `hunted_keys.txt` 可能已有同一账号的 key。用第 3 步校验，能用就不重抓。
3. **换公开的微信 4.x 密钥工具**：搜 `wechat4-db-decrypt`、`WeChat 4.0 db key` 等。
   只选只读、开源、能说明原理的项目；运行前先备份现有 `all_keys.json`。
4. **最后才考虑注入**：Frida spawn hook 需要重启微信，先让用户确认，
   并说明可能触发风控。只读，不改数据库文件。

## 3. 校验密钥（必须做，不能跳过）

每个库用自己的文件头 16 字节 salt + enc_key 做 SQLCipher 4 的 page-1 HMAC。
工具内部已经校验；如果自己写脚本，注意微信 4.x 是 SQLCipher 4：
PBKDF2-HMAC-SHA512、256k 轮、AES-256-CBC、HMAC-SHA512，页大小 4096。

只有 HMAC 校验返回 True 的 key 才能写进 `all_keys.json`。

## 4. 交付

- 把有效的 `all_keys.json` 放到软件当前使用的密钥文件位置：
  以 `.local/config.json` 的 `paths.all_keys` 为准，通常是
  `<数据根>\密钥\all_keys.json`。
- 文件格式（以工具实际输出为准）：

```json
{
  "message/message_0.db": {
    "enc_key": "<64 位 hex>",
    "salt": "<32 位 hex>",
    "source": "config-cipher/..."
  }
}
```

- 完成后告诉用户：下次微信同步会直接使用新 key，不需要重启软件。
- 最后提醒一次：密钥不要外传，只在本机使用。

## 5. 界面集成边界

软件只提供「一键抓取」和「复制 skill」：

- 一键抓取是确定性的只读扫描，适合「微信已登录 + 版本没变」的绝大多数情况；
- 版本更新、权限、多账号、用户改盘等需要诊断的情况，交给 AI 按本 skill 排查；
- 不要假装一个按钮能解决所有失败，也不要把「抓不到」说成「功能坏了」。