"""
================================================================================
DATOS DE INVERSIONES
================================================================================
Carga posiciones de renta fija y renta variable desde archivos (CSV) guardados
en un Blob Storage de Azure separado del que usa el dashboard de gastos (o
localmente vía INVESTMENTS_LOCAL_PATH para pruebas), y trae precios en vivo +
tasa USD/COP desde Twelve Data.

Esquemas internos (agnósticos de la fuente, para que cambiar de dónde vienen
los datos no obligue a tocar el resto del código):
  - fixed_df:    id_producto, tipo_producto, entidad, capital_invertido, tea,
                 moneda, fecha_apertura, fecha_vencimiento
  - var_pos_df:  id_posicion, ticker, nombre, cantidad, precio_compra,
                 precio_actual_csv, moneda, fecha_compra
  - price_df:    id_posicion, ciclo, precio

No depende de Streamlit (funciones puras de pandas + requests), para poder
testearlo sin levantar la app.
================================================================================
"""

import logging

import pandas as pd
import requests

logger = logging.getLogger(__name__)

TWELVE_DATA_BASE_URL = "https://api.twelvedata.com"
TWELVE_DATA_TIMEOUT = 10

# Fallback sin API key para la tasa USD/COP cuando Twelve Data falla o se
# queda sin créditos del minuto (ver fetch_usd_cop_rate_fallback).
FALLBACK_FX_URL = "https://open.er-api.com/v6/latest/USD"
FALLBACK_FX_TIMEOUT = 10


def _log_twelve_data_error(endpoint: str, resp: requests.Response | None, data: dict | None, exc: Exception | None = None):
    if exc is not None:
        logger.warning("Twelve Data [%s]: excepción al llamar/parsear la respuesta: %r", endpoint, exc)
        return
    code = data.get("code") if isinstance(data, dict) else None
    msg = data.get("message") if isinstance(data, dict) else None
    if code == 429:
        logger.warning("Twelve Data [%s]: límite de créditos por minuto excedido (status=%s): %s",
                        endpoint, resp.status_code if resp is not None else "?", msg)
    else:
        logger.warning("Twelve Data [%s]: respuesta inesperada (status=%s, code=%s): %s",
                        endpoint, resp.status_code if resp is not None else "?", code, data)


# ------------------------------------------------------------------------------
# CARGA DESDE CSV (renta fija / renta variable)
# ------------------------------------------------------------------------------
def load_fixed_income(df_raw: pd.DataFrame) -> pd.DataFrame:
    """Normaliza el CSV de renta fija (Portafolio_Fijo.csv) al esquema interno.

    Columnas esperadas del CSV: Producto, Tipo, Entidad,
    Capital Invertido (COP), TEA (%), Moneda, Fecha Apertura,
    Fecha Vencimiento (vacío si no aplica).
    """
    return pd.DataFrame({
        "id_producto": df_raw["Producto"].astype(str),
        "tipo_producto": df_raw["Tipo"].astype(str),
        "entidad": df_raw["Entidad"].astype(str),
        "capital_invertido": pd.to_numeric(df_raw["Capital Invertido (COP)"]),
        "tea": pd.to_numeric(df_raw["TEA (%)"]) / 100.0,
        "moneda": df_raw["Moneda"].astype(str),
        "fecha_apertura": pd.to_datetime(df_raw["Fecha Apertura"]),
        "fecha_vencimiento": pd.to_datetime(df_raw["Fecha Vencimiento"], errors="coerce"),
    })


def load_variable_positions(df_raw: pd.DataFrame) -> pd.DataFrame:
    """Normaliza el CSV real de renta variable (Portafolio_Activo.csv) al
    esquema interno. Solo toma las columnas necesarias para el seguimiento
    de capital; el resto (tesis, stop-loss, notas, etc.) no se usa por ahora.
    """
    return pd.DataFrame({
        "id_posicion": df_raw["Ticker"].astype(str),
        "ticker": df_raw["Ticker"].astype(str),
        "nombre": df_raw["Nombre"].astype(str),
        "cantidad": pd.to_numeric(df_raw["Cantidad"]),
        "precio_compra": pd.to_numeric(df_raw["Precio Entrada (USD)"]),
        "precio_actual_csv": pd.to_numeric(df_raw["Precio Actual (USD)"]),
        "moneda": "USD",
        "fecha_compra": pd.to_datetime(df_raw["Fecha Entrada"]),
    })


