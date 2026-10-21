# 淘宝天猫算账助手

给电脑端 Agent 使用的通用 Skill 与离线程序，**购物范围限定淘宝、天猫国内普通购物**。用户说“看看我的购物车怎么买划算”时，Agent 默认打开浏览器读取购物车，核对清单及当前规则后得到选券、拆单、加购及逐次购买步骤；可以接着纠错、回核结算、记录已购和规划剩余商品。浏览器不可用时才回退截图或文字。

Agent 接口保持通用，优惠规则只维护淘宝天猫。官方依据、当前覆盖和待补能力见 [淘宝天猫规则参考](references/taobao-tmall.md)；不会把历史活动门槛当作当前默认规则。浏览器登录、读取范围和只读红线见 [浏览器采集协议](references/browser-cart.md)。

**核心 Python 3.10+，标准库，无模型密钥或外部服务；可选浏览器适配器使用 Playwright。** 图片理解、对话和网页操作由宿主 Agent 提供；金额由程序计算。

## 默认流程

1. 用户提出算购物车，即使没有发截图，Agent 也先尝试打开可见浏览器。
2. 优先复用适配器的持久化登录态；未登录时打开登录页，请用户扫码/验证后继续。
3. 读取购物车、商品详情、优惠/券说明和只读结算预览，生成 `cart-snapshot.json`。
4. 通过 `import-cart` 导入本地任务并计算；页面给不出的信息才问用户。
5. 浏览器不可用、用户拒绝登录或被风控阻断时，才回退截图/文字，并说明原因。

浏览器采集全程只读：不修改购物车、不领券、不下单、不付款。进入结算预览前会告知用户，采集后尝试恢复原勾选。

## 浏览器采集（可选）

核心计算不需要浏览器。要让 Agent 在本机默认打开购物车，安装可选适配器：

```bash
python3 -m pip install -r optional/browser/requirements.txt
# 优先使用系统已安装的 Chrome/Edge；没有时再装自带 Chromium
python3 -m playwright install chromium
```

```bash
python3 optional/browser/browser_cart.py capture --task-dir /你的任务目录
python3 scripts/shopping.py import-cart /你的任务目录 --snapshot /你的任务目录/browser-cart.json
python3 scripts/shopping.py compare /你的任务目录 --format text
```

Playwright 不能直接接管 macOS 默认的 Safari，也不能直接读取日常 Chrome 的 Cookie。默认使用独立配置，首次扫码登录一次；需要复用已开调试端口的 Chrome 时用 `--cdp-url`。参数、快照格式和失败码见 [optional/browser/README.md](optional/browser/README.md) 与 [浏览器采集协议](references/browser-cart.md)。

## 立即试用

```bash
python3 scripts/shopping.py demo
python3 scripts/shopping.py demo --discounts
```

这是明确标注的假设案例：

| 选择 | 总支出 | 结算次数 | 新增商品 |
| --- | ---: | ---: | --- |
| 只买原清单 | 390元 | 1 | 无 |
| 同意清洁布，少花钱 | 345元 | 2 | 10元清洁布 |
| 同意清洁布，少操作 | 375元 | 1 | 10元清洁布 |

`--discounts` 演示假设的100元商品依次立减15%、按剩余金额享95折、使用10元红包，再按明确的示例积分规则抵扣，预计67.22元。示例只验证模型，不代表当前平台比例或兑换规则。

默认使用真实输入时不允许加购，需明确批准具体候选品：

```bash
python3 scripts/shopping.py solve fixtures/demo-cart.json --format text
python3 scripts/shopping.py solve fixtures/demo-cart.json --approve cloth --format text
```

## 在 Agent 中使用

将本目录整体放入宿主支持的 Skill 目录，由宿主加载 [SKILL.md](SKILL.md)。没有目录式 Skill 支持，但能读文件和运行 Python 的 Agent，也可以直接使用这段指令：

> 请读取 `/实际路径/promo-shopping-assistant/SKILL.md`，按其中流程帮我算购物优惠。默认先打开浏览器读取我的淘宝/天猫购物车；未登录就引导我扫码。使用你已有的浏览器工具整理资料，调用该目录里的程序计算，把任务保存在独立目录，后续继续读取同一任务。

然后用户只需说：

