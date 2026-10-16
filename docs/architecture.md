# Arsitektur fase quality gate + orkestrasi + eksekusi lokal opsional

Aplikasi web Next.js meneruskan request same-origin di `/backend/*` ke FastAPI. FastAPI memakai **OrchestratorService** deterministik: buat task → peran planner → rencana tervalidasi → **persetujuan rencana eksplisit** → peran implementer → `BasicQualityGate` menurut `validation_profile` → (opsional) otorisasi eksekusi terpisah.

## Adapter

- `AGENT_ADAPTER=mock` (default): `MockAgentAdapter` untuk pengembangan dan tes deterministik tanpa jaringan.
- `AGENT_ADAPTER=sumopod`: `SumoPodAgentAdapter` memakai OpenAI Python SDK ke `SUMOPOD_BASE_URL` (default `https://ai.sumopod.com/v1`) dengan `SUMOPOD_API_KEY` dan `SUMOPOD_MODEL`.

Model dipilih lewat environment agar dapat diganti tanpa mengubah kode. Ketersediaan dan harga model harus diverifikasi di dashboard SumoPod.

Adapter dipilih saat aplikasi start dari `AGENT_ADAPTER` (default `mock`). Backend memuat `apps/api/.env` dari path absolut paket API tanpa menimpa variabel proses yang sudah ada. `GET /health` mengembalikan field `adapter` (`mock` atau `sumopod`) dan `execution_runner` tanpa membocorkan rahasia.

Jika `AGENT_ADAPTER=sumopod`, factory selalu menginstansiasi `SumoPodAgentAdapter` (tidak jatuh diam-diam ke mock). API key divalidasi saat task dijalankan, bukan saat start. Mode mock tidak membutuhkan kredensial.

Tidak ada retry otomatis. Timeout, kesalahan jaringan, HTTP 401/403/429/5xx, serta respons kosong dipetakan ke pesan aman. Traceback, API key, header Authorization, prompt lengkap, dan output model tidak dicatat ke log secara default.

## Validation profiles

Field opsional `validation_profile` pada `POST /tasks` (default `generic_text`):

| Profil | Pemeriksaan |
|---|---|
| `generic_text` | `output_not_empty`, `artifact_present` saja |
| `product_catalog_json` | JSON standar ketat + skema katalog; field ekstra diizinkan; NaN/Infinity/duplikat key ditolak |
| `python_unit_tests` | AST pytest-lite: `test_*` modul + `Test*.test_*`; **statis saja**; tanpa eksekusi/`tests_passed` |
| `python_code` | Artefak + struktur `def`/`class` via AST (statis) |
| `python_test_execution` | Setelah approval: validasi statis seperti `python_unit_tests`; `lifecycle=awaiting_execution_auth`; runtime hanya via `POST /tasks/{id}/execute-tests` |

Profil tidak dikenal ditolak dengan HTTP 422. Tidak ada inference semantik dari teks instruksi bebas. Memilih profil **tidak** mengotorisasi eksekusi kode. Persetujuan rencana **tidak** mengotorisasi eksekusi kode.

## Task, lifecycle, dan quality gate

Task dan event orkestrasi disimpan di SQLite (`TASK_DB_PATH`, default `apps/api/var/rajosa.sqlite3`, di luar workspace task). Asumsi **single-process**: jangan menjalankan beberapa worker uvicorn terhadap satu file DB.

- Restart dengan path DB yang sama membuat task/event tetap terbaca; tidak ada auto-resume planner/implementer/runner saat startup.
- Startup (store yang dimiliki aplikasi): mid-flight `created`/`planning`/`generating`/`validating` → `failed` (`interrupted`); wait states dan terminal tidak diubah. Reconciliation workspace bersifat read-only (orphan/missing/digest mirror/staging).
- `approval_digest` di DB adalah bukti untuk diverifikasi ulang, bukan bukti approval yang cukup sendiri.
- Tes offline tetap dapat meng-inject `MemoryTaskStore` (tanpa startup recovery otomatis).

Field:

