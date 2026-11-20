# 保护区外业档案和联合评估收件

本站模拟独立报送的保护区及评估办公室，地点、人员、季节档案、观测及收件均为合成资料。运行依赖为 Python 3.11 标准库。

```sh
python3 source_service.py --port 8040
python3 -m unittest discover -s tests -v
```

根路径写有当前 window 和剩余整包领取名额。演练人员用 `POST /clock` 发送整数 window 推进轮次。重新启动收件端不重置站点发布或办公室回条。

## 站点档案

`GET /campaigns` 提供站点每个工作季的预期访问编号。访问编号在站点、季节内识别不同申报的队次。另据到访正文团队和编号确定访问事实。同站点相同访问的多个部件必须分别核对。目录 `GET /catalog?station=north&season=2026` 返回 edition 和成员列表。后续页继续传 edition 和 next 所给的 after 值。409 表示当前代次已经替换，从旧页拼出的清单没有完整性承诺。open 目录的 missing 明确表示当前尚未申报的访问。sealed 是站点锁定目录，仍须按目录辨别实际缺少的访问或领取失败的部件。

成员记录给 id、visit、类型、归档文件名、完整 SHA-256 和可选短期直取 href。introduced 是机构宣告拟提供成员的轮次，实际可用状态仍以领取响应为准。404 表示未到，410 表示短期入口关闭。状态 withdrawn 由目录显式确认，应撤去当前版本的这份部件。archive 为整季 ZIP，等同于该目录的版次，文件名按成员清单对应，压缩包开放后每轮最多领一次，再领返回 429。之前收到的同一部件可以用 id 和摘要核对，后来的包不能覆盖先前已送审时使用的真实材料。整份记录合成，无现场照片和敏感地点。

## 到访与对应卡

visit 文件提供 visit、plot、team、method、outcome、sections。visited 表示站点声称到访，独立 presence 文件的 confirmed 才可作为已到访凭据。cancelled 是明确取消，保留该次访问的记录。observation 内的 species_count 是该访问在所列小区划范围内的观测数量。若同一访问含多个小区划，文件的 sections 按小区划分开记录。零值必须同时具备同次有效到访凭据、有效观测表和要求的小区划覆盖。没有确认到访或没有观测表属于缺证。

`GET /cards` 返回已发布的样地对应卡。previous 和 target 分别为 2019、2026 样地，sections 是该比较单元两期都需覆盖的部位，method 约定两期使用的方法。unit 是办公室采用的比较单元。comparable 指可以用相应部位做两期观察比对，context_only 仅表明地理关联，review 表示站点需要复勘。supersedes 明确替换一张已发布卡。对多队次记录逐次核对，不能以一个同地域样点的总次数充当另一小区划的实际到访次数。

## 办公室回条

`GET /office/receipts` 返回当前已知的批次回条，units 中各单元有独立接受或退回事实。退回时 missing 是办公室指出的证据编号。`POST /office/submit` JSON 含 receipt_request（一次提交的编号）及 units。每项有 unit、action（submit 或 withdraw）、materials（本次证明的部件标识），送审可带 card。已送审单元撤下时带 previous 回条编号。同一提交编号必须对应原内容。办公室可分别受理同一批次中的多个单元，历史受理和后续撤下会分别保留回条。
