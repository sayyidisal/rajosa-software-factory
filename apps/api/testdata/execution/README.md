# Fixed benign execution fixtures (not part of the pytest suite)

These files are for controlled Docker / runner verification only.

- `test_pass_benign.py` — three deterministic passing asserts
- `test_fail_benign.py` — intentional failing assert

They live outside `apps/api/tests/` so they are not collected by `pytest apps/api/tests`.

Do not substitute model-generated or arbitrary user code when verifying the runner.
