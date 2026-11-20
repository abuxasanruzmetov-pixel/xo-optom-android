# XASANBOY_OPTOM — Android ilova

`app/src/main/assets/index.html` sahifasini ochadigan Android ilova. Ichida `window.AndroidPrinter` ko'prigi bor:
telefonning Bluetooth sozlamalarida ulangan (paired) printerlar ro'yxatini ko'rsatadi va chekni
Bluetooth Classic (SPP) orqali chop etadi. MHT-P8003, Xprinter va boshqa ESC/POS printerlar bilan ishlaydi.

## APK olish (GitHub orqali, kompyutersiz)
1. GitHub'da yangi repo oching (masalan `xo-optom-android`) va shu papkadagi hamma faylni yuklang.
2. **Actions** bo'limida "APK yig'ish" jarayoni o'zi ishga tushadi (taxminan 3–5 daqiqa).
3. Jarayon tugagach, sahifaning pastidagi **Artifacts** bo'limidan `XASANBOY_OPTOM-apk` ni yuklab oling.
4. Zip ichidagi `app-debug.apk` ni telefonga o'rnating ("Noma'lum manbalar"ga ruxsat bering).

## Birinchi ishga tushirish
1. Printerni telefonning **Sozlamalar → Bluetooth** bo'limida ulang (PIN odatda `0000` yoki `1234`).
2. Ilovani oching va "Yaqin atrofdagi qurilmalar" (Bluetooth) ruxsatini bering.
3. 🖨️ → printer ro'yxatidan printerni tanlang → **🧪 Test chek**.

## Sahifani yangilash
`index.html` ni o'zgartirsangiz, uni `app/src/main/assets/index.html` ga almashtiring va GitHub'ga yuklang. Yangi APK o'zi yig'iladi.
Eslatma: ma'lumotlar telefonda (ilova ichida) saqlanadi. Ilovani o'chirishdan oldin Profil → **Eksport (JSON)** qiling.