# ------------------------------------------------------------------------------
# TWELVE DATA (precios en vivo + tasa USD/COP)
# ------------------------------------------------------------------------------
def fetch_live_prices(tickers: list[str], api_key: str | None) -> dict[str, float]:
    """Precio actual por ticker. Devuelve {} si no hay API key o falla la
    llamada (el resto del código debe hacer fallback al precio del CSV)."""
    tickers = list(dict.fromkeys(tickers))
    if not api_key:
        logger.warning("Twelve Data [price]: TWELVE_DATA_API_KEY no configurada, se omite la llamada")
        return {}
    if not tickers:
        return {}
    resp = None
    try:
        resp = requests.get(f"{TWELVE_DATA_BASE_URL}/price",
                             params={"symbol": ",".join(tickers), "apikey": api_key},
                             timeout=TWELVE_DATA_TIMEOUT)
        data = resp.json()
    except Exception as e:
        _log_twelve_data_error("price", resp, None, exc=e)
        return {}

    if len(tickers) == 1:
        if "price" in data:
            logger.info("Twelve Data [price]: %s = %s", tickers[0], data["price"])
            return {tickers[0]: float(data["price"])}
        _log_twelve_data_error("price", resp, data)
        return {}

    out = {}
    for t in tickers:
        v = data.get(t)
        if isinstance(v, dict) and "price" in v:
            out[t] = float(v["price"])
        else:
            logger.warning("Twelve Data [price]: sin precio para %s en la respuesta batch: %s", t, v)
    if out:
        logger.info("Twelve Data [price]: precios en vivo obtenidos para %s", list(out.keys()))
    if len(out) < len(tickers) and not out:
        _log_twelve_data_error("price", resp, data)
    return out


def fetch_usd_cop_rate(api_key: str | None) -> float | None:
    """Tasa de cambio USD/COP actual, o None si no hay key o falla la llamada."""
    if not api_key:
        logger.warning("Twelve Data [exchange_rate]: TWELVE_DATA_API_KEY no configurada, se omite la llamada")
        return None
    resp = None
    try:
        resp = requests.get(f"{TWELVE_DATA_BASE_URL}/exchange_rate",
                             params={"symbol": "USD/COP", "apikey": api_key},
                             timeout=TWELVE_DATA_TIMEOUT)
        data = resp.json()
    except Exception as e:
        _log_twelve_data_error("exchange_rate", resp, None, exc=e)
        return None

    if "rate" in data:
        logger.info("Twelve Data [exchange_rate]: USD/COP = %s", data["rate"])
        return float(data["rate"])
    _log_twelve_data_error("exchange_rate", resp, data)
    return None


def fetch_usd_cop_rate_fallback() -> float | None:
    """Fallback sin API key para USD/COP vía open.er-api.com (ExchangeRate-API,
    tier gratuito, sin registro). Se usa cuando Twelve Data falla (créditos
    agotados, sin key, error de red). None si también falla este fallback."""
    resp = None
    try:
        resp = requests.get(FALLBACK_FX_URL, timeout=FALLBACK_FX_TIMEOUT)
        data = resp.json()
    except Exception as e:
        logger.warning("Fallback FX [open.er-api.com]: excepción al llamar/parsear la respuesta: %r", e)
        return None

    rate = data.get("rates", {}).get("COP") if isinstance(data, dict) else None
    if data.get("result") == "success" and rate is not None:
        logger.info("Fallback FX [open.er-api.com]: USD/COP = %s", rate)
        return float(rate)
    logger.warning("Fallback FX [open.er-api.com]: respuesta inesperada (status=%s): %s",
                    resp.status_code if resp is not None else "?", data)
    return None


def fetch_historical_monthly_series(symbols: list[str], api_key: str | None,
                                     outputsize: int = 36) -> pd.DataFrame:
    """Cierres mensuales históricos por símbolo (tickers o pares de divisa
    como 'USD/COP'), vía Twelve Data time_series. Pide TODOS los símbolos en
    una sola llamada batched (Twelve Data soporta 'symbol=A,B,C') en vez de
    una llamada por símbolo, para no comerse el límite de créditos/minuto del
    plan gratuito. Devuelve columnas: simbolo, fecha, precio. DataFrame vacío
    si no hay key o falla la llamada."""
    empty = pd.DataFrame([], columns=["simbolo", "fecha", "precio"])
    symbols = list(dict.fromkeys(symbols))
    if not api_key:
        logger.warning("Twelve Data [time_series]: TWELVE_DATA_API_KEY no configurada, se omite la llamada")
        return empty
    if not symbols:
        return empty

    resp = None
    try:
        resp = requests.get(f"{TWELVE_DATA_BASE_URL}/time_series",
                             params={"symbol": ",".join(symbols), "interval": "1month",
                                     "outputsize": outputsize, "apikey": api_key},
                             timeout=TWELVE_DATA_TIMEOUT)
        data = resp.json()
    except Exception as e:
        _log_twelve_data_error("time_series", resp, None, exc=e)
        return empty

    # respuesta batch: {"AAPL": {"values": [...]}, "MSFT": {...}}; con un
    # solo símbolo, Twelve Data devuelve el objeto sin el wrapper por símbolo.
    if len(symbols) == 1:
        data = {symbols[0]: data}

    rows = []
    for symbol in symbols:
        entry = data.get(symbol)
        if not isinstance(entry, dict) or "values" not in entry:
            _log_twelve_data_error("time_series", resp, entry if isinstance(entry, dict) else data)
            continue
        for bar in entry.get("values", []):
            try:
                rows.append({"simbolo": symbol, "fecha": pd.Timestamp(bar["datetime"]),
                             "precio": float(bar["close"])})
            except (KeyError, ValueError) as e:
                logger.warning("Twelve Data [time_series]: barra inválida para %s: %r (%s)", symbol, bar, e)
    if rows:
        logger.info("Twelve Data [time_series]: %d barras mensuales obtenidas para %s",
                    len(rows), symbols)
    return pd.DataFrame(rows, columns=["simbolo", "fecha", "precio"])


