# Arsitektur fase quality gate + eksekusi lokal opsional

Aplikasi web Next.js meneruskan request same-origin di `/backend/*` ke FastAPI. FastAPI membuat direktori workspace, menjalankan agent adapter, lalu menilai hasil dengan `BasicQualityGate` menurut `validation_profile`.

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
| `python_test_execution` | Validasi statis sama seperti `python_unit_tests` pada create; runtime hanya via `POST /tasks/{id}/execute-tests` |

Profil tidak dikenal ditolak dengan HTTP 422. Tidak ada inference semantik dari teks instruksi bebas. Memilih profil **tidak** mengotorisasi eksekusi kode.

## Task dan quality gate

Task disimpan di memori proses. Restart backend menghapus seluruh task.

`status` bernilai `passed` hanya jika quality gate juga lulus. HTTP 200 dari provider tidak berarti output task benar. Kegagalan validator deterministik menandai task `failed` meskipun artefak berhasil ditulis.

Quality gate membedakan:
1. Agent selesai dieksekusi.
2. Artefak ada.
3. Output lolos validator profil.
4. Task lulus gate.

Pemeriksaan `test_structure_valid` / `code_structure_valid` bersifat statis. Field terpisah `execution` membawa bukti runner (jika ada). Generated code **tidak** dijalankan pada `POST /tasks`.

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

## Keputusan yang ditunda

Multi-agent, autentikasi, persistensi, worker asinkron, queue, Turborepo atau Nx, remote Git, design system, dan remote execution workers.
