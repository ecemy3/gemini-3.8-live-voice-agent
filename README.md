# Gerçek Zamanlı Çok Modlu Sesli Asistan (Demo)

FastAPI + Google ADK (`Runner.run_live` + `LiveRequestQueue`) + Vertex AI Gemini Live.
Framework'süz tek sayfa arayüz; tarayıcı ↔ sunucu arası WebSocket.

| Bileşen | Değer |
|---|---|
| Live modeli | `gemini-3.8-live` (GA, us-central1) |
| Flash modeli | `gemini-3.8-flash` (GA, `us` çoklu bölge) — Aşama 5+ |
| Transkripsiyon modeli | `gemini-3.5-transcribe-live-preview` (Preview, global) — Aşama 6 |
| Model sabitleri | `app/config.py` |
| Doğrulanan sürümler | google-adk 2.10.0, google-genai 2.25.0, Python 3.12 |

## Durum

- [x] **Aşama 1** — Sadece ses (mikrofon → PCM16 → Live → 24 kHz oynatma, transkript)
- [x] **Aşama 2** — Barge-in (kesme → çalan/kuyruktaki ses anında durur)
- [x] **Aşama 3** — Araçlar ve aksiyon kartları (4 senaryo, 7 araç)
- [x] **Aşama 4** — Kamera ve ekran paylaşımı (1 kare/sn JPEG + bağlam sıkıştırma)
- [x] **Aşama 5** — Flash alt-ajanı + Google Search grounding (`analyze_with_flash`)
- [x] **Aşama 6** — Çağrı merkezi Agent Assist modu (transkripsiyon + tur içgörüleri + CRM özeti)

## Kurulum

```bash
# 1) Google Cloud
gcloud auth login
gcloud config set project PROJE_ID
gcloud services enable aiplatform.googleapis.com
gcloud auth application-default login          # ADC
gcloud auth application-default set-quota-project PROJE_ID

# Kullanıcı / servis hesabının en az roles/aiplatform.user rolü olmalı.

# 2) Ortam
cp .env.example .env        # GOOGLE_CLOUD_PROJECT'i doldurun
./run.sh                    # ilk çalıştırmada .venv oluşturur
```

Tarayıcıda <http://localhost:8765> açın. Mikrofon için sayfa `localhost` veya HTTPS
üzerinden açılmalıdır.

> Python 3.10–3.12 önerilir. 3.14'te `venv`/`ensurepip` sorun çıkarabilir;
> gerekirse `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

## Mimari

```
 Tarayıcı                                   Sunucu (FastAPI / uvicorn)              Vertex AI
 ────────                                   ──────────────────────────              ─────────
 getUserMedia (mikrofon)          getUserMedia(video) / getDisplayMedia
   │                                   │  <video> önizleme → canvas, 1 kare/sn
   │                                   │  JPEG q=0.6, uzun kenar ≤ 1024
   │                                   │  {"type":"image"} ─────────────┐
   │                                                                   │
 AudioContext 16 kHz
   │
 AudioWorklet  Float32 → Int16, 100 ms
   │  {"type":"audio","data":b64}           /ws/live  (bağlantı başına)
   └──────────── WebSocket ───────────────▶ upstream görevi
                                              │ types.Blob(audio/pcm;rate=16000)
                                              │ types.Blob(image/jpeg)  ◀─────────┘
                                              ▼
                                            LiveRequestQueue ──▶ Runner.run_live ◀══▶ gemini-3.8-live
                                                                 (Agent + InMemoryRunner)   (us-central1)
                                                                        │ araç çağrısı
                                                                        ▼
                                                        tools.py: sahte veri araçları
                                                                  analyze_with_flash ──▶ gemini-3.8-flash
                                                                                         + Google Search
                                                                                         (global, ayrı client)
                                              ┌───────────────────── Event'ler ◀┘
                                            downstream görevi
   ┌──────────── WebSocket ◀──────────────  audio (24 kHz PCM16) · transcript ·
   │                                         turn_complete · telemetry · error
 24 kHz AudioContext
 nextStartTime ile boşluksuz sıralı oynatma
 Transkript paneli · Telemetri logu
