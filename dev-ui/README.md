# Dev UI — TTS va billingni qo'lda sinash uchun

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

## Tokenlar haqida

`localStorage`da (`synora-tts-devui` kaliti). Bu ishlab chiqish uchun ataylab
qilingan: token ko'rinib turadi, `tozalash` hammasini o'chiradi. Ishlab
chiqarishdagi frontend uchun namuna emas.
