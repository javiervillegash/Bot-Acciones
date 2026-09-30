"""
Bot de Telegram: informe diario de acciones.

Para cada acción:
  - Máximo y mínimo de las últimas 30 velas diarias.
  - Punto medio = (máximo + mínimo) / 2.
  - Bandas a +3% y -3% del punto medio.
  - Tendencia diaria, semanal y mensual.

Uso:
  python bot.py            -> calcula y envía el informe a Telegram
  python bot.py --prueba   -> solo lo muestra en pantalla (no envía)
  python bot.py --chat-id  -> muestra tu chat_id (escríbele antes algo al bot)
"""
import html
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

ACCIONES = {
    "AAPL": ("Apple", "🍎"),
    "AMZN": ("Amazon", "📦"),
    "GOOGL": ("Google", "🔎"),
    "MSFT": ("Microsoft", "🪟"),
    "NVDA": ("Nvidia", "🟩"),
    "TSLA": ("Tesla", "🚗"),
    "META": ("Meta", "👥"),
}
VELAS = 30          # velas diarias para el rango
BANDA = 0.03        # 3% por arriba y por abajo del punto medio
EMA_PERIODO = 20    # media usada para la tendencia
PENDIENTE = 3       # periodos para medir si la media sube o baja


def cargar_env():
    """Lee TELEGRAM_TOKEN y TELEGRAM_CHAT_ID del archivo .env (si existe)."""
    fichero = Path(__file__).with_name(".env")
    if not fichero.exists():
        return
    for linea in fichero.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            clave, valor = linea.split("=", 1)
            os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


def tendencia(cierres: pd.Series) -> str:
    """
    Alcista: precio por encima de la EMA20 y la EMA20 subiendo.
    Bajista: precio por debajo de la EMA20 y la EMA20 bajando.
    Lateral: cualquier otro caso.
    """
    if len(cierres) < EMA_PERIODO + PENDIENTE:
        return "⚪ Sin datos"
    ema = cierres.ewm(span=EMA_PERIODO, adjust=False).mean()
    precio = cierres.iloc[-1]
    subiendo = ema.iloc[-1] > ema.iloc[-1 - PENDIENTE]
    if precio > ema.iloc[-1] and subiendo:
        return "🟢 Alcista"
    if precio < ema.iloc[-1] and not subiendo:
        return "🔴 Bajista"
    return "🟡 Lateral"


def agrupar(cierres: pd.Series, periodo: str) -> pd.Series:
    """Convierte cierres diarios en cierres semanales ('W') o mensuales ('M')."""
    return cierres.groupby(cierres.index.to_period(periodo)).last()


def analizar(ticker: str) -> dict:
    df = yf.Ticker(ticker).history(period="5y", interval="1d", auto_adjust=False)
    if df.empty or len(df) < VELAS:
        raise ValueError("sin datos suficientes")
    df.index = df.index.tz_localize(None) if df.index.tz is not None else df.index

    ultimas = df.tail(VELAS)
    maximo = ultimas["High"].max()
    minimo = ultimas["Low"].min()
    medio = (maximo + minimo) / 2
    precio = df["Close"].iloc[-1]
    cierres = df["Close"]

    return {
        "precio": precio,
        "maximo": maximo,
        "minimo": minimo,
        "medio": medio,
        "arriba": medio * (1 + BANDA),
        "abajo": medio * (1 - BANDA),
        "dist": (precio / medio - 1) * 100,
        "fecha": df.index[-1],
        "t_diaria": tendencia(cierres),
        "t_semanal": tendencia(agrupar(cierres, "W")),
        "t_mensual": tendencia(agrupar(cierres, "M")),
    }


def posicion(d: dict) -> str:
    if d["precio"] > d["arriba"]:
        return "por ENCIMA de la banda +3% 🚀"
    if d["precio"] < d["abajo"]:
        return "por DEBAJO de la banda -3% ⚠️"
    lado = "encima" if d["dist"] >= 0 else "debajo"
    return f"dentro de la banda (por {lado} del medio)"


def construir_informe() -> str:
    lineas = [
        f"📊 <b>Informe diario — {datetime.now():%d/%m/%Y}</b>",
        f"<i>Rango de las últimas {VELAS} velas diarias · bandas ±{BANDA:.0%}</i>",
        "",
    ]
    for ticker, (nombre, emoji) in ACCIONES.items():
        try:
            d = analizar(ticker)
        except Exception as e:  # una acción caída no debe tumbar el informe
            lineas += [f"{emoji} <b>{nombre} ({ticker})</b>: ⚠️ error: {html.escape(str(e))}", ""]
            continue
        lineas += [
            f"{emoji} <b>{nombre} ({ticker})</b> — ${d['precio']:.2f}",
            f"Máx: {d['maximo']:.2f} · Mín: {d['minimo']:.2f}",
            f"🎯 Punto medio: <b>{d['medio']:.2f}</b>",
            f"⬆️ +3%: {d['arriba']:.2f} · ⬇️ -3%: {d['abajo']:.2f}",
            f"📍 {d['dist']:+.2f}% vs medio → {posicion(d)}",
            f"📈 D: {d['t_diaria']} · S: {d['t_semanal']} · M: {d['t_mensual']}",
            "",
        ]
    lineas.append("<i>Tendencia: precio vs EMA20 y pendiente de la EMA20 en cada marco.</i>")
    return "\n".join(lineas)


def enviar(texto: str):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("Falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID en el archivo .env")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": texto, "parse_mode": "HTML"},
        timeout=30,
    )
    if not r.ok:
        sys.exit(f"Telegram respondió {r.status_code}: {r.text}")
    print("Informe enviado ✔")


def mostrar_chat_id():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        sys.exit("Pon primero TELEGRAM_TOKEN en el archivo .env")
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
             for u in r.get("result", []) if "message" in u}
    if not chats:
        print("No hay mensajes. Escríbele cualquier cosa a tu bot en Telegram y vuelve a ejecutar.")
    for cid, nombre in chats.items():
        print(f"chat_id: {cid}  ({nombre})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cargar_env()
    if "--chat-id" in sys.argv:
        mostrar_chat_id()
    elif "--prueba" in sys.argv:
        print(construir_informe())
    else:
        enviar(construir_informe())
