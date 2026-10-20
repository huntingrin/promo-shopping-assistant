# 输入协议 v1

金额推荐用人民币元的十进制字符串，如 "199.90"，最多两位小数。CLI 也接受 JSON 数字并用 Decimal 解析。内部使用整数分。Python API 接受字符串、整数和 Decimal，不接受二进制 float 金额。

## 顶层

| 字段 | 说明 |
| --- | --- |
| version | 必填，整数1 |
| items | 必填，商品数组，数量展开后1–12件，至少一件必买 |
| promotions | 必填，按执行先后排列的规则数组，最多12条；无优惠填 [] |
| shipping | 店铺运费数组，未配置店铺按0元模拟并警示 |
| max_orders | 最多结算次数，整数1–12，默认商品件数 |
| label | 情景名称，默认“用户输入情景” |
| assumptions | 字符串数组，保存假设、时间、规则核实状况 |

未知字段、重复 JSON 键、负金额、超过两位小数、重复ID、没有适用商品的规则均报错。省略项使用下面明确列出的默认值。

## 商品 items[]

一行描述一个SKU。全部金额为**单件**，quantity 会展开为 id#1、id#2 等独立商品，支持拆分同款多件。

| 字段 | 说明 |
| --- | --- |
| id | 必填，唯一非空字符串，不含 # |
| platform, shop | 必填，平台与店铺标识，精确匹配、区分大小写 |
| price | 必填，订单优惠之前的商品活动价/预售总价；不是尾款，不是已扣券到手价 |
| name | 展示名称，默认id |
| original_price | 展示原价，默认price；仅在规则显式指定此基数时影响优惠 |
| presale_discount | 已确定的预售立减额，默认0；如定金50抵100，额外立减为50，实际支付50不能也当优惠 |
| deposit_paid | 已实际支付、包含于总价中的定金，默认0 |
| threshold_price | 门槛专用单件计入金额，默认 price - presale_discount，需按实际口径核实 |
| required | 是否必买，默认true；false是可选凑单品；已付定金商品必须必买 |
| quantity | 整数1–12，默认1 |
| category | 一个品类标识，默认general，多种属性可用tags |
| tags | 可选非空字符串数组，如 ["cross-200", "vip"] |
| checkout_group | 可合并结算的组，默认default |
| source | 可选来源元数据，推荐URL或原文位置；不参与计算 |

同一结算批次必须 platform 和 checkout_group 同时相同。批次可含多店，它不是物流包裹，也不是平台最终生成的子订单。checkout_group 只表达能否合并，不处理付款时间先后。预售与现货允许合并时可设同组。

有效商品金额 = price - presale_discount，已付定金不得超过此金额。本版只模拟继续买下必买品，不计算放弃预售、定金退款或沉没成本比较。未付定金时 deposit_paid=0，结果是完整购入成本，不生成分阶段付款计划。

## 优惠 promotions[]

```json
{
  "id": "shop-coupon", "name": "A店满300减50券",
  "platform": "demo", "mode": "selectable", "shops": ["A"],
  "threshold": "300.00", "discount": "50.00", "basis": "threshold_price",
  "repeat": false, "total_uses": 1,
  "exclusive_group": "shop-coupons-A", "exclusive_scope": "checkout"
}
```

必填 id、platform、mode、threshold、discount、basis。规则id唯一，name默认id。

mode=automatic 表示满足条件必须执行的自动满减，必须不限账户次数且无互斥组；mode=selectable 是可选择不使用的券或活动，枚举使用/跳过。不能用 selectable 替代不可关闭的自动优惠，否则可能虚构“跳过自动满减以触发更大券”的方案。

total_uses 是本次规划整个账户剩余可用次数，整数0–12，或 null 表示不限。selectable默认1，automatic默认null。一次结算同一规则最多用一次；同次结算允许叠加两张同类券时，请明确新增两条规则。total_uses=2表示可在两次结算各用一次。最多8条有次数限制的规则。

### 范围与门槛

可用 shops、categories、item_ids、tags 四个可选非空数组过滤；字段之间AND，数组内部OR，tags匹配任一标签。优惠只把符合所有条件的商品计入门槛并分配减免。item_ids匹配原商品id，不填#1后缀。

