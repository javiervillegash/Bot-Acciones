# Bot de acciones para Telegram

Informe diario de AAPL, AMZN, GOOGL, MSFT, NVDA, TSLA y META:

- Máximo y mínimo de las **últimas 30 velas diarias** (ventana móvil: se recalcula cada día).
- **Punto medio** = (máx + mín) / 2, y bandas a **+3% / -3%** del punto medio.
- Dónde está el precio respecto al punto medio y a las bandas.
- **Tendencia diaria, semanal y mensual**: 🟢 alcista si el precio está por encima de la EMA20 y esta sube,
  🔴 bajista si está por debajo y baja, 🟡 lateral en otro caso.

## 1. Crear el bot en Telegram
1. Abre Telegram, busca **@BotFather** y envía `/newbot`. Copia el **token**.
2. Abre el chat con tu bot nuevo y escríbele cualquier cosa (p. ej. "hola").

## 2. Instalar y configurar (Windows)
1. Instala Python 3.12 desde https://www.python.org/downloads/ (marca **"Add python.exe to PATH"**).
2. En esta carpeta:
   ```
   pip install -r requirements.txt
   copy .env.example .env
   ```
3. Pon tu token en `.env` y ejecuta `python bot.py --chat-id` para ver tu chat_id. Cópialo en `.env`.
4. Prueba: `python bot.py --prueba` (solo pantalla) y luego `python bot.py` (envía a Telegram).

## 3. Envío automático diario
**Opción A – Programador de tareas de Windows** (el PC debe estar encendido), lunes a viernes a las 22:30:
```
schtasks /create /tn "InformeAcciones" /tr "python \"%USERPROFILE%\Desktop\DROPSHIPPING\bot_acciones\bot.py\"" /sc weekly /d MON,TUE,WED,THU,FRI /st 22:30
```

**Opción B – GitHub Actions** (gratis, sin PC encendido): sube esta carpeta a un repo privado de GitHub y
añade los secretos `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID`. El archivo `.github/workflows/informe.yml`
ya lo lanza de lunes a viernes a las 21:30 UTC.

## Personalizar
En `bot.py`: `ACCIONES` (lista de tickers), `VELAS` (30), `BANDA` (0.03), `EMA_PERIODO` (20).