```

### WebSocket protokolü

İstemci → sunucu

| Mesaj | Açıklama |
|---|---|
| `{"type":"audio","data":"<b64 PCM16 16kHz mono>"}` | ~100 ms'lik mikrofon parçası |
| `{"type":"image","data":"<b64 JPEG>","source":"camera\|screen"}` | 1 kare/sn, kalite 0,6, uzun kenar ≤ 1024 px |
| `{"type":"text","text":"..."}` | Test için metin girdisi |
| `{"type":"ping","t":<ms>}` | Gidiş-dönüş ölçümü |

Sunucu → istemci

| Mesaj | Açıklama |
|---|---|
| `{"type":"session",...}` | Oturum hazır (model, bölge, ses, senaryo) |
| `{"type":"audio","mime":"audio/pcm;rate=24000","data":"..."}` | Model sesi |
| `{"type":"transcript","role":"user\|model","text":"...","partial":bool}` | `partial=true` → parça ekle, `false` → tam metin, balonu kapat |
| `{"type":"interrupted"}` | Kullanıcı modelin sözünü kesti; istemci tüm sesi durdurur ve kuyruğu temizler |
| `{"type":"tool_call","id","name","args"}` | Model bir araç çağırdı (kart “çalışıyor…” olarak açılır) |
| `{"type":"tool_result","id","name","data","ms","ui"?}` | Araç sonucu ve süresi (kart güncellenir). `ui` yalnızca arayüze giden ek veridir (araştırma kaynak linkleri, Google arama önerileri); modele gönderilmez |
| `{"type":"turn_complete"}` | Model turu bitti |
| `{"type":"assist_pending","turn","text"}` | Agent Assist: müşteri turu analize alındı |
| `{"type":"assist_insight","turn","text","data","errors","ms"}` | `data`: `sentiment_score`, `emotion`, `intent`, `next_best_action`, `risk_flags` |
| `{"type":"telemetry","kinds":[...]}` | Ses dışındaki her ADK event'i |
| `{"type":"error","code":"...","message":"..."}` | Kullanıcıya gösterilen hata |

Sorgu parametreleri: `/ws/live?scenario=musteri_hizmetleri&voice=Aoede`.

## Güvenlik notları

- Gizli bilgi kodda yok; kimlik doğrulama ADC ile. Ayarlar `.env` (bkz. `.env.example`).
- `ALLOWED_ORIGINS` hem HTTP CORS'u hem de WebSocket `Origin` başlığını kısıtlar
  (izinsiz origin → HTTP 403).
- Canlıda: tam demo erişim koduyla (HttpOnly çerez), herkese açık `ecem` senaryosu
  origin + IP/günlük + eşzamanlı oturum + süre sınırlarıyla korunur (`app/security.py`).
  Bunlar maliyeti sınırlar ama gerçek kimlik doğrulama değildir: Origin başlığı tarayıcı
  dışı istemcilerce taklit edilebilir. Daha sıkı koruma için Cloudflare Turnstile /
  reCAPTCHA, kısa ömürlü imzalı token veya IAP eklenmeli.
- **Production için WebSocket kimlik doğrulaması gereklidir.** Bu demo `/ws/live`'ı
  kimlik doğrulamasız açar; herkes sizin Vertex AI kotanızı harcayabilir. Örneğin
  kısa ömürlü imzalı token (sorgu parametresi veya ilk mesaj), oturum çerezi ya da
  IAP / API Gateway arkasına alma kullanın; ayrıca bağlantı başına süre/kota sınırı koyun.

## Test senaryoları

### Aşama 1 — Sadece ses

1. `./run.sh` → <http://localhost:8765>. Telemetride `yapılandırma yüklendi: live=gemini-3.8-live` görünmeli.
2. **Oturumu başlat** → mikrofon izni verin. Durum “Canlı — konuşabilirsiniz”, sohbette
   “Oturum başladı” görünmeli; telemetride `ws gidiş-dönüş … ms`.
3. “Merhaba, bana kısaca kendini tanıtır mısın?” deyin.
   - Mikrofon çubuğu hareket etmeli, “Gönderilen ses” artmalı.
   - Kullanıcı balonu transkriptinizle, ardından asistan balonu kademeli dolmalı.
   - Ses kesintisiz, cızırtısız ve doğru hızda (çipmunk/yavaş değil) çalmalı.
   - Telemetride `gecikme ≈ … ms` ve `event: output_transcription`, `turn_complete` satırları.
4. **Ses** seçimini değiştirip yeni oturum açın; farklı ses duyulmalı.
5. **Metin gönder** kutusuna yazıp gönderin; model sesli yanıt vermeli (mikrofonsuz test).
6. Hata senaryoları:
   - Mikrofon iznini reddedin → kırmızı banner “Mikrofon izni reddedildi…”.
   - Oturum açıkken sunucuyu durdurun (Ctrl+C) → “Sunucu bağlantısı koptu” + durum “Bağlantı koptu”.
   - `gcloud auth application-default revoke` sonrası oturum açın → “Google Cloud kimlik bilgisi bulunamadı…”.
   - `.env`'de `LIVE_LOCATION=europe-west9` gibi desteklenmeyen bölge → model/bölge bulunamadı hatası.
   - Farklı bir origin'den bağlanma denemesi → sunucu logunda `izinsiz origin`, istemcide 403.

### Aşama 2 — Barge-in

> Kulaklık kullanın. Hoparlörde modelin sesi mikrofona geri dönüp modelin kendi
> sözünü kesmesine yol açabilir (tarayıcı yankı engellemesi her cihazda yeterli değil).

1. “Bana İstanbul'un tarihini uzun uzun anlat” deyin.
2. Model konuşurken araya girin: “Dur, başka bir şey soracağım.”
   - Ses **anında** kesilmeli (kuyrukta bekleyen saniyeler çalmamalı).
   - Modelin balonu sağda turuncu çizgi ve “— kesildi” ile işaretlenmeli; aynı metin
     ikinci bir balonda tekrar görünmemeli.
   - Telemetride `interrupted: N kaynak durduruldu, X ms ses atıldı · konuşma başlangıcından Y ms sonra`.
3. Yeni sorunuza model yeni bir balonda cevap vermeli.
4. Kısa bir öksürük/“hı hı” deneyin: 3.8 Live proaktif ses filtrelemesi nedeniyle her
   sese kesilmeyebilir; bu beklenen davranış.

Doğrulanan davranış (sentetik ses ile, 2026-09-29): konuşma başlangıcından ~330 ms
sonra `interrupted` geldi. ADK kesilen turun tam transkriptini `interrupted`'dan
önce **veya** sonra gönderebiliyor; istemci iki sırayı da tek balonda birleştirir.

### Aşama 3 — Araçlar ve aksiyon kartları

Senaryolar (`app/scenarios.py`) ve araçlar (`app/tools.py`):

| Senaryo | Araçlar |
|---|---|
| Genel asistan | – |
| Müşteri hizmetleri (varsayılan) | `get_order_status`, `approve_refund`, `freeze_card` |
| Restoran rezervasyonu | `check_table_availability`, `make_reservation` |
| BT teknik destek | `check_service_status`, `create_support_ticket` |

Sahte veri: siparişler `SP-1001` (kargoda), `SP-1002` (teslim, iade edilebilir),
`SP-1003` (hazırlanıyor), `SP-1004` (teslimden 40 gün geçmiş → iade reddi);
kartlar `4821`, `7390` (aktif), `1156` (zaten dondurulmuş). Veri sunucu belleğindedir;
iade/dondurma gibi değişiklikler sunucu yeniden başlayana kadar kalır.

Araç yanıtları 3.8 Live kuralına uygun olarak her zaman `status`, `retryable`,
`message` içerir; system instruction'da “art arda en fazla 2 araç çağrısı” kuralı var.
Her senaryo `custom_vocabulary` ile kendi terimlerini (ör. `SP-1001`) transkripsiyona bildirir.

1. Müşteri hizmetleri → “SP 1001 numaralı siparişim nerede?”
   → sağda 📦 kart: önce mavi “çalışıyor…”, sonra yeşil “tamam” + kargo bilgisi + süre (ms).
2. “SP 1002'yi iade etmek istiyorum, beden olmadı.” → model **önce onay istemeli**;
   “evet” deyince ↩️ kart yeşil “onaylandı” + iade no.
3. “SP 1004'ü iade etmek istiyorum” → turuncu “reddedildi” (iade süresi dolmuş); model
   garanti seçeneğini önermeli, tekrar tekrar denememeli.
4. “Kartımı kaybettim, son dört hanesi 9999” → turuncu “bulunamadı”; model haneleri
   yeniden sormalı. 4821 ile tekrar → 🧊 yeşil “donduruldu”.
5. Oturum açıkken senaryoyu **Restoran** yapın → oturum kendiliğinden yeniden başlar;
   “Yarın akşam dört kişilik masa var mı?” → saat seçenekleri → isim → onay → 🍽️ kart.
6. **BT teknik destek** → “VPN'e bağlanamıyorum” → bilinen kesinti bildirilmeli, kayıt
   **açılmamalı**. “Outlook sürekli şifre soruyor” → basit öneriler → kayıt açma (🎫).
7. Ses seçimini değiştirin → oturum yeni sesle yeniden başlar.
8. Sol paneldeki örnek cümlelere tıklayarak mikrofonsuz da test edebilirsiniz.

Doğrulanan davranış (2026-09-29, metin girdisiyle): 4 senaryonun hepsinde doğru araç
çağrıldı, geri alınamaz işlemlerden önce onay istendi, araç süreleri 0,5–0,9 sn
(yapay gecikme). 3.8 Live araçları asenkron çağırdığı için model önce “kontrol
ediyorum” deyip sonucu bir sonraki turda söyler.

### Aşama 4 — Kamera ve ekran paylaşımı

**ADK görüntü iletimi (inceleme sonucu):** google-adk 2.10'da
`LiveRequestQueue.send_realtime(Blob(mime_type="image/jpeg"))`, model Gemini 3.x Live
olarak tanınırsa `send_realtime_input(video=...)` ile — yani doğru alanla — gönderilir
(`google/adk/models/gemini_llm_connection.py`, `send_realtime`). Tanınmazsa eski
`media=` alanına düşer. `gemini-3.8-live` tanınıyor; bu yüzden **monkey-patch gerekmedi**.
Model adı değişirse sorun sessiz kalmasın diye sunucu açılışta bunu kontrol edip loga
yazar (`Görüntü kareleri … 'video' alanıyla gönderilecek` ya da bir uyarı).

`RunConfig`'e `ContextWindowCompressionConfig(sliding_window=SlidingWindow())` eklendi:
saniyede bir kare bağlamı hızla doldurur; kayan pencere eski içeriği atar.

1. Oturumu başlatın → **📷 Kamera** → izin verin. Solda önizleme ve “● Kamera”
   rozeti; “Gönderilen kare” saniyede bir artmalı (tipik 15–60 KB/kare).
2. Kameraya bir nesne veya yazı gösterip “Kamerada ne görüyorsun?” deyin.
3. Müşteri hizmetleri senaryosunda kameraya “SP-1003” yazan bir kâğıt gösterip
   “Bu siparişin durumu ne?” deyin → model numarayı okuyup 📦 kartı açmalı.
4. **🖥️ Ekran** → bir sekme/pencere seçin → “Ekranımda ne var, özetler misin?”.
   Kamera açıksa ekrana geçince kamera kapanır (tek kaynak).
5. Tarayıcının “Paylaşımı durdur” çubuğuna basın → önizleme kapanmalı, telemetride
   “kaynak tarafından sonlandırıldı”.
6. Ekran paylaşım penceresinde **İptal** → uyarı çıkmamalı, telemetride “iptal edildi”.
7. Kamera iznini reddedin → kırmızı banner “Kamera izni reddedildi…”.

Doğrulanan davranış (2026-09-29, canvas'tan üretilmiş sahte kamera akışıyla):
model karedeki “SP-1003”, “Kahve makinesi” yazılarını ve kırmızı daireyi doğru
tanıdı; “Görüntüdeki siparişin durumu ne?” sorusunda `get_order_status(SP-1003)`
çağırdı. Kareler ortalama ~18 KB.

### Aşama 5 — Derin analiz alt-ajanı + Google Search grounding

`analyze_with_flash(topic, context)` (`app/tools.py`) `gemini-3.8-flash`'ı Google Search
aracıyla çağırır. Live modeli güncel bilgi veya derin analiz gerektiğinde bu aracı
kullanır (Genel asistan + araştırma ve BT teknik destek senaryolarında açık).

- **Bölgeler:** Live ve Flash kendi `genai.Client`'larını `LIVE_LOCATION` /
  `FLASH_LOCATION` ile, bölgeyi açıkça vererek oluşturur. `GOOGLE_CLOUD_LOCATION`
  kullanılmaz; bu değişken yanlış bir bölgeye ayarlıyken de Live `us-central1`,
  Flash `global` kullanır (doğrulandı).
- **Modele giden / arayüze giden:** Live modeli yalnızca özet cevabı ve kaynak
  başlıklarını alır. Kaynak linkleri ve Google arama önerileri HTML'i (~5 KB)
  function_call_id ile ayrı tutulur ve yalnızca `tool_result.ui` olarak arayüze gider.
- **Google Search koşulu:** Grounding kullanım koşulları gereği arama önerileri
  (`search_entry_point.rendered_content`) kartta, izole (sandbox) bir iframe içinde
  gösterilir.
- 40 sn zaman aşımı; hata/zaman aşımında `retryable: false` ile modele açıklama döner.

1. Senaryo: **Genel asistan + araştırma** → “Bugün İstanbul'da hava nasıl?”
   - Model “araştırıyorum” demeli; sağda 🔎 kart “araştırıyor…” olarak açılmalı.
   - 5–15 sn içinde kart yeşile dönmeli: özet, **Kaynaklar** listesi (tıklanabilir),
     🔍 arama sorguları ve beyaz kutuda Google arama önerileri.
   - Model sonucu kısa cümlelerle sesli özetlemeli (listeyi okumamalı).
2. “Elektrikli araba mı hibrit mi, şehir içi için karşılaştırır mısın?” → uzun cevap;
   kartta “Devamını göster” çıkmalı.
3. Basit sohbet (“Nasılsın?”) → araç **çağrılmamalı**.
4. Araştırma sürerken başka bir şey sorun → 3.8 Live asenkron araç çağrısı sayesinde
   model konuşmaya devam edebilmeli; sonuç gelince aktarmalı.
5. BT teknik destek → “Windows'ta 0x80070005 hatası alıyorum” → araştırma kartı.

Doğrulanan davranış (2026-09-29): hava + döviz sorusu tek araştırmada birleştirildi,
Flash 9,5 sn'de 5 kaynak ve 2 arama sorgusuyla döndü; Live modeli sonucu doğru aktardı.

### Aşama 6 — Çağrı merkezi Agent Assist modu

Senaryo listesinde **Çağrı merkezi · Agent Assist**. Model konuşmaz; müşterinin sesini
yazıya döker, her müşteri turundan sonra arka planda içgörü üretir.

```
mikrofon ─▶ /ws/live?scenario=agent_assist ─▶ ADK run_live ─▶ gemini-3.5-transcribe-live-preview (global)
                                                   │ tur sonu (final input_transcription)
                                                   ▼
                          ┌─ analyze_emotion(text, audio_chunk) ─▶ sentiment_score, emotion   (app/emotion.py)
            paralel,15 sn ┤
                          └─ Flash (JSON şema) ──────────────────▶ intent, next_best_action, risk_flags
                                                   ▼
                                  {"type":"assist_insight"} → sağda 🎧 kart
