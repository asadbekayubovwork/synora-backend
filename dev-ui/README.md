# Dev UI — gateway'lar va billingni qo'lda sinash uchun

Bog'liqliksiz bitta HTML fayl (`index.html`) + kichik statik server (`serve.py`).
API'ni brauzerdan chaqiradi, shuning uchun CORS, `Access-Control-Expose-Headers`,
`402` ning shakli va audio oqimi haqiqiy sharoitda tekshiriladi — Swagger'da
ko'rinmaydigan narsalar aynan shular.

## Ishga tushirish

```bash
.venv/bin/uvicorn app.main:app --port 8000     # API
python dev-ui/serve.py                         # UI → http://localhost:3000
```

**Port 3000 ataylab.** `CORS_ORIGINS` ning standart qiymati
`http://localhost:3000,http://127.0.0.1:3000` — boshqa portdan ochsangiz brauzer
har bir so'rovni to'xtatadi va konsolda CORS xatosi chiqadi.

Yuqoridagi `API` maydonida manzil `http://127.0.0.1:8000/api/v1`. Boshqa portda
ishlatsangiz, shu maydonni o'zgartirasiz — `localStorage`da saqlanadi.

## Noldan sinovga

1. **Auth** → `register`. `EXPOSE_DEV_OTP=true` bo'lsa `dev_code` javobda
   qaytadi va o'zi OTP maydoniga tushadi. → `verify-otp`.
2. **Admin** → `kredit berish`. Superuser talab qiladi:
   `.venv/bin/python devtools/set_superuser.py <email>`. `User ID` maydoni
   `/auth/me` bosilganda o'zi to'ladi.
3. **Nutq** → `estimate` (hech narsa yechilmaydi), so'ng `sintez qilish`.
4. **Wallet** → `tranzaksiyalar`: bitta sintez `hold` → `release` → `debit`
   qoldiradi, uchtasi bitta `group_id` ostida.
5. **Usage** → bizning `usage_events` jadvalimizdan o'qilgan hisobot.

## Nutq yorliqi nimani ko'rsatadi

Sintezdan keyin UI o'zi ikkita tekshiruv qiladi — qo'lda solishtirishga hojat yo'q:

| Tekshiruv | Nega |
| --- | --- |
| yechilgan summa `X-Synora-Price-Micros` ga teng | narx birinchi baytdan oldin qat'iy, keyin o'zgarmasligi kerak |
| `reserved` nolga qaytdi | qaytmagan hold — mijoz sarflay olmaydigan kredit, va reconcile uni tirik sessiya deb biladi |

Bundan tashqari birinchi baytgacha ketgan vaqt, jami vaqt, bo'laklar soni va
`x-` bilan boshlanadigan barcha headerlar log panelida turadi. Brauzer bu
headerlarni faqat `app/main.py` dagi `expose_headers` ularni nomlagani uchun
o'qiy oladi.

## Nimalarni qamraydi

| Yorliq | Endpointlar |
| --- | --- |
| Auth | `register`, `verify-otp`, `resend-otp`, `login`, `refresh`, `/auth/me` |
| Nutq | `POST /tts/estimate`, `POST /tts/speech` (audio pleyer bilan) |
| Batch | `POST/GET /tts/batch`, `GET /tts/batch/{id}`, `/results`, `DELETE` |
| Ovozlar | `GET/POST /tts/voices`, `DELETE /tts/voices/{id}` |
| Matnga | `POST /stt/transcribe` — fayl yuklash yoki oxirgi sintezni qaytarib o'qish |
| Jonli | `WS /stt/stream` — mikrofondan real vaqtda transkripsiya |
| Agent | `GET /voice/config`, `POST /voice/sessions`, `/candidates`, `/heartbeat`, `DELETE /voice/sessions/{id}`, `GET /voice/sessions[/{id}]` — ovozli agent bilan WebRTC qo'ng'iroq, `voice-agent-client.js` orqali |
| Usage | `GET /usage` |
| Wallet | `GET /wallet`, `GET /wallet/transactions` (kursor bilan) |
| Admin | `credits`, `freeze`, `unfreeze`, `GET /admin/wallets/{id}`, `reconcile` |