# ------------------------------------------------------------------------------
# SERIE DE PRECIOS POR CICLO (histórico + en vivo)
# ------------------------------------------------------------------------------
def _asof_price(historial: pd.DataFrame, ciclo_ts: pd.Timestamp) -> float | None:
    """Cierre mensual disponible más cercano (sin pasarse) al final del ciclo."""
    if historial.empty:
        return None
    candidatos = historial[historial["fecha"] <= ciclo_ts + pd.DateOffset(months=1)]
    if candidatos.empty:
        return None
    return float(candidatos.iloc[-1]["precio"])


def build_variable_price_series(variable_pos_df: pd.DataFrame, ciclos: list[str],
                                 historical_df: pd.DataFrame,
                                 live_prices: dict[str, float]) -> pd.DataFrame:
    """Arma la serie de precio por ciclo para cada posición: usa el cierre
    mensual histórico más cercano de Twelve Data cuando está disponible, si no
    cae al precio del CSV; el último ciclo se sobreescribe con el precio en
    vivo cuando hay uno."""
    ciclos_sorted = sorted(ciclos)
    rows = []
    for _, pos in variable_pos_df.iterrows():
        historial = historical_df[historical_df["simbolo"] == pos["ticker"]].sort_values("fecha")
        activos = [c for c in ciclos_sorted if pd.Timestamp(c) >= pos["fecha_compra"]]
        for ciclo in activos:
            precio = _asof_price(historial, pd.Timestamp(ciclo))
            if precio is None:
                precio = float(pos.get("precio_actual_csv", pos["precio_compra"]))
            rows.append({"id_posicion": pos["id_posicion"], "ciclo": ciclo, "precio": precio})
        if activos and pos["ticker"] in live_prices:
            rows[-1]["precio"] = live_prices[pos["ticker"]]

    return pd.DataFrame(rows, columns=["id_posicion", "ciclo", "precio"])


def build_fx_series(ciclos: list[str], live_rate: float | None) -> dict[str, float]:
    """Tasa USD/COP aplicada a todos los ciclos. Simplificación: no se arma
    un histórico real de FX (una llamada de time_series aparte) para no
    sumar cuota de Twelve Data en cada carga — se usa la tasa en vivo actual
    también para convertir ciclos pasados. Devuelve {} si no hay tasa."""
    if not live_rate:
        return {}
    return {ciclo: live_rate for ciclo in ciclos}


