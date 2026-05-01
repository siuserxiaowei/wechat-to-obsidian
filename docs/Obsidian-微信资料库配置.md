---
title: 微信资料库配置
aliases:
  - WeChat Obsidian Bridge 配置
  - 微信 Obsidian 桥配置
tags:
  - obsidian
  - wechat
  - weflow
  - knowledge-base
source: wechat-obsidian-bridge
updated: 2026-05-01
---

# 微信资料库配置

这份文档用于把微信聊天记录、文件传输助手、学习资料、链接卡片、图片、视频、语音和附件导入到 Obsidian。

当前方案不是 Obsidian 插件自动同步，而是：

```text
微信 / WeFlow
  -> 本地 API 或 JSON 导出
  -> WeChat Obsidian Bridge
  -> Obsidian Vault 里的 Markdown + attachments
```

导入完成后，Obsidian 会把微信资料当成普通 Markdown 笔记处理，可以搜索、链接、打标签、做 Dataview 查询。

## 截图总览

下面是示意截图，内容使用示例数据，不包含真实聊天隐私。

![三条导入路线总览](https://raw.githubusercontent.com/siuserxiaowei/wechat-to-obsidian/main/assets/screenshots/01-overview.png)

![WeFlow API 导入流程](https://raw.githubusercontent.com/siuserxiaowei/wechat-to-obsidian/main/assets/screenshots/02-weflow-api.png)

![不用 WeFlow 的直接解库流程](https://raw.githubusercontent.com/siuserxiaowei/wechat-to-obsidian/main/assets/screenshots/03-direct-db.png)

![导入到 Obsidian 后的效果](https://raw.githubusercontent.com/siuserxiaowei/wechat-to-obsidian/main/assets/screenshots/04-obsidian-result.png)

![三种方式对比](https://raw.githubusercontent.com/siuserxiaowei/wechat-to-obsidian/main/assets/screenshots/05-route-comparison.png)

## 目标效果

- 在 Obsidian 中查看微信文件传输助手内容。
- 在 Obsidian 中查看聊天记录、链接、图片、视频、语音、表情和附件。
- 用 Obsidian 搜索微信里沉淀过的学习资料。
- 按日期沉淀微信资料，形成可长期整理的个人知识库。
- 后续可以把高价值资料再整理成永久笔记、主题笔记或项目笔记。

## 推荐目录

建议在 Obsidian Vault 中使用下面结构：

```text
Obsidian Vault/
├── 00-系统/
│   └── 微信资料库配置.md
├── 微信渠道/
│   ├── 文件传输助手/
│   │   ├── 2026-01/
│   │   │   ├── 2026-01-03.md
│   │   │   └── attachments/
│   │   └── _weflow_import_manifest.json
│   ├── 重要群聊/
│   └── 私聊/
└── 素材库/
```

推荐主目录：

```text
微信渠道
```

推荐子目录：

```text
文件传输助手
重要群聊
私聊
公众号与链接
待整理资料
```

## 本机路径配置

项目目录：

```bash
/Users/siuserxiaowei/Desktop/dont哥 对谈/wechat-to-obsidian
```

推荐 Obsidian Vault：

```bash
~/Documents/Obsidian\ Vault
```

如果你的 Vault 名字不同，把后面命令里的 `--vault` 改成真实路径。

## 三种导入方式

不是只能使用 WeFlow。当前支持三种方式：

| 方式 | 适合场景 | 优点 | 代价 |
| --- | --- | --- | --- |
| WeFlow API | 日常同步、文件传输助手、群聊/私聊持续导入 | 最省事，WeFlow 负责解密和媒体解析 | 需要打开 WeFlow API 服务 |
| WeFlow JSON | 一次性归档、手动筛选后导入 | 不需要 API 常驻，适合离线导入 | 需要先在 WeFlow 导出 JSON |
| 直接解微信本地库 | 不使用 WeFlow、需要底层控制 | 可直接处理 macOS 微信 4.x 本地数据库 | 需要 Frida 抓 key、解密 DB，步骤更多 |

推荐顺序：

```text
WeFlow API -> WeFlow JSON -> 直接解微信本地库
```

## WeFlow 设置

推荐优先使用 WeFlow，因为它已经处理了很多微信本地数据读取、媒体解析和 API 输出。

在 WeFlow 里打开：

```text
设置 -> API 服务 -> 开启
```

默认 API：

```text
http://127.0.0.1:5031
```

Token：

- 如果 WeFlow 设置了 Access Token，命令里加 `--token "你的 token"`。
- 如果 Token 为空，可以不传。
- CLI 会自动尝试读取：

```text
~/Library/Application Support/weflow/WeFlow-config.json
```

## 第一次检查

进入项目目录：

```bash
cd /Users/siuserxiaowei/Desktop/dont哥\ 对谈/wechat-to-obsidian
```

检查工具：

```bash
python3 scripts/wechat2obsidian.py doctor
```

列出 WeFlow 会话：

```bash
python3 scripts/wechat2obsidian.py weflow-sessions --limit 100
```

搜索文件传输助手：

```bash
python3 scripts/wechat2obsidian.py weflow-sessions --keyword 文件
```

搜索群聊或好友：

```bash
python3 scripts/wechat2obsidian.py weflow-sessions --keyword 关键词
```

## 导入文件传输助手

最推荐先导入文件传输助手，因为大量学习资料、链接、截图和临时文件都在这里。

```bash
python3 scripts/wechat2obsidian.py import-weflow-api \
  --talker filehelper \
  --vault ~/Documents/Obsidian\ Vault \
  --folder "微信渠道" \
  --subfolder "文件传输助手" \
  --media
```

如果 WeFlow 配了 token：

```bash
python3 scripts/wechat2obsidian.py import-weflow-api \
  --talker filehelper \
  --vault ~/Documents/Obsidian\ Vault \
  --folder "微信渠道" \
  --subfolder "文件传输助手" \
  --token "填入 WeFlow Token" \
  --media
```

## 按时间范围导入

导入 2026 年以来的文件传输助手：

```bash
python3 scripts/wechat2obsidian.py import-weflow-api \
  --talker filehelper \
  --vault ~/Documents/Obsidian\ Vault \
  --folder "微信渠道" \
  --subfolder "文件传输助手" \
  --since 2026-01-01 \
  --until 2026-05-01 \
  --media
```

以后日常同步可以只导入最近几天或最近一个月。

## 导入某个群聊

先查群聊 ID：

```bash
python3 scripts/wechat2obsidian.py weflow-sessions --keyword 群名
```

假设查到的 talker 是：

```text
123456789@chatroom
```

导入：

```bash
python3 scripts/wechat2obsidian.py import-weflow-api \
  --talker "123456789@chatroom" \
  --vault ~/Documents/Obsidian\ Vault \
  --folder "微信渠道" \
  --subfolder "重要群聊/群名" \
  --media
```

## 导入某个好友私聊

先查好友：

```bash
python3 scripts/wechat2obsidian.py weflow-sessions --keyword 好友备注
```

假设查到的 talker 是：

```text
wxid_xxxxx
```

导入：

```bash
python3 scripts/wechat2obsidian.py import-weflow-api \
  --talker "wxid_xxxxx" \
  --vault ~/Documents/Obsidian\ Vault \
  --folder "微信渠道" \
  --subfolder "私聊/好友备注" \
  --media
```

## 导入 WeFlow JSON

如果你已经在 WeFlow 里导出了 JSON：

```bash
python3 scripts/wechat2obsidian.py import-weflow-json \
  --input ~/Downloads/weflow-export.json \
  --vault ~/Documents/Obsidian\ Vault \
  --folder "微信渠道" \
  --subfolder "WeFlow导入"
```

适合一次性导入、归档导入、手动筛选导入。

## 不用 WeFlow：直接解微信本地库

如果你不想使用 WeFlow，也可以直接处理 macOS 微信 4.x 本地数据库。

这条路线更底层，步骤更多：

```text
签名微信副本 -> Frida 抓 key -> 解密 message_0.db -> 找会话 -> 导出 Obsidian
```

安装依赖：

```bash
cd /Users/siuserxiaowei/Desktop/dont哥\ 对谈/wechat-to-obsidian

python3 -m pip install -r requirements.txt
```

检查环境：

```bash
python3 scripts/wechat2obsidian.py doctor
```

签名一个可被 Frida attach 的微信副本：

```bash
python3 scripts/wechat2obsidian.py sign-wechat \
  --dest ~/Desktop/WeChat-Obsidian.app
```

抓取数据库 key：

```bash
python3 scripts/wechat2obsidian.py capture-keys \
  --wechat-app ~/Desktop/WeChat-Obsidian.app \
  --launch \
  --wait 300
```

抓 key 时，在微信里打开你要导出的聊天，例如：

```text
文件传输助手
某个好友
某个群聊
收藏
```

定位微信用户目录：

```bash
USER_DIR=$(python3 scripts/wechat2obsidian.py locate-user --print-path)
```

解密消息库：

```bash
python3 scripts/wechat2obsidian.py decrypt \
  --db "$USER_DIR/db_storage/message/message_0.db" \
  --out /tmp/message_0.decrypted.db
```

列出可导出的会话：

```bash
python3 scripts/wechat2obsidian.py list-targets \
  --db /tmp/message_0.decrypted.db \
  --limit 100
```

导出文件传输助手：

```bash
python3 scripts/wechat2obsidian.py export-chat \
  --db /tmp/message_0.decrypted.db \
  --target filehelper \
  --vault ~/Documents/Obsidian\ Vault \
  --folder "微信渠道" \
  --subfolder "文件传输助手" \
  --with-senders
```

导出某个好友或群聊：

```bash
python3 scripts/wechat2obsidian.py export-chat \
  --db /tmp/message_0.decrypted.db \
  --target "wxid_xxxxx 或 123456789@chatroom" \
  --vault ~/Documents/Obsidian\ Vault \
  --folder "微信渠道" \
  --subfolder "私聊或群聊名称" \
  --with-senders
```

这条路线不依赖 WeFlow，但要处理 Frida、key、SQLCipher 和微信本地库。除非你明确不想用 WeFlow，否则日常使用仍然建议走 WeFlow API。

## 导入后是什么样

导入后会生成：

```text
微信渠道/
└── 文件传输助手/
    ├── 2026-01/
    │   ├── 2026-01-03.md
    │   └── attachments/
    ├── 2026-02/
    │   ├── 2026-02-18.md
    │   └── attachments/
    └── _weflow_import_manifest.json
```

每天一个 Markdown 文件。

每条消息大致是：

```markdown
## 09:30:12 · me · text

这是一条微信消息
```

媒体文件会尽量写成：

```markdown
![image](attachments/photo.jpg)
```

或：

```markdown
[voice](attachments/voice.wav)
```

## Obsidian 推荐插件

不是必须，但推荐：

| 插件 | 用途 |
| --- | --- |
| Dataview | 查询微信资料库里的每日记录和统计字段 |
| Omnisearch | 更强全文搜索 |
| Advanced Tables | 看表格更舒服 |
| Tag Wrangler | 管理标签 |
| Outliner | 整理聊天内容为大纲 |

## Dataview 查询

下面查询需要安装 Dataview。

### 最近导入的微信记录

```dataview
TABLE date, message_count, exported_at
FROM "微信渠道"
WHERE source = "weflow" OR source = "wechat"
SORT exported_at DESC
LIMIT 30
```

### 消息最多的日期

```dataview
TABLE date, message_count, file.folder
FROM "微信渠道"
WHERE message_count
SORT message_count DESC
LIMIT 30
```

### 文件传输助手记录

```dataview
TABLE date, message_count
FROM "微信渠道/文件传输助手"
WHERE source = "weflow" OR source = "wechat"
SORT date DESC
LIMIT 50
```

### 最近 30 篇微信资料笔记

```dataview
LIST
FROM "微信渠道"
WHERE source = "weflow" OR source = "wechat"
SORT file.name DESC
LIMIT 30
```

## Obsidian 搜索语法

搜微信资料：

```text
path:"微信渠道" 关键词
```

只搜文件传输助手：

```text
path:"微信渠道/文件传输助手" 关键词
```

搜链接：

```text
path:"微信渠道" https
```

搜图片记录：

```text
path:"微信渠道" attachments
```

搜某天：

```text
path:"微信渠道" 2026-05-01
```

## 日常使用流程

### 每天或每周同步一次

1. 打开 WeFlow。
2. 开启 API 服务。
3. 运行文件传输助手导入命令。
4. 打开 Obsidian。
5. 在 `微信渠道/文件传输助手` 查看新增记录。
6. 把有价值内容整理到主题笔记。

### 只同步最近一周

把日期改成最近一周：

```bash
python3 scripts/wechat2obsidian.py import-weflow-api \
  --talker filehelper \
  --vault ~/Documents/Obsidian\ Vault \
  --folder "微信渠道" \
  --subfolder "文件传输助手" \
  --since 2026-04-24 \
  --until 2026-05-01 \
  --media
```

### 防止覆盖已整理文件

如果你手动编辑过导出的日记文件，可以加：

```bash
--mode skip
```

完整示例：

```bash
python3 scripts/wechat2obsidian.py import-weflow-api \
  --talker filehelper \
  --vault ~/Documents/Obsidian\ Vault \
  --folder "微信渠道" \
  --subfolder "文件传输助手" \
  --since 2026-04-24 \
  --until 2026-05-01 \
  --media \
  --mode skip
```

## 整理建议

不要直接把所有聊天记录都当成最终知识。

推荐分三层：

```text
微信渠道 = 原始资料层
待整理资料 = 临时加工层
主题笔记 / 项目笔记 = 最终知识层
```

处理一条有价值微信资料时：

1. 在 `微信渠道` 里搜索到原始消息。
2. 把核心链接、截图或文件引用到主题笔记。
3. 给主题笔记打标签。
4. 保留原始消息作为出处。

## 常见问题

### Obsidian 安装这个仓库后会自动同步吗？

不会。当前不是 Obsidian 插件，而是导入工具。运行命令后，工具会把微信数据写进 Obsidian vault。

### 能看到聊天文件吗？

能看到本地可拿到的文件。WeFlow 或微信本地没有缓存的云端文件，不一定能拿到。

### 能看到图片、语音、视频吗？

能看到本地可导出的媒体。导入时加 `--media`，工具会尽量复制到 `attachments/`。

### 能导入所有群聊吗？

可以逐个导入。先用 `weflow-sessions` 找 talker，再对每个 talker 跑 `import-weflow-api`。

### 能实时同步吗？

当前不是实时同步。可以手动定期运行命令。后续可以继续做自动任务或 Obsidian 插件。

### 微信收藏可以直接导入吗？

当前推荐通过 WeFlow 或聊天记录中的收藏/转发内容导入。直接解析 `favorite.db` 是后续增强项。

### 为什么有些图片不显示？

常见原因：

- WeFlow 没有返回本地媒体路径。
- 原图没有缓存到本机。
- 文件在微信缓存里已经被清理。
- Obsidian 中附件相对路径被移动。

### 为什么 API 连接不上？

检查：

```bash
python3 scripts/wechat2obsidian.py weflow-sessions --limit 10
```

如果失败：

1. 确认 WeFlow 正在运行。
2. 确认 WeFlow 设置里开启了 API 服务。
3. 确认端口是 `5031`。
4. 如果设置了 Token，命令里加 `--token`。

## 一键命令模板

把文件传输助手导入 Obsidian：

```bash
cd /Users/siuserxiaowei/Desktop/dont哥\ 对谈/wechat-to-obsidian

python3 scripts/wechat2obsidian.py import-weflow-api \
  --talker filehelper \
  --vault ~/Documents/Obsidian\ Vault \
  --folder "微信渠道" \
  --subfolder "文件传输助手" \
  --media
```

查看可导入会话：

```bash
cd /Users/siuserxiaowei/Desktop/dont哥\ 对谈/wechat-to-obsidian

python3 scripts/wechat2obsidian.py weflow-sessions --limit 200
```

## 后续升级方向

- 做成真正的 Obsidian 插件。
- 在 Obsidian 里提供配置界面。
- 支持一键同步文件传输助手。
- 支持定时增量导入。
- 支持微信收藏库 `favorite.db` 专门导入。
- 支持更强的数据统计、关系图和资料清洗。
