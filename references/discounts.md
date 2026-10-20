# 比例、件数、余额与支付折扣协议

v0.3 增补；计算引擎 `discounts-v1`，淘宝天猫适配 `taobao-tmall-v2`。原满减协议仍有效；存在新字段时由扩展引擎处理，不修改原 `optimize.py` 快照。

所有数值来自本次材料，不内置官方立减、88VIP或金币的当前比例、叠加顺序与舍入规则。缺少关键参数时保存草稿和阻塞问题。下例都是假设规则，不能作为平台当前活动模板直接套用。

## 比例与会员折扣

```json
{
  "id":"vip", "name":"示例会员95折", "platform":"taobao", "marketplaces":["tmall"],
  "mode":"automatic", "kind":"percentage", "stage":"goods",
  "threshold":"0", "basis":"price", "rate":"0.05", "discount_basis":"remaining",
  "rounding":"half_up", "rounding_scope":"item", "cap":"20.00",
  "source":{"kind":"demo","note":"假设按前序优惠后商品金额减5%，逐件取整，每次封顶20元"}
}
```

`rate` 是**减免比例**：立减15%为 `0.15`，95折为 `0.05`；不是支付比例。最多六位小数。金额用十进制字符串，内部全部为整数分。

| 字段 | 语义 |
| --- | --- |
| `kind` | `fixed` 为原固定满减（默认），`percentage` 比例减免，`balance` 共享余额抵扣 |
| `stage` | `goods`（默认）在商品计价阶段；`payment` 在运费确定后的支付计价阶段。所有goods规则必须列在payment规则之前，阶段内仍按输入顺序执行 |
| `basis` | 判断满减门槛的基数，保留原四种口径，新增 `payable` 表示适用商品当前未付金额；在payment且明确 `include_shipping=true` 时包含适用店铺剩余运费 |
| `discount_basis` | 比例减免的独立计算基数，可为 `price/original_price/threshold_price/remaining/payable`。`remaining` 包含定金，`payable` 排除已付定金；实际减免都不能退回定金 |
| `rounding` | 必填：`floor` 向下到分、`half_up` 四舍五入到分、`ceil` 向上到分。不能自行假定平台采用哪一种 |
| `rounding_scope` | 必填：`item` 对数量展开后的每件分别取整；`checkout` 对全部适用金额合计取整。含运费且选item时每个店铺运费也是独立计价分量 |
| `cap` | 本次结算该规则的总减免上限，不是账户累计封顶 |
| `per_item_cap` | 每件减免上限，仅用于 `rounding_scope=item`，再受总cap约束 |
| `min_item_payable` | 每件商品须保留的未付商品款，默认0；不会要求重复支付定金 |
| `min_payable` | 本规则适用金额合计至少保留的未付金额，默认0；与逐件保底共同限制，避免重复扣除保底 |
| `min_quantity` | 适用商品至少几件才生效，数量展开计件，默认1 |
| `whole_checkout` | 默认false；true时本批次全部商品必须属于适用范围。不能用普通SKU过滤替代该限制 |

`price` 对应预售立减后的有效活动价；其余原口径见 [input-spec.md](input-spec.md)。固定规则继续使用 `discount`，不允许同时填写比例字段。比例规则不能再填写固定 `discount` 或金额 `repeat`。

逐件比例先算每件可减金额、保护定金和保底，再应用总封顶。整单比例先算适用总额的减免，再按当前可减容量分摊；总封顶或共享额度压缩逐件减免时，按逐件可减金额比例分摊。分摊使用带容量保护的最大余数法，余数相同按商品输入顺序。后续规则会使用该分摊结果；若平台真实分摊无法用此语义表达，保留未支持问题，不能承诺逐分一致或将模拟分摊当退款金额。

`mode=automatic` 满足条件且有剩余额度就必须执行，不通过“跳过自动折扣”制造优惠；`selectable` 才可用/不用。自动规则不能放入可选择互斥组；件数档位使用单条 `tiers`。

## 多件折扣与阶梯

满2件9折可写 `kind=percentage, min_quantity=2, rate=0.10`，仍需基数与舍入参数。不同件数档位写成同一条规则：

```json
{
  "id":"multi", "platform":"taobao", "mode":"automatic", "kind":"percentage",
  "threshold":"0", "basis":"price", "discount_basis":"price",
  "rounding":"half_up", "rounding_scope":"item",
  "tiers":[{"min_quantity":2,"rate":"0.10"},{"min_quantity":3,"rate":"0.20"}]
}
```

档位按件数严格递增，选择最高已满足件数档；`tiers` 与 `rate` 互斥。只数符合该规则全部筛选条件的商品。每2件减10元使用 `kind=fixed, quantity_step=2, discount=10`；减免次数为适用件数整除2，不能同时配置按金额 `repeat`。

“任选N件中最低价那件打折”“买一赠一指定品”“第N件半价”尚无专用选择模型，不与满N件全部打折混用；保存原文待单独建模。

## 共享红包、积分与账户封顶

购物资料顶层新增 `resources`。金额资源以元的字符串表示余额，积分资源以非负整数表示数量：