> 帮我看看淘宝天猫购物车怎么买划算。
>
> 台灯改成199，不要洁面。不要额外加购。
>
> 第一单已经买了，下一单怎么结算？

宿主需要本地文件读写和 Python 执行能力；本地浏览器适配器还需要 Playwright，或者由宿主自带浏览器工具生成同格式快照。纯聊天、无法执行程序也没有浏览器工具的宿主可以整理用户提供的材料，但不能独立完成浏览器采集或本地精确求解。本仓库已验证本地CLI及整体复制运行，尚未逐一在不同品牌的 Agent 中实测，不能保证每家都自动发现 Skill 或自动打开浏览器。

## 可持续的本地任务

```bash
# 方式一：浏览器快照（默认流程）
python3 scripts/shopping.py import-cart /你的任务目录 --snapshot fixtures/browser-cart-snapshot.json
# 方式二：从已有购物资料创建
python3 scripts/shopping.py new /你的任务目录 --input fixtures/demo-cart.json
python3 scripts/shopping.py show /你的任务目录 --format text
python3 scripts/shopping.py compare /你的任务目录 --format text
```

修改通过 `update --patch`，已购通过 `purchase --receipt`，详见 [工作流协议](references/workflow.md)。任务完整保存在一个 `session.json` 中，可跨对话继续。旧结果随修改失效；用券按账户共享次数记账；缺价格、运费或阻塞信息时保留资料并提示补充。

## 结构与验证

```text
SKILL.md                     Agent 的使用入口
scripts/shopping.py           稳定命令入口，支持单次计算、持续任务和浏览器快照导入
scripts/workflow.py           状态、偏好、方案比较与购买记录
scripts/browser_import.py     浏览器快照校验、价格口径映射和任务导入
scripts/taobao.py             淘宝天猫平台范围、店铺类型及规则适配
scripts/discounts.py          比例/多件折扣、共享权益、支付阶段及结算顺序求解
scripts/optimize.py           精确计算内核，保留上游快照
optional/browser/             可选 Playwright 浏览器适配器
references/                   任务、金额规则与浏览器采集协议
fixtures/                     明确标注的演示、预售与浏览器快照案例
tests/                        计算、状态、导入和CLI行为验证
docs/                         来源和验证记录
```

```bash
python3 -m unittest discover -s tests -v
python3 scripts/build_release.py
```

构建生成 `dist/promo-shopping-assistant-0.4.0.zip` 及其SHA-256，可解压后整体迁移。构建不打包本地任务、浏览器配置或缓存。验证范围及限制见 [docs/VERIFICATION.md](docs/VERIFICATION.md)，计算来源见 [docs/UPSTREAM.md](docs/UPSTREAM.md)。

## 范围

支持淘宝天猫小购物车中已核实的满减、每满减与封顶、优惠范围与互斥、账户券次数、拆单、明确批准的候选品、固定店铺运费和已付定金。淘宝店和天猫店在已确认可合并的组内共同计算，天猫专属券按店铺类型限制；券后价、尾款及未知价格口径会阻止计算，避免重复优惠。当前计算范围最多12件、12条优惠；复杂输入可能超时，会明确返回未完成。

v0.3 增加官方立减/会员比例折扣、满件/阶梯折扣、每N件减额、逐件/整单舍入、单次及共享封顶、红包/金币余额和明确条件的固定/比例支付优惠。所有拆单共用余额及次数，实际购买后扣减；自动封顶导致顺序影响价格时会比较合法结算顺序。协议和示例见 [折扣参考](references/discounts.md)。

v0.4 增加浏览器优先采集：可选 Playwright 适配器打开购物车、等待用户登录、读取商品详情/优惠说明/只读结算预览，输出可校验的 `cart-snapshot.json`；核心新增 `import-cart` 转换和价格口径保护。适配器不可用时，宿主浏览器工具可以生成同格式快照；仍不可用才回退截图/文字。

其他购物平台、天猫国际及跨境税费暂不支持；买一赠一/指定第N件折扣、付费储值与购券成本、返现/赠券循环、退货凑单和加购预算约束仍需独立模型。浏览器选择器、登录流程和真实平台页面结构仍需在真实账户上复核；当前资格、比例、基数、顺序、舍入和兑换规则必须有当次依据，不能从优惠名称推断。程序只计算和记账；可选适配器只读访问购物账号页面，不提交订单、不付款。
