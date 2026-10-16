# Rajosa AI Software Factory

Fondasi lokal untuk menjalankan satu task melalui orchestrator deterministik (rencana → persetujuan eksplisit → implementasi → quality gate), workspace dengan penahanan path, dan (opsional, default off) runner tes Python terisolasi lokal. Mendukung mock adapter dan SumoPod. Belum production-ready; isolasi kontainer bukan jaminan keamanan lengkap.

## Prasyarat

- Node.js 24 dan npm
- Python 3.12
- Git
- (Opsional) Docker Desktop untuk `EXECUTION_RUNNER=local_container` — daemon harus berjalan dan image pinned harus sudah ada lokal

## Persiapan

```bash
python3 -m venv apps/api/.venv
apps/api/.venv/bin/pip install -e "./apps/api[dev]"
npm --prefix apps/web install
```

Salin `apps/api/.env.example` ke `apps/api/.env` dan `apps/web/.env.example` ke `apps/web/.env.local` saat konfigurasi lokal. **Wajib** set `RAJOSA_API_TOKEN` yang sama di keduanya (rahasia acak panjang). Jangan menyimpan token API, SumoPod key, atau rahasia lain di frontend bundle atau variabel `NEXT_PUBLIC_*`.

Generate contoh token (jalankan lokal; jangan commit hasilnya):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Mode mock (default)

Default `AGENT_ADAPTER=mock`. Backend dapat dijalankan dan diuji tanpa SumoPod key serta tanpa akses jaringan provider. Token API (`RAJOSA_API_TOKEN`) tetap diperlukan agar rute `/tasks*` tidak fail-closed dengan HTTP 401.

```bash
make dev-api
make dev-web
```

`make dev-api` menjalankan **satu** proses API (`python -m app.run_dev`, Uvicorn `--reload`). Jangan menambah `--workers` atau mengatur `WEB_CONCURRENCY>1`. Beberapa worker/instance yang berbagi SQLite/workspace yang sama **tidak didukung**.

UI memanggil `/backend/*`; Next.js menyuntikkan `Authorization: Bearer` di **server** dari `RAJOSA_API_TOKEN`. Rewrite mentah tanpa token **bukan** batas autentikasi — gate ada di FastAPI.

API: `http://127.0.0.1:8000`  
Web: `http://127.0.0.1:3000` (dan `http://localhost:3000`)

`make dev-web` mengikat frontend ke `127.0.0.1` agar rewrite `/backend` tidak dapat dijangkau dari jaringan lokal.

## Mode SumoPod

1. Salin template: `cp apps/api/.env.example apps/api/.env`
2. Edit `apps/api/.env` dan setel **wajib**:

```bash
AGENT_ADAPTER=sumopod
SUMOPOD_API_KEY=your-key-here
SUMOPOD_BASE_URL=https://ai.sumopod.com/v1
SUMOPOD_MODEL=gpt-4.1-mini
SUMOPOD_TIMEOUT_SECONDS=60
```

Tanpa baris `AGENT_ADAPTER=sumopod`, backend memakai mock (default) dan output akan diawali `Mock result`.
3. Jangan commit file `.env`. Pastikan file berisi pasangan `KEY=value` yang valid (bukan teks bebas).
4. Pastikan nama model tersedia pada akun sebelum dipakai. Katalog dan harga dapat berubah; verifikasi di dashboard SumoPod.
5. Hentikan proses API lama, lalu jalankan ulang `make dev-api` dari root repository.
6. Periksa adapter aktif:

```bash
curl -s http://127.0.0.1:8000/health
```

Harus memuat `"adapter":"sumopod"`. Jika masih `"mock"`, konfigurasi belum terbaca. Rute task memerlukan header bearer:

```bash
curl -s -H "Authorization: Bearer $RAJOSA_API_TOKEN" \
  http://127.0.0.1:8000/tasks/<task-id>
```

Jika `AGENT_ADAPTER=sumopod` tetapi `SUMOPOD_API_KEY` kosong, aplikasi tetap start dan task gagal dengan pesan konfigurasi yang aman — bukan output mock.

## Validation profiles

`POST /tasks` menerima field opsional `validation_profile` (default `generic_text`):

| Profil | Deterministik? | Isi |
|---|---|---|
| `generic_text` | Ya (terbatas) | Output tidak kosong + artefak ada + batas ukuran output. Tidak ada skor semantik. |
| `product_catalog_json` | Ya | JSON standar murni (tolak NaN/Infinity/duplikat key/fence); `products` tepat 3 item; id/name/price/in_stock. Field ekstra pada objek **diizinkan**. |
| `python_unit_tests` | Statis (AST) | Pytest-compatible discovery. **Tidak mengeksekusi tes.** Tidak ada `tests_passed`. |
| `python_code` | Statis (AST) | Syntax + `def`/`class`; **tidak dijalankan.** |
| `python_test_execution` | Statis setelah approval | Setelah approval: menunggu `awaiting_execution_auth`; runtime hanya setelah `execute-tests`. |

Profil tidak dikenal ditolak (HTTP 422). Profil tidak diinfer dari teks instruksi. Memilih profil **tidak** menjalankan kode.

## Alur orkestrasi (Phase 4B)

1. `POST /tasks` → `lifecycle=awaiting_plan_approval` + `plan` tervalidasi (belum implementasi).
2. `POST /tasks/{id}/approve-plan` dengan `{"approved":true}` → implementasi + quality gate.
3. Profil statis → `lifecycle=completed` / `status=passed` jika gate lulus.
4. `python_test_execution` → `lifecycle=awaiting_execution_auth` hingga eksekusi eksplisit.
5. `{"approved":false}` menolak rencana tanpa implementasi.

