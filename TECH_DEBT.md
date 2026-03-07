Tech debt and follow-ups from PR #3 (SDK migration)

Priority: High
- Add SDK-path unit tests that mock `b2sdk` objects to exercise `b2_client.py`.
- Make the `B2Client` usage thread-safe: avoid sharing one client across threads; use `threading.local()` or inject per-worker clients.
- Run full CI and fix any failing tests, lints, and SonarCloud findings.

Priority: Medium
- Expand CLI JSON parsing robustness for unexpected shapes.
- Add integration tests for large `b2 sync` runs and tune `SYNC_TIMEOUT` if needed.
- Consider removing CLI fallback once SDK coverage is complete.

Priority: Low
- Add linters and formatters to CI (ruff/black) and resolve style issues.
- Improve logging and metrics for long-running sync/upload operations.

Notes:
- These items reflect issues and recommendations surfaced during the SDK migration PR (feat/sdk-migration -> main).
- Addressing the high-priority items will significantly reduce flakes and increase confidence in the SDK path.
