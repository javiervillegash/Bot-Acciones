"""
Bot EMA Bounce (velas de 1 hora) para Telegram.

Reglas (compras; las ventas son al revés):
  1. Tendencia: los dos últimos máximos y mínimos de swing suben y EMA25 > EMA50.
     Swing = máximo/mínimo con 3 velas más bajas/altas a cada lado (se confirma 3 velas después).
  2. Retroceso: el cierre está por debajo del último máximo de swing.
  3. Rebote: el mínimo de la vela toca la EMA25 y la vela cierra por encima de la EMA50.
  4. Confirmación: pin bar alcista o envolvente alcista.
  5. Entrada a mercado al abrir la vela siguiente.
  6. Stop: mínimo de la vela de señal - 0,1 ATR(14), y como mínimo a 2 ATR de la entrada.
  7. Take profit: 3R.
  8. Riesgo 500 $ por operación, máximo 3 posiciones y una por instrumento.

Uso:
  python ema_bounce.py              -> revisión normal (señales, stops/objetivos, comandos)
  python ema_bounce.py --historico  -> envía las señales de los últimos ~60 días (no toca el estado)
"""
import json
import math
import os
import sys
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

# nombre: (ticker de Yahoo, decimales, $ que se gana/pierde por 1 punto de precio con 1 lote)
# Comprueba en tu plataforma el valor del punto de oro e índices; si no coincide, cámbialo aquí.
INSTRUMENTOS = {
    "EURUSD": ("EURUSD=X", 5, 100_000),
    "GBPUSD": ("GBPUSD=X", 5, 100_000),
    "ORO": ("GC=F", 2, 100),
    "US100": ("NQ=F", 2, 1),
    "US500": ("ES=F", 2, 1),
}
FUTUROS = {"ORO", "US100", "US500"}  # sus precios en Yahoo son del futuro, no del CFD

CUENTA = 100_000
RIESGO = 500                # $ por operación (0,5%)
OBJETIVO_PCT = 0.10         # objetivo del challenge
PERDIDA_MAX_PCT = 0.10      # pérdida máxima total
PERDIDA_DIARIA_PCT = 0.03   # pérdida máxima diaria
MAX_POSICIONES = 3

EMA_RAPIDA, EMA_LENTA, ATR_N, SWING_N, TP_R = 25, 50, 14, 3, 3
MIN_VELAS = 100             # velas mínimas para que las medias estén estables
ZONA_HORARIA = "Europe/Madrid"
ESTADO = Path(__file__).with_name("estado_ema.json")
UNA_HORA = pd.Timedelta(hours=1)


# ---------------------------------------------------------------- utilidades

def cargar_env():
    fichero = Path(__file__).with_name(".env")
    if not fichero.exists():
        return
    for linea in fichero.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            clave, valor = linea.split("=", 1)
            os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


def num(x, dec=2, signo=False):
    """Formato español: 1.234,56"""
    texto = f"{x:+,.{dec}f}" if signo else f"{x:,.{dec}f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def hora_local(ts) -> str:
    return pd.Timestamp(ts).tz_convert(ZONA_HORARIA).strftime("%d/%m %H:%M")


def enviar(texto: str):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(texto)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": texto, "parse_mode": "HTML"},
        timeout=30,
    )
    if not r.ok:
        print(f"Telegram respondió {r.status_code}: {r.text}")


def cargar_estado() -> dict:
    base = {"saldo": CUENTA, "posiciones": [], "cerradas": [], "ultima_vela": {}, "offset": 0}
    if ESTADO.exists():
        base.update(json.loads(ESTADO.read_text(encoding="utf-8")))
    return base


