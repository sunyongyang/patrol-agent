# AGENTS.md

给在此仓库工作的 AI 编码助手的说明。人类也建议一读。

---

## 先读什么

1. **`docs/巡逻Agent_Spec.md` 的附录 A** —— 实现现状、实测数据、**踩过的坑**、尚未实现清单。
   附录 A.6 和 A.7 能省掉你重复踩坑、以及误以为某功能已经存在的时间。
2. `docs/巡逻Agent_交互设计.md` —— 对话流与卡片模板
3. `README.md` —— 快速开始、脚本一览、能力边界

**Spec 正文是设计意图，附录 A 是实现事实。冲突时以附录 A 为准。**

---

## 这个项目是什么

在没有实体机器人的前提下，用一台带摄像头的 PC 搭出可端到端演示的安防巡逻 Agent。
两个独立进程：

```
robot_sim :8100      摄像头 + YOLO 真实推理 + 点位/任务/告警
agent_service :8200  飞书长连接 + LLM 编排 + 报表 + 定时推送
                     ↕ 共享 SQLite (data/patrol.db)
```

**拆成两个进程是刻意的**：将来把模拟器换成真机或真平台时，`agent_service` 一行都不用改。

---

## 不可破坏的不变量

违反这些会让系统出现难以排查的问题。每条都对应一次实际踩坑。

| 不变量 | 为什么 |
|---|---|
| **写操作必须走两段式**：先 `prepare_*` 落 `pending_action` 出确认卡，用户点按钮才 `execute_*` | LLM 有幻觉，绝不能让它一句话直接对物理世界生效 |
| **卡片/图片结果不写入对话历史**，只把真正的文本回复写入 | 曾经把卡片摘要写进 assistant 轮次，LLM 学会那个"格式"，不再调工具，直接吐一句没有数据的假卡片文案 |
| **飞书事件回调必须 3 秒内返回**：立刻回 toast，干活丢后台线程，完成后 PATCH 卡片 | 长连接是单线程处理事件的，一个慢动作会把后面所有消息一起堵住，且触发超时重试 |
| **幂等去重用 `message_id`，不要用 `event_id`** | 飞书官方明确要求 |
| **system prompt 必须注入当前日期** | 不注入时 LLM 会把「昨天」解析成训练数据里的日期 |
| **点位只上报绑定算法的检测结果** | `Point.algorithm` 是唯一事实来源；`target_label` 从它派生，不要单独配置 |
| **编号用 `db.next_id()`**，不要自己拼 `COUNT(*)+1` | 曾经因为编号规则不一致导致用户点「确认下发」收到 500 |
| **自检不要动共享句柄**（如 `Camera.probe` 不要 `close()`） | `/health` 会调它，关掉常驻摄像头后下次抓帧要重开 6 秒 |
| **模拟数据必须标 `is_mock=1`** | 与真实推理混淆会让整套「可当证据」的定位塌掉 |
| **不要在文档/卡片里承诺没实现的能力** | 曾经确认卡显示「22:00」但机器人立刻出发 —— 功能缺失不是问题，承诺了做不到才是 |

---

## 环境（本机特有，容易踩）

```powershell
# 必须用项目自己的 venv。本机 PATH 里的 python 指向另一个工具的 venv，
# 往里装 torch 会污染它。
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 起服务
.venv\Scripts\python.exe -m uvicorn robot_sim.main:app --port 8100
.venv\Scripts\python.exe -m uvicorn agent_service.main:app --port 8200
```

- **配置文件**：`config/config.yaml`（含密钥，已在 .gitignore）。模板见 `config/config.example.yaml`
- **端口被占**：先确认占用进程确实是本项目的 uvicorn 再处理；历史上出现过「pwsh 包装器退出了但 python 子进程还活着」
- **摄像头独占**：`robot_sim` 启动时就 `camera.open()`，**持有到进程退出**。
  Windows 相机默认独占访问，所以它跑着的时候飞书/钉钉开会拿不到画面（已实测）。
  别在用户要开会时留着服务跑。