"Özet oluştur" ─▶ POST /api/assist/summary ─▶ Flash (JSON şema) ─▶ 📝 CRM kartı
```

**Neden ayrı bir transkripsiyon modeli:** Vertex, `gemini-3.8-live` için yalnızca metin
modunu kabul etmiyor (`1007 Unsupported modality`), sesli modda ise model konuşur.
`gemini-3.5-transcribe-live-preview` cevap üretmez, ADK `run_live` ile çalışır ve
“SP bin üç” gibi ifadeleri `SP-1003` olarak biçimlendirir. Kısıtları: **Preview**,
yalnızca `global`, oturum başına **en fazla 10 dk** ses. Sunucu transkripsiyon
oturumunu 9 dk'da bir sessizce yeniler (`ASSIST_SEGMENT_SECONDS`).

**Duygu analizi arayüzü (`app/emotion.py`):** Uygulama yalnızca
`analyze_emotion(text, audio_chunk)` çağırır. Varsayılan: Flash, metin tabanlı. Kendi
modelinizi bağlamak için:

```python
# paketim/modulum.py
from app.emotion import EmotionResult

class BenimAnalizorum:
    name = "benim-modelim"
    async def analyze(self, text: str, audio_chunk: bytes | None) -> EmotionResult:
        # audio_chunk: o müşteri turunun ham sesi, 16 kHz 16-bit mono PCM (≤ 60 sn)
        skor = ...  # kendi modeliniz
        return EmotionResult(sentiment_score=skor, emotion="öfkeli", confidence=0.8)