## Tez-tez uchraydigan ikki holat

**`TTS: sozlanmagan`** — yuqoridagi pill shuni yozsa, `TTS_BASE_URL` yoki
`TTS_API_KEY` protsessga yetib bormagan. Bunda **hamma** `/tts` route `503`
qaytaradi, hatto upstreamga chiqmaydigan `/estimate` ham (`app/api/v1/tts.py`
dagi izoh: bajarolmaydigan ishga narx aytish ma'nosiz). Boshqa hech narsa
buzilmaydi — wallet, usage, admin ishlaydi.

**Batchda `state` o'zgarmayapti** — broker (`RABBITMQ_URL`) bo'lmasa jobni
**o'qish uni oldinga suradi**: upstreamdan holatni so'raydigan boshqa hech narsa
yo'q. `avto-poll 2s` ni yoqing yoki `o'qish` ni bosib turing.

## GPU'siz sinash — soxta speech box

`TTS_BASE_URL` bo'lmasa `/tts` ishlamaydi, lekin haqiqiy GPU ham shart emas:

```bash
python dev-ui/fake_speech_box.py                                  # → :8100
TTS_BASE_URL=http://127.0.0.1:8100 TTS_API_KEY=fake-key \
    .venv/bin/uvicorn app.main:app --port 8000
```

Soxta box `tts_client` kutgan barcha yo'llarni beradi va belgilar soniga
proporsional WAV toni chiqaradi (nutq emas — maqsad pulning harakatini ko'rish).
`TTS_API_KEY` ni almashtirib xato yo'llarini ham ko'rasiz: `reject-me`, `quota`,
`busy`, `bad-input`, `garbage`. Batafsili faylning o'z izohida.

**Format `wav` bo'lib turaversin.** Soxta box faqat wav/pcm chiqaradi; `mp3`
so'ralsa baytlar baribir wav bo'ladi va pleyer ochmaydi.

## Ikki shlyuzni bir aylanishda sinash

**Matnga** yorlig'idagi "oxirgi sintezni o'qish" tugmasi **Nutq** yorlig'ida
hozirgina yaratilgan audioni STT ga yuboradi: matn → audio → matn. Ikkala
shlyuz, ikkala hisob-kitob va ikkala `reserved` bir bosishda ko'rinadi.

STT sozlanmagan bo'lsa (`STT_BASE_URL` yo'q) route `503 stt_not_configured`
qaytaradi — TTS bilan bir xil qoida.

## Realtime STT ni sinash

Ikki yo'l bor.

**Mikrofon bilan** — **Jonli** yorlig'i. "mikrofonni yoqish" bosasiz, brauzer
ruxsat so'raydi, gapirasiz. Har bir gap tugaganda VAD segmentni yopadi va matn
darhol chiqadi; yashil `▍` — `speech_started`, ya'ni voice-agent uchun barge-in
signali. "to'xtatish" bosilganda `stop` ketadi va `done` hisobni qaytaradi.

Brauzer mikrofoni odatda 44.1 yoki 48 kHz da ishlaydi; sahifa
`AudioContext({sampleRate: 16000})` bilan qayta namunalashni brauzerga
topshiradi va faqat PCM16 ni uzatadi.

**Fayldan** — mikrofonsiz mashina yoki takrorlanadigan kirish uchun:

```bash
python dev-ui/stream_file.py clip.wav --language uz
```

Audio real vaqt tezligida yuboriladi, ataylab: faylni tez otib yuborish ham
transkripsiya beradi, lekin kechikish haqida hech nima aytmaydi va
`session_ms` ni ma'nosiz qiladi.

Klip mono PCM16 bo'lishi kerak:

```bash
ffmpeg -i input.m4a -ar 16000 -ac 1 -c:a pcm_s16le clip.wav
```

Chap paneldagi `reserved` ni kuzating: sessiya ochilganda shift band qilinadi
(standart 10 daqiqa ≈ 14 kredit) va `done` da ortig'i qaytadi.

## Ovozli agentni sinash

**Agent** yorlig'i frontendga beriladigan `voice-agent-client.js` ni
o'zgartirmasdan ishlatadi: shu yerda ishlagan qo'ng'iroq frontendda ham
ishlaydi. Ovoz brauzer bilan agent o'rtasida to'g'ridan-to'g'ri (WebRTC) oqadi.
Backend faqat signallingni uzatadi, agent kalitini o'zida saqlaydi va
qo'ng'iroqni **heartbeat** bo'yicha hisoblaydi — audio bizdan o'tmaydi, shuning
uchun davomiylikning asosiy dalili shu. Klient jim bo'lib qolsa, backend uning
so'ziga ishonmaydi, agentdan so'raydi: "bu qo'ng'iroq hali sendami?" (bo'sh
nomzodlar ro'yxati bilan `PATCH /api/offer`). Agent ushlab turgan qo'ng'iroq
yopilmaydi va hisob davom etadi; tashlagani — oxirgi tirik dalilgacha plus
bitta oraliq bilan yopiladi. Hech heartbeat yubormagan qo'ng'iroqni esa agent
ushlab tursa ham darhol hisoblamaydi — "ushlab turibdi" ICE hali muvaffaqiyatsiz
tugamaganini ham anglatishi mumkin. Unga bitta nudge (hech qayerga yetmaydigan
ICE nomzodi) yuboriladi va qo'ng'iroq faqat nudge'dan 90 s va javobdan 120 s
keyin ham agentda bo'lsa, javobdan boshlab hisoblanadi.

Modul `import()` bilan yuklanadi, ya'ni sahifa `serve.py` orqali ochilishi
kerak: `file://` dan ochilganda faqat shu yorliq ishlamaydi. Modul `fetch` ni
o'zi chaqiradi va `api()` dan o'tmaydi, shuning uchun sahifa `fetch` ni o'rab,
uning so'rovlarini — offer, candidates, heartbeat, `DELETE` — ham logga
qo'shadi. SDP logda uzunligi bilan almashtirilgan.

### Soxta agent bilan

```bash
.venv/bin/pip install aiortc                    # faqat dev uchun — requirements'ga kirmaydi
.venv/bin/python dev-ui/fake_voice_agent.py     # → :8200 (boshqa port: --port)
VOICE_AGENT_BASE_URL=http://127.0.0.1:8200 VOICE_AGENT_API_KEY=fake-key \
    .venv/bin/uvicorn app.main:app --port 8000
```

**aiortc bilan** soxta agent haqiqiy WebRTC peer: mikrofoningizni qaytarib
yuboradi (o'z ovozingizni eshitasiz) va har bir necha soniyada soxta
`user-transcription` / `bot-output` hodisalarini chiqaradi — **Suhbat**
oynasi shular bilan to'ladi. Yopilgan qo'ng'iroqni, haqiqiy agentdek, `ping`
to'xtagandan 3 soniya ichida tashlaydi. Offerdan keyin 60 soniya ichida
ulanmagan peer'ni ham — aiortc bilan ham, aiortc'siz ham — jonli agentdek o'zi
yopadi: busiz birorta ham nomzod olmagan aiortc peer abadiy "checking"da
turardi va probe'ga "hali ulangan" bo'lib ko'rinardi.

**O'zida yo'q `pc_id` ga `404`.** Soxta agent ham haqiqiy agent kabi o'zi
ushlab turmagan `pc_id` uchun `PATCH /api/offer` ga `404` qaytaradi. Backend
agentning javoblariga ishonishdan oldin aynan shuni tekshiradi — hech qachon
bo'lmagan `pc_id` bilan "canary" so'rov yuboradi va faqat `404` olsa ishonadi.
Shuning uchun soxta agentga qarshi probe'lar ishonchli hisoblanadi va yangi
qoidalar — jim qolgan qo'ng'iroqni ushlab qolish, tugatilgan-lekin-ulangan
qo'ng'iroq liniyani band qilishi — haqiqiy agentdagidek ishlaydi. Probe
birinchi marta kerak bo'lganda (masalan, tugatilgan qo'ng'iroqdan keyingi
boshlashda) API logida bir marta `voice agent liveness probes trusted` chiqadi.

**aiortc'siz** ham ishga tushadi, lekin faqat soxta SDP qaytaradi: brauzer uni
`setRemoteDescription` da rad etadi, klient qo'ng'iroqni darhol `DELETE` qiladi
va hisob `0.000000`. Ulanmagan qo'ng'iroq bepul ekanini ko'rishning eng qisqa
yo'li shu. Bunday qo'ng'iroq hech qachon ulanmaydi, shuning uchun soxta agent
uni offerdan 60 s o'tib unutadi:

- `DELETE` qilingandan keyingi **boshlash** rad etilmaydi: hech qachon
  ulanmagan, tugatilgan qo'ng'iroq ulanganini isbotlamaguncha liniyani band
  qilmaydi, soxta agent esa uni undan ancha oldin tashlaydi. `DELETE` paytida
  backend peer'ga bitta **nudge** (turtki) yuboradi — hech qayerga yetmaydigan
  ICE nomzodi, `192.0.2.1` — va agent qabul qilsa, bazadagi
  `voice_calls.nudged_at` to'ladi.
- `DELETE` qilinmagan qo'ng'iroqni (masalan, curl bilan ochilgan) sweep jim
  deb topadi. 60 s dan oldin topsa — bir marta ushlab qoladi va nudge yuboradi,
  keyingi safar `gone` oladi va hold'ni to'liq qaytaradi (`0.000000`).

Heartbeat'ni 15 soniyalab kutmaslik uchun oraliqni qisqartiring (timeout kamida
ikki oraliq bo'lishi kerak). Sweep'ni ham qisqartiring — aks holda jim
qo'ng'iroq navbatdagi o'tishni 30 soniyagacha kutadi:

```bash
VOICE_AGENT_HEARTBEAT_SECONDS=3 VOICE_AGENT_HEARTBEAT_TIMEOUT_SECONDS=9 VOICE_AGENT_SWEEP_SECONDS=5 ...
```

### Haqiqiy agent bilan

O'sha ikki o'zgaruvchi, operator bergan qiymatlar bilan — `.env` ga, repoga
emas:

```bash
VOICE_AGENT_BASE_URL=https://agent.example.com
VOICE_AGENT_API_KEY=pv_ak_replace-me
```

Jonli agent hozircha vaqtinchalik Cloudflare tunnel ortida: tunnel qayta ishga
tushsa manzil o'zgaradi va har bir qo'ng'iroq `502 voice_agent_unreachable`
oladi — yangi manzilni operatordan olib, API'ni qayta ishga tushiring.

Kalit faqat API protsessida yashaydi va brauzerga hech qachon yetmaydi —
proxy aynan shuning uchun bor. Narx ham kerak: `devtools/seed_price_book.py`
`voice_agent` / `session_ms` qatorini qo'yadi (daqiqasi 0.5, boshlangan daqiqa
to'liq olinadi; 10 daqiqalik shift → hold 5 kredit). Narx bo'lmasa **Sozlama**
kartasi buni aytadi va qo'ng'iroq ochilmaydi.

Agent sizdan boshqa tarmoqda bo'lsa, TURN kerak. Eng yaxshisi — har
foydalanuvchiga muddati o'tadigan parol: `VOICE_AGENT_TURN_URLS` (vergul bilan
ajratilgan `turn:`/`turns:` manzillar) va `VOICE_AGENT_TURN_SECRET` (coturn'da
`use-auth-secret` va aynan shu qiymatli `static-auth-secret`). Shunda
`GET /voice/config` har so'rovda `username` i `<expiry>:<user_id>` bo'lgan
yangi yozuv qo'shadi va **Sozlama** kartasi `TURN bor` deydi. Bu yozuv faqat
agent sozlangan bo'lsa beriladi. `VOICE_AGENT_ICE_SERVERS` dagi statik `turn:`
yozuv ham ishlaydi, lekin `username` va `credential` siz rad etiladi: ishlab
chiqarishda boot'da, developmentda `GET /voice/config` ning
`503 voice_agent_misconfigured` javobi bilan. **Sozlama** kartasi `TURN yo'q`
deb turgan bo'lsa, media ulanmasligi mumkin (quyida 1-holat).

