# Arsitektur fase pertama

Aplikasi web Next.js meneruskan request same-origin di `/backend/*` ke FastAPI. FastAPI membuat direktori workspace, menjalankan `MockAgentAdapter`, lalu menilai hasil dengan `BasicQualityGate`.

Task disimpan di memori proses. Restart backend menghapus seluruh task.

`status` bernilai `passed` hanya jika quality gate juga lulus. Exception adapter ditangkap sekali, tanpa retry, dan disimpan sebagai `failed` dengan pesan tetap. Teks exception tidak dikembalikan ke klien.

Workspace menahan path di dalam root yang ditentukan, termasuk penolakan path absolut, komponen `..`, dan symlink yang menunjuk keluar. Ini adalah path containment, bukan isolasi eksekusi yang aman untuk arbitrary code.

API default mendengarkan `127.0.0.1`. API tidak boleh diekspos ke internet tanpa autentikasi dan kontrol keamanan yang sesuai.

## Keputusan yang ditunda

Provider model, autentikasi, persistensi, worker asinkron, isolasi container, Turborepo atau Nx, peran agent, remote Git, dan design system.
