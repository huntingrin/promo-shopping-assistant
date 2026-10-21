# 计算来源与版本

2026-10-20 从用户已有的相邻研究项目 `double11/promo-simulator-research/promo-order-optimizer` 引入快照：

- `scripts/optimize.py`：Python标准库计算内核，未修改。
- `references/input-spec.md`：原输入语义与限制，未修改。
- `fixtures/presale.json`：原预售假设案例，未修改。

逐文件SHA-256和本地相对来源见 [upstream.json](upstream.json)。本项目复制代码以独立分发，不依赖相邻目录存在；后续改动应作为明确的版本更新重新验证，避免两个目录隐式同步。

`tests/test_optimizer.py` 沿用上游的22项行为测试和50组带种子的独立穷举交叉核对，仅调整了项目内路径。`fixtures/original-example.json` 保存原完整假设案例。`fixtures/demo-cart.json` 来自上游 `results/consumer-options.json` 的允许加购情景，补充规格和明确的演示来源标记。

`workflow.py` 和 `shopping.py` 为本项目新增，负责草稿与版本、具体加购同意、三个策略的同基准比较、历史、结算记账、扣券与后续规划。高层缺失运费时阻止求解；原内核低层入口保留上游的0元模拟及警告行为。

v0.2 新增 `scripts/taobao.py`：产品范围限定淘宝天猫，将两类店铺编译到同一交易域，以商品ID交集实现店铺类型过滤，保留原内核快照不变。`demo-cart.json` 已改为淘宝天猫格式，仍是原假设金额；新建 `taobao-presale.json` 作为可通过高层入口的假设预售案例。原 `presale.json` 与通用内核测试继续保留用于校验原快照。

v0.3 新增 `scripts/discounts.py`，复用原整数金额、商品解析、结算明细等基础，独立实现比例/件数折扣、共享金额/积分、支付阶段和顺序敏感求解。只含原字段时仍走原求解器；原快照SHA-256不变。新协议在 `references/discounts.md`，假设叠加案例在 `fixtures/discounts.json`，不硬编码当前平台比例或金币兑换规则。

v0.4 新增 `scripts/browser_import.py` 和可选 `optional/browser/`：前者只做快照校验、价格口径映射、脱敏和任务导入，不修改计算内核；后者用 Playwright 读取购物车并输出快照。核心程序仍无网络调用和第三方依赖。

本仓库不包含真实购物资料或第三方平台实时活动数据。
