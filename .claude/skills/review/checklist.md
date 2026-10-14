# 上线前评审清单（Pre-landing Review）

适用仓库：**巡逻 Agent Demo**（`D:\code\patrol_agent`）

用法：提交/推送前逐条走一遍。标 **[一票否决]** 的项不过就停，不要"先推了再说"。

> 本清单里的每条结论都是在本仓库**实测**得到的，不是通用建议。
> 如果你发现某条与当前实际不符，先核实再改这份清单，不要凭现象下结论
> —— 这个仓库历史上就出现过「照着一个没有依据的结论去排查」的事故。

---

## 0. 范围界定（先做）

- [ ] 本次改动**只包含一个功能或一个修复**，没有夹带无关文件
- [ ] 逐条看过 `git status`，确认没有把**其他会话/工具未完成的改动**一起带进来
- [ ] 确认这些**不该进版本库的没进**：
      `data/`、`logs/`、`models/`、`*.db`、`*.pt`、`config/config.yaml`

```powershell
git status --short
git diff --cached --name-only | Select-String '^data/|^logs/|^models/|\.db$|\.pt$'
```

---

## 1. 凭据安全 **[一票否决]**

```powershell
# 1) 配置文件绝不能进
git diff --cached --name-only | Select-String 'config/config\.yaml$'

# 2) 暂存内容里搜凭据特征（把下面的模式换成当前项目的真实值）
git grep --cached -n --fixed-strings -- 'APP_ID_PREFIX_REPLACE_ME' ; git grep --cached -n --fixed-strings -- 'sk-'
```

- [ ] 仓库里只有 `config/config.example.yaml`（占位符），没有真实凭据
- [ ] 新加的文档里没有贴 app_id / secret / token（**文档也要查**，不只是代码）

> 历史事故：批量复制文档时把 vault 里的 `账号和密钥.md` 一起复制进了 `docs/`。
> 批量操作前先看清单，别用 `*.md` 这种通配符直接复制。

---

## 2. 行尾与文件格式

**本仓库全部使用 LF 行尾，`core.autocrlf=false`。** 新文件必须也是 LF。

```powershell
# 检查工作区文件的行尾（CRLF>0 且 LF=0 就是纯 CRLF，与仓库不一致）
Get-ChildItem -Recurse -File -Include *.py,*.md,*.yaml,*.ps1 |
  Where-Object { $_.FullName -notmatch '\\\.venv\\|\\data\\|\\logs\\' } |
  ForEach-Object {
    $b=[IO.File]::ReadAllBytes($_.FullName); $c=0;$l=0
    for($i=0;$i -lt $b.Length;$i++){ if($b[$i] -eq 10){ if($i -gt 0 -and $b[$i-1] -eq 13){$c++}else{$l++} } }
    if($c -gt 0){ "{0}  CRLF={1} LF={2}" -f $_.Name,$c,$l }
  }
```

- [ ] 上面命令没有输出（或输出为空）

**修复方式**（把 CRLF 文件转成 LF）：

```powershell
$files = @("路径1","路径2")
foreach ($f in $files) {
  $t = [IO.File]::ReadAllText($f) -replace "`r`n", "`n"
  [IO.File]::WriteAllText($f, $t)
}
```

- [ ] 没有引入尾随空格、BOM（CSV 导出**例外**，那里必须用 `utf-8-sig`，见下方不变量）

> 注意：`git diff --check` 会把 CRLF 文件的行尾 CR 报成空白错误。
> 那不是行尾风格问题被检测出来了，而是**文件真的不符合仓库规范**。
> 不要用 `core.whitespace=cr-at-eol` 去掩盖它。

**建议的一次性改进**（需谨慎，会影响所有人的下次 `git add`）：
新增 `.gitattributes` 写 `* text=auto eol=lf`，从机制上杜绝复发。

---

## 3. 项目不变量（每条都对应一次真实踩坑）

改动若触碰以下任一项，必须显式确认没破坏它。详见 `AGENTS.md` 与
`docs/巡逻Agent_Spec.md` 附录 A.6。

| 不变量 | 怎么验证 |
|---|---|
| 写操作两段式：`prepare_*` 出确认卡 → 用户点按钮才 `execute_*` | 检查没有新代码直接从编排层调机器人写接口 |
| 卡片/图片结果**不写入对话历史** | 搜 `memory.add_assistant` 的调用点，只应有真正的文本回复 |
| 飞书回调 **3 秒内返回** | 新增的卡片动作必须丢后台线程，不在回调里同步做耗时操作 |
| 幂等去重用 `message_id` | 搜 `event_id`，不应出现在去重逻辑里 |
| system prompt **注入当前日期** | `intents.system_prompt()` 仍包含日期注入 |
| 点位只上报**绑定算法**的检测结果 | 改检测链路后，确认结果图与文字回显仍只含 `point.algorithm.coco_label` |
| 编号用 `db.next_id()` | 搜 `COUNT(*)+1`，不应出现在 ID 生成里 |
| 自检不动共享句柄 | `Camera.probe()` 不应 `close()` |
| 模拟数据标 `is_mock=1` | 新增的造假数据路径必须打标 |
| 不承诺未实现的能力 | 卡片/工具描述里不出现做不到的功能（如定时巡逻） |
| CSV 导出用 `utf-8-sig` | 否则 Excel 打开中文乱码 |

---

## 4. 验证（从内往外，便于定位）

```powershell
$py = "D:\code\patrol_agent\.venv\Scripts\python.exe"

