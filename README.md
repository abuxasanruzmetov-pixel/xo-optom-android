# XASANBOY OPTOM — onlayn do'kon serveri

Bitta server uchta ishni bajaradi:
1. **Telegram bot**: mijoz ro'yxatdan o'tadi (telefon raqam va joylashuv), buyurtma holati haqida xabar oladi.
2. **Mijoz Mini App** (`webapp/index.html`): katalog, savatcha, buyurtma berish, "Buyurtmalarim".
3. **POS uchun API**: XASANBOY_OPTOM ilovasi katalogni yuklaydi va buyurtmalarni oladi.

Faqat Python standart kutubxonasi ishlatiladi, tashqi paketlar kerak emas.

## Railway sozlamalari
| O'zgaruvchi | Qiymat |
|---|---|
| `BOT_TOKEN` | @BotFather bergan token |
| `ADMIN_KEY` | O'zingiz o'ylab topgan maxfiy kalit (kamida 8 belgi). POS ilovaga ham shu kalit kiritiladi |
| `DATA_DIR` | `/data` (Volume shu manzilga ulanadi) |

**Volume**: `/data` manziliga ulang, aks holda qayta yuklanganda baza o'chib ketadi.
**Domain**: Settings → Networking → Generate Domain.

## Do'kon egasini ulash
Botga `/admin ADMIN_KEY` deb yozing. Shundan keyin yangi buyurtmalar Telegram'ga ham keladi.
