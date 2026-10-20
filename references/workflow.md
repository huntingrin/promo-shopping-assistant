# 程序与任务协议 v1

Python 3.10+，无依赖；任意工作目录均可执行。以下的 `<skill-dir>` 指包含 `SKILL.md` 的目录。输入字段区分大小写，金额推荐十进制字符串，数量和版本为整数。程序不直接处理图片：宿主 Agent 将材料整理为下述资料。

## 命令

```text
python3 <skill-dir>/scripts/shopping.py demo [--format json|text]
python3 <skill-dir>/scripts/shopping.py solve CART.json [--approve ITEM_ID ...] [--prefer cheapest|fewest_checkouts] [--format json|text]
python3 <skill-dir>/scripts/shopping.py new TASK_DIR [--input CART.json] [--title TITLE]
python3 <skill-dir>/scripts/shopping.py show TASK_DIR [--history] [--format json|text]
python3 <skill-dir>/scripts/shopping.py update TASK_DIR --patch PATCH.json
python3 <skill-dir>/scripts/shopping.py compare TASK_DIR [--time-limit 15] [--format json|text]
python3 <skill-dir>/scripts/shopping.py purchase TASK_DIR --plan cheapest --order 1 --receipt RECEIPT.json
```

`--approve` 可重复，例如 `--approve cloth --approve box`，由 Agent 代为传递已经得到的具体商品同意。`solve` 不保存状态；`demo` 自动批准假设案例中的清洁布并展示390/345/375元三种情景，不写任务文件。

`new` 不覆盖已有任务。大多数命令默认标准输出JSON；演示默认可读文字。`show` 默认省略历史，`--history` 返回历史快照。

## 初始购物资料

完整字段与计算语义见 [input-spec.md](input-spec.md)。包装层额外接受商品 `spec`（规格字符串），把它带入购买步骤，求解前移除此展示字段。其他商品及优惠字段沿用内核协议，不接受未知规则字段。

最小可计算例：

```json
{
  "version": 1,
  "items": [
    {"id": "lamp", "name": "台灯", "spec": "暖白光", "platform": "demo", "shop": "B",
     "price": "199.00", "quantity": 1,
     "source": {"kind": "user", "text": "台灯199元，1件", "observed_at": "2026-10-20"}}
  ],
  "promotions": [],
  "shipping": [
    {"platform": "demo", "shop": "B", "fee": "0.00", "basis": "price",
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

来源缺失会标为待核实，但不会伪造来源。程序检查结构和金额，来源真实性、截图去重、价格口径与规则完备性由 Agent 核实；不确定时保存阻塞问题。`data_as_of` 表示最近一次资料更新时点，不表示每个字段在同一时刻重新验价；各字段的观察时间见来源。

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
| `remove_items`, `remove_promotions` | 删除指定ID；商品退出购买用 `remove_items`，不是把 `required` 改成可选 |
| `preferences` | 合并偏好；候选品需先存在且 `required=false`；批准清单不能包含必买品或未知ID |
| `questions` | 替换当前手动补问列表；每条必填 `id`, `text`, `blocking`，可有 `source` |
| `resolve_questions` | 删除已经核实的手动问题ID；缺字段/运费的自动问题随资料更新自动消失 |
| `assumptions` | 替换假设数组 |
| `max_orders` | 更新可接受的最多结算次数，1–12；`preferred` 只改变推荐偏好，不改变计算输入 |
| `data_as_of`, `label` | 可选的资料时点或情景名称 |

同次更新中的ID不能重复，也不能同时修改和删除。删除商品后清理失效的商品专属规则和运费；投影到不加购方案时同样清理不适用规则，适用的账户券次数仍保持全局共享。修改价格等事实但未给新来源时，保存此次修改原因作为用户修正来源，旧资料留在历史；对截图提取的修正请显式传准确来源。

优惠顺序就是输入数组顺序。需要重排时，保存当前状态后在一次更新里删除相关旧规则，再通过**下一次更新**按正确顺序添加；每次都使用新的版本号。金额/规则核实和人工问答不由程序自动完成。

## 计算结果

完整求解后 `status=compared_under_input_rules`。`plans` 有三个稳定键：

- `original`：仅必买品，在当前结算次数上限内求最低总支出。
- `cheapest`：必买品和已批准候选品中求最低总支出；同金额优先少加购，再少结算。
- `fewest_checkouts`：先取必买品所需的最少可行结算组数，在该次数下求最低总支出。

v1 规则不限制“必须用某张券”或每单最低购买金额，因此每个不同的 `(platform, checkout_group)` 都可以一次买齐，组数恰为最少可行次数。规则扩展后若破坏此条件，必须更新此策略。优惠账户次数仍由同一个求解器统一约束。

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

仅在用户确认某次已经买下后使用。不能将点击“去结算”、预览截图或推荐步骤当成已购。

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

任务内另存 `coupon_balances`，即使规则随已购商品清理也保留剩余次数。之后重新引入同ID可选优惠而未填 `total_uses` 时，沿用已记录余额；显式填写 `total_uses` 表示重新核实后的当前余额，不能把旧截图的初始券数填回去。

每次只记录一个结算步骤；之后旧方案失效，先重新计算再记录下一次，避免顺序变化后误用旧订单序号。凭据ID不能重复；实付金额不含此前已付定金，程序额外记录完整支出。

商品数量按本次结算扣除；原清单全部买完后返回 `complete`，剩余未买的可选候选不强制购买。若实付金额与预计不同，会生成阻塞差异问题；查清是否影响剩余规则后，通过更新和 `resolve_questions` 解除。

## 本地文件与并发

所有状态原子写入 `TASK_DIR/session.json`，包含输入、偏好、补问、已购凭据、历史和当前结果。命令通过任务内 `.shopping.lock` 阻止并发写入；若命令崩溃遗留锁，先确认其中PID已结束再删除该锁，不删除任务文件。历史可能包含用户截图路径、来源原文及消费记录，不上传到代码仓库。