Jonli agentda o'lchangan (2026-09-25), nima kutish kerak:

- offer 6–8 soniyada javob oladi (agent qo'ng'iroq uchun pipeline quradi);
  agent qayta ishga tushgandan keyingi birinchisi 30 s gacha;
- agent **birinchi bo'lib gapiradi** — o'zbekcha salomlashadi, ro'yxatga olish
  idorasi yordamchisi sifatida; hech narsa demasangiz ham eshitasiz;
- har bir javobdan oldin pauza bor: agentning LLM'i
  (`google/gemma-4-31B-it`) birinchi baytgacha ~13 s;
- agentning gapini bo'lsangiz, bo'lingan gap ham **Suhbat**da qoladi: agent
  bunday gapga faqat `new` va keyin `bot-interrupted` yuboradi, hech qachon
  `completed` emas, modul esa uni `interrupted: true` bilan beradi. Oynada
  barge-in tizim qatori va agent boshlagan gap chiqadi;
- 16–40 soniyalik oddiy qo'ng'iroq — `0.500000` (bitta boshlangan daqiqa);
  qulagan tab — oxirgi heartbeat + 15 s, `disputed`;
- probe'lar: tirik `pc_id` ga bo'sh `PATCH` — `200 {"status":"success"}`,
  noma'lumiga — `404 {"detail":"Peer connection not found"}`, shuning uchun
  canary jonli agentga ishonadi; brauzer yopgan qo'ng'iroq ~4 s ichida `404`.

