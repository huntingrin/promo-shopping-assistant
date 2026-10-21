# 可选浏览器适配器

核心 `scripts/shopping.py` 只依赖 Python 标准库。本目录是额外的采集层，用 Playwright 打开用户自己的淘宝/天猫购物车，读取页面后生成 `cart-snapshot.json`。

## 安装

```bash
python3 -m pip install -r optional/browser/requirements.txt
```

优先使用系统已安装的 Google Chrome 或 Microsoft Edge：

```bash
python3 optional/browser/browser_cart.py capture --task-dir /你的任务目录 --browser auto
```

如果系统没有 Chrome/Edge，可安装 Playwright 自带 Chromium：

```bash
python3 -m playwright install chromium
python3 optional/browser/browser_cart.py capture --task-dir /你的任务目录 --browser chromium
```

Playwright 不能直接接管 macOS 默认的 Safari，也不能直接读取日常 Chrome 的登录 Cookie。默认使用独立配置目录，首次需要扫码登录一次，之后复用。需要复用已开调试端口的 Chrome 时使用 `--cdp-url`。

## 常用参数

| 参数 | 作用 |
| --- | --- |
| `--task-dir DIR` | 快照写入 `DIR/browser-cart.json`，截图写入 `DIR/evidence/` |
| `--output FILE` | 指定快照路径；与 `--task-dir` 二选一 |
| `--browser auto\|chrome\|msedge\|chromium` | 优先使用的系统浏览器 |
| `--headless` | 无头运行；默认打开可见浏览器 |
| `--profile DIR` | 独立浏览器配置目录 |
| `--cdp-url URL` | 附加到已开远程调试端口的 Chrome |
| `--no-details` | 不打开商品详情页 |
| `--no-dialogs` | 不打开券/活动说明 |
| `--no-checkout` | 不进入只读结算预览 |
| `--screenshot` | 保存页面截图 |
| `--keep-open` | 采集后等待回车再关闭浏览器 |
| `--login-timeout 300` | 等待用户登录/验证的秒数 |

非交互运行时，适配器采集完成后会关闭自己启动的浏览器；需要让用户继续在页面上查看时，使用 `--keep-open` 在终端运行，或改用宿主自带浏览器工具。CDP 附加模式不会关闭用户原有浏览器。

## 只读边界

允许：打开购物车、商品详情、券/活动说明、只读结算预览；勾选商品；读取页面。

禁止：提交订单、付款、删除商品、修改数量、加购、领券、修改地址、保存支付信息。

进入结算预览前会提示；采集结束后尝试恢复原勾选状态。程序不会提交订单，也不会尝试绕过登录、验证码或风控。

## 输出

```bash
python3 scripts/shopping.py import-cart /你的任务目录 \
  --snapshot /你的任务目录/browser-cart.json

python3 scripts/shopping.py compare /你的任务目录 --format text
```

快照格式与跨 Agent 约定见 [`references/browser-cart.md`](../../references/browser-cart.md)。

## 失败码

| 退出码 | 含义 |
| --- | --- |
| 0 | 已生成快照；可能带待核实提示 |
| 2 | 参数或读取错误 |
| 3 | Playwright/浏览器不可用 |
| 4 | 等待登录或安全验证超时 |

页面结构、登录流程和平台规则会变化，选择器需要在真实账户上复核。没有 Playwright 的宿主可以改用自带浏览器工具生成同格式快照，或回退到截图/文字。