- `lifecycle` — tahap aktual (`created` … `awaiting_plan_approval` … `completed` / `failed`).
- `status` — kompatibilitas: `pending` | `passed` | `failed` (`passed` hanya jika `lifecycle=completed`).
- `execution` — bukti runner terpisah (fail-closed).

`POST /tasks` **tidak** menjalankan implementasi; task masuk `awaiting_plan_approval` dengan `plan` tervalidasi. Implementasi dimulai hanya setelah `POST /tasks/{id}/approve-plan` dengan `{"approved": true}`.

HTTP 200 dari provider tidak berarti output task benar. Kegagalan validator deterministik menandai task `failed` meskipun artefak berhasil ditulis.

Quality gate membedakan:
1. Agent selesai dieksekusi.
2. Artefak ada.
3. Output lolos validator profil.
4. Task lulus gate.

Pemeriksaan `test_structure_valid` / `code_structure_valid` bersifat statis. Field terpisah `execution` membawa bukti runner (jika ada). Generated code **tidak** dijalankan pada `POST /tasks` maupun pada approve-plan.

Workspace menahan path di dalam root yang ditentukan. Ini adalah path containment, bukan isolasi eksekusi yang aman untuk arbitrary code.

## Isolated test execution (Phase 3B)

- Default: `EXECUTION_RUNNER=off` — tidak ada eksekusi kontainer.
- Mode opsional: `local_container` memakai Docker CLI dengan request terstruktur (bukan shell arbitrer).
- Otorisasi eksplisit: `POST /tasks/{task_id}/execute-tests` + `{"confirm": true}`.
- Staging per-execution di bawah `workspace/_executions/<id>`; dibersihkan setelah selesai.
- Concurrency lokal: satu eksekusi pada satu waktu.
- Desain lanjutan: [isolated-execution-design.md](./isolated-execution-design.md).
- Matriks kontrol & inspeksi runtime (Phase 3C): [execution-controls.md](./execution-controls.md).
- Definisi image yang diusulkan (belum di-build sampai disetujui): [../docker/execution/README.md](../docker/execution/README.md).

Isolasi kontainer lokal **bukan** jaminan keamanan lengkap. `EXECUTION_RUNNER` tetap `off` secara default.

## Keamanan operasional

API default mendengarkan `127.0.0.1`. Frontend development diikat ke localhost. API key SumoPod hanya boleh ada di environment backend, bukan di frontend atau `NEXT_PUBLIC_*`.

API tidak boleh diekspos ke internet tanpa autentikasi dan kontrol keamanan yang sesuai.

## Orkestrasi agen (Phase 4B)

Orchestrator deterministik + approval rencana wajib: [agent-orchestration-design.md](./agent-orchestration-design.md).

## Perencanaan terstruktur (Phase 4C)

Kontrak rencana `schema_version: "2"`, JSON ketat, dan digest approval (plan + profile): [phase-4c-design.md](./phase-4c-design.md).

## Persistensi (Phase 4E.1–4E.4)

SQLite task repository + wiring aplikasi: migrasi wajib saat open; failure migrasi menggagalkan startup; koneksi ditutup pada shutdown untuk store yang dimiliki aplikasi. Integrity approval (re-validate plan + profile + digest) pada get/approve/execute bersifat fail-closed tanpa auto-repair. `PRAGMA busy_timeout` (5s) mengurangi gagal langsung saat kontensi singkat; **bukan** jaminan multi-worker.

Startup recovery (4E.3): fail mid-flight tasks dengan `error_category=interrupted` secara idempotent (lifecycle persisted). `list_by_lifecycle` diurutkan `id ASC`. Workspace reconciliation mendeteksi orphan/missing/digest-mirror/staging leftovers tanpa menghapus artefak. Kegagalan recovery DB menggagalkan startup; kegagalan scan FS dicatat sebagai laporan tidak lengkap. `GET /health` menyertakan ringkasan `startup` (counts/flags/task ids saja; tanpa path absolut atau pesan error mentah).

## Keputusan yang ditunda

Cleanup/quarantine orphan otomatis, retry terbatas (4F), reviewer agent (4D), autentikasi, worker asinkron / multi-worker SQLite, queue, Turborepo atau Nx, remote Git, design system, dan remote execution workers.