### Mikrofon

Brauzer mikrofonni faqat xavfsiz manzilda beradi. `http://localhost:3000` va
`http://127.0.0.1:3000` xavfsiz hisoblanadi; LAN manzil
(`http://192.168.…:3000`) esa yo'q — u yerda `navigator.mediaDevices` umuman
bo'lmaydi va yorliq buni tugma bosilganda aytadi (modulning o'zi bu holatda
`insecure_origin` kodini qaytaradi). Telefondan sinash uchun HTTPS (masalan,
tunnel) va o'sha origin `CORS_ORIGINS` da kerak.

### Nimani kuzatish kerak

| Qayerda | Nima ko'rinadi | Nega |
| --- | --- | --- |
| Chap panel, `reserved` | qo'ng'iroq ochilishi bilan hold'ga ko'tariladi, tugashi bilan `0` ga qaytadi | hold — shift narxi; ortig'i hisob yopilganda qaytadi |
| So'rovlar logi | `POST /voice/sessions/{id}/heartbeat` har 15 s; o'tmagani har 2 s qayta yuboriladi | audio bizdan o'tmaydi — davomiylikning asosiy dalili shu |
| **Hisob** kartasi | yechilgan = `narx` (`mos`), `reserved` `qaytdi`, sahifa soati bilan `billed_ms` farqi o'nlab ms | hisob agent javob bergan paytdan tugatishgacha, server soatida |
| **Qo'ng'iroqlar** | birinchi heartbeatsiz qo'ng'iroq `ulanmagan`, `0.000000` | ulanmagan qo'ng'iroq bepul — hold to'liq qaytadi |
| **Wallet** → tranzaksiyalar | `hold`, so'ng `release` — ikkalasida qo'ng'iroqning `ai_session_id`si; ulangan bo'lsa `debit` | `debit` sessiyani emas, `usage_event_id` ni ko'rsatadi |
| API logi | `voice agent liveness probes trusted`; jim qolib agent ushlab qolgan qo'ng'iroqda `voice_call_kept` (hech heartbeat yubormaganida `connected=nudged`, keyin `not yet`, isbotlangach `yes`); tugatilgan, lekin agentda hali ulangan qo'ng'iroqda `voice_call_outlived_settlement` | agentning javobi hisobga va liniyaga ta'sir qiladigan joylar |

