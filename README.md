# 医析台 MedBench

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Streamlit](https://img.shields.io/badge/Streamlit-1.32%2B-red)
![License](https://img.shields.io/badge/license-MIT-green)

> **本地优先的临床数据分析工作台**：Excel 进，分析报告出。统计计算全部在本机完成。

医析台（MedBench）面向**临床科研与科室质控**场景，把「数据清洗 → 描述统计 → 统计推断 →
预测建模 → 因果推断 → Word 报告导出」串成一条完整链路，界面全中文、操作面向非统计专业的使用者。

程序默认只监听本机地址 `127.0.0.1`，并已关闭 Streamlit 匿名使用统计 —— **不上传任何个体数据行**。
AI 解读为可选项，默认关闭，且只发送数据结构摘要。

<!-- 界面预览：docs/images/ 中的截图是旧版本界面（含旧名称与二维码），重新拍摄后再贴到此处 -->

---

## 功能一览

| 模块 | 内容 |
| ---- | ---- |
| 📊 总览与洞察 | 数据概况与质量指标、结构画像与推荐分析、可选的 AI 分析建议（解读 + 方案）与探索性分析 |
| 🧹 数据准备 | 表格结构诊断与整理、变量类型手工指定、宽表转长表、缺失模式与多重插补（MICE）、多表关系识别与关联、撤销上一步、数据查看 / 筛选 / 脱敏导出 |
| 📥 数据接入 | 多 Sheet、表头行 / 大标题行识别、合并单元格向下填充 |
| 📈 描述性分析 | 数值 / 分类描述统计、正态性检验、常用图表 |
| 🔬 统计推断 | 组间比较、配对比较、相关分析、诊断一致性（Kappa / ICC / Bland-Altman）、多重比较校正（FDR / Tukey HSD） |
| ⏱️ 时间与质控 | 率的置信区间、感染密度（‰）、SPC 控制图、趋势检验、同比环比 |
| 🤖 预测建模 | 多重线性回归（含 VIF / Durbin-Watson / 残差诊断 / 异常点）、Logistic 回归、Cox 生存回归、ROC / 校准曲线 / 决策曲线 / 列线图、LASSO 变量筛选与 Bootstrap 内部验证、剂量-反应关系（限制性立方样条）、最佳截断值、外部验证、纵向重复测量（LMM / GEE）、时间序列预测 |
| 📚 论文辅助 | 倾向性评分匹配（PSM）、逆概率加权（IPTW）、亚组分析与森林图、敏感性分析与 E-value、Table 1 基线特征表自动化 |
| 📄 报告导出 | 一键导出 Word 分析报告（含方法说明、图表），可选 AI 审阅与 AI 写作 |

---

## 快速开始

### 方式一：直接运行打包好的程序（Windows，推荐给临床使用者）

双击 `医析台.exe` 即可，无需安装 Python 或依赖。程序会自动打开浏览器页面。

> 首次启动需要解包与加载科学计算库，通常等待 30–60 秒。
> 若 8501 端口被占用，程序会自动改用 8502、8503…，实际地址记录在 `%USERPROFILE%\医析台端口.txt`。

### 方式二：从源码运行

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt     # Windows
.venv/bin/pip install -r requirements.txt         # Linux / macOS

streamlit run app.py
```

打开 `http://127.0.0.1:8501`，点击侧边栏 **「加载示例数据（300例）」** 即可试用全部功能。

### 可选：接入 AI 解读

程序通过 OpenAI 兼容接口调用 AI（本地 vLLM / Ollama / 任意兼容网关均可）：

1. 在界面侧边栏「AI 助手」中填写 `base_url` / `model`（本地模型可留空 API Key），或
2. 在程序目录放置 `ai_server_config.json`（模板见 `ai_server_config.example.json`），或
3. 用环境变量：`MEDBENCH_BASE_URL` / `MEDBENCH_API_KEY` / `MEDBENCH_MODEL` / `MEDBENCH_TIMEOUT` / `MEDBENCH_REDACT`

只发送**数据结构摘要**（字段名、类型、唯一值数、缺失率、结构角色），不发送任何个体数据行，
发送前可逐字预览。

---

## 服务器 / 多人部署

Streamlit 自身没有认证功能，多人使用需要前置 Nginx 做访问认证与 HTTPS。
完整的 Windows / Linux 部署步骤（含 systemd、Nginx 反代、自签证书、并发容量估算）见
**[docs/服务器部署说明.md](docs/服务器部署说明.md)**。

---

## 打包为单文件 exe

```bash
py -3.13 scripts/打包.py --rebuild     # 重建 onedir 并生成单文件
py -3.13 scripts/打包.py               # 用现有 dist 目录重新生成单文件
```

产物：

- `dist/医析台/医析台.exe` —— onedir 版，可直接运行
- `dist_single/医析台.exe` —— 单文件版，双击即用

---

## 项目结构

```
MedBench/
├── app.py                      # 应用主入口（Streamlit 界面与全部页签）
├── modules/                    # 分析功能模块（30+ 个）
├── sample_data/                # 示例数据（300 例演示数据）
├── .streamlit/config.toml      # 运行配置（默认只绑本机、关闭使用统计）
├── docs/                       # 使用说明、服务器部署说明、界面截图
├── scripts/
│   ├── 打包.py                 # 打包脚本（onedir + 单文件）
│   ├── 单文件启动器.py         # 单文件 exe 启动器
│   ├── streamlit_entry.py      # 服务入口（端口、监听地址、日志）
│   ├── 自检.py                 # 冒烟自检（页签渲染 / 入口运行 / 行为护栏）
│   ├── 依赖体检.py             # 比对源码 import 与打包归档
│   └── 生成说明文档.py         # 使用说明 Markdown → HTML / PDF
├── requirements.txt
├── CHANGELOG.md
└── LICENSE
```

---

## 开发

```bash
python scripts/自检.py           # 全部检查（约 6 分钟）
python scripts/自检.py --fast    # 跳过耗时的分析运行（约 1 分钟）
```

自检会启动一个离线测试实例，逐个页签与页内分析类型渲染一遍，并校验报告收录等行为护栏，
**全部显示 OK 才算通过**。

更新日志见 [CHANGELOG.md](CHANGELOG.md)。

---

## 许可

[MIT License](LICENSE)

## 免责声明

本工具仅用于**数据分析与统计计算**，输出结果不构成任何医疗建议或诊断结论。
使用者需自行确认数据来源的合规性，并遵守所在机构的数据管理与隐私保护规定。
因使用本工具产生的任何后果，作者不承担责任。

---

## English

**MedBench (医析台)** is a local-first clinical data analysis workbench built with Streamlit.
It covers the full path from Excel data cleaning and descriptive statistics to statistical
inference, predictive modeling (linear / logistic / Cox / LASSO / dose-response / LMM-GEE /
time series), causal inference (PSM, IPTW, E-value, Table 1), and one-click Word report export.
All computation runs locally — the app binds to `127.0.0.1` only and sends no data rows to
any server. An optional OpenAI-compatible AI assistant can interpret results when enabled.

```bash
pip install -r requirements.txt
streamlit run app.py
```

Licensed under the [MIT License](LICENSE).
