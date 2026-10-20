# 淘宝天猫算账助手

给电脑端 Agent 使用的通用 Skill 与离线程序，**购物范围限定淘宝、天猫国内普通购物**。用户在当前 Agent 中发购物截图或文字，核对清单及当前规则后得到选券、拆单、加购及逐次购买步骤；可以接着纠错、回核结算、记录已购和规划剩余商品。

Agent 接口保持通用，优惠规则只维护淘宝天猫。官方依据、当前覆盖和待补能力见 [淘宝天猫规则参考](references/taobao-tmall.md)；不会把历史活动门槛当作当前默认规则。

**Python 3.10+，标准库，无模型密钥、外部服务或固定浏览器依赖。** 图片理解、对话和网页操作由宿主 Agent 提供；金额由程序计算。

## 立即试用

```bash
python3 scripts/shopping.py demo
```

这是明确标注的假设案例：

| 选择 | 总支出 | 结算次数 | 新增商品 |
| --- | ---: | ---: | --- |
| 只买原清单 | 390元 | 1 | 无 |
| 同意清洁布，少花钱 | 345元 | 2 | 10元清洁布 |
| 同意清洁布，少操作 | 375元 | 1 | 10元清洁布 |

默认使用真实输入时不允许加购，需明确批准具体候选品：

```bash
python3 scripts/shopping.py solve fixtures/demo-cart.json --format text
python3 scripts/shopping.py solve fixtures/demo-cart.json --approve cloth --format text
```

## 在 Agent 中使用

将本目录整体放入宿主支持的 Skill 目录，由宿主加载 [SKILL.md](SKILL.md)。没有目录式 Skill 支持，但能读文件和运行 Python 的 Agent，也可以直接使用这段指令：

> 请读取 `/实际路径/promo-shopping-assistant/SKILL.md`，按其中流程帮我算购物优惠。使用你已有的看图和浏览器工具整理资料，调用该目录里的程序计算，把任务保存在独立目录，后续继续读取同一任务。

然后用户只需说：

> 帮我看看淘宝天猫这几样怎么买划算。［截图］
>
> 台灯改成199，不要洁面。不要额外加购。
>
> 第一单已经买了，下一单怎么结算？

宿主需要本地文件读写和 Python 执行能力；读图和网页能力按宿主实际可用工具确定。纯聊天、无法执行程序的宿主可以整理材料，但不能独立完成本地精确求解。本仓库已验证本地CLI及整体复制运行，尚未逐一在不同品牌的 Agent 中实测，不能保证每家都自动发现 Skill。

## 可持续的本地任务

```bash
python3 scripts/shopping.py new /你的任务目录 --input fixtures/demo-cart.json
python3 scripts/shopping.py show /你的任务目录 --format text
python3 scripts/shopping.py compare /你的任务目录 --format text
```

修改通过 `update --patch`，已购通过 `purchase --receipt`，详见 [工作流协议](references/workflow.md)。任务完整保存在一个 `session.json` 中，可跨对话继续。旧结果随修改失效；用券按账户共享次数记账；缺价格、运费或阻塞信息时保留资料并提示补充。

## 结构与验证

```text
SKILL.md                     Agent 的使用入口
scripts/shopping.py           稳定命令入口，支持单次计算和持续任务
scripts/workflow.py           状态、偏好、方案比较与购买记录
scripts/taobao.py             淘宝天猫平台范围、店铺类型及规则适配
scripts/optimize.py           精确计算内核，保留上游快照
references/                   任务与金额规则协议
fixtures/                     明确标注的演示与预售案例
tests/                        计算、状态和CLI行为验证
docs/                         来源和验证记录
```

```bash
python3 -m unittest discover -s tests -v
python3 scripts/build_release.py
```

构建生成 `dist/promo-shopping-assistant-0.2.0.zip` 及其SHA-256，可解压后整体迁移。构建不打包本地任务和缓存。验证范围及限制见 [docs/VERIFICATION.md](docs/VERIFICATION.md)，计算来源见 [docs/UPSTREAM.md](docs/UPSTREAM.md)。

## 范围

支持淘宝天猫小购物车中已核实的满减、每满减与封顶、优惠范围与互斥、账户券次数、拆单、明确批准的候选品、固定店铺运费和已付定金。淘宝店和天猫店在已确认可合并的组内共同计算，天猫专属券按店铺类型限制；券后价、尾款及未知价格口径会阻止计算，避免重复优惠。当前计算范围最多12件、12条优惠；复杂输入可能超时，会明确返回未完成。

其他购物平台、天猫国际及跨境税费暂不支持。官方立减若需按比例计算、88VIP折扣、件数折扣、红包/淘金币、支付权益、购券成本、返现/赠券循环、退货凑单和加购预算约束尚未完整建模；遇到会影响方案的未支持条件时补问或说明范围。图片识别质量依赖宿主，尚无真实平台验价记录；预计金额只适用于本次资料及已表达的规则。程序只计算和记账，不访问购物账号、下单或付款。