# ------------------------------------------------------------------------------
# CÓMPUTO POR CICLO
# ------------------------------------------------------------------------------
def _cycle_bounds(ciclo: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    inicio = pd.Timestamp(ciclo)
    fin = inicio + pd.DateOffset(months=1)
    return inicio, fin


def compute_fixed_income_cycle_returns(fixed_df: pd.DataFrame, ciclos: list[str]) -> pd.DataFrame:
    ciclos_sorted = sorted(ciclos)
    out = pd.DataFrame({"ciclo": ciclos_sorted})
    out["interes_renta_fija"] = 0.0
    out["valor_fin_ciclo_fija"] = 0.0

    for _, prod in fixed_df.iterrows():
        r_m = (1 + prod["tea"]) ** (1 / 12) - 1
        saldo = prod["capital_invertido"]
        for i, ciclo in enumerate(ciclos_sorted):
            inicio, fin = _cycle_bounds(ciclo)

            if prod["fecha_apertura"] >= fin:
                continue
            if pd.notna(prod["fecha_vencimiento"]) and prod["fecha_vencimiento"] <= inicio:
                continue

            activo_desde = max(inicio, prod["fecha_apertura"])
            activo_hasta = fin if pd.isna(prod["fecha_vencimiento"]) else min(fin, prod["fecha_vencimiento"])
            if activo_hasta <= activo_desde:
                continue

            dias_activos = (activo_hasta - activo_desde).days
            dias_ciclo = (fin - inicio).days
            proporcion = dias_activos / dias_ciclo if dias_ciclo else 0.0

            interes = saldo * r_m * proporcion
            out.loc[out["ciclo"] == ciclo, "interes_renta_fija"] += interes
            saldo += saldo * r_m * proporcion
            out.loc[out["ciclo"] == ciclo, "valor_fin_ciclo_fija"] += saldo

    return out


def compute_variable_cycle_returns(variable_pos_df: pd.DataFrame, price_df: pd.DataFrame, ciclos: list[str]) -> pd.DataFrame:
    ciclos_sorted = sorted(ciclos)
    out = pd.DataFrame({"ciclo": ciclos_sorted})
    out["valorizacion_renta_variable"] = 0.0
    out["valor_fin_ciclo_variable"] = 0.0

    for _, pos in variable_pos_df.iterrows():
        precios_pos = price_df[price_df["id_posicion"] == pos["id_posicion"]].set_index("ciclo")["precio"]
        valor_anterior = None
        for ciclo in ciclos_sorted:
            if ciclo not in precios_pos.index:
                continue
            valor_fin = pos["cantidad"] * precios_pos.loc[ciclo]
            if valor_anterior is None:
                valorizacion = valor_fin - pos["cantidad"] * pos["precio_compra"]
            else:
                valorizacion = valor_fin - valor_anterior
            out.loc[out["ciclo"] == ciclo, "valorizacion_renta_variable"] += valorizacion
            out.loc[out["ciclo"] == ciclo, "valor_fin_ciclo_variable"] += valor_fin
            valor_anterior = valor_fin

    return out


def build_investment_cycle_summary(fixed_df: pd.DataFrame, variable_pos_df: pd.DataFrame,
                                    price_df: pd.DataFrame, ciclos: list[str],
                                    fx_by_cycle: dict[str, float] | None = None) -> pd.DataFrame:
    """fx_by_cycle (USD->COP) es necesario para poder sumar el recaudo de
    renta variable (USD) con el de renta fija (COP) en las columnas
    combinadas. Si no está disponible, las columnas combinadas excluyen la
    renta variable en vez de sumar montos en monedas distintas por error."""
    fija = compute_fixed_income_cycle_returns(fixed_df, ciclos)
    variable = compute_variable_cycle_returns(variable_pos_df, price_df, ciclos)

    cycle_df = fija.merge(variable, on="ciclo", how="outer").fillna(0.0)
    cycle_df = cycle_df.sort_values("ciclo").reset_index(drop=True)

    fx_by_cycle = fx_by_cycle or {}
    cycle_df["fx_disponible"] = cycle_df["ciclo"].map(lambda c: c in fx_by_cycle)
    cycle_df["valorizacion_renta_variable_cop"] = cycle_df.apply(
        lambda r: r["valorizacion_renta_variable"] * fx_by_cycle[r["ciclo"]] if r["fx_disponible"] else 0.0,
        axis=1)

    cycle_df["retorno_acumulado_fija"] = cycle_df["interes_renta_fija"].cumsum()
    cycle_df["retorno_acumulado_variable"] = cycle_df["valorizacion_renta_variable"].cumsum()
    cycle_df["retorno_acumulado_variable_cop"] = cycle_df["valorizacion_renta_variable_cop"].cumsum()
    cycle_df["retorno_acumulado_total"] = cycle_df["retorno_acumulado_fija"] + cycle_df["retorno_acumulado_variable_cop"]

    cycle_df["mom_fija"] = cycle_df["interes_renta_fija"].diff().fillna(cycle_df["interes_renta_fija"])
    cycle_df["mom_variable"] = cycle_df["valorizacion_renta_variable"].diff().fillna(cycle_df["valorizacion_renta_variable"])
    cycle_df["mom_variable_cop"] = cycle_df["valorizacion_renta_variable_cop"].diff().fillna(cycle_df["valorizacion_renta_variable_cop"])
    cycle_df["mom_total"] = cycle_df["mom_fija"] + cycle_df["mom_variable_cop"]

    return cycle_df


def summarize_investment_kpis(cycle_df: pd.DataFrame) -> dict:
    if cycle_df.empty:
        zero = {"retorno_total": 0.0, "mom_delta": 0.0}
        return {"combinado": zero, "renta_fija": zero, "renta_variable": zero, "fx_disponible": False}

    last = cycle_df.iloc[-1]
    return {
        "combinado": {"retorno_total": last["retorno_acumulado_total"], "mom_delta": last["mom_total"]},
        "renta_fija": {"retorno_total": last["retorno_acumulado_fija"], "mom_delta": last["mom_fija"]},
        "renta_variable": {"retorno_total": last["retorno_acumulado_variable"], "mom_delta": last["mom_variable"]},
        "fx_disponible": bool(last["fx_disponible"]),
    }
