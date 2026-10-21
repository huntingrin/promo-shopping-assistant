# 程序与任务协议 v1

核心 Python 3.10+，无第三方依赖；任意工作目录均可执行。以下的 `<skill-dir>` 指包含 `SKILL.md` 的目录。输入字段区分大小写，金额推荐十进制字符串，数量和版本为整数。程序不直接处理图片或访问网页：宿主 Agent 默认用浏览器采集购物车并整理为下述资料；浏览器不可用时才用截图/文字。浏览器协议见 [browser-cart.md](browser-cart.md)。可选浏览器适配器另装 Playwright。

v0.4 的产品入口只接受淘宝/天猫国内普通购物。先读 [taobao-tmall.md](taobao-tmall.md) 核实当前规则；原 `input-spec.md` 是保留的通用内核协议，不能据其任意平台字段推定产品支持其他平台。比例、多件、共享余额与支付优惠使用 [discounts.md](discounts.md) 的增补协议；原内核文档的“未覆盖比例”等说明只适用于原快照。

## 命令

```text
python3 <skill-dir>/scripts/shopping.py demo [--format json|text]
python3 <skill-dir>/scripts/shopping.py solve CART.json [--approve ITEM_ID ...] [--prefer cheapest|fewest_checkouts] [--format json|text]
python3 <skill-dir>/scripts/shopping.py new TASK_DIR [--input CART.json] [--title TITLE]
python3 <skill-dir>/scripts/shopping.py import-cart TASK_DIR --snapshot SNAPSHOT.json
python3 <skill-dir>/scripts/shopping.py show TASK_DIR [--history] [--format json|text]
python3 <skill-dir>/scripts/shopping.py update TASK_DIR --patch PATCH.json
python3 <skill-dir>/scripts/shopping.py compare TASK_DIR [--time-limit 15] [--format json|text]
python3 <skill-dir>/scripts/shopping.py purchase TASK_DIR --plan cheapest --order 1 --receipt RECEIPT.json
```

`demo --discounts` 运行新折扣叠加假设案例，预计67.22元，无任务副作用。

`--approve` 可重复，例如 `--approve cloth --approve box`，由 Agent 代为传递已经得到的具体商品同意。`solve` 不保存状态；`demo` 自动批准假设案例中的清洁布并展示390/345/375元三种情景，不写任务文件。

`new` 不覆盖已有任务。大多数命令默认标准输出JSON；演示默认可读文字。`show` 默认省略历史，`--history` 返回历史快照。

## 浏览器快照导入

默认流程是 `optional/browser/browser_cart.py capture` 生成 `cart-snapshot.json`，再执行：

```bash
python3 <skill-dir>/scripts/shopping.py import-cart TASK_DIR --snapshot SNAPSHOT.json
```

- 任务不存在时创建，已存在时按商品/优惠/运费ID合并；保留任务里其他手工修正。
- 快照中的缺失价格、店铺、运费或不完整优惠生成阻塞问题；导入成功不等于已算出金额。
- `source.kind=browser`，来源记录页面URL、页面类型、观察时间和关键原文；地址、手机号、支付信息不导入。
- 价格标签决定 `price_kind`：活动价/售价按优惠前价；券后/到手价、尾款或未知口径阻止求解。
- `promotion_hints` 中未被完整规则覆盖的优惠只生成待核实问题，不猜门槛或叠加方式。
- 只读结算预览写入 `checkout` 供核对，不视为已购买；`purchase` 仍需用户明确确认。

快照字段、登录和只读红线见 [browser-cart.md](browser-cart.md)。

## 初始购物资料

完整计算语义见 [input-spec.md](input-spec.md)。包装层额外接受商品 `spec`（规格字符串）、`marketplace`（淘宝/天猫店铺类型）和 `price_kind`（价格口径），优惠额外接受 `marketplaces` 范围条件；求解前将这些元数据编译为内核可用的输入。其他商品及优惠字段沿用内核协议，不接受未知规则字段。

| 字段 | 淘宝天猫适配语义 |
| --- | --- |
| 商品 `platform` | `taobao` 是统一交易域；输入 `tmall` 自动转换为 `taobao` 并标记天猫店；其他平台拒绝 |
| 商品 `marketplace` | 可填 `taobao` 或 `tmall`；未知可省略/设为 `null`，影响限定店铺类型的用券时会补问 |
| 商品 `price_kind` | `before_order_promotions` 符合原计算协议；`after_coupons` / `tail_payment` / `unknown` 均阻止求解。省略表示调用者按原协议提供优惠前价格；Agent 从浏览器或截图提取时应显式标记 |
| 优惠 `platform` | `taobao` 可涵盖淘宝及天猫店；`tmall` 简写等价于 `platform=taobao, marketplaces=["tmall"]` |
| 优惠 `marketplaces` | 与店铺、商品ID、品类、标签共同以 AND 匹配；淘宝店专属写 `["taobao"]`，天猫专属写 `["tmall"]` |
| 运费 `platform` | 淘宝/天猫均归入 `taobao` 交易域，按实际唯一店铺标识配置，不能为同店重复配置 |

