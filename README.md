# 5san — anonim imageboard

## Kurulum
    python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
    pip install -r requirements.txt
    python app.py                                        # http://127.0.0.1:5000

İlk çalıştırmada `data/` klasörü oluşur:
- `5san.db` veritabanı, `uploads/` ve `thumbs/` görseller, `logs/app.log`
- `secret.key` (oturum + IP hash anahtarı; silme, yoksa herkes çıkış yapar ve IP hash'leri/banlar değişir)
- `admin.pass` yönetici şifresi (konsola da yazılır). `/admin/login` adresinden gir.

## Ortam değişkenleri
HOST, PORT, FIVESAN_DATA, FLASK_SECRET_KEY, ADMIN_PASSWORD, TRUST_PROXY=1 (nginx/Cloudflare arkasında), HTTPS=1

## Üretim
    waitress-serve --listen=0.0.0.0:5000 app:app
nginx arkasında `TRUST_PROXY=1` ve `HTTPS=1` ayarla; `client_max_body_size 6m;` ekle.