Qisqa qo'ng'iroq ham bir daqiqa: seed narxlarida 10 soniya — `0.500000`.
Mikrofonni o'chirish hisobni to'xtatmaydi, qo'ng'iroq ochiq qoladi.

### Xato yo'llari — soxta agent kaliti

`VOICE_AGENT_API_KEY` ni almashtirib API'ni qayta ishga tushiring:

| Kalit | Toast / kod |
| --- | --- |
| `reject-me` | `503 voice_agent_key_rejected` — agent **bizning** kalitimizni rad etdi |
| `busy` | `429 voice_agent_busy`, `Retry-After` bilan |
| `warming` | `503 voice_agent_unavailable`, `Retry-After` bilan |
| `bad-offer` | `400 voice_offer_rejected` — agentning o'z so'zlari bilan |

Har birida hold qo'yiladi va o'sha zahoti to'liq qaytadi: `debit` yo'q,
**Qo'ng'iroqlar** da `upstream_error` va `0.000000`. Bu kalitlarda soxta agent
probe'ning `PATCH` iga ham xuddi shunday rad javob beradi, shuning uchun canary
unga ishonmaydi va hisob faqat heartbeat bo'yicha yuradi (logda
`liveness probes not trusted`).

Agentgacha yetmaydigan rad etishlar ham toast bilan chiqadi: `402` — balans
holddan kam (**Sozlama** kartasi buni tugma bosilishidan oldin aytadi);
`voice_call_limit` — shu hisobda ochiq qo'ng'iroq bor: boshqa tabda, yoki hali
yopilmagan qulagan tabniki (`Retry-After` — heartbeat timeout, 45 s);
`voice_call_still_connected` — tugatilgan qo'ng'iroqning ulanishi agentda hali
ochiq, uni ushlab turgan tab yoki ilova yopilishi kerak (`Retry-After: 5`; shu
5 soniya ichidagi qayta urinish agentdan qayta so'ramaydi, oldingi javobni
ishlatadi); `voice_call_rate_limited` — bir daqiqada juda ko'p qo'ng'iroq
(bazada sanaladi, Redis'siz ham ishlaydi) yoki juda ko'p urinish (faqat Redis
bo'lsa: `VOICE_AGENT_MAX_OPENS_PER_MINUTE` ning uch baravari, kamida 30);
`microphone_denied`.

### Uch nosozlik holati

Hisob-kitob uchun muhim uchta holat — har birini shu yorliqda ko'rish mumkin:

1. **Hech qachon ulanmadi.** aiortc'siz soxta agent, yoki TURNsiz turli
   tarmoqlar. Klient media'ni 45 s kutadi (`connection_failed`) yoki javobni
   o'sha zahoti rad etadi, so'ng `DELETE` yuboradi. Birinchi heartbeat
   bo'lmagani uchun narx `0.000000`, hold to'liq qaytadi. Agent ulanmagan
   peer'ni yana bir muddat ushlab turishi mumkin (jonli agent — offerdan ~60 s
   gacha), lekin bu qayta urinishga xalaqit bermaydi: hech qachon ulanmagan
   qo'ng'iroq ulanganini isbotlamaguncha (nudge'dan 90 s va javobdan 120 s
   keyin ham agentda tursa) liniyani band qilmaydi, shuning uchun darhol
   bosilgan **boshlash** ishlaydi. `DELETE` paytida backend peer'ga nudge
   yuboradi — o'z timeout'i bo'lmagan agent ham uni ~64 s da tashlaydi.
2. **Heartbeat o'tmayapti.** Qo'ng'iroq paytida DevTools → Network →
   **Offline**. Media (UDP) uzilmaydi — suhbat davom etadi — lekin
   heartbeat'lar logda `ERR`; klient ularni har 2 soniyada qayta yuboradi.
   - Timeoutdan (standart 45 s) **qisqaroq** offline bo'lib, qaytib Online
     qilsangiz, keyingi heartbeat `continue` oladi va hech narsa yo'qolmaydi,
     hisob `disputed` ham bo'lmaydi.
   - **Uzoqroq** bo'lsa, klient qo'ng'iroqni o'zi yopadi (`heartbeat_lost`):
     media va mikrofon o'chadi. `DELETE` offline'da yetib bormaydi, hisobni
     server o'zi yopadi: sweep agentdan so'raydi, agent yopilgan ulanishni
     allaqachon tashlagan bo'ladi, va qo'ng'iroq oxirgi tirik dalil (oxirgi
     heartbeat yoki agentning oxirgi "hali shu yerda" javobi) plus bitta
     oraliq bilan yopiladi — `heartbeat_timeout` + `disputed`. Online
     qilgach **Qo'ng'iroqlar** → `yangilash` ko'rsatadi.