# 1) 语法与编译
& $py -m compileall -q agent_service robot_sim shared scripts

# 2) 脱离飞书测编排层（意图 → 工具 → 卡片）
& $py scripts\test_agent_local.py "昨天的巡逻报表" "让机器人去巡逻一圈"

# 3) 机器人模拟器冒烟（需要 :8100 在跑，自动时间快进）
& $py scripts\smoke_robot_sim.py

# 4) 本次改动的专项测试
& $py -m pytest scripts -q       # 若有 pytest 用例
```

- [ ] 编译通过
- [ ] 受影响路径**实际跑过一遍**，不是"读了代码觉得没问题"
- [ ] 新增测试覆盖了本次改动的主路径
- [ ] 若改了卡片渲染，**看过渲染结果**（卡片/图片都要目视确认，别只看返回值）

> 涉及摄像头时注意：`robot_sim` 会**独占摄像头**直到进程退出，
> 跑测试期间别的程序（飞书会议）拿不到画面。

---

## 5. 文档同步

改了行为却不动文档，下一个接手的人（或 AI）会按错误的规格改代码。
契约表见 `docs/README.md`。

- [ ] 改了接口/数据模型/能力边界 → 更新 `docs/巡逻Agent_Spec.md`（**含附录 A**）
- [ ] 改了意图/卡片 → 更新 `docs/巡逻Agent_交互设计.md`
- [ ] 踩了新坑 → **写进 Spec 附录 A.6**（这一节对后来者价值最高）
- [ ] 实现 A.7 里"尚未实现"的某项 → 把它从 A.7 移除
- [ ] README 的能力边界表若受影响，一并更新

---

## 6. 提交信息

- [ ] 说明**为什么**改，不只是改了什么
- [ ] 修 bug 的，写清**故障现象**（便于日后搜到）
- [ ] 如果上一次提交里的结论后来被推翻，**在本次提交里更正它**，不要沉默修改

> 提交信息里含 `$`、反引号等字符时，用 `git commit -F <消息文件>`，
> 不要用 `-m "..."` —— PowerShell 会展开变量，把命令搞坏。

---

## 7. 推送

**本机到 github.com 的 TLS 失败率约 1/3**（实测：6 次里失败 2 次），
报错形如 `TLS connect error: error:0A000126:SSL routines::unexpected eof while reading`。
**这不是认证问题，重试就能成。所以不要直接 `git push`。**

```powershell
.\scripts\git_push.ps1                 # 自动重试 8 次
.\scripts\git_push.ps1 -MaxAttempts 15
```

- [ ] 用脚本推送，不要裸跑 `git push`
- [ ] 失败时看**完整**错误，不要只看最后一行

---

## 8. 必须停下来问人的情况

不要自行决定、也不要"猜一个合理的做法"：

- [ ] 要改写**已推送**的历史（`push --force`）—— 高风险，必须人工确认
- [ ] 要删数据或改数据库结构且**无法回滚**
- [ ] 发现凭据泄露（哪怕已经删除，也要报告，因为可能已进过历史）
- [ ] 改动会**破坏既有功能**而你没有对应的修复方案
- [ ] 发布流程要求的产物缺失（如本清单），且**无法从仓库内推断**该怎么补
- [ ] 不确定该推 `main` 还是功能分支时 —— 先问，不要默认推 `main`

---

## 附：本仓库的已知环境坑

| 现象 | 真相 |
|---|---|
| `fatal: '<40位SHA>' does not appear to be a git repository` | **PowerShell 变量名不区分大小写**：`$remote` 与 `$Remote` 是同一个变量，SHA 覆盖了远端名。SHA 变量一律带 `Sha` 后缀 |
| `Repository not found` / `correct access rights` | 不要臆断成代理问题。先 `gh auth status`，再看完整错误 |
| `schannel` 握手失败 | 系统级是 schannel，本仓库已设 `http.sslBackend=openssl`。换仓库需手工设一次 |
| 摄像头被占 | `robot_sim` 常驻持有，Windows 相机独占。别在用户要开会时留着服务跑 |
| 端口被占 | 先确认占用进程确实是本项目的 uvicorn；出现过「pwsh 包装器退出了但 python 子进程还活着」 |

凭据由 **gh CLI** 提供并写入**全局** git config，VSCode / 其它模型 / 其它工具都能直接推送，
无需各自登录。失效时 `gh auth login`。
