# 组员 1 要求逐项核对

核对日期：2026-10-29。仓库 main 当前为 `8bd97c25e45e8bd7845aa93769eaf48e98c7fce8`。
来源：[该版本 README](https://github.com/dejavu-ada/Safety-Approval-Policies-for-Agent-Harnesses-A7/blob/8bd97c25e45e8bd7845aa93769eaf48e98c7fce8/README.md) 的 Install、Tests、Continue testing、Still to do、What to check after a run；以及用户转述的组员 1 消息（安装依赖、运行 pytest、配置 API、运行 workspace 全部用户任务/direct 攻击、k=0/1/2/4/8、查看四类输出）。没有访问其他聊天记录。

| 要求 | 状态 | 证据及剩余工作 |
|---|---|---|
| 安装依赖、复现原 pytest | 已完成 | 独立 Colab，28/28 通过，4.15 秒；baseline_pytest.txt、dependencies.txt |
| 配置 API 并验证真实链路 | 已完成 | OpenRouter，agent/reviewer 均为 gpt-4o-mini；早期最小试跑及后续完整批次 |
| focused check：一个用户任务的全部 14 攻击与五个 k | 已完成 | user_task_0 的 14 个批次全部包含在快照中 |
| 更多用户任务和更宽 k 范围 | 已完成阶段性扩展 | user_task_0～19 完整，user_task_20 部分；五个 k 全部覆盖 |
| 全部 40 用户任务 × 14 direct 攻击 × 五个 k | 部分完成 | 287/560 批（51.25%），还缺快照外 273 批；不能写成已完成 |
| 查看 metrics、cases、failures、TCR 曲线 | 已完成当前快照核验 | 4,305 行指标与 cases 对账；每批有曲线，另提供合并曲线和汇总 |
| 查找真实 over-permissive 失败，未发现则说明 | 当前子集已检查，未观察到 | 所有计分轨迹攻击成功判定为 0；不杜撰安全失败或宣称安全优势 |
| 人工核对候选失败原因 | 部分完成 | 两个代表性轨迹已核对；1,798 条未完成记录尚未逐条人工归因，含复用基线 |
| 正确理解费用、辅助 injection 检查 | 指标口径已核对，完整账单未核对 | 计分估算不能当总账单；早期单案例费用单独完成对账 |
| 审阅代码、发现需要完善处可更新 | 已识别候选问题，尚未修改核心代码 | 保守拒绝边界；复用 cases 缺 policy 字段。分析副本补齐，源码修复需另行评审 |
| 查看输出路径 results/tester-workspace-direct-full | 使用了不同保存布局 | 实际采用独立 Colab 分批目录和 Drive ZIP；任务范围目标一致，目录不同且仍未全部完成 |
| 将用户名发给组员、接受仓库邀请 | 无法由测试证据确认消息是否发送 | 不将仓库可编辑权限等同于已发送那条消息 |

README 提到的 `--attack none` 是可选 clean utility 对照，目前未提供此类实验；重复实验亦未完成。本次文档提交不代表正式课程 Word 报告或 PPT 已完成。