### 推送代码（这条最容易卡住）

**本机到 github.com 的 TLS 连接失败率约 1/3**（实测 6 次里失败 2 次），
报错形如 `TLS connect error: error:0A000126:SSL routines::unexpected eof while reading`。
**这不是认证问题，是网络抖动，重试就能成。**

所以**不要直接 `git push`**，用仓库自带的带重试脚本：

```powershell
.\scripts\git_push.ps1                  # 推当前分支，自动重试 8 次
.\scripts\git_push.ps1 -MaxAttempts 15
```

它会先检查 TLS 后端、代理是否可达、有无待推送内容，再带退避重试。
失败时打印**完整**错误，不要只看最后一行。

几个坑：

| 现象 | 真相 |
|---|---|
| `unexpected eof while reading` | 纯网络抖动，重试即可（实测失败率约 1/3） |
| `schannel` 相关失败 | 系统级是 schannel，本仓库已设 `http.sslBackend=openssl`（openssl 明显更稳）。若换到别的仓库遇到同样问题，就 `git config http.sslBackend openssl` |
| `fatal: '<40位SHA>' does not appear to be a git repository` | **PowerShell 变量名不区分大小写**导致参数被覆盖。`$remote` 和 `$Remote` 是同一个变量 —— 写脚本时 SHA 一律带 `Sha` 后缀（如 `$remoteSha`），不要用 `$remote` |

凭据由 **gh CLI** 提供，且已写入**全局** git config，所以 VSCode、其它模型、其它工具都能直接用，不需要各自登录：

```
credential.https://github.com.helper = !'C:\Program Files\GitHub CLI\gh.exe' auth git-credential
```

若某天失效：`gh auth login` 重新登录一次即可。

> 仓库里还有 `http.sslBackend=openssl` 这一条是**仓库本地**配置（`.git/config`）。
> 在别处 clone 这个仓库时不会带过去，需要时手工设一次。

---

## 排查顺序

从内往外测，能把「LLM/工具/卡片」的问题和「IM 通道」的问题分开定位：

```
scripts/test_agent_local.py   脱离飞书测编排层（意图 → 工具 → 卡片）
scripts/smoke_robot_sim.py    机器人模拟器冒烟（自动时间快进跑完一轮）
scripts/listen_feishu.py      飞书通道全链路（收/发/图/卡片/按钮/PATCH）
scripts/inspect_db.py         存储体检（表行数、索引、真假数据构成）
```

---

## 改动后必须做

1. **同步更新 `docs/`** —— 见 `docs/README.md` 的维护契约表。
   改了行为却不动文档，下一轮的人（或 AI）会按错误的规格改代码。
2. **踩了新坑就写进 Spec 附录 A.6** —— 这一节对后来者价值最高。
3. **提交前确认没有密钥进版本库**：

   ```powershell
   git diff --cached --name-only | Select-String 'config/config.yaml|^data/|^logs/|^models/'
   ```

   仓库里只应有 `config/config.example.yaml`（占位符），不含真实凭据。
   历史上 vault 的文档目录里混进过 `账号和密钥.md`，批量复制文档时要当心。

---

## 代码地图

| 你想要的 | 在哪 |
|---|---|
| 点位/算法的绑定关系 | `config/routes.yaml` + `config/mapping.yaml` → `shared/scene.py` |
| 检测 → 告警的映射规则 | `robot_sim/mapper.py`（**绑定在这里生效**） |
| 巡检执行流程 | `robot_sim/robot.py` |
| 意图与工具定义 | `agent_service/core/intents.py` |
| 编排主循环 | `agent_service/core/orchestrator.py` |
| 卡片模板 | `agent_service/report/feishu_card.py` |
| 表结构与 SQL | `shared/db.py` + `shared/repo.py` |
| 飞书收发 | `agent_service/im/feishu.py` |