同交易域不自动保证同次能付款，`checkout_group` 仍表示已核实的可合并阶段/渠道。天猫与淘宝店的类型不单独导致拆单。适配版本保存在结果的 `market=taobao-tmall` 和 `rule_profile=taobao-tmall-v2` 中，并计入输入指纹，旧适配版本的缓存不能冒充新结果。

最小可计算例：

```json
{
  "version": 1,
  "items": [
    {"id": "lamp", "name": "台灯", "spec": "暖白光", "platform": "taobao", "marketplace": "tmall", "shop": "B",
     "price_kind": "before_order_promotions",
     "price": "199.00", "quantity": 1,
     "source": {"kind": "user", "text": "台灯199元，1件", "observed_at": "2026-10-20"}}
  ],
  "promotions": [],
  "shipping": [
    {"platform": "taobao", "shop": "B", "fee": "0.00", "basis": "price",
     "source": {"kind": "user", "text": "确认包邮"}}
  ],
  "assumptions": ["用户确认无其他优惠，台灯199元为优惠前活动价。"]
}
```

草稿允许缺失或 `null` 的关键金额；有ID就可以先保存。商品缺价格、平台或店铺，优惠缺核心条件，或缺明确的运费配置，会产生阻塞问题。超过12件的清单仍保存，但不求解。允许范围按必买商品与用户已批准的候选计算，未批准候选不参与金额计算。

`source` 可以是文字或对象。截图示例：

```json
{"kind":"screenshot","path":"/task/cart-01.png","region":"上半部第二行","observed_at":"2026-10-20T21:00:00+08:00","fields":{"price":"活动价199元","quantity":"数量1"}}
```

浏览器示例：

```json
{"kind":"browser","page":"cart","url":"https://cart.taobao.com/cart.htm","observed_at":"2026-10-21T18:00:00+08:00","text":"台灯 暖白光 活动价 ¥199.00 数量1"}
```

来源缺失会标为待核实，但不会伪造来源。程序检查结构和金额，来源真实性、页面去重、价格口径与规则完备性由 Agent 核实；不确定时保存阻塞问题。`data_as_of` 表示最近一次资料更新时点，不表示每个字段在同一时刻重新验价；各字段的观察时间见来源。

底层 `optimize.py` 会按0元模拟未配置运费并警告；高层 `shopping.py` 会先阻止该情况。面向用户使用高层入口。

## 更新文件

每次先 `show`，取当前 `revision` 放入 `expected_revision`。修改成功版本加1，当前结果清空，旧资料和旧结果写入历史；版本冲突不修改文件。

```json
{
  "expected_revision": 1,
  "reason": "用户确认台灯199元，清洁布用得上，可以带一条",
  "items": [
    {"id": "lamp", "price": "199.00", "source": {"kind": "user", "text": "台灯199元"}}
  ],
  "preferences": {
    "allow_addons": true,
    "approved_optional_ids": ["cloth"],
    "preferred": "cheapest"
  }
}
```

| 字段 | 行为 |
| --- | --- |
| `items`, `promotions` | 按ID合并字段；不存在则新增；数组顺序保持优惠执行顺序，新增优惠追加到末尾 |
| `shipping` | 按 `platform + shop` 合并；要取消包邮门槛，显式将 `free_threshold` 设为 `null` |
| `resources` | 按ID合并金额余额或积分数量；已确定的单位类型不能改变，已购后剩余值持续保存 |
| `remove_items`, `remove_promotions` | 删除指定ID；商品退出购买用 `remove_items`，不是把 `required` 改成可选 |
| `preferences` | 合并偏好；候选品需先存在且 `required=false`；批准清单不能包含必买品或未知ID |
| `questions` | 替换当前手动补问列表；每条必填 `id`, `text`, `blocking`，可有 `source` |
| `resolve_questions` | 删除已经核实的手动问题ID；缺字段/运费的自动问题随资料更新自动消失 |
| `assumptions` | 替换假设数组 |
| `max_orders` | 更新可接受的最多结算次数，1–12；`preferred` 只改变推荐偏好，不改变计算输入 |
| `data_as_of`, `label` | 可选的资料时点或情景名称 |

同次更新中的ID不能重复，也不能同时修改和删除。删除商品后清理失效的商品专属规则和运费；投影到不加购方案时同样清理不适用规则，适用的账户券次数仍保持全局共享。修改价格等事实但未给新来源时，保存此次修改原因作为用户修正来源，旧资料留在历史；对浏览器或截图提取的修正请显式传准确来源。

优惠顺序就是输入数组顺序。需要重排时，保存当前状态后在一次更新里删除相关旧规则，再通过**下一次更新**按正确顺序添加；每次都使用新的版本号。金额/规则核实和人工问答不由程序自动完成。

## 计算结果

完整求解后 `status=compared_under_input_rules`。`plans` 有三个稳定键：

