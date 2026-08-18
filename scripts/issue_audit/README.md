# Issue audit reproductions

Run `uv run python scripts/issue_audit/repro.py` from the repository root.

Required for the OceanBase and DeepSeek cases:

```bash
export POWERCONTEXT_TEST_OCEANBASE_URL='mysql+aoceanbase://root%40test:powercontext-e2e@127.0.0.1:2881/powercontext?charset=utf8mb4'
export DEEPSEEK_API_KEY=...
```

The JSON report is written to `results.json`. Do not put API keys in the report, the script, or git.

The findings are documented in `docs/zh/development/issue-audit.md`.
