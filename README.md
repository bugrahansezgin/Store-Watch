# Store Watch

Rakip App Store listelerini her gün kontrol eder: screenshot'lar (ve sıraları), ikon, isim, fiyat, versiyon, açıklama, rating. Değişiklikleri panelde gösterir, her pazartesi Slack'e özet atar.

- **Çalışma:** GitHub Actions (günlük 08:17 TR saati) → Apple'ın public iTunes Lookup API'si → `data/` klasörüne commit
- **Panel:** GitHub Pages'te statik `index.html` (Activity · Watchlist · Before/After karşılaştırma)
- **Bildirim:** Pazartesi 08:43 TR saati, Slack incoming webhook
- **Maliyet:** 0 — sadece Python stdlib, API key yok

## Kurulum (~5 dk)

1. **Repo oluştur** — GitHub'da yeni repo (ör. `store-watch`), bu klasörün içeriğini push'la.
   > Pages ücretsiz planda sadece **public** repo'da çalışır. Private istiyorsan GitHub Pro gerekir.
2. **Actions'a yazma izni ver** — Settings → Actions → General → Workflow permissions → **Read and write**.
3. **Pages'i aç** — Settings → Pages → Source: *Deploy from a branch* → `main` / `(root)`.
   Panel adresi: `https://<kullanıcı>.github.io/store-watch/`
4. **Slack webhook** — api.slack.com/apps → Create App → Incoming Webhooks → kanal seç → URL'i kopyala.
   Repo → Settings → Secrets and variables → Actions:
   - Secret: `SLACK_WEBHOOK_URL` = webhook URL
   - Variable: `PANEL_URL` = Pages adresi (Slack'teki "View" linkleri için)
5. **İlk çalıştırma** — Actions → **Daily check** → *Run workflow*. Baseline screenshot'lar kaydedilir; ertesi günden itibaren değişiklikler akar.
   Slack'i test etmek için Actions → **Weekly Slack digest** → *Run workflow*.

## App ekleme

- **Kolay yol:** Panel → Watchlist → **+ Add app** → açılan "Add app" workflow'unda linki yapıştır. Ülke linkten okunur (`/us/`, `/tr/`); başka ülke istersen alana yaz. Baseline hemen alınır.
- **Elle:** `apps.json`'a `{ "id": "123456789", "country": "us" }` ekle ve commit'le.

Aynı app'i birden fazla ülkede takip edebilirsin (ayrı satır, farklı `country`).

## Neyi yakalar

| Tip | Nasıl |
|---|---|
| Screenshots | URL'ler asset bazında karşılaştırılır → *replaced / added / removed / reordered*. iPhone ve iPad ayrı. |
| Icon | Asset değişimi |
| Release | Versiyon + release notes |
| Name / Price / Description | Doğrudan karşılaştırma, açıklamada satır diff'i |
| Rating | ±0.05 ve üzeri değişim |

## Bilinen sınırlar

- **Subtitle ve promotional text** Lookup API'de yok, yakalanmaz.
- **Custom Product Pages / In-App Events** yakalanmaz — sadece varsayılan listing.
- Screenshot'lar Apple CDN'inden gösterilir; Apple eski bir asset'i silerse o eski görsel panelde kırılabilir (nadiren olur). Kalıcı arşiv istersen `track.py`'ye indirme adımı eklenebilir, repo boyutu büyür.
- Lookup API web listesinden birkaç saat geride kalabilir; günlük kontrolde fark etmez.

## Lokal test

```bash
python3 scripts/track.py                 # gerçek fetch
DRY_RUN=1 python3 scripts/digest.py      # Slack mesajını yazdır, gönderme
python3 -m http.server                   # http://localhost:8000
```
