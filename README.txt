# 5san — yerel imageboard başlangıç sürümü

## Özellikler
- SQLite veritabanı (tek `5san.db` dosyası)
- E-posta/Gmail gerektirmeyen isteğe bağlı nickname
- Nickname boşsa Anonim görünür
- Aynı tarayıcıdan aynı gönderiye ikinci beğeni engeli (veritabanında UNIQUE anahtar)
- Gönderi oluşturma, kategoriler, arama, sıralama, raporlama
- Basit gönderi aralığı ve metin uzunluğu sınırı

## Kurulum (Windows)
1. Python 3.10 veya daha yenisini kur.
2. Bu klasörde terminal aç.
3. `py -m venv .venv`
4. `.venv\Scripts\activate`
5. `py -m pip install -r requirements.txt`
6. `py app.py`
7. Tarayıcıda `http://127.0.0.1:5000` aç.

## Önemli notlar
- Bu sürüm yerel bilgisayarında çalışır; internette herkese açık değildir.
- SQLite şu an için başlangıçta yeterli. Çok yüksek trafik ve devasa veri için PostgreSQL'e geçiş, yedekleme, moderasyon ve barındırma planı gerekir.
- Beğeni koruması tarayıcı çerezindeki anonim kimliği kullanır. Çerez silinirse veya farklı tarayıcı kullanılırsa kişi yeniden beğenebilir. İnternette kesin kötüye kullanım önleme için hesap/kimlik doğrulaması, hız sınırı ve sunucu tarafı kötüye kullanım tespiti gerekir.
- Bu demo görsel yükleme ve gerçek yanıt kaydetme işlevlerini henüz içermez.
- Gerçek yayına almadan önce CSRF koruması, güvenli moderasyon kuyruğu, yedekleme ve üretim sunucusu ayarları eklenmelidir.