3. **Tab yopildi.** Qo'ng'iroq paytida sahifani yangilang yoki yoping. Klient
   `pagehide` da `DELETE` ni `keepalive` bilan yuboradi va qo'ng'iroq yopilgan
   soniyagacha hisoblanadi (`client_hangup`) — Chrome shunday qiladi. API
   boshqa portda bo'lgani uchun bu so'rov CORS preflight talab qiladi;
   keepalive so'rovga preflight qilmaydigan brauzerda `DELETE` yetib bormaydi
   va qo'ng'iroq 2-holatdagidek yopiladi. Qaysi biri bo'lganini
   **Qo'ng'iroqlar** → `yangilash` ko'rsatadi. Yangilangan sahifada yangi
   qo'ng'iroq bir-ikki soniya kechroq ochilishi mumkin: server avval eski
   ulanishni agent tashlaganiga ishonch hosil qiladi.

Klient jim bo'lib suhbatni davom ettirishi yoki `DELETE` qilib ulanishni ochiq
qoldirishi — referens klient bunday qilmaydi, shuning uchun bu yorliqda
ko'rinmaydi. Heartbeat'siz gaplashishni `voice_call.py --no-heartbeat` bilan
sinash mumkin (quyida); ikkalasining qoidalarini (`kept`, nudge, liniyani band
qilish) `tests/test_voice_agent_liveness.py` tekshiradi.

