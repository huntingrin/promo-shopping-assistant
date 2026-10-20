# 计算来源与版本

2026-10-20 从用户已有的相邻研究项目 `double11/promo-simulator-research/promo-order-optimizer` 引入快照：

- `scripts/optimize.py`：Python标准库计算内核，未修改。
- `references/input-spec.md`：原输入语义与限制，未修改。
- `fixtures/presale.json`：原预售假设案例，未修改。

逐文件SHA-256和本地相对来源见 [upstream.json](upstream.json)。本项目复制代码以独立分发，不依赖相邻目录存在；后续改动应作为明确的版本更新重新验证，避免两个目录隐式同步。

`tests/test_optimizer.py` 沿用上游的22项行为测试和50组带种子的独立穷举交叉核对，仅调整了项目内路径。`fixtures/original-example.json` 保存原完整假设案例。`fixtures/demo-cart.json` 来自上游 `results/consumer-options.json` 的允许加购情景，补充规格和明确的演示来源标记。

`workflow.py` 和 `shopping.py` 为本项目新增，负责草稿与版本、具体加购同意、三个策略的同基准比较、历史、结算记账、扣券与后续规划。高层缺失运费时阻止求解；原内核低层入口保留上游的0元模拟及警告行为。

本仓库不包含真实购物资料或第三方平台实时活动数据。