不同跨店活动档位，应分别给参与商品打标签，用只匹配各自标签的规则表示，不能混算。

| basis | 门槛采用金额 |
| --- | --- |
| price | 有效活动价总和，即price减预售立减；不因前序订单优惠降低 |
| threshold_price | 用户录入的门槛金额总和；不因前序优惠降低 |
| original_price | 展示原价总和，只适用于明确按原价计门槛的规则 |
| remaining | 前序规则执行并分摊后的剩余商品金额，包含此前已付定金 |

平行满减通常指定优惠前基数，逐次满减可用remaining。同一规则只有一种门槛口径。不同优惠各自需要不同逐件计入金额时，本版不能完整表达，需扩展模型，不能只改一处threshold_price便假称覆盖所有优惠。

### 减免、封顶、叠加

- repeat=false（默认）：满门槛只减一次discount；threshold可为0，表示无门槛。
- repeat=true：每满threshold减discount，即 floor(基数/threshold) × discount；threshold必须大于0。
- cap：可选，每次结算此规则的减免上限，默认不限制；不是账户累计减免上限。
- 无互斥组的优惠按列表顺序叠加，只抵商品款，不抵运费。
- exclusive_group：同组优惠互斥，默认无组；不同组可叠加。
- exclusive_scope=checkout（默认）：同组最多一个优惠用于一次结算。
- exclusive_scope=item：同组优惠适用商品集合不重合时可同时使用；有交集则互斥。同组任一已选规则为checkout范围时，整次结算互斥。

适用商品集合是筛选后的整个集合，本版不会在同次结算中再挑部分适用商品给券，不枚举平台内部子单的任意用券分配。需这种分配时可分开结算，或扩展模型后求更完整的最优解。整次结算必须全部商品可用、最低件数等条件，也不能用部分范围过滤代替。

优惠实际减免不得超过适用商品尚未支付金额总和。按各件**尚未支付金额**比例分摊到整数分，最大余数法补齐，余数相同按商品输入顺序。每件商品累计优惠不能抵回已付定金。平台分摊、最低实付规则可能不同，逐件结果不能充当平台退款金额。

## 运费 shipping[]

```json
{"platform":"demo", "shop":"A", "fee":"6.00", "free_threshold":"99.00", "basis":"remaining"}
```

必填platform、shop、fee、basis，同平台店铺仅一条配置。每次结算每店各算一次运费，基数只含该店商品。

达到free_threshold免邮，未达到按固定fee计费；省略或null时按固定fee，fee=0即无运费。basis使用上表四种口径，remaining指全部商品优惠后。可附source。不同批次运费不合并；跨店统一运费、偏远地区阶梯运费、运费券未建模。

## 输出和故障协议

默认完整JSON，--format text输出终端文字；--output path保存文件，否则stdout。

summary.pay_now是现在还需付的商品款加运费；deposit_paid是已付金额；total_spend = pay_now + deposit_paid。比较总支出不要只看尾款。

required_only_pay_now是同规则和max_orders下不买任何可选品的最优结果；savings_vs_required_only已扣全部凑单成本。required_only_single_checkout_pay_now是所有必买品合并结算的最优应付，不能合并则为null。

同金额优先少买可选品，再减少结算次数。结果含逐件金额、优惠门槛基数/触发次数/实际优惠/分摊、各店运费、券次数、假设和统计。

| 退出码 | 结果 |
| --- | --- |
| 0 | 完整求解，status=optimal_under_input_rules |
| 2 | 输入、约束或文件错误，stderr为status=invalid_input的JSON |
| 3 | 搜索超时/超限，stderr为status=search_limit的JSON，无最优结果 |

默认15秒，--time-limit 60可增加，上限300秒。另有20万DP状态和80万订单优惠组合的保护上限。12件/12条规则只是输入上限，不保证复杂输入一定及时完成。失败不会更新--output指定的旧文件，调用者必须检查退出码，不能读取上次结果冒充本次结果。

未覆盖：比例折扣、跨档自动选择、件数折扣、会员价选择、优惠发放循环、支付立减、红包资产余额、退货凑单、返现、换购、赠品估值、收货地址、实时库存、时效校验及变价。有效期和资格在输入前核实；不同时间点分别计算情景，不模拟跨时间调度。
