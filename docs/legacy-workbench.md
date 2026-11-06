# 旧版工作台使用说明

此页只用于维护冻结的旧版 SQLite 工作台。新版默认入口为 Marketing Tests，第一次使用请阅读[新版操作教程](user-guide.md)。旧版的维度排列组合和 win/learn/lose 不定义新版测试组织与结论。

## 本地启动

```bash
# 1. 进入项目目录，创建虚拟环境（Python 3.11+）
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置旧版工作台的 API key
cp .env.example .env
# 用编辑器打开 .env，填入你有的 key（填哪个哪个可用，都不填也能用 Mock 模式跑通流程）

# 4. 启动（自动执行 Alembic migration、启动持久化 Worker，并打开浏览器）
python run.py
```

打开 `http://127.0.0.1:8787`，按左侧 1→4 步操作：

1. **产品信息**：填名称/描述/卖点，**上传产品实拍图**（保真生成的关键——生成时锁定产品外观只换场景）
2. **角度维度**：点「AI 生成维度」（用 LLM 按产品定制），生成后可任意增删改；每个取值的英文 prompt 片段会拼进生图 prompt
3. **排列组合**：勾选参与测试的取值。组合太多时选「覆盖采样」设上限——贪心算法保证每个取值都出现、组合间差异最大（正交实验的轻量近似）
4. **批量生成**：选模型 / 画幅（Meta 信息流 1:1、4:5，Stories 9:16）/ 每组合张数，看预估成本，开跑

右侧「生成任务」看进度，「素材画廊」按维度取值筛选、做**质量审核**（✅ Approved / ✎ Needs Changes / ⛔ Rejected——人只判"能不能用"；"好不好卖"由 Meta 投放数据回写为 🏆/🎓/❌ 投放判定，两套状态分离），最后**导出素材包（zip + manifest）**——manifest 带 quality + verdict 双字段与组合属性标签，直接对接 Meta 投放与归因。

## 旧版图片供应商

| 供应商               | 环境变量         |
| -------------------- | ---------------- |
| Gemini               | `GEMINI_API_KEY` |
| OpenAI               | `OPENAI_API_KEY` |
| Seedream（火山方舟） | `ARK_API_KEY`    |
| Mock                 | 无需 Key         |

模型覆盖配置见根目录 [.env.example](../.env.example)。这些全局 Key 不替代新版 Workspace 凭据。`run.py` 会迁移 SQLite 并启动 Worker，无需再启动第二个 Worker；不能用于托管 PostgreSQL。
