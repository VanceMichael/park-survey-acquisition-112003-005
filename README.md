# 保护区外业档案和联合评估收件

本站模拟独立报送的保护区及评估办公室，地点、人员、季节档案、观测及收件均为合成资料。运行依赖为 Python 3.11 标准库。

```sh
python3 source_service.py --port 8040
python3 -m unittest discover -s tests -v
```

根路径写有当前 window 和剩余整包领取名额。演练人员用 `POST /clock` 发送整数 window 推进轮次。重新启动收件端不重置站点发布或办公室回条。

## 站点档案

`GET /campaigns` 提供站点每个工作季的预期访问编号。访问编号在站点、季节内识别不同申报的队次。另据到访正文团队和编号确定访问事实。同站点相同访问的多个部件必须分别核对。目录 `GET /catalog?station=north&season=2026` 返回 edition 和成员列表。后续页继续传 edition 和 next 所给的 after 值。409 表示当前代次已经替换，从旧页拼出的清单没有完整性承诺。open 目录的 missing 明确表示当前尚未申报的访问。sealed 是站点锁定目录，仍须按目录辨别实际缺少的访问或领取失败的部件。

成员记录给 id、visit、类型、归档文件名、完整 SHA-256 和可选短期直取 href。introduced 是机构宣告拟提供成员的轮次，实际可用状态仍以领取响应为准。404 表示未到，410 表示短期入口关闭。状态 withdrawn 由目录显式确认，应撤去当前版本的这份部件。replaces 声明确认新成员替代哪份旧件，旧件仅保留历史用途。archive 为整季 ZIP，等同于该目录的版次，文件名按成员清单对应，压缩包开放后每轮最多领一次，再领返回 429。之前收到的同一部件可以用 id 和摘要核对，后来的包不能覆盖先前已送审时使用的真实材料。整份记录合成，无现场照片和敏感地点。

## 到访与对应卡

visit 文件提供 visit、plot、team、method、outcome、sections。visited 表示站点声称到访，独立 presence 文件的 confirmed 才可作为已到访凭据。cancelled 是明确取消，保留该次访问的记录。observation 内的 species_count 是该访问在所列小区划范围内的观测数量。若同一访问含多个小区划，文件的 sections 按小区划分开记录。零值必须同时具备同次有效到访凭据、有效观测表和要求的小区划覆盖。没有确认到访或没有观测表属于缺证。

`GET /cards` 返回已发布的样地对应卡。previous 和 target 分别为 2019、2026 样地，sections 是该比较单元两期都需覆盖的部位，method 约定两期使用的方法。unit 是办公室采用的比较单元。comparable 指可以用相应部位做两期观察比对，context_only 仅表明地理关联，review 表示站点需要复勘。supersedes 明确替换一张已发布卡。requires 是此修订新增的必要访问凭证，必须对应今次部件身份。对多队次记录逐次核对，不能以一个同地域样点的总次数充当另一小区划的实际到访次数。

## 办公室回条

`GET /office/receipts` 返回当前已知的批次回条，units 中各单元有独立接受或退回事实。退回时 missing 是办公室指出的证据编号。`POST /office/submit` JSON 含 receipt_request（一次提交的编号）及 units。每项有 unit、action（submit 或 withdraw）、materials（本次证明的部件标识），送审可带 card。已送审单元撤下时带 previous 回条编号。同一提交编号必须对应原内容。办公室可分别受理同一批次中的多个单元，历史受理和后续撤下会分别保留回条。

## 联合评估收件工作台

`joint_workbench` 是收件与比对的工作台层，读取上面的站点/办公室数据源，不修改原始档案。运行：

```sh
python3 source_service.py --port 8040          # 另开终端
python3 workbench.py --source http://127.0.0.1:8040 --view digest
python3 workbench.py --source http://127.0.0.1:8040 --window 1 --view checklist --station north
python3 workbench.py --source http://127.0.0.1:8040 --window 6 --view evaluator --unit U-East
python3 workbench.py --source http://127.0.0.1:8040 --window 6 --view unit --unit U-East
python3 workbench.py --source http://127.0.0.1:8040 --window 5 --view archive-gap --archive NF26R
python3 workbench.py --source http://127.0.0.1:8040 --window 6 --view archivist --station north
python3 workbench.py --source http://127.0.0.1:8040 --window 6 --view committee
python3 workbench.py --source http://127.0.0.1:8040 --window 6 --view field --station north
python3 workbench.py --source http://127.0.0.1:8040 --window 6 --view ready
python3 workbench.py --state workbench-state.json --view ready     # 载入检查点并沿用已收部件
```

也可以作为库使用：

```python
from joint_workbench import JointWorkbench

work = JointWorkbench("http://127.0.0.1:8040")
work.refresh()
work.advance(1)
work.fetch_object("north", "2026", "V-N26-1")
work.fetch_archive("NF19")            # 每轮名额内只开一次，同一代次不重复占用
print(work.committee_view())          # 委员会：真的复测过 / 没到 / 找过但未观察到
print(work.field_reconciliation("north"))  # 两轮逐次访问对齐，先确认收全
print(work.checklist("north"))        # 站点清点单：按访问轮次补交
print(work.evaluator_view())          # 评估员：可比 / 不可比（含待复勘）/ 缺证 / 真实未观测 / 没到
print(work.archivist_view("north"))   # 资料员：还须追哪个目录、包、部件
```

关键规则：

* **目录代次**：翻页始终带当前 edition 和 `after`；中途 409 会丢弃旧页并从新代次重读，不把两代次拼成一份清单。每次 `refresh()`/`advance()` 重读目录，封期以现版目录为准。
* **部件收件**：只按目录声明的 id、件名和完整 SHA-256 收；后到同摘要合并为一份，后到不同摘要不覆盖先收的真实材料。`withdrawn` 部件不入比较证据，`replaces` 声明的旧件只保留历史用途。
* **整包**：每轮只开少量包，同一代次再领返回缓存结果而不重复占名额。包内逐件与目录成员核对，未声明文件记为 `extra`，未带入的声明件按「先收原件保留 / 短期传输缺失 / 已撤回」分类，不因有一个封期包就把其他站或旧访问视为收妥。
* **访问事实**：`visited` 只是站点声称，独立 `presence.confirmed` 才是到访凭据；零值还须有有效观测表和要求小区划覆盖，否则只算缺材料。取消访问保留站点说明，不计为到访，也不再按未交凭据追收。
* **比较单元**：以最新对应卡为准（`supersedes` 链取末端）。两期方法须一致，按卡上 `sections` 逐部位核对；`context_only` 永不作为两期比较，`review` 在复勘前不重建比较。`requires` 只认今次目录中可核查的部件身份，不能用历史相同观测值抵消补交义务。同一旧样地的重叠部位不能同时支撑两个新样地；小区划不重叠的试点不受影响。
* **办公室范围**：已受理单元默认不重送；对应卡改判或迟交影响已受理单元时，给出原送审资料与需撤下的结论，办公室确认前原批仍按送审依据解释。`withdraw_changed` 只撤受影响单元，`submit` 可先推进其他已具备依据的单元；同一 `receipt_request` 重放必须对应原内容。
