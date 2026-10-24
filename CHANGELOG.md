# Değişiklik Günlüğü

Bu dosya kullanıcıları ilgilendiren değişiklikleri listeler. Format [Keep a Changelog](https://keepachangelog.com/)'u temel alır.

## [Yayınlanmamış]

### Eklendi
- Tek satırlık kurulum: macOS ve Windows'ta `install.sh` / `install.ps1` tarafından sürüm otomatik indirilir, kurulur ve güncellenebilir

## [v2026.10.24] - 2026-10-24

### Değişti
- Bot artık **bütün branşlarda** (tenis, fitness, yüzme, vb.) çalışıyor; arayüz ve mesajlarda "salon" ifadesi kullanılıyor (kort/havuz vb. yerine genel terim)
- Hedefler artık **salon bazında** (aynı salonun diğer saatleriyle) eşleştiriliyor
- Yeni bir kurulum yaptığınızda, önceki kurulumdan kalan hedefler artık otomatik olarak kopyalanmıyor — her kurulum kendi hedef listesiyle başlıyor

### Düzeltildi
- Windows masaüstü kısayolunun adı artık doğru şekilde **"Spor İstanbul Rezervasyon Botu.lnk"** olarak oluşuyor
- **Tenis Seviye-2** hedeflerde açılış saati tahmini (66/72 saat kuralı) düzeltildi

### Güvenlik
- Birden fazla hesap kayıtlıyken etkin hesap değiştirildiğinde, eklendiğinde ya da silindiğinde tarayıcı oturumu artık sıfırlanıyor; bot önceki hesabın oturumuyla işlem yapamıyor. Bot çalışırken hesap eklenip silinemiyor
- Tarayıcı profili, sayfa kayıtları ve loglar artık yalnızca bilgisayardaki kendi kullanıcı hesabınızca okunabiliyor; tanı paketi de öyle oluşuyor
- Uygulama penceresi açılırken adres satırına gerçek erişim anahtarı yerine tek kullanımlık bir kod konuyor
- Windows bildirimlerinde siteden gelen metin artık güvenli biçimde gösteriliyor
- Siteden gelen sayfa öğesi kimlikleri tıklamadan önce doğrulanıyor
- Tanı paketinde adınız büyük/küçük harf farkı gözetmeden maskeleniyor
- Komut satırından `bot.py save-login` ve `forget-login` uygulama açıkken artık çalışmıyor (önce uygulamayı kapatın)
- Başarısız bir güncelleme geri alınırken yeni sürümün eklediği dosyalar da kaldırılıyor
