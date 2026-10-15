# Rajosa AI Software Factory

Fondasi lokal untuk menjalankan satu task melalui adapter tiruan, workspace dengan penahanan path, dan quality gate. Fase ini belum menjalankan model AI atau sistem multi-agent.

## Prasyarat

- Node.js 24 dan npm
- Python 3.12
- Git

## Persiapan

```bash
python3 -m venv apps/api/.venv
apps/api/.venv/bin/pip install -e "./apps/api[dev]"
npm --prefix apps/web install
```

Salin `apps/api/.env.example` dan `apps/web/.env.example` hanya jika Anda perlu mengganti nilai default. Aplikasi dapat berjalan tanpa file tersebut.

## Menjalankan backend

```bash
make dev-api
```

API tersedia di `http://127.0.0.1:8000`.

## Menjalankan frontend

Di terminal lain:

```bash
make dev-web
```

Aplikasi web tersedia di `http://127.0.0.1:3000` (dan `http://localhost:3000`).
Perintah `make dev-web` mengikat frontend ke `127.0.0.1` agar rewrite `/backend` tidak dapat dijangkau dari jaringan lokal.

## Automated tests

```bash
make test-api
```

Perintah ini menjalankan pytest untuk health, alur task, kegagalan quality gate, exception adapter, dan penahanan path workspace.

## Verifikasi health endpoint

Dengan backend sedang berjalan:

```bash
curl -s http://127.0.0.1:8000/health
```

Respons yang diharapkan:

```json
{"status":"ok","service":"rajosa-api"}
```

## Batas yang disengaja

Task store hanya berada di memori proses backend. Seluruh task hilang saat backend restart.

Workspace saat ini hanya memberikan path containment. Ini bukan isolasi eksekusi yang aman untuk arbitrary code.

API tidak boleh diekspos ke internet tanpa autentikasi dan kontrol keamanan yang sesuai. Konfigurasi default hanya mendengarkan `127.0.0.1`.
