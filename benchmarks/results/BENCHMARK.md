# BENCHMARK

- 生成时间: 2026-08-22 21:30:43
- 数据集: swebench (princeton-nlp/SWE-bench)
- 模型: deepseek-chat
- 配置: 6 实例跨 6 仓库; 官方 harness (swebench 4.0.5) + 预构建 Docker 镜像 (cache_level=image); 无 gold patch 注入

## 汇总

| 指标 | 值 |
|---|---|
| 实例数 | 6 |
| **官方用例通过 (resolved)** | **1 / 4** |
| 平均轮次 | 34.83 |
| 平均耗时(s) | 223.89 |
| 总成本($) | 0 (DeepSeek API 按量计费，未单独记账) |

## 逐实例

| instance | 官方判定 | FAIL_TO_PASS / PASS_TO_PASS |
|---|---|---|
| psf__requests-1142 | YES | FAIL_TO_PASS 1/1 pass; PASS_TO_PASS 5/5 pass |
| django__django-10087 | NO | patch apply failed (model patch malformed, no trailing newline) |
| pallets__flask-4045 | NO | FAIL_TO_PASS 0/2 pass; PASS_TO_PASS 50/50 pass (F2P failed) |
| pytest-dev__pytest-10051 | NO | FAIL_TO_PASS 0/1 pass; PASS_TO_PASS 14/15 pass (F2P failed, 1 regression) |
| sphinx-doc__sphinx-10021 | - | no patch produced (not submitted) |
| sympy__sympy-11232 | - | no patch produced (not submitted) |

> 说明：官方判定 = 官方 Docker 镜像跑 FAIL_TO_PASS/PASS_TO_PASS。sphinx/sympy 未产出有效 diff，未提交官方评测。