### Brauzersiz: `voice_call.py`

`stream_file.py` realtime STT uchun qilgan ishni ovozli agent uchun qiladi:
API orqali haqiqiy WebRTC qo'ng'iroq ochadi (aiortc), mikrofon o'rniga 440 Hz
ton yuboradi, serverning oralig'ida heartbeat yuboradi va oxirida hisobni
tekshiradi. Soxta agentga ham, haqiqiy agentga ham ishlaydi — farqi faqat
API'ning `VOICE_AGENT_BASE_URL`ida.

```bash
.venv/bin/pip install aiortc httpx                        # faqat dev uchun
.venv/bin/python dev-ui/voice_call.py --email ali@example.com --password Str0ngPassw0rd
.venv/bin/python dev-ui/voice_call.py --email … --password … --seconds 40 --wav clip.wav --events
.venv/bin/python dev-ui/voice_call.py --email … --password … --no-hangup
.venv/bin/python dev-ui/voice_call.py --email … --password … --no-heartbeat --seconds 240 --no-hangup
```

| Parametr | Standart | Nima qiladi |
| --- | --- | --- |
| `--api` | `http://127.0.0.1:8000/api/v1` | API manzili |
| `--email`, `--password` | majburiy | Hisobidan yechiladigan foydalanuvchi |
| `--seconds` | `20` | Ulangandan keyin qo'ng'iroqda qancha turish; shu vaqt davomida heartbeat yuboriladi |
| `--wav` | 440 Hz ton | Mikrofon o'rniga yuboriladigan audio fayl |
| `--events` | o'chiq | Data channel'dan kelgan har bir hodisani kelishi bilan JSON ko'rinishida chop etadi (900 belgigacha) |
| `--no-heartbeat` | o'chiq | Suiiste'mol sinovi: ulanadi, agentga `ping` yuboradi, lekin `--seconds` davomida birorta heartbeat yubormaydi — server qo'ng'iroq haqida agentdan bilishi kerak. `--no-hangup` bilan ishlating |
| `--no-hangup` | o'chiq | Qulagan tabni taqlid qiladi: peer connection'ni yopadi, `DELETE` yubormaydi va server qo'ng'iroqni o'zi yopguncha kutadi — heartbeat timeout + 90 s gacha |