- `original`：仅必买品，在当前结算次数上限内求最低总支出。
- `cheapest`：必买品和已批准候选品中求最低总支出；同金额优先少加购，再少结算。
- `fewest_checkouts`：先取必买品所需的最少可行结算组数，在该次数下求最低总支出。

当前规则的件数/金额门槛只决定优惠是否生效，不禁止正常购买，所以每个不同的 `(platform, checkout_group)` 都可以一次买齐，组数仍是最少可行次数。自动共享封顶可能改变购买顺序及后续券是否生效，新引擎在该次数内也比较顺序，并可在省钱策略下增加已批准的凑单批次；不会通过跳过自动优惠伪造门槛。

每个方案保留 `summary`, `orders`, `coupon_usage`, 假设、提示和求解统计。包装层的 `summary.net_savings_vs_original` 始终与**当前资料、同一上限下的原清单全局最优总支出**比较；可能为负数。内核基于各自次数上限计算的局部节省字段在包装层删除，避免混用基准。

`choices` 将金额、商品、步骤一致的方案合并，`aliases` 说明它对应哪些策略；`recommended` 指当前购买偏好。输入版本、输入指纹和生成时间随结果保存，`show` 不返回指纹/版本失效的旧结果。

有已购记录时 `scope=remaining_cart`，`purchased.total_spend` 为已购完整支出，各方案 `summary.total_spend` 为剩余清单预计支出，`summary.task_total_spend` 为两者之和。尚未买过时 `scope=whole_cart`，两项总支出相同。

| 退出码 | 状态/含义 |
| --- | --- |
| 0 | 操作成功，或 `compared_under_input_rules` 完整方案，或 `complete` 原清单已购完 |
| 2 | 输入/文件/版本/并发冲突；解析及更新错误在stderr输出JSON，求解输入错误在stdout返回 `invalid_input` |
| 3 | `search_limit`，本次比较未完成，不返回部分方案 |
| 4 | `needs_information`，资料已保存但缺关键信息，不返回金额方案 |

`compare` 的15秒默认上限覆盖整次比较，最高300秒；单次求解还有状态数保护。计算失败也会替换任务里的 `latest_result`，不会把上次成功结果当成新结果。错误、超限或缺信息时，结果的 `plans` 为空。

## 结算记账

仅在用户确认某次已经买下后使用。不能将点击“去结算”、只读结算预览或推荐步骤当成已购。

```json
{
  "expected_revision": 2,
  "id": "receipt-20261020-001",
  "pay_now": "200.00",
  "coupon_ids": ["beauty"],
  "source": {"kind": "user", "text": "第一单已经买了，实付200，用了美妆券"}
}
```

```bash
python3 <skill-dir>/scripts/shopping.py purchase <task-dir> --plan cheapest --order 1 --receipt <receipt-file>
python3 <skill-dir>/scripts/shopping.py compare <task-dir>
```

`coupon_ids` 填本次实际执行的 `mode=selectable` 优惠ID（无则 `[]`），不包含自动满减；包括不限次数的可选活动。程序要求它们与当前该次结算一致，否则先核对和更新输入。只对有次数上限的优惠扣除一次，保留全局资源约束。

任务内另存 `coupon_balances`，即使规则随已购商品清理也保留剩余次数。之后重新引入同ID可选优惠而未填 `total_uses` 时，沿用已记录余额；显式填写 `total_uses` 表示重新核实后的当前余额，不能把旧截图或旧快照的初始券数填回去。

每次只记录一个结算步骤；之后旧方案失效，先重新计算再记录下一次，避免顺序变化后误用旧订单序号。凭据ID不能重复；实付金额不含此前已付定金，程序额外记录完整支出。

商品数量按本次结算扣除；原清单全部买完后返回 `complete`，剩余未买的可选候选不强制购买。若实付金额与预计不同，会生成阻塞差异问题；查清是否影响剩余规则后，通过更新和 `resolve_questions` 解除。

## 从v0.1/v0.2/v0.3继续任务

仍使用 `session.json` v1。旧任务的淘宝/天猫别名在读取时规范化，旧结果因规则适配指纹变化而失效，需要重新计算。普通 `show` 不改写原文件；下一次更新/计算会原子保存规范化的资料。

原来使用 `demo`、`p`、`q` 等虚构平台或其他真实平台的任务不属于新产品范围，会明确拒绝；源文件保留，不自动把它们冒充淘宝资料。假设演示请使用当前附带的淘宝天猫格式案例。低层 `optimize.py` 仅作为原算法快照与测试入口保留，面向用户使用 `shopping.py`。

## 本地文件与并发

所有状态原子写入 `TASK_DIR/session.json`，包含输入、偏好、补问、已购凭据、历史和当前结果。命令通过任务内 `.shopping.lock` 阻止并发写入；若命令崩溃遗留锁，先确认其中PID已结束再删除该锁，不删除任务文件。历史可能包含用户截图路径、浏览器快照、来源原文及消费记录，不上传到代码仓库。
