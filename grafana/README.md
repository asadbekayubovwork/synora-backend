# Grafana — metrikalarni ko'rish

Ikkita konteyner (Prometheus + Grafana) va provisioning fayllari. API bu
compose'ga kirmaydi: u sizning `.env`ingiz, bazangiz va venv'ingiz bilan
hostda ishlaydi, va uni faqat scrape uchun konteynerlashtirish ilovani ishga
tushirishning ikkinchi usulini qo'llab-quvvatlash demakdir.

```bash
.venv/bin/uvicorn app.main:app --port 8000            # API → /metrics
docker compose -f grafana/docker-compose.yml up -d    # Grafana → :3001
```

| | |
| --- | --- |
| Grafana | http://localhost:3001 — dashboard darhol ochiladi, login yo'q |
| Prometheus | http://localhost:9090 — panelga aylantirishdan oldin so'rovni sinash uchun |
| Scrape manzili | `host.docker.internal:8000/metrics`, har 15 sekundda |

Port 3001, chunki 3000 `dev-ui/` niki. Anonim kirish ataylab yoqilgan — bu
lokal stack, va `localhost` dashboardining oldidagi login oynasi hech nima
o'rgatmaydi. Ishlab chiqarish uchun bu compose fayli emas: u yerda o'z
Grafana'ngiz bo'ladi va u xuddi shu `/metrics` ga `METRICS_TOKEN` bilan boradi.

## Dashboard nimani ko'rsatadi

Beshta qator: **Traffic**, **Speech**, **Money**, **Sessions/jobs/process** va
**Batch outcomes and reconciliation**. Har bir panelning tavsifi (`i` belgisi)
raqam nimani anglatishini aytadi. Eng foydali uchtasi:

| Panel | Nega muhim |
| --- | --- |
| **Held right now** | Sessiyalarga band qilingan kredit. Chaqiruvlar paytida ko'tarilib, keyin tushishi kerak. Tinch tunda pastki chegarasi o'sib borsa — qaytmagan hold, va `POST /admin/reconcile` uni tiklaydi |
| **Ledger divergence** | Balansi o'z ledgeri bilan mos kelmagan hisoblar. Bu yerdagi noldan katta har qanday qiymat alert'ga arziydi |
| **Syntheses by outcome** | `billed` / `free` / `replay`. `free` ning o'sishi — upstream birinchi baytdan oldin rad etayotgani; `replay` ning o'sishi — mijoz idempotency kalitini qayta yuborayotgani |

## Metrikalar qayerdan keladi

Hech biri logdan yoki bazadan keyinchalik chiqarilmaydi: har bir hisoblagich
javobni allaqachon bilgan kodning yonida oshiriladi — `_finalise` stream qanday
tugaganini biladi, `settle_oneshot` qancha yechilganini biladi. Sabablari va
qoidalari (yorliqlar cheklovi, micros butun son bo'lib qolishi, bitta worker
sharti) `app/core/metrics.py` ning boshida yozilgan.

Faqat to'rtta ko'rsatkichni hisoblagich bila olmaydi — band kredit, ochiq
sessiyalar, ochiq batch joblari va hisoblardagi qoldiq. Ular jarayon qayta
ishga tushganda ham saqlanadi va satrlar yig'indisi bo'ladi, shuning uchun
`refresh_db_gauges` ularni scrape paytida uchta agregat so'rov bilan o'qiydi.
`METRICS_DB_GAUGES=false` buni o'chiradi.

## Ishlab chiqarishda

1. `METRICS_TOKEN` ni o'rnating — usiz `/metrics` development'dan tashqarida
   `404` qaytaradi va startup logida sababi yoziladi:

   ```
   synora: Metrics: not served (set METRICS_TOKEN; /metrics answers 404 without one)
   ```

   Sabab nginx: API `location /` orqali proksilanadi, ya'ni qo'shilgan route
   paydo bo'lishi bilanoq ochiq, `/metrics` esa chaqiruvlar hajmini, kredit
   harakatini va mijozlar sonini e'lon qiladi. Bu boot xatosi emas — ataylab:
   dashboard hech qachon o'zi kuzatayotgan API'ning relizini yiqita olmasligi
   kerak.
2. `prometheus.yml` dagi `authorization` blokini oching va tokenni fayldan
   bering (repoga yozmang).
3. Qo'shimcha qatlam sifatida nginx'da yopish ham arziydi:
   ```nginx
   location = /metrics { deny all; }
   ```
4. `--workers 1` bo'lib qolsin. Har bir jarayonning o'z registri bo'ladi va
   Prometheus proksi tanlagan bittasini scrape qiladi — natijada trafikning bir
   qismini ko'rsatadigan grafik chiqadi. `WORKER_COUNT > 1` bo'lsa endpoint
   o'zini o'chiradi (yana 404), ya'ni yolg'on grafik o'rniga hech nima.

## Dashboardni tahrirlash

Fayllar konteynerga read-only ulangan va `allowUiUpdates: false`, ya'ni
Grafana'ning o'z muharriri ustidan saqlay olmaydi. Panelni UI'da sozlab ko'ring,
so'ng **Dashboard settings → JSON Model** dan nusxa olib
`dashboards/synora-ops.json` ga yozing va `docker compose restart grafana`.
Konteyner bazasida yashaydigan dashboard — bu boshqa hech kimda yo'q dashboard.
