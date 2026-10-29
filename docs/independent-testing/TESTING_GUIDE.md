# 独立测试说明

## 固定环境与配置

- 被测代码：`8bd97c25e45e8bd7845aa93769eaf48e98c7fce8`；与本次文档提交的 commit 区分。
- Google Colab Linux，Python 3.13.16，AgentDojo 0.1.35，pytest 9.1.1；依赖快照见 dependencies.txt。作者环境为 Python 3.12.14，因此是独立环境复现，不是完全相同环境。
- workspace，benchmark_version=v1.2.2，direct，40 个用户任务和 14 个注入任务；k=0,1,2,4,8；rule_based/full_approve/full_escalate。
- agent/reviewer：OpenRouter 的 `openai/gpt-4o-mini`。API key 通过 Colab Secrets 读取，不写入代码或日志。
- 原分批源码的 tracked diff 为空；每批 run_config.json 固定代码、依赖哈希、模型与范围。早期受预算保护的 k=0/1 smoke 有不同 provider/token/retry 设置，不能混入此快照比较。

## 安装与离线运行

在独立 checkout 中运行，勿重置其他人的工作目录：

```bash
git clone https://github.com/dejavu-ada/Safety-Approval-Policies-for-Agent-Harnesses-A7.git source
cd source
git checkout 8bd97c25e45e8bd7845aa93769eaf48e98c7fce8
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m pip install -e . --no-build-isolation --no-deps
./.venv/bin/python -m pytest tests
```

历史 Colab 的 venv 创建失败后改用 virtualenv 成功。依赖版本需与 dependencies.txt 核对；直接重装 requirements.txt 不保证未来解析出同一环境。历史实际解释器为 `/content/pe6203_independent_testing/test_env/bin/python`，源码目录为相邻的 `source`。

新增边界用例见 boundary_examples.py（未放入默认 tests 目录，以免把尚未达成一致的策略期望变成必过 CI）。可用安装了被测包的 Python 显式运行 `python -m pytest /path/to/boundary_examples.py`；在历史版本预期观察到 13 passed、2 failed，失败详情见 boundary_pytest.txt。本次提交没有重新调用模型 API。

## 真实实验命令

先在运行环境提供 OPENROUTER_API_KEY。单批示例（产生 API 费用）：

```bash
./.venv/bin/python scripts/run_experiment.py \
  --suite workspace --benchmark-version v1.2.2 \
  --user-task user_task_20 --injection-task injection_task_7 \
  --attack direct --agent-model openai/gpt-4o-mini \
  --reviewer-model openai/gpt-4o-mini --k-values 0,1,2,4,8 \
  --output results/tester-task20-injection7-NEW-RUN-ID
```

去掉 `--user-task` 和 `--injection-task` 可运行全套；使用新的输出目录。完整原脚本命令不等于可断点续跑，也不保证比同样范围的分批运行快速完成。

## 本次 Colab 的分批与断点恢复

以下描述已使用的 notebook 包装逻辑；这些完成标记不是原 scripts/run_experiment.py 自带功能。本提交不另启动或替换正在运行的 notebook。

1. 每批为一个 task×injection，内部执行三个策略及五个 k；Full-approve 跨 k 缓存，所以 15 行对应 11 条独立计分轨迹，另有辅助 injection 检查。
2. 只有子进程退出 0、metrics/cases 各有 15 行且策略/k 组合齐全、每行 task_cases=1、曲线存在，才保存正式 ZIP 并写 `.done.json`。
3. Drive 目录 `MyDrive/PE6203_workspace_direct_full_v1` 中，同名 `.zip` 与 `.done.json` 同时存在才跳过。失败备份为 `_INCOMPLETE_时间戳.zip`，不得混入正式指标。
4. 原包装器单批超时 30 分钟；报错或超时会保存并停止整轮。它尚未实现失败后自动跳过和最后补跑。
5. 如 runtime 被回收，先恢复相同源码、解释器、依赖并重新挂载 Drive、授权 Secrets；保持实验名与配置一致，再运行原分批单元格。配置哈希不一致时应查明差异，不应删除配置绕过检查。
6. 若为 HTTP 402，先解决服务端余额/key 限额，再续跑；不能把 402 当成策略任务失败。更改模型、token 上限或策略代码需要新实验目录，避免混合条件。
7. 从用户提供的截止日志恢复时，应跳过 287 批，从 user_task_20/injection_task_7 重跑；若后来又完成了更多批次，应以 Drive 当前完整标记为准。

## 结果统计规则

只合并完整批次；保存 source_batch；TCR 用安全完成数之和除以案例数之和。未运行/运行异常不写为 0 分任务。HITL Load 只计安全完成任务的审核次数，总审核量使用 total_escalations。
Full-approve 缓存 cases 在原导出中有 1,148 行缺 policy；可从 audit_file 首段恢复并记录修复。计费时仅计其 k=0 一次；估算仍不包括辅助检查或失败重试。