```

`.env`: `EMOTION_ANALYZER=paketim.modulum:BenimAnalizorum` (modül PYTHONPATH'te olmalı).
Sahte bir analizörle doğrulandı: turun sesi baytlarıyla birlikte analizöre ulaşıyor.

**Flash bölgesi:** Ölçümde `global` uç noktası 5–99 sn dalgalandı, `us` çoklu bölgesi
1,8–2,8 sn'de tutarlı kaldı; bu yüzden `FLASH_LOCATION=us` varsayılan. Düşünme seviyesi
`LOW` (bu modelde `MINIMAL` desteklenmiyor: 400). Her analiz 15 sn zaman aşımına sahip;
aşılırsa kart “Kısmi sonuç: … zaman aşımı” gösterir.

1. Senaryo: **Çağrı merkezi · Agent Assist** → Ses seçimi ve kamera/ekran gizlenir,
   **📝 Özet oluştur** butonu görünür. Oturumu başlatın; durum “Dinleniyor — model konuşmaz”.
2. Müşteri gibi konuşun: “Üç gündür siparişimi bekliyorum, kimse dönmüyor, iptal etmek
   istiyorum.” → sohbette turuncu **Müşteri** balonu; sağda 🎧 kart “analiz ediliyor…”,
   birkaç saniyede: duygu çubuğu (kırmızı, ~15/100 · öfkeli), niyet, **Sonraki en iyi
   aksiyon**, ⚠ risk etiketleri (kayıp riski, şikayet eskalasyonu). Model **ses çıkarmamalı**.
3. “Kartımdan tanımadığım bir çekim yapılmış” → ⚠ Dolandırıcılık şüphesi.
4. “Tamam, teşekkür ederim, çok yardımcı oldunuz.” → yeşil ~95/100; solda
   “Duygu skoru 95/100 ↑”.
5. **Oturumu bitir** → **📝 Özet oluştur** → 📝 CRM kartı: özet, talep, kategori,
   öncelik, duygu seyri, anahtar bilgiler, sonraki adımlar; “JSON'u kopyala”.
6. Mikrofonsuz: sol paneldeki örnek cümlelere tıklayın (metin doğrudan müşteri turu sayılır).
7. 10 dk'dan uzun bir görüşmede telemetride “Transkripsiyon oturumu yenilendi (#2)” görünmeli;
   transkript kesintisiz devam etmeli.

> Üretimde müşteri ve temsilci sesi ayrı kanallardan (telefon santralinin iki bacağı)
> gelmelidir; bu demo tek mikrofonu müşteri kabul eder.

Doğrulanan davranış (2026-09-29): `say` ile üretilmiş Türkçe müşteri sesi doğru yazıya
döküldü (SP-1003 dahil), model hiç çıktı üretmedi; üç turun içgörüleri 3–6 sn'de
geldi (skorlar 15 → 10 → 95), risk işaretleri doğru; CRM özeti ~7–12 sn.

## Canlıya alma (Cloud Run) ve ecem.dev asistanı

**Canlı adres:** <SERVICE_URL>
(tam demo erişim koduyla kilitli; kod `.deploy-secrets` dosyasında, git'e girmez)

```bash
./deploy.sh
```

`deploy.sh` şunları yapar: API'leri açar; iki en az yetkili servis hesabı oluşturur
(`live-asistan-run`: yalnızca `roles/aiplatform.user`; `live-asistan-build`: yalnızca
`roles/run.builder`); `gcloud run deploy --source .` ile Cloud Build'de derleyip yükler.
Ayarlar: `--max-instances 1` (maliyet tavanı + bellek içi sınırlar global kalır),
`--timeout 3600` (WebSocket), `--session-affinity`, `REQUIRE_ORIGIN=true`.

**Domain gerekmez:** Cloud Run ücretsiz HTTPS `*.run.app` adresi verir. Widget GitHub
Pages'teki ecem.dev'e tek satırla eklenir; sunucu Cloud Run'da çalışır (GitHub Pages
yalnızca statik dosya sunar, Python/WebSocket/kimlik bilgisi barındıramaz).

### ecem.dev widget'ı (`app/static/widget/ecem-widget.js`)

```html
<script src="<SERVICE_URL>/static/widget/ecem-widget.js"
        data-bottom="84" defer></script>