```json
[
  {"id":"red","kind":"money","balance":"10.00","source":"用户当前红包余额截图"},
  {"id":"coins","kind":"points","balance":500,"source":"用户当前可用积分截图"},
  {"id":"vip-budget","kind":"money","balance":"30.00","source":"用户当前剩余会员优惠额度"}
]
```

最多8项资源。资源是免费优惠存量或折扣预算，不能把购物金/储值卡本金、现金余额、买券成本写成免费折扣。余额未知可留空或null作为草稿，实际使用前补齐；0表示已经确认没有可用余额。所有拆单和各商品共用同一ID的剩余额度。

比例/固定规则可附 `resource_id` 引用money资源，实际减免同时受当前账户余额限制，适用于已核实的会员累计优惠封顶。`total_uses` 继续限制次数，两项可以同时存在。每日/年度多种限制并存时，先核实当前有效可用额度；程序不模拟跨日恢复、过期或未来发券。

可拆用红包用 `balance` 规则：

```json
{
  "id":"red-use", "platform":"taobao", "mode":"selectable", "kind":"balance", "stage":"payment",
  "threshold":"0", "basis":"payable", "resource_id":"red", "allocation":"maximum", "cap":"5.00"
}
```

`allocation=maximum` 表示本次选用后按余额、可抵金额和本单封顶尽量用满；用户可取消该权益时用selectable，不能取消时用automatic。只有真实允许主动选择部分使用金额时才用 `allocation=flexible`，程序按每一抵扣步长枚举合法用量。不能把不可拆用的券面额当余额；一次性券使用原fixed和次数模型。

金币/积分必须明确每步抵扣金额和消耗数量，不默认通兑比例：

```json
{
  "id":"coins-use", "platform":"taobao", "mode":"selectable", "kind":"balance", "stage":"payment",
  "threshold":"0", "basis":"payable", "resource_id":"coins", "allocation":"maximum",
  "units_per_step":1, "step_value":"0.01", "rate":"0.05", "discount_basis":"payable",
  "rounding":"floor", "rounding_scope":"checkout"
}
```

这个假设是每1积分抵0.01元，且最多抵当前应付5%，并非平台通用规则。只能使用完整步长，不透支积分。`balance` 的rate是本次可抵金额比例上限，不是直接发放同额折扣；还受余额/积分约束。money资源按分消耗，`step_value` 默认0.01元，可明确更大步长，不能另设 `units_per_step`。

未提供 `total_uses` 时，balance不限使用次数直到余额耗尽；automatic也默认不限次数；其他selectable规则默认1次。自动优惠可以明确剩余次数和共享封顶，程序会比较结算顺序；不同顺序可能改变后续券的门槛。

## 支付抵扣与运费

已核实资格及条件的固定/比例支付优惠也可使用 `stage=payment`，并用次数、互斥、共享资源表达上限。`include_shipping` 默认false；只有实际允许抵扣运费时显式设true。比例优惠是否把运费作为折扣基数由 `discount_basis=payable` 和该开关共同决定，不能从“满额券”名称推定。

运费在所有goods规则执行后、payment规则执行前确定一次；支付优惠不会重新触发或破坏包邮。输出保留原运费 `fee` 和支付抵扣后还需付的运费 `pay_now`，不会用负运费补账。

现金总支出为本次实付现金加已付定金，不把免费红包/积分面值算作再次付款。消耗的权益另列，所有方案与同一资料、同一资源起点的原清单比较。真实资格、允许支付渠道、可选性和限额仍需当次核实；程序不操作任何付款。

## 更新、结算与求解保护

`update --patch` 接受 `resources`，按ID合并；已确定的money/points类型不能用同ID互换。余额修正须有原因及来源，不能拿旧截图恢复已消费的存量。清理失效商品或优惠不会清空共享资产。

结算凭据新增 `resource_usage`：金额资源用元字符串，积分用整数。只有当前步骤确有消耗时必填对应ID，金额/数量必须与已核对的实际使用相符；程序核对后原子扣余额和次数。示例：

```json
{"expected_revision":2,"id":"receipt-1","pay_now":"67.22","coupon_ids":["red","coins"],
 "resource_usage":{"red":"10.00","coins":353},"source":"用户确认已买，结算明细与当前方案一致"}
```

`coupon_ids` 指本次所有selectable活动ID，包括余额活动；automatic不放入此列表但有限次数仍会记账扣减。已消耗次数在规则移除/重新引入后仍保留。

若某步骤依赖此前结算已消耗自动折扣额度，结果保存 `automatic_resources_before`。记账会核对起点，不能跳过前一步却沿用后一步的额度假设。每次记账后旧结果失效，重新求解剩余清单再继续。

扩展求解同时比较合法拆单和结算顺序；自动共享封顶可能使已批准的纯凑单批次也影响最低支出，程序会将其明确列为额外结算。最多12件/12条优惠、8项余额；复杂flexible分配可能触发状态/时间限制。整次方案比较仍默认15秒，超限不返回部分“最优”。