Qo'ng'iroq davomida foydalanuvchi va agentning yakuniy gaplari chiqadi,
oxirida esa hisob, agentdan kelgan audio kadrlar soni va data channel
hodisalarining **turlari bo'yicha** soni. Jonli agentda bu ro'yxat to'liq RTVI
to'plami: `bot-output`, `bot-llm-text`, `user-transcription`, `metrics` va
boshqalar (`docs/VOICE_AGENT.md` → *What the data channel carries*); soxta
agentda — faqat `user-*` / `bot-output` / `bot-*-speaking`.

Chiqish kodi `0` faqat shunda: agentdan audio keldi (soxta agent mikrofonni
qaytargani uchun bu ikki tomon ham ishlaganini bildiradi), qo'ng'iroq `ended`
va hech narsa band emas, ikkinchi `DELETE` aynan o'sha hisobni qaytardi
(`--no-hangup` da tekshirilmaydi) va wallet'dagi `reserved` nolga qaytdi.
Aks holda `1` va sababi `XATO:` bilan chiqadi.

`--no-hangup` da kutiladigan natija — soxta agentda ham, jonli agentda ham
(o'lchangan): `heartbeat_timeout`, `disputed=True`, oxirgi heartbeat plus
bitta oraliq. Agent yopilgan peer'ni bir necha soniyada tashlaydi, sweep'ning
probe'i esa `gone` oladi. Tezroq ko'rish uchun API'ni yuqoridagi qisqa
heartbeat/sweep sozlamalari bilan ishga tushiring.

`--no-heartbeat` natijasi qo'ng'iroq qancha davom etganiga bog'liq — sinovning
maqsadi ham shu. Sweep qo'ng'iroqni javobdan 45–75 s keyin jim deb topadi,
ushlab qoladi va nudge yuboradi; javobdan boshlab ulangan deb faqat ikkala
grace o'tgach hisoblaydi — nudge'dan 90 s va javobdan 120 s keyin ham agentda
bo'lsa, standart oraliqlarda 135–225 s da. `--seconds 240` bilan hisob
javobdan agentning oxirgi tasdig'i plus bitta oraliqqacha, `disputed`; API
logida avval `voice_call_kept … connected=nudged`, keyinroq `connected=yes`.
Qisqaroq bo'lsa, qo'ng'iroq bepul tugaydi — ulanganini isbotlamagan qo'ng'iroq
uchun to'g'ri javob. Grace'lar kodda qat'iy: heartbeat/sweep oraliqlarini
qisqartirish birinchi `kept` ni tezlashtiradi, hisob boshlanishini emas.

aiortc barcha nomzodlarni offerning o'ziga yozadi, shuning uchun bu skript
`/candidates` ni sinamaydi — uni brauzer (**Agent** yorlig'i) sinaydi.

## Tokenlar haqida

`localStorage`da (`synora-tts-devui` kaliti). Bu ishlab chiqish uchun ataylab
qilingan: token ko'rinib turadi, `tozalash` hammasini o'chiradi. Ishlab
chiqarishdagi frontend uchun namuna emas.