```

- Konuşan küre: konuşurken çıkış sesinin şiddetiyle büyür ve mor dalgalar yayar;
  dinlerken turkuaz, mikrofon seviyesine göre; bağlanırken dönen yay.
  `prefers-reduced-motion` açıksa dalgalar kapanır.
- TR/EN seçici (tarayıcı diline göre varsayılan, seçim hatırlanır); dil değişince yeni oturum.
- Senaryo `ecem`: bilgi kaynağı `app/profiles/ecem.md` (sitedeki içerik); bilinmeyen
  soruda uydurmaz, e-posta/LinkedIn'e yönlendirir. `show_on_page` aracıyla konuşurken
  sayfada ilgili bölüme kaydırır veya vaka çalışmasını açar.
- Herkese açık senaryo sınırları: oturum başına 3 dk (20 sn önce uyarı), IP başına
  günde 10 oturum, en fazla 6 eşzamanlı oturum, kamera/ekran kapalı.
- Shadow DOM: sitenin stilleriyle çakışmaz. `data-bottom`/`data-right` ile konum.

**Maliyet:** Cloud Run'ın ücretsiz katmanı bu ölçekte barındırmayı genellikle karşılar;
Gemini kullanımı ise ücretlidir (gemini-3.8-live: ses girişi $3, ses çıkışı $12 / 1M token;
~25 ses token/sn). Bütçe uyarısı kurmanız önerilir.

1. Canlı sitede sağ altta mor küre ve “Bana Ecem'i sor 👋” balonu görünmeli.
2. Küreye dokunun → mikrofon izni → asistan kendini tanıtır.
3. “Tezinden bahseder misin?” → sayfada tez vaka çalışması açılmalı, asistan anlatmalı.
4. EN'e geçin → oturum İngilizce yeniden başlar.
5. “Maaş beklentisi nedir?” → bilmediğini söyleyip iletişime yönlendirmeli.
6. 2 dk 40 sn sonra “20 sn kaldı” uyarısı, 3 dk'da oturum kapanmalı.