def guardar_estado(estado: dict):
    ESTADO.write_text(json.dumps(estado, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- datos e indicadores

def velas(ticker: str) -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period="60d", interval="1h", auto_adjust=False)
    if df.empty:
        raise ValueError("sin datos")
    df.index = df.index.tz_convert("UTC")
    df = df[["Open", "High", "Low", "Close"]].dropna()
    df["ema_r"] = df["Close"].ewm(span=EMA_RAPIDA, adjust=False).mean()
    df["ema_l"] = df["Close"].ewm(span=EMA_LENTA, adjust=False).mean()
    cierre_prev = df["Close"].shift()
    rango_real = pd.concat(
        [df["High"] - df["Low"], (df["High"] - cierre_prev).abs(), (df["Low"] - cierre_prev).abs()],
        axis=1,
    ).max(axis=1)
    df["atr"] = rango_real.ewm(alpha=1 / ATR_N, adjust=False).mean()
    return df


def solo_cerradas(df: pd.DataFrame, ahora: pd.Timestamp) -> pd.DataFrame:
    return df[df.index + UNA_HORA <= ahora]


def pivotes(df: pd.DataFrame):
    """Índices de máximos y mínimos de swing (SWING_N velas a cada lado)."""
    h, l = df["High"].to_numpy(), df["Low"].to_numpy()
    altos, bajos = [], []
    for i in range(SWING_N, len(df) - SWING_N):
        if h[i] > h[i - SWING_N:i].max() and h[i] > h[i + 1:i + SWING_N + 1].max():
            altos.append(i)
        if l[i] < l[i - SWING_N:i].min() and l[i] < l[i + 1:i + SWING_N + 1].min():
            bajos.append(i)
    return altos, bajos


def senal(df: pd.DataFrame, t: int, altos: list, bajos: list):
    """Devuelve la señal de la vela t (ya cerrada) o None."""
    # solo swings ya confirmados en la vela t
    a = [i for i in altos if i + SWING_N <= t]
    b = [i for i in bajos if i + SWING_N <= t]
    if len(a) < 2 or len(b) < 2 or t < 1:
        return None
    H, L = df["High"].to_numpy(), df["Low"].to_numpy()
    h1, h2, l1, l2 = H[a[-2]], H[a[-1]], L[b[-2]], L[b[-1]]
    v, p = df.iloc[t], df.iloc[t - 1]

    rango = v.High - v.Low
    if rango <= 0:
        return None
    cuerpo = abs(v.Close - v.Open)
    mecha_inf = min(v.Open, v.Close) - v.Low
    mecha_sup = v.High - max(v.Open, v.Close)

    # COMPRA
    if h2 > h1 and l2 > l1 and v.ema_r > v.ema_l and v.Close < h2 \
            and v.Low <= v.ema_r and v.Close > v.ema_l:
        pin = mecha_inf >= 2 * cuerpo and mecha_inf >= 0.5 * rango and v.Close >= v.Low + rango * 2 / 3
        env = v.Close > v.Open and p.Close < p.Open and v.Close >= p.Open and v.Open <= p.Close
        if pin or env:
            previos = [i for i in b if i < a[-1]]
            fibo = (h2 - v.Low) / (h2 - L[previos[-1]]) if previos and h2 > L[previos[-1]] else None
            return {"dir": "COMPRA", "patron": "pin bar alcista" if pin else "envolvente alcista",
                    "extremo": float(v.Low), "fibo": fibo}

    # VENTA
    if h2 < h1 and l2 < l1 and v.ema_r < v.ema_l and v.Close > l2 \
            and v.High >= v.ema_r and v.Close < v.ema_l:
        pin = mecha_sup >= 2 * cuerpo and mecha_sup >= 0.5 * rango and v.Close <= v.High - rango * 2 / 3
        env = v.Close < v.Open and p.Close > p.Open and v.Close <= p.Open and v.Open >= p.Close
        if pin or env:
            previos = [i for i in a if i < b[-1]]
            fibo = (v.High - l2) / (H[previos[-1]] - l2) if previos and H[previos[-1]] > l2 else None
            return {"dir": "VENTA", "patron": "estrella fugaz (pin bar bajista)" if pin else "envolvente bajista",
                    "extremo": float(v.High), "fibo": fibo}
    return None


def niveles(s: dict, atr: float, entrada: float):
    if s["dir"] == "COMPRA":
        sl = min(s["extremo"] - 0.1 * atr, entrada - 2 * atr)
        dist = entrada - sl
        return sl, entrada + TP_R * dist, dist
    sl = max(s["extremo"] + 0.1 * atr, entrada + 2 * atr)
    dist = sl - entrada
    return sl, entrada - TP_R * dist, dist


def resultado(compra: bool, sl: float, tp: float, df: pd.DataFrame):
    """Recorre las velas en orden; si en la misma vela tocan stop y objetivo, cuenta stop."""
    for ts, v in df.iterrows():
        if compra:
            if v.Low <= sl:
                return "stop", sl, ts
            if v.High >= tp:
                return "objetivo", tp, ts
        else:
            if v.High >= sl:
                return "stop", sl, ts
            if v.Low <= tp:
                return "objetivo", tp, ts
    return None


# ---------------------------------------------------------------- challenge

def pnl_hoy(estado: dict) -> float:
    hoy = pd.Timestamp.now(tz=ZONA_HORARIA).date()
    return sum(c["pnl"] for c in estado["cerradas"]
               if pd.Timestamp(c["cerrada"]).tz_convert(ZONA_HORARIA).date() == hoy)


def texto_challenge(estado: dict) -> str:
    saldo = estado["saldo"]
    objetivo = CUENTA * (1 + OBJETIVO_PCT)
    hoy = pnl_hoy(estado)
    lineas = [
        f"💼 Challenge: <b>{num(saldo, 0)} $</b> ({num((saldo / CUENTA - 1) * 100, 2, True)}%)",
        f"🎯 Objetivo {num(objetivo, 0)} $ · faltan {num(max(0, objetivo - saldo), 0)} $",
        f"📅 Hoy: {num(hoy, 0, True)} $ (límite -{num(CUENTA * PERDIDA_DIARIA_PCT, 0)} $)",
    ]
    if saldo >= objetivo:
        lineas.append("🏆 <b>¡Objetivo del challenge alcanzado!</b>")
    if saldo <= CUENTA * (1 - PERDIDA_MAX_PCT) or hoy <= -CUENTA * PERDIDA_DIARIA_PCT:
        lineas.append("⛔ <b>Límite de pérdida superado.</b>")
    return "\n".join(lineas)


def abrir(estado: dict, nombre: str, df: pd.DataFrame, t: int, s: dict, precio: float):
    _, dec, valor = INSTRUMENTOS[nombre]
    if any(p["instrumento"] == nombre for p in estado["posiciones"]):
        print(f"{nombre}: señal ignorada, ya hay una posición abierta")
        return
    if len(estado["posiciones"]) >= MAX_POSICIONES:
        print(f"{nombre}: señal ignorada, máximo de {MAX_POSICIONES} posiciones")
        return

    compra = s["dir"] == "COMPRA"
    sl, tp, dist = niveles(s, float(df["atr"].iloc[t]), precio)
    lotes = max(0.01, math.floor(RIESGO / (dist * valor) * 100) / 100)
    estado["posiciones"].append({
        "instrumento": nombre, "dir": s["dir"], "entrada": precio, "sl": sl, "tp": tp,
        "dist": dist, "lotes": lotes, "abierta": (df.index[t] + UNA_HORA).isoformat(),
    })

    tendencia = "alcista" if compra else "bajista"
    lineas = [
        f"{'🟢' if compra else '🔴'} <b>{s['dir']} {nombre} — 1H</b>",
        f"Motivo: tendencia {tendencia}, retroceso a la EMA25, {s['patron']}",
        "",
        f"Entrada: a mercado ahora (ref. {num(precio, dec)})",
        f"Stop loss: {num(sl, dec)}  (distancia {num(dist, dec)})",
        f"Take profit: {num(tp, dec)}  ({TP_R}R)",
        f"Tamaño: {num(lotes, 2)} lotes → riesgo {RIESGO} $",
    ]
    if nombre in FUTUROS:
        lineas += ["", f"ℹ️ Precios del futuro de Yahoo; en tu plataforma usa las distancias: "
                       f"stop a {num(dist, dec)} y objetivo a {num(dist * TP_R, dec)} de tu entrada."]
    if s["fibo"] is not None:
        ok = 0.382 <= s["fibo"] <= 0.618
        lineas.append(f"Fibonacci: {s['fibo'] * 100:.0f}% del impulso {'✅' if ok else '❌'} (zona 38,2–61,8%)")
    lineas += ["Revisa a ojo la línea de tendencia (3 toques).",
               f"Posiciones abiertas: {len(estado['posiciones'])}/{MAX_POSICIONES}"]
    enviar("\n".join(lineas))


def revisar_posiciones(estado: dict, nombre: str, df: pd.DataFrame):
    _, dec, valor = INSTRUMENTOS[nombre]
    for pos in [p for p in estado["posiciones"] if p["instrumento"] == nombre]:
        compra = pos["dir"] == "COMPRA"
        res = resultado(compra, pos["sl"], pos["tp"], df[df.index >= pd.Timestamp(pos["abierta"])])
        if not res:
            continue
        tipo, salida, ts = res
        signo = 1 if compra else -1
        pnl = pos["lotes"] * (salida - pos["entrada"]) * valor * signo
        r = (salida - pos["entrada"]) * signo / pos["dist"]
        estado["saldo"] += pnl
        estado["posiciones"].remove(pos)
        estado["cerradas"].append({**pos, "salida": salida, "resultado": tipo, "r": r, "pnl": pnl,
                                   "cerrada": pd.Timestamp(ts).isoformat()})
        icono = "✅ OBJETIVO" if tipo == "objetivo" else "❌ STOP"
        enviar(f"{icono} <b>{nombre}</b> ({pos['dir']}) · {num(r, 1, True)}R → {num(pnl, 0, True)} $\n"
               f"Salida: {num(salida, dec)} · {hora_local(ts)}\n\n{texto_challenge(estado)}")


# ---------------------------------------------------------------- comandos de Telegram

AYUDA = (
    "🤖 <b>Bot EMA Bounce (velas de 1 hora)</b>\n\n"
    "Revisa EURUSD, GBPUSD, oro, US100 y US500 cada 30 minutos.\n"
    "• <b>Señal</b>: tendencia clara (swings + EMA25/EMA50), retroceso que toca la EMA25 "
    "y vela de rechazo (pin bar o envolvente).\n"
    "• <b>Stop</b> tras la vela de señal (mín. 2 ATR) · <b>objetivo</b> 3R · riesgo 500 $.\n"
    "• Máximo 3 posiciones y una por instrumento.\n"
    "• Te avisa cuando salta el stop o el objetivo.\n\n"
    "Comandos:\n/estado – posiciones abiertas y progreso del challenge\n/ayuda – este mensaje\n\n"
    "<i>Las respuestas pueden tardar hasta 30 min (el bot se despierta cada media hora).</i>"
)


def texto_estado(estado: dict, precios: dict) -> str:
    lineas = [f"📋 <b>Estado EMA Bounce</b>",
              f"Posiciones abiertas ({len(estado['posiciones'])}/{MAX_POSICIONES}):"]
    if not estado["posiciones"]:
        lineas.append("— ninguna —")
    for p in estado["posiciones"]:
        dec = INSTRUMENTOS[p["instrumento"]][1]
        linea = (f"{'🟢' if p['dir'] == 'COMPRA' else '🔴'} {p['dir']} {p['instrumento']} · "
                 f"entrada {num(p['entrada'], dec)} · SL {num(p['sl'], dec)} · TP {num(p['tp'], dec)}")
        if p["instrumento"] in precios:
            signo = 1 if p["dir"] == "COMPRA" else -1
            r = (precios[p["instrumento"]] - p["entrada"]) * signo / p["dist"]
            linea += f" · ahora {num(r, 1, True)}R"
        lineas.append(linea)

    cerradas = estado["cerradas"]
    if cerradas:
        ganadas = sum(1 for c in cerradas if c["r"] > 0)
        lineas += ["", f"Operaciones cerradas: {len(cerradas)} · aciertos {ganadas / len(cerradas):.0%} · "
                       f"total {num(sum(c['r'] for c in cerradas), 1, True)}R"]
    lineas += ["", texto_challenge(estado)]
    return "\n".join(lineas)


def atender_comandos(estado: dict, precios: dict):
    token, chat_id = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                     params={"offset": estado.get("offset", 0), "timeout": 0}, timeout=30).json()
    for u in r.get("result", []):
        estado["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(chat_id):
            continue  # solo te responde a ti
        texto = (m.get("text") or "").strip().lower()
        if texto.startswith("/estado"):
            enviar(texto_estado(estado, precios))
        elif texto.startswith(("/ayuda", "/start", "/help")):
            enviar(AYUDA)


# ---------------------------------------------------------------- modos

def revision():
    estado = cargar_estado()
    ahora = pd.Timestamp.now(tz="UTC")
    precios = {}
    try:
        for nombre, (ticker, _, _) in INSTRUMENTOS.items():
            try:
                df = velas(ticker)
            except Exception as e:
                print(f"{nombre}: error de datos: {e}")
                continue
            precios[nombre] = float(df["Close"].iloc[-1])
            revisar_posiciones(estado, nombre, df)

            cerradas = solo_cerradas(df, ahora)
            if len(cerradas) < MIN_VELAS:
                continue
            t = len(cerradas) - 1
            clave = cerradas.index[t].isoformat()
            if estado["ultima_vela"].get(nombre) == clave:
                continue  # esta vela ya se revisó
            estado["ultima_vela"][nombre] = clave
            if ahora - (cerradas.index[t] + UNA_HORA) > pd.Timedelta(hours=2):
                continue  # vela antigua (mercado cerrado): no se entra tarde
            altos, bajos = pivotes(cerradas)
            s = senal(cerradas, t, altos, bajos)
            print(f"{nombre}: vela {hora_local(cerradas.index[t])} → {s['dir'] if s else 'sin señal'}")
            if s:
                abrir(estado, nombre, cerradas, t, s, precios[nombre])
        atender_comandos(estado, precios)
    finally:
        guardar_estado(estado)


def historico():
    """Señales de los últimos ~60 días y cómo habrían terminado (sin límite de posiciones)."""
    ahora = pd.Timestamp.now(tz="UTC")
    filas, total_r, ganadas, perdidas, abiertas = [], 0.0, 0, 0, 0
    for nombre, (ticker, _, _) in INSTRUMENTOS.items():
        try:
            df = solo_cerradas(velas(ticker), ahora)
        except Exception as e:
            filas.append((ahora, f"⚠️ {nombre}: error de datos: {e}"))
            continue
        altos, bajos = pivotes(df)
        for t in range(MIN_VELAS, len(df) - 1):
            s = senal(df, t, altos, bajos)
            if not s:
                continue
            entrada = float(df["Open"].iloc[t + 1])
            sl, tp, _ = niveles(s, float(df["atr"].iloc[t]), entrada)
            res = resultado(s["dir"] == "COMPRA", sl, tp, df.iloc[t + 1:])
            if res is None:
                abiertas += 1
                fin = "⏳ abierta"
            elif res[0] == "objetivo":
                ganadas += 1
                total_r += TP_R
                fin = f"✅ +{TP_R}R"
            else:
                perdidas += 1
                total_r -= 1
                fin = "❌ -1R"
            filas.append((df.index[t], f"{hora_local(df.index[t])} {nombre} {s['dir']} · {s['patron']} · {fin}"))
    filas.sort(key=lambda f: f[0])
    cerradas = ganadas + perdidas
    lineas = [
        "🧪 <b>Señales de los últimos ~60 días</b> (sin límite de posiciones)",
        f"Total: {len(filas)} · ✅ {ganadas} · ❌ {perdidas} · ⏳ {abiertas}",
        f"Aciertos: {ganadas / cerradas:.0%} · resultado {num(total_r, 0, True)}R" if cerradas else "",
        "",
        *[texto for _, texto in filas[-25:]],
    ]
    enviar("\n".join(lineas))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cargar_env()
    if "--historico" in sys.argv:
        historico()
    else:
        revision()