Lihat [docs/agent-orchestration-design.md](./docs/agent-orchestration-design.md).

## Isolated Python test execution (default: off)

Lihat [docs/execution-controls.md](./docs/execution-controls.md) untuk inspeksi runtime, matriks verifikasi Phase 3C, dan residual risk. Definisi image yang diusulkan (belum di-build sampai disetujui): [docker/execution/README.md](./docker/execution/README.md).

Alur:

1. Buat task dengan `validation_profile=python_test_execution`.
2. Setujui rencana (`approve-plan`).
3. Setelah `awaiting_execution_auth`, minta eksekusi eksplisit:

```bash
curl -s http://127.0.0.1:8000/tasks/<task-id>/execute-tests \
  -H "Authorization: Bearer $RAJOSA_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"confirm":true}'
```

Atau tombol **Jalankan tes terisolasi** di UI saat lifecycle menunggu otorisasi eksekusi. Persetujuan rencana **tidak** menjalankan runner.

Default:

```bash
EXECUTION_RUNNER=off
```

Untuk mencoba runner Docker lokal (setelah daemon hidup dan image pinned dimuat secara manual — aplikasi **tidak** menarik image saat task):

```bash
EXECUTION_RUNNER=local_container
EXECUTION_IMAGE=rajosa-exec-python:3.12-pytest
```

Tanpa daemon/image, permintaan execute-tests gagal aman (`runner_unavailable`, `tests_passed=false`). Tidak ada fallback eksekusi di host.

Batas awal: timeout 30s, memori 512 MiB, concurrency 1, stdout/stderr terbatas.

## Automated tests

```bash
make test-api
```

Tes default memakai mock client/adapter dan **fake execution runner**. Tidak memanggil SumoPod berbayar. Tidak mengeksekusi kode generatif arbitrer. Integrasi Docker live ditandai skip di suite default.

Frontend:

```bash
npm --prefix apps/web run lint
npm --prefix apps/web run build
```

## Verifikasi health endpoint

```bash
curl -s http://127.0.0.1:8000/health
```

Contoh:

```json
{"status":"ok","service":"rajosa-api","adapter":"mock","execution_runner":"off"}
```

## Batas yang disengaja

Task dan event orkestrasi disimpan di SQLite secara default (`TASK_DB_PATH`, biasanya `apps/api/var/rajosa.sqlite3`, di luar workspace task). Task dan event **bertahan** melewati restart backend selama path DB sama.

Saat startup (store yang dimiliki aplikasi): task di state interruptible (`created`, `planning`, `generating`, `validating`) ditandai `failed` dengan `error_category=interrupted`. Task `awaiting_plan_approval` atau `awaiting_execution_auth` tetap menunggu tindakan manusia eksplisit. Recovery **tidak** otomatis melanjutkan planner/implementer atau mengeksekusi kode.

Persistensi ini **bukan** jaminan exactly-once execution, keamanan multi-process/multi-worker terhadap satu file DB, atau pemulihan setiap side effect eksternal (artefak di disk, container, dsb.).

**Topologi yang didukung:** tepat satu proses API per DB + workspace (`make dev-api`) dengan `RAJOSA_API_TOKEN` dikonfigurasi. Prefer `TASK_DB_PATH` / `WORKSPACE_ROOT` absolut. **Tidak didukung:** `uvicorn --workers N` (N>1), beberapa instance berbagi storage yang sama, paparan eksternal / multi-tenant tanpa kontrol tambahan. Loopback tidak menggantikan bearer auth. Guard pada entry point dev menolak `WEB_CONCURRENCY!=1` dan `--workers`≠1.

**Rotasi token:** ganti `RAJOSA_API_TOKEN` di `.env` API dan `.env.local` web, restart `dev-api` + `dev-web`, buang nilai lama.

**Backup SQLite:** DB memakai WAL. Jangan hanya menyalin file `.sqlite3` hidup. Hentikan API bila bisa, lalu dari `apps/api`:

```bash
.venv/bin/python -m app.persistence.backup_cli backup \
  --source var/rajosa.sqlite3 --dest-dir var/backups
.venv/bin/python -m app.persistence.backup_cli verify \
  --path var/backups/<file>.sqlite3 --checksum var/backups/<file>.sqlite3.sha256
```

Prosedur lengkap: [docs/sqlite-backup.md](docs/sqlite-backup.md).

Workspace memberikan path containment. Runner kontainer (jika diaktifkan) menambah containment terpisah untuk tes Python yang diotorisasi secara eksplisit — tetap bukan sandbox produksi. `EXECUTION_RUNNER` default `off`.

`status` task yang `passed` dari quality gate **tidak** berarti `execution.tests_passed`. Bukti runtime ada di field `execution`.

API tidak boleh diekspos ke internet tanpa autentikasi dan kontrol keamanan yang sesuai. Konfigurasi default hanya mendengarkan `127.0.0.1`.

## Live check manual (opsional)

Prosedur ini terpisah dari tes otomatis dan dapat memakai kredit SumoPod:

1. Setel `AGENT_ADAPTER=sumopod` dan `SUMOPOD_API_KEY`.
2. Jalankan backend serta frontend.
3. Kirim satu task sederhana dari UI.
4. Periksa status, output, dan `apps/api/var/workspaces/<task-id>/result.txt`.
