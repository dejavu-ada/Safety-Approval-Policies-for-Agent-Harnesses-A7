# 独立测试记录（287 批快照）

核对日期：2026-10-29（Asia/Singapore）。这是成员 2 的独立测试交付，不是完整 benchmark 完成声明。

- [任务完成情况核对](TASK_STATUS.md)：对照仓库 README 和组员 1 的消息。
- [测试说明](TESTING_GUIDE.md)：环境、版本、运行和恢复步骤。
- [覆盖清单](coverage.csv)：全部 560 个任务×攻击组合，287 个完成、273 个未包含在快照中。
- [实验报告](EXPERIMENT_REPORT.md)：离线测试、真实 API 试跑、287 批分析与限制。
- [策略汇总](policy_summary.csv)、[逐任务汇总](task_summary.csv)、[完整批次指标](metrics_all.csv)。
- [TCR 曲线](tcr_partial287.svg)：仅当前子集。
- [失败案例证据](failure_examples.json)：从原始失败记录提取的两个案例，附用户请求。
- [校验统计](validation.json)、[轨迹检查](trace_validation.json)。

`missing_from_this_export` 不等于测试失败，也不声称当前 Colab 仍未完成该案例。Full-approve 跨 k 复用；不要将重复行当成独立重复实验或重复计费。

源快照：PE6203_first287_batches_20261029_070120_799353.zip；SHA256：429f383fd9926cdf54f4241cb7cda029b982dd007f6e6699b8071bafd8426beb。原始批次 ZIP 由测试负责人保留于实验 Drive 目录 `PE6203_workspace_direct_full_v1`；本提交不声称已公开提供完整原始档案。
