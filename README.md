# Spor İstanbul Rezervasyon Botu

Üyeliğinizin kapsadığı branşlarda (tenis, fitness, yüzme, vb.) boş seansları otomatik olarak rezerve eden masaüstü uygulaması (Windows + macOS).

## Ne Yapar?

**online.spor.istanbul** sayfasında istediğiniz branş, tesis, kort/salon, gün ve saat için bir seans **açılır açılmaz** (seans saatinden tam **72 saat önce** — ör. Cuma 20:00 seansı Salı 20:00'de açılır) ya da **iptalle yer boşalınca** otomatik olarak rezervasyonu yapar. Bilgisayarınız açık ve internet bağlantılı olduğu sürece arka planda çalışır. SMS doğrulama kodunu siz girersiniz (5 dakika içinde).

Bot her hedefin açılış anını **ve** mevcut rezervasyonlarda iptal olup olmadığını aynı anda izler — yani bir seans hem ilk açıldığı anda hem de başkası iptal ettiği için sonradan boşaldığında yakalanabilir. **Hafta sınırı önemli**: 72 saatlik pencere yüzünden bazı açılışlar hafta değişiminde (Pazartesi 00:00 civarı) gerçekleşir — bot bunu da izler, ama "her zaman Salı/Çarşamba açılır" gibi bir varsayımda bulunmayın; gerçek açılış anı `bot.py probe` veya `bot.py watch` ile ölçülebilir (bkz. `analysis/open-rule-2026-10/`).

**Tenis Seviye-2 için 66/72 saat kuralı** (Ekim 2026 duyurusu): TENİS Seviye-2 hedeflerde seans, son 7 günde yeni bir seans seçmişseniz 66 saat, seçmemişseniz 72 saat önce açılabilir — hangisi olacağı önceden kesin bilinemez. Bot bu iki anı da otomatik olarak sessizce izler; siz Pano'da ve hatırlatmalarda sadece tek bir tahmini açılış anı görürsünüz. Diğer tüm hedefler (Seviye-1/3/"Herhangi" dahil, tenis olmayan branşlar da dahil) her zaman sabit 72 saat kuralını kullanır.

## Kurulum — Windows

1. **PowerShell'i açın**: Başlat menüsüne `PowerShell` yazıp **Windows PowerShell**'i açın (Komut İstemi/cmd değil). Windows 10 ya da 11 güncel olmalı.

2. **Şu satırı kopyalayıp PowerShell'e yapıştırın ve Enter'a basın**:
   ```
   irm https://raw.githubusercontent.com/furkankeremselimoglu/spor-istanbul-rezervasyon-botu/main/install.ps1 | iex
   ```
   - En yeni sürümü indirir, doğrular, `%LOCALAPPDATA%\Programs\spor-istanbul-rezervasyon-botu` klasörüne kurar ve kurulumu başlatır; birkaç dakika sürer. Uyarı penceresi çıkmaz, yönetici izni gerekmez
   - Python yoksa kurulum onu kendisi kurmayı dener; olmazsa python.org sayfasını açar (kurarken **"Add python.exe to PATH"** kutusunu işaretleyin). Ekranda istenen adımı yapın (gerekirse Windows oturumunu kapatıp açın), sonra aynı satırı tekrar yapıştırın

3. **İlk kez çalıştır**:
   - Masaüstündeki **"Spor İstanbul Rezervasyon Botu"** simgesine çift tıklayın
   - Uygulama penceresi açılır → "Hesap" sekmesine gidin (aşağıya bakın)

## Kurulum — macOS

1. **Terminal'i açın**: `⌘ + Boşluk` tuşlarına basın, `terminal` yazıp Enter'a basın.

2. **Şu satırı kopyalayıp Terminal'e yapıştırın ve Enter'a basın**:
   ```
   bash -c "$(curl -fsSL https://raw.githubusercontent.com/furkankeremselimoglu/spor-istanbul-rezervasyon-botu/main/install.sh)"
   ```
   - En yeni sürümü indirir, doğrular, ana klasörünüzdeki `spor-istanbul-rezervasyon-botu` klasörüne kurar ve kurulumu başlatır; birkaç dakika sürer. "Apple doğrulayamadı" uyarısı çıkmaz
   - Python 3.12 veya üstü yoksa kurulum python.org indirme sayfasını açar; Python'u kurduktan sonra aynı satırı tekrar yapıştırın
   - **"Terminal masaüstüne erişmek istiyor"** diye bir izin sorarsa **İzin Ver**'e basın (masaüstü kısayolu için)

3. **İlk kez çalıştır**:
   - Masaüstündeki **"Spor İstanbul Rezervasyon Botu"** uygulamasına çift tıklayın
   - Uygulama penceresi açılır → "Hesap" sekmesine gidin (aşağıya bakın)

## Alternatif: ZIP ile Kurulum

Tek satırlık kurulum işlemedi veya eski bir kurulumunuz varsa:

1. [Releases sayfasından](https://github.com/furkankeremselimoglu/spor-istanbul-rezervasyon-botu/releases/latest) **Assets** altındaki `spor-istanbul-rezervasyon-botu-vYYYY.MM.DD.zip` dosyasını indirin (sayfanın üstündeki yeşil **"Code"** düğmesinden **"Download ZIP"** yapmayın, o kopya güncellemeleri doğru tanıyamaz)
2. ZIP dosyasını çıkarın
3. **Windows**: Klasörde `install.bat` dosyasını çift tıklayın. **macOS**: Klasörde `install.command` dosyasına çift tıklayın, **"Apple ... doğrulayamadı"** uyarısı çıkarsa "Bitti"ye basıp Sistem Ayarları › Gizlilik ve Güvenlik › "Yine de Aç"ya basın (veya Terminal'de `bash ` yazıp `install.command`'ı Terminal penceresine sürükleyin)

**Not:** Daha sonra yeni bir sürüm çıktığında ZIP'i tekrar indirmeniz gerekmez — uygulama içinden tek tıkla güncellenir (bkz. aşağıdaki "Güncelleme" bölümü).

## İlk Kullanım — Adım Adım

Bot açıldığında dört sekme görürsünüz: **Pano**, **Hedefler**, **Rezervasyonlarım**, **Hesap**. Sırasıyla ayarlayın:

### 1️⃣ **Hesap** sekmesi

1. **TC Kimlik No** (veya pasaport no) ve **Şifre** alanlarını doldurun
2. **"Kaydet ve giriş yap"** tıklayın — tek tıkla hem kaydedilir hem giriş denenir
3. Şifreniz işletim sisteminizin şifre yöneticisine kaydedilir (dosyaya yazılmaz)
4. **"Bağlantıyı test et"** tıklayın → başarılıysa durum "Giriş yapılı" olur
5. Giriş olmazsa **"Tarayıcıda elle giriş"** butonuyla tarayıcıda elle giriş yapabilirsiniz
6. **"Yardım"** kartındaki **"Tanı paketi oluştur"** butonu: bir sorun yaşarsanız bu butona basın, Masaüstüne redakte edilmiş (kişisel bilgileriniz maskelenmiş) bir `ibbspor-tani-<tarih>.zip` dosyası iner — bunu botun yazarına gönderin. Aynı işlem terminalden `python bot.py diagnostics` ile de yapılabilir. Bkz. aşağıda "Tanı Paketi (Destek)".

Not: Birden fazla hesap ekleyip "Kayıtlı hesaplar" listesinden aralarında geçiş yapabilirsiniz. Farklı branşlar için farklı üyelik kullanmanız gerekiyorsa "Üyelik" kartındaki "Üyelikleri getir" ile hangi üyeliğin kullanılacağını seçebilirsiniz.

### 2️⃣ **Hedefler** sekmesi

Burada arzuladığınız seansları tanımlarsınız. Bir hedef eklemek için:

1. **"Hedef ekle"** tıklayın
2. **Branş** seçin; liste boşsa yanındaki **"Branşları getir"** ile yükleyin (TENİS, FİTNESS, YÜZME, vs.)
3. **Tesis** seçin; yanındaki **"Tesisleri getir"** ile o branşın tesislerini yükleyin
4. **Seviye** seçin (Seviye-1, Seviye-2, vs. veya "Herhangi")

Yarım saatte başlayan seanslar (ör. 06:30) için hedefi kaydetmeden önce "Salon ve saatleri tara" yapın ya da "Belirli saatler" ile saati girin.

5. **"Salon ve saatleri tara"** ile o tesisteki salonları getirin, isterseniz belirli salonları seçin (hiçbiri seçilmezse tüm salonlar)
6. **Günler** seçin (Pazartesi...Pazar, birden fazla seçilebilir)
7. **Saat** tanımlayın:
   - **Saat aralığı**: "Başlangıç" (ör. 18:00) ve "Bitiş" (ör. 21:00)
   - **Belirli saatler**: saat çiplerinden seçin veya "Özel saat ekle" ile ekleyin (ör. 20:00, 21:00)
8. **Kaydet** tıklayın

**Listeler**: Şubeler, tesisler, salonlar, seviyeler **ve saatleri** tamamen katalogdan çekiliyor (bot tarafından önceden tarandı). İlk açıldığında kısıtlı olabilir; bot çalışırken zenginleşir.

### 3️⃣ **Pano** sekmesi

1. İsteğe bağlı: **"Deneme modu"** açın (seansı seçer ama Kaydet'e basmaz, test için)
2. İsteğe bağlı: **"İlk rezervasyonda dur"** anahtarı — açıksa bot bir rezervasyon yaptıktan sonra durur, kapalıysa diğer hedeflere devam eder (varsayılan: açık)
3. **"Başlat"** tıklayın → bot başlar, tarayıcı simge durumuna indirilir (minimized); isterseniz "Tarayıcıyı göster" ile tekrar görünür yapabilirsiniz
4. Durum **"İzleniyor"** / **"Açılış penceresi"** / **"Seans bulundu"** gibi mesajlar verecek
5. **SMS gelince** (Kaydet'e bastıktan sonra): kırmızı panel açılır, SMS kodunu kutuya yazın, **"Doğrula"** tıklayın
6. Durum **"Rezerve edildi"** olunca bot durur veya otomatik duracak (yukarıdaki ayara göre)

**Seans bulamadıysanız**: Etkinlik günlüğünü okuyun, **"Durdur"** tıklayın, ayarları revize edin, tekrar deneyin.

### 4️⃣ **Rezervasyonlarım** sekmesi

- **"Yenile"** tıklayın → mevcut rezervasyonlarınızı listeler
- **"İptal et"** tıklayın → seçilen seansı iptal eder (seanstan 3 saat öncesine kadar)

## Bilmeniz Gerekenler

- **Bilgisayar açık ve prizde kalmalı.** macOS'ta bot çalışırken uykuya geçişi kendisi engeller (`caffeinate`); **Windows'ta bunu siz ayarlamalısınız** — aşağıdaki "Windows'ta Bilgisayarın Uyumaması İçin" bölümüne bakın
- **Aynı hesapla iki cihazda çalıştırmayın** — site kilitler
- **Telefon yanınızda olsun** — SMS kodu almanız gerek
- **"Kaydet"e her çalıştırmada en fazla 1 kez basılır** (varsayılan: `config.toml` → `[safety] max_save_attempts = 1`). SMS yağmurunu önlemek için telefon yanınızda bulunmalı
- **Oturum düşerse** bot otomatik yeniden giriş yapmaya çalışır (kimlik bilgisi kaydedilmişse)
- Oturum kaydedilmemişse elle giriş yapmanız istenir
- **Sürüm numarası** sol kenar çubuğunda (sidebar) görünür — bir sorunu bildirirken bu numarayı da belirtin (ya da terminalden `python bot.py version`)
- **Bir güne zaten rezervasyonunuz varsa** bot o güne ikinci bir seans almaz (branş farklı olsa bile).

## Hatırlatma Bildirimi

Bot, belirttiğiniz seanslar için 15 dakika öncesinden hatırlatma gösterebilir. Bu, uygulamanın kapalı olduğu anda bile çalışır (işletim sisteminin görev zamanlayıcısı tarafından).

### Nasıl Çalışır?

**Pano** sekmesinin **Durum** kartında "Hatırlatma bildirimi" aç/kapat anahtarı bulunur. Aynı sayfanın sağındaki "Hatırlatmalar" kartı salt okunurdur: durumu ve önümüzdeki hatırlatmaları listeler. Özellikler:

- **İçerik**: Yaklaşan her seans için gün/saat, açılış anı ve hedef adı mesaja eklenir
- **Uygulama kapalıyken**: masaüstünde bir pencere açılır — macOS'ta "Uygulamayı aç" / "Kapat" düğmeleriyle, Windows'ta işletim sisteminin kendi "Evet"/"Hayır" düğmeleriyle
- **Uygulama açık ama bot çalışmıyorken**: pencere öne gelir, tek bir "Tamam" düğmesiyle
- **Uygulama Açma**: "Uygulamayı aç" seçeneğini seçerseniz bot otomatik başlar — ancak aramayı başlatmaz, sadece açılır; aramayı başlatmak için Pano'dan "Başlat"a basmanız gerekir
- **Sessiz Çalışma**: Bot zaten çalışıyorsa ve arama devam ediyorsa hiçbir pencere açılmaz

### Kapatma

Hatırlatmaları iki yoldan kapatabilirsiniz:

1. **Uygulamada**: **Pano** › **Hatırlatma bildirimi** anahtarını kapatın
2. **Komut satırından**: Terminalden `python bot.py reminders --off` komutunu çalıştırın

### Manuel Kaldırma

Görev zamanlayıcısının kaydını elle kaldırmak isterseniz (örneğin; "Hatırlatma bildirimi" düğmesi çalışmazsa):

- **Windows**: Görev Zamanlayıcı açıp `IBBSporRezervasyonHatirlatma` görevini silin
- **macOS**: Terminal'de `rm ~/Library/LaunchAgents/com.ibbspor.rezervasyon.hatirlatma.plist` komutunu çalıştırın

## Windows'ta Bilgisayarın Uyumaması İçin

Bot arka planda beklerken (ör. gece boyunca bir seansın açılmasını izlerken) Windows'un
bilgisayarı uyku moduna almaması veya kapağı kapatınca kapatmaması gerekir. **Denetim
Masası › Donanım ve Ses › Güç Seçenekleri** (ya da Ayarlar › Sistem › Güç ve Pil) üzerinden:

1. **"Kapağı kapatınca yapılacak işlemi seçin"** (laptop ise) → **Prize takılıyken**: **"Hiçbir şey yapma"**
2. **Plan ayarlarını değiştir** → **Bilgisayarı uyku durumuna geçir** → **Prize takılıyken**: **"Asla"**
3. Bilgisayarı **şarj kablosuna takılı** tutun (pil modunda farklı ayarlar geçerli olabilir, bot sadece "prize takılıyken" ayarına güvenir)

Bu ayarları yapmazsanız bilgisayar uykuya geçtiğinde bot duraklar ve seansı kaçırabilirsiniz.

## Tanı Paketi (Destek)

Bir sorun yaşadığınızda botun yazarına göndermek üzere **redakte edilmiş** (kişisel
bilgileriniz otomatik olarak maskelenmiş) bir tanı paketi oluşturabilirsiniz:

- Uygulamada: **Hesap** sekmesi › **Yardım** › **"Tanı paketi oluştur"** butonu → Masaüstüne `ibbspor-tani-<tarih-saat>.zip` iner
- Terminalden: `python bot.py diagnostics` (varsayılan hedef: Masaüstü; `--dest KLASÖR` ile değiştirilebilir)

Pakette: güncel loglar, son 40 sayfa anlık görüntüsü (yalnızca HTML, ekran görüntüsü
**değil**), ayarlarınız ve sürüm/sistem bilgisi bulunur. TC kimlik no, telefon numarası,
ad-soyad ve oturum belirteçleri otomatik maskelenir; giriş oturumunuz (çerezler) ve
şifreniz **hiçbir zaman** pakete dahil edilmez. Ayrıntı için `state/README.md`'ye bakın.

## Güncelleme (Alan İçin)

Uygulamaya yeni sürüm gelirse web arayüzünde bir bildirim çubuğu görüntülenir. Otomatik güncelleme şu şekilde işler:

### Otomatik Kontrol

Uygulama başladıktan 5 saniye sonra GitHub'da yeni bir sürüm olup olmadığını kontrol eder, sonra her 6 saatte bir tekrar kontrol eder. Yeni bir sürüm bulunursa:
- **Arayüzde bildirim çubuğu** görüntülenir ("Uygulamaya güncelleme geldi (v…), lütfen güncelleyiniz.")
- **İşletim sistemi bildirimi** gönderilir
- "Şimdi güncelle" düğmesi tıklanırsa:
  - Güncelleme indirilir
  - Uygulama kapanır
  - Güncelleme uygulanır
  - Uygulama yeniden açılır

### Sınırlamalar

"Şimdi güncelle" düğmesi şu durumda devre dışı bırakılır:
- Bot çalışırken (arama sürüyorsa)
- SMS kodu bekliyorken

İndirme sürerken çubukta **"Güncelleme indiriliyor, uygulama yeniden başlayacak…"** yazar; bu sırada "Başlat" kullanılamaz.

### Korunan Dosyalar

Güncelleme sırasında hiçbir zaman dokunulmaz:
- `state/` klasörü tamamen (ayarlar, katalog, seanslar, oturum bilgileri)
- `.venv/` klasörü (Python ortamı)

Eski dosyaların yedeği `state/update/backup/` klasörüne kaydedilir. Güncelleme başarısız olursa otomatik olarak geri yüklenir.

### Günlükler

Güncelleme işlemi ayrıntılarını `state/logs/update.log` dosyasında bulabilirsiniz.

### Elle Güncelleme

Güncelleme bildirimi gelmiyorsa veya elle güncelleme yapmak istiyorsanız:

**Tek satırlık kurumla kurduysanız:** Aynı komutları tekrar çalıştırın (`state/` klasörü (ayarlar, hedefler, seanslar) otomatik korunur):

- **Windows**: `irm https://raw.githubusercontent.com/furkankeremselimoglu/spor-istanbul-rezervasyon-botu/main/install.ps1 | iex`
- **macOS**: `bash -c "$(curl -fsSL https://raw.githubusercontent.com/furkankeremselimoglu/spor-istanbul-rezervasyon-botu/main/install.sh)"`

**ZIP ile kurduysanız:**

1. [GitHub Releases sayfasından](https://github.com/furkankeremselimoglu/spor-istanbul-rezervasyon-botu/releases/latest) **Assets** altındaki en yeni ZIP dosyasını indirin (yeşil "Code" düğmesinden değil)
2. ZIP dosyasını çıkarın, eski proje klasörünün **üzerine** çıkarın
3. **`state/` klasörüne dokunmayın** (ayarlar, katalog, seanslar, oturum dosyaları hepsi burada)
4. `install.bat` (Windows) ya da `install.command` (macOS)'u çalıştırın
5. **"Spor İstanbul Rezervasyon Botu"** uygulamasını başlatın

## Sorun Giderme

**SmartScreen / Gatekeeper uyarısı** (yalnız ZIP ile kurulumda çıkar; tek satırlık kurulumda çıkmaz):
- Windows: "Daha fazla bilgi" › "Yine de çalıştır"
- macOS: uyarıda "Bitti" › Sistem Ayarları › Gizlilik ve Güvenlik › "Yine de Aç" (macOS 15 ve sonrasında "sağ tık › Aç" artık işe yaramıyor)

**"Uygulama açık; önce uygulamayı kapatın" / "Uygulama zaten açık" hatası:**
- Önce gerçekten başka bir pencere/işlem açık mı kontrol edin (Görev Yöneticisi / Activity Monitor'da `pythonw`/`python` arayın) ve varsa kapatın
- Hâlâ açılmıyorsa (çökme sonrası kilit takılı kalmış olabilir): `state/app.lock` dosyasını silin ve tekrar deneyin
- Not: `bot.py`'nin tarayıcı kullanan komutları (`login`, `probe`, `watch`, `run`, `scan`, `reservations`, `cancel`, `catalog`) uygulama açıkken bilerek çalışmayı reddeder (çıkış kodu 4) — aynı anda iki yerden siteye girmemek için

**Giriş yapamıyor:**
- Hesap sekmesinde **"Tarayıcıda elle giriş"** butonunu kullanın

**Loglar ve ekran görüntüleri:**
- `state/logs/bot.log` → detaylı günlük
- `state/artifacts/` → kritik noktaların ekran görüntüleri (HTML + PNG)

---

**Komutun ne yaptığını görmek için:** [install.sh](https://github.com/furkankeremselimoglu/spor-istanbul-rezervasyon-botu/blob/main/install.sh) (macOS) / [install.ps1](https://github.com/furkankeremselimoglu/spor-istanbul-rezervasyon-botu/blob/main/install.ps1) (Windows)

Tüm hakları saklıdır. Kaynak kod incelenebilir; izinsiz yeniden dağıtılamaz.
