"""
================================================================================
DASHBOARD DE GASTOS PERSONALES
================================================================================
Lee el esquema estrella (Gold) desde Azure Blob con DuckDB y presenta:
  - BALANCE ingreso vs. gasto (foco principal)
  - Gasto por categoria
  - Tendencia por ciclo
  - Uso por cuenta/tarjeta
  - Necesario vs Extra vs Ahorro
  + filtros, tabla explorable y export CSV.

Ejecutar:
    pip install -r requirements_dashboard.txt
    # Nube (Blob):  definir AZURE_STORAGE_CONNECTION_STRING en secrets/env
    # Local (prueba): definir GOLD_LOCAL_PATH=./sample_gold
    streamlit run dashboard.py
================================================================================
"""

import os
import re
import math
import logging
from datetime import datetime
import duckdb
import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from dotenv import load_dotenv

import investments_data

load_dotenv()

# Logging de diagnóstico (sobre todo para Twelve Data: queda visible en la
# terminal donde corre `streamlit run` o en los logs de Streamlit Cloud).
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ------------------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------------------
st.set_page_config(page_title="Gastos · Balance", page_icon="◐",
                   layout="wide", initial_sidebar_state="expanded")

def _secret(name):
    # st.secrets.get(...) revienta con StreamlitSecretNotFoundError si no hay
    # ningún secrets.toml (hasattr no alcanza a filtrar eso), así que lo
    # envolvemos para que un despliegue sin secrets.toml no tumbe la app.
    try:
        return st.secrets.get(name)
    except Exception:
        return None


CONTAINER = "lakehouse"
CONN_STR = os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or _secret("AZURE_STORAGE_CONNECTION_STRING")
GOLD_LOCAL = os.environ.get("GOLD_LOCAL_PATH")   # si está, lee de disco (modo prueba)

# Inversiones: Blob Storage APARTE del de gastos (misma convención de env vars).
INVESTMENTS_CONTAINER = "datalake"
INVESTMENTS_PATH = "Dashboard_Finanzas_Personales"
INV_CONN_STR = os.environ.get("AZURE_INVESTMENTS_CONNECTION_STRING") or _secret("AZURE_INVESTMENTS_CONNECTION_STRING")
INVESTMENTS_LOCAL = os.environ.get("INVESTMENTS_LOCAL_PATH")   # modo prueba local
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY") or _secret("TWELVE_DATA_API_KEY")

# Valor de respaldo fijo para la tasa USD/COP: último recurso si Twelve Data
# Y el fallback sin key (open.er-api.com) fallan los dos. Configurable para
# no depender de que ninguna API externa esté arriba.
_USD_COP_FALLBACK_RAW = os.environ.get("USD_COP_FALLBACK_RATE") or _secret("USD_COP_FALLBACK_RATE")
try:
    USD_COP_FALLBACK_RATE = float(_USD_COP_FALLBACK_RAW) if _USD_COP_FALLBACK_RAW else None
except (TypeError, ValueError):
    USD_COP_FALLBACK_RATE = None

# Red de seguridad para el error "Problem with the SSL CA cert" en contenedores
# tipo Streamlit Cloud: le decimos al transporte curl de la extensión azure
# dónde está el bundle de certs del sistema (Debian/Ubuntu), sin pisar un valor
# que ya venga seteado en el entorno. Esa ruta no existe fuera de Linux, así
# que en Windows/macOS la dejamos sin forzar (el transporte por defecto del
# SDK de Azure ya sabe usar el almacén de certs del sistema).
if os.name != "nt":
    os.environ.setdefault("CURL_CA_INFO", "/etc/ssl/certs/ca-certificates.crt")

# ------------------------------------------------------------------------------
# IDENTIDAD VISUAL — inspirada en el portfolio (aviles17.github.io/My_react_resume):
# navy profundo, acento verde menta, tipografía Raleway en negrita con subrayado.
# ------------------------------------------------------------------------------
INK      = "#ccd6f6"   # texto principal / líneas fuertes (headers, valores)
PAPER    = "#0a192f"   # fondo de página (navy profundo)
SURFACE  = "#112240"   # panel elevado: tarjetas KPI, área de gráficos
BORDER   = "#1d3a63"   # bordes y líneas sutiles sobre navy
SAGE     = "#66ff87"   # ingreso / acento principal (verde menta de marca)
RUST     = "#ff3333"   # gasto (rojo)
GOLD_AC  = "#ffcb6b"   # ahorro / acento ámbar
MUTED    = "#8892b0"   # texto secundario
CYAN_AC  = "#64ffda"   # acento cian (línea "Total" en gráficos de inversión)
CHIP_BG  = "#1f4d3a"   # fondo de los chips de filtro (oscuro, para texto blanco legible)
CAT_SEQ  = ["#66ff87","#64ffda","#82aaff","#c792ea","#ff3333","#ffcb6b",
            "#f78c6c","#89ddff","#c3e88d","#ff8fa3","#5ccfe6"]

st.markdown(f"""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Raleway:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500&display=swap');

  .stApp {{ background: {PAPER}; }}
  html, body, [class*="css"] {{ font-family: 'Raleway', sans-serif; color: {INK}; }}

  h1,h2,h3 {{ font-family: 'Raleway', sans-serif; color: {INK}; font-weight: 800; letter-spacing: -0.01em; }}
  h2, h3 {{ display:inline-block; border-bottom: 4px solid {SAGE}; padding-bottom:.3rem; margin-bottom: 1.1rem; }}

  /* eyebrow / masthead */
  .masthead {{ border-bottom: 1px solid {BORDER}; padding-bottom: .6rem; margin-bottom: 1.4rem; }}
  .eyebrow {{ font-family:'JetBrains Mono',monospace; font-size:.72rem; letter-spacing:.18em;
              text-transform:uppercase; color:{SAGE}; }}

  /* tarjetas KPI — misma altura sin importar el largo del texto */
  .kpi {{ background:{SURFACE}; border:1px solid {BORDER}; border-radius:6px; padding:1.1rem 1.2rem;
          height:140px; display:flex; flex-direction:column; justify-content:center;
          box-shadow: 0 4px 14px rgba(2,12,29,.35); transition: transform .3s ease, border-color .3s ease; }}
  .kpi:hover {{ transform: translateY(-3px); border-color:{SAGE}; }}
  .kpi .lbl {{ font-family:'JetBrains Mono',monospace; font-size:.7rem; letter-spacing:.12em;
               text-transform:uppercase; color:{MUTED}; margin-bottom:.35rem; }}
  .kpi .val {{ font-family:'Raleway',sans-serif; font-size:2rem; font-weight:800; line-height:1; }}
  .kpi .sub {{ font-size:.78rem; color:{MUTED}; margin-top:.3rem; }}
  .pos {{ color:{SAGE}; }} .neg {{ color:{RUST}; }} .acc {{ color:{GOLD_AC}; }}

  [data-testid="stSidebar"] {{ background:{PAPER}; border-right:1px solid {BORDER}; }}
  .stDataFrame {{ border:1px solid {BORDER}; border-radius:6px; overflow:hidden; }}
  div[data-testid="stMetricValue"] {{ font-family:'Raleway',sans-serif; }}

  .stDownloadButton > button {{ background:transparent; color:{INK}; border:2px solid {BORDER};
      border-radius:4px; font-weight:600; transition: all .3s ease; }}
  .stDownloadButton > button:hover {{ border-color:{SAGE}; color:{SAGE}; }}

  /* chips de multiselect — fondo mas oscuro para que el texto blanco se lea bien */
  span[data-baseweb="tag"] {{ background-color:{CHIP_BG} !important; border:1px solid {SAGE}; }}
  span[data-baseweb="tag"] span {{ color:#ffffff !important; }}
  span[data-baseweb="tag"] svg {{ fill:#ffffff !important; }}
</style>
""", unsafe_allow_html=True)


# ------------------------------------------------------------------------------
# CARGA DE DATOS  (cache 10 min)
# ------------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner="Cargando datos…")
def load_data():
    con = duckdb.connect()
    if GOLD_LOCAL:
        base = GOLD_LOCAL.rstrip("/")
        fact = f"'{base}/fact_gastos.parquet'"
        dcat = f"'{base}/dim_categoria.parquet'"
        dcon = f"'{base}/dim_conto.parquet'"
        dfec = f"'{base}/dim_fecha.parquet'"
    else:
        con.execute("INSTALL azure; LOAD azure;")
        # transporte por defecto del SDK de Azure no siempre encuentra el bundle
        # de CA certs del contenedor (Streamlit Cloud) -> forzamos transporte curl,
        # que sí respeta la ubicación estándar de certs del sistema (o CURL_CA_INFO/CURL_CA_PATH).
        # En Windows/macOS dejamos el transporte por defecto, que ya usa el
        # almacén de certificados del sistema operativo.
        if os.name != "nt":
            con.execute("SET azure_transport_option_type = 'curl';")
        con.execute(f"""
            CREATE OR REPLACE SECRET azsecret (
                TYPE azure,
                CONNECTION_STRING '{CONN_STR}'
            );
        """)
        b = f"azure://{CONTAINER}/gold"
        fact = f"'{b}/fact_fatture/**/*.parquet'"
        dcat = f"'{b}/dim_categoria/*.parquet'"
        dcon = f"'{b}/dim_conto/*.parquet'"
        dfec = f"'{b}/dim_giorno/*.parquet'"

    hp = "hive_partitioning=true" if not GOLD_LOCAL else ""
    df = con.execute(f"""
        SELECT f.giorno, f.ciclo, f.valore, f.es_ingreso, f.commento,
               dcat.categoria, dcat.tipo_i, dcat.tipo,
               dc.conto, dc.tipo_cuenta,
               df.quincena, df.dia_semana
        FROM read_parquet({fact}{(', '+hp) if hp else ''}) f
        LEFT JOIN read_parquet({dcat}) dcat ON f.sk_categoria = dcat.sk_categoria
        LEFT JOIN read_parquet({dcon}) dc   ON f.sk_conto     = dc.sk_conto
        LEFT JOIN read_parquet({dfec}) df   ON f.sk_fecha     = df.sk_fecha
        ORDER BY f.giorno
    """).df()
    return df


# Capa de estandarización TEMPORAL: en Gold algunas categorías vienen con
# emoji al inicio del nombre (ej. "🍔 Mercado") y otras sin él (ej. "Mercado"),
# lo que las separa en grupos distintos en los gráficos/tablas. Esto debería
# resolverse en el pipeline de origen (dim_categoria); mientras tanto, se
# limpia acá para que la agrupación sea consistente.
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # pictogramas, símbolos, emoji extendidos
    "\U00002600-\U000026FF"  # símbolos misceláneos (☀ ☕ ⚽ etc.)
    "\U00002700-\U000027BF"  # dingbats (✂ ✈ ❤ etc.)
    "\U0001F1E6-\U0001F1FF"  # banderas (pares de letras regionales)
    "\U00002B00-\U00002BFF"  # flechas/símbolos misceláneos adicionales
    "️"                 # variation selector (modificador de emoji)
    "‍"                 # zero-width joiner (emoji compuestos)
    "]+"
)


def _strip_emoji(texto):
    if not isinstance(texto, str):
        return texto
    return _EMOJI_PATTERN.sub("", texto).strip()


def money(x):
    """Formato abreviado ($29.3M / $450K) — solo para la pestaña Balance,
    donde las cifras son grandes y mostrar todas las cifras significativas
    agregaría ruido en vez de precisión."""
    sign = "-" if x < 0 else ""
    absx = abs(x)
    if absx >= 1_000_000:
        v = f"{absx/1_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{sign}${v}M"
    if absx >= 1_000:
        v = f"{absx/1_000:.1f}".rstrip("0").rstrip(".")
        return f"{sign}${v}K"
    return f"{sign}${absx:,.0f}"


def _decimals_for_sigfigs(absx, n):
    if absx == 0:
        return 0
    return max(0, n - int(math.floor(math.log10(absx))) - 1)


def sig_money(x, n=3):
    """Formatea a n cifras significativas (no decimales fijos), para no
    perder precisión en montos chicos ni mostrar falsa precisión en grandes."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    sign = "-" if x < 0 else ""
    absx = abs(x)
    decimals = _decimals_for_sigfigs(absx, n)
    return f"{sign}${absx:,.{decimals}f}"


def sig_pct(x, n=3):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    decimals = _decimals_for_sigfigs(abs(x), n)
    return f"{x:.{decimals}f}%"


def _utc_offset_label(dt):
    """'UTC-5' / 'UTC+5:30' a partir del offset de un datetime tz-aware."""
    offset = dt.utcoffset()
    if offset is None:
        return None
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    h, m = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def timestamp_label(fetched_at):
    """Timestamp legible del último fetch real (hora local + zona horaria),
    para reemplazar frases vagas como '(cierre reciente)' por una referencia
    de tiempo concreta."""
    if not fetched_at:
        return None
    base = fetched_at.strftime("%d %b %H:%M")
    tz = _utc_offset_label(fetched_at)
    return f"{base} {tz}" if tz else base


def freshness_badge(source, fetched_at, stale_after_min=15):
    """Indicador de qué tan reciente es un dato que viene de una fuente externa
    (Twelve Data / TRM): color + 'hace X min' según el timestamp del último
    fetch real (no de cuándo se renderiza la página)."""
    if source is None:
        return f'<span style="color:{MUTED}">●</span> no disponible'
    if fetched_at is None:
        return f'<span style="color:{MUTED}">●</span> no se actualiza (valor fijo)'
    now = datetime.now(fetched_at.tzinfo) if fetched_at.tzinfo else datetime.now()
    delta_min = (now - fetched_at).total_seconds() / 60
    if delta_min < 1:
        edad = "hace instantes"
    elif delta_min < 60:
        edad = f"hace {int(delta_min)} min"
    else:
        edad = f"hace {int(delta_min // 60)} h"
    color = SAGE if delta_min <= stale_after_min else RUST
    return f'<span style="color:{color}">●</span> {edad}'


# ------------------------------------------------------------------------------
# DATOS DE INVERSIONES  (CSV en Blob/local + precios en vivo de Twelve Data)
# ------------------------------------------------------------------------------
def _load_investment_sources():
    if INVESTMENTS_LOCAL:
        base = INVESTMENTS_LOCAL.rstrip("/")
        var_raw = pd.read_csv(f"{base}/Portafolio_Activo.csv")
        fija_raw = pd.read_csv(f"{base}/Portafolio_Fijo.csv")
    else:
        con = duckdb.connect()
        con.execute("INSTALL azure; LOAD azure;")
        if os.name != "nt":
            con.execute("SET azure_transport_option_type = 'curl';")
        con.execute(f"""
            CREATE OR REPLACE SECRET azsecret_inv (
                TYPE azure,
                CONNECTION_STRING '{INV_CONN_STR}'
            );
        """)
        b = f"azure://{INVESTMENTS_CONTAINER}/{INVESTMENTS_PATH}"
        var_raw = con.execute(f"SELECT * FROM read_csv_auto('{b}/Portafolio_Activo.csv')").df()
        fija_raw = con.execute(f"SELECT * FROM read_csv_auto('{b}/Portafolio_Fijo.csv')").df()
    return var_raw, fija_raw


@st.cache_data(ttl=600, show_spinner="Cargando posiciones de inversión…")
def load_investment_positions():
    var_raw, fija_raw = _load_investment_sources()
    fixed_df = investments_data.load_fixed_income(fija_raw)
    var_pos_df = investments_data.load_variable_positions(var_raw)
    tickers = var_pos_df["ticker"].tolist()
    historical_df = investments_data.fetch_historical_monthly_series(tickers, TWELVE_DATA_API_KEY)
    # timestamp del fetch real (no de cuándo se renderiza la página), para el
    # indicador de frescura de datos — None si no vinieron de Twelve Data.
    precios_fetched_at = datetime.now().astimezone() if not historical_df.empty else None
    return fixed_df, var_pos_df, historical_df, precios_fetched_at


@st.cache_data(ttl=60, show_spinner=False)
def load_live_fx_rate():
    # Ojo: Twelve Data cobra 1 crédito POR SÍMBOLO aunque la llamada de
    # históricos venga "batcheada" (agrupar solo ahorra viajes de red, no
    # cuota). Con 4 tickers, histórico (4 créditos) + precio en vivo (4
    # créditos más) ya deja sin margen el límite de 8 créditos/minuto del
    # plan gratuito para la tasa USD/COP. Por eso NO pedimos precio en vivo
    # aparte: usamos la última barra del histórico mensual como "precio
    # actual" (se actualiza con el precio de hoy mientras el mes está en
    # curso) y reservamos el cupo para la tasa USD/COP, que sí se refresca
    # cada minuto.
    #
    # Aun así Twelve Data puede fallar (créditos agotados, red, etc.), y la
    # TRM es una métrica base para convertir toda la cartera variable a COP,
    # así que encadenamos 2 respaldos antes de rendirnos: una API alterna sin
    # key, y por último un valor fijo configurable por .env/secrets.
    rate = investments_data.fetch_usd_cop_rate(TWELVE_DATA_API_KEY)
    if rate:
        return rate, "Twelve Data", datetime.now().astimezone()

    rate = investments_data.fetch_usd_cop_rate_fallback()
    if rate:
        return rate, "open.er-api.com", datetime.now().astimezone()

    if USD_COP_FALLBACK_RATE:
        logging.getLogger(__name__).warning(
            "Tasa USD/COP: Twelve Data y el fallback fallaron, usando valor "
            "fijo configurado (USD_COP_FALLBACK_RATE=%s)", USD_COP_FALLBACK_RATE)
        return USD_COP_FALLBACK_RATE, "valor fijo (.env)", None

    return None, None, None


@st.cache_data(ttl=600, show_spinner="Generando datos de inversión…")
def load_investment_data(ciclos_all):
    fixed_df, var_pos_df, historical_df, precios_fetched_at = load_investment_positions()
    fx_rate, fx_source, fx_fetched_at = load_live_fx_rate()

    var_price_df = investments_data.build_variable_price_series(
        var_pos_df, ciclos_all, historical_df, {})
    fx_by_cycle = investments_data.build_fx_series(ciclos_all, fx_rate)
    cycle_df = investments_data.build_investment_cycle_summary(
        fixed_df, var_pos_df, var_price_df, ciclos_all, fx_by_cycle)
    precios_en_vivo = not historical_df.empty
    meta = {
        "fx_rate": fx_rate, "fx_source": fx_source, "fx_fetched_at": fx_fetched_at,
        "precios_en_vivo": precios_en_vivo, "precios_fetched_at": precios_fetched_at,
    }
    return fixed_df, var_pos_df, var_price_df, cycle_df, meta


# ------------------------------------------------------------------------------
# APP
# ------------------------------------------------------------------------------
try:
    data = load_data()
except Exception as e:
    st.error(f"No se pudieron cargar los datos de Gold. Revisá la conexión.\n\n{e}")
    st.stop()

if data.empty:
    st.info("No hay datos en Gold todavía. Corré el pipeline con un CSV en landing/.")
    st.stop()

data["categoria"] = data["categoria"].map(_strip_emoji)

ciclos_all = sorted(data["ciclo"].unique())

try:
    (inv_fixed_df, inv_var_pos_df, inv_var_price_df, inv_cycle_df, inv_meta) = load_investment_data(ciclos_all)
    inv_error = None
except Exception as e:
    inv_error = str(e)
    inv_fixed_df = pd.DataFrame(columns=["id_producto", "tipo_producto", "entidad",
                                          "capital_invertido", "tea", "moneda",
                                          "fecha_apertura", "fecha_vencimiento"])
    inv_var_pos_df = pd.DataFrame(columns=["id_posicion", "ticker", "nombre", "cantidad",
                                            "precio_compra", "precio_actual_csv", "moneda",
                                            "fecha_compra"])
    inv_var_price_df = pd.DataFrame(columns=["id_posicion", "ciclo", "precio"])
    inv_cycle_df = investments_data.build_investment_cycle_summary(
        inv_fixed_df, inv_var_pos_df, inv_var_price_df, ciclos_all, {})
    inv_meta = {"fx_rate": None, "fx_source": None, "fx_fetched_at": None,
                "precios_en_vivo": False, "precios_fetched_at": None}

inv_fx_rate = inv_meta["fx_rate"]
inv_fx_source = inv_meta["fx_source"]
inv_precios_en_vivo = inv_meta["precios_en_vivo"]

tab_balance, tab_inversiones = st.tabs(["Balance", "Inversiones"])

# ================================================================================
# PESTAÑA: BALANCE
# ================================================================================
with tab_balance:
    # --- Masthead ---
    st.markdown(f"""
    <div class="masthead">
      <div class="eyebrow">Finanzas personales</div>
      <h1 style="margin:.1rem 0 0 0; font-weight:900; font-size:2.4rem;">El Balance</h1>
    </div>
    """, unsafe_allow_html=True)

    # --- Sidebar: filtros ---
    st.sidebar.markdown("### Filtros")
    sel_ciclos = st.sidebar.multiselect("Ciclo (17 a 17)", ciclos_all, default=ciclos_all)
    contos = sorted(data["conto"].dropna().unique())
    sel_contos = st.sidebar.multiselect("Cuenta / tarjeta", contos, default=contos)
    cats = sorted(data["categoria"].dropna().unique())
    sel_cats = st.sidebar.multiselect("Categoría", cats, default=cats)

    df = data[
        data["ciclo"].isin(sel_ciclos) &
        data["conto"].isin(sel_contos) &
        data["categoria"].isin(sel_cats)
    ].copy()

    if df.empty:
        st.warning("Ningún registro con esos filtros. Ampliá la selección.")
        st.stop()

    # separar gasto/ingreso
    gastos = df[~df["es_ingreso"]]
    ingresos = df[df["es_ingreso"]]
    tot_gasto = gastos["valore"].sum()
    tot_ingreso = ingresos["valore"].sum()
    balance = tot_ingreso - tot_gasto
    tasa_ahorro = (balance / tot_ingreso * 100) if tot_ingreso else 0

    # ============================ FILA KPI: BALANCE ============================
    c1, c2, c3, c4 = st.columns(4, gap="medium")
    with c1:
        st.markdown(f'<div class="kpi"><div class="lbl">Ingresos</div>'
                    f'<div class="val pos">{money(tot_ingreso)}</div>'
                    f'<div class="sub">{len(ingresos)} movimientos</div></div>', unsafe_allow_html=True)
    with c2:
        st.markdown(f'<div class="kpi"><div class="lbl">Gastos</div>'
                    f'<div class="val neg">{money(tot_gasto)}</div>'
                    f'<div class="sub">{len(gastos)} movimientos</div></div>', unsafe_allow_html=True)
    with c3:
        cls = "pos" if balance >= 0 else "neg"
        signo = "Superávit" if balance >= 0 else "Déficit"
        st.markdown(f'<div class="kpi"><div class="lbl">Balance</div>'
                    f'<div class="val {cls}">{money(balance)}</div>'
                    f'<div class="sub">{signo}</div></div>', unsafe_allow_html=True)
    with c4:
        cls = "pos" if tasa_ahorro >= 0 else "neg"
        st.markdown(f'<div class="kpi"><div class="lbl">Tasa de ahorro</div>'
                    f'<div class="val {cls}">{tasa_ahorro:.0f}%</div>'
                    f'<div class="sub">del ingreso</div></div>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ============================ BALANCE POR CICLO (foco) ============================
    st.markdown("### Ingreso vs. gasto por ciclo")
    bal = df.groupby(["ciclo", "es_ingreso"])["valore"].sum().reset_index()
    bal["tipo_mov"] = bal["es_ingreso"].map({True: "Ingreso", False: "Gasto"})
    piv = bal.pivot_table(index="ciclo", columns="tipo_mov", values="valore", fill_value=0).reset_index()
    for col in ["Ingreso", "Gasto"]:
        if col not in piv: piv[col] = 0
    piv["Balance"] = piv["Ingreso"] - piv["Gasto"]

    # nota: la renta variable se convierte a COP (valorizacion_renta_variable_cop)
    # para poder mostrarla junto a Ingreso/Gasto/renta fija en la misma gráfica;
    # queda en 0 en los ciclos donde no hubo tasa USD/COP disponible.
    piv = piv.merge(
        inv_cycle_df[["ciclo", "interes_renta_fija", "valorizacion_renta_variable_cop"]],
        on="ciclo", how="left"
    ).fillna(0)

    fig = go.Figure()
    fig.add_bar(x=piv["ciclo"], y=piv["Ingreso"], name="Ingreso", marker_color=SAGE)
    fig.add_bar(x=piv["ciclo"], y=-piv["Gasto"], name="Gasto", marker_color=RUST)
    fig.add_bar(x=piv["ciclo"], y=piv["interes_renta_fija"],
                name="Recaudo renta fija", marker_color=MUTED)
    fig.add_bar(x=piv["ciclo"], y=piv["valorizacion_renta_variable_cop"],
                name="Recaudo renta variable", marker_color=GOLD_AC)
    fig.add_trace(go.Scatter(x=piv["ciclo"], y=piv["Balance"], name="Balance",
                             mode="lines+markers", line=dict(color=INK, width=2.5),
                             marker=dict(size=7)))
    fig.update_layout(barmode="relative", height=380, plot_bgcolor=SURFACE, paper_bgcolor=PAPER,
                      font=dict(family="Raleway", color=INK),
                      legend=dict(orientation="h", y=1.12, x=0),
                      margin=dict(l=10, r=10, t=30, b=10),
                      yaxis=dict(title="", gridcolor=BORDER, zerolinecolor=INK))
    st.plotly_chart(fig, use_container_width=True)

    # ============================ FILA DE 2: USO POR CUENTA + NECESARIO/EXTRA (tortas) ============================
    colA, colB = st.columns(2)

    with colA:
        st.markdown("### Uso por cuenta / tarjeta")
        # solo gasto (no ingresos)
        cuenta = gastos.groupby("conto")["valore"].agg(["sum", "count"]).reset_index()
        cuenta.columns = ["conto", "total", "movimientos"]
        cuenta = cuenta.sort_values("total", ascending=False)
        fig4 = px.pie(cuenta, values="total", names="conto", hole=.55,
                      color_discrete_sequence=CAT_SEQ, hover_data=["movimientos"])
        fig4.update_traces(textposition="outside", textinfo="percent+label")
        fig4.update_layout(height=420, showlegend=False, paper_bgcolor=PAPER,
                           font=dict(family="Raleway", color=INK),
                           margin=dict(l=60, r=60, t=40, b=40))
        st.plotly_chart(fig4, use_container_width=True)

    with colB:
        st.markdown("### Necesario · Extra · Ahorro")
        # tipo_i sin los ingresos (que son 'Reddito')
        ti = gastos.groupby("tipo_i")["valore"].sum().reset_index()
        fig3 = px.pie(ti, values="valore", names="tipo_i", hole=.55,
                      color_discrete_sequence=[RUST, SAGE, GOLD_AC, MUTED])
        fig3.update_traces(textposition="outside", textinfo="percent+label")
        fig3.update_layout(height=420, showlegend=False, paper_bgcolor=PAPER,
                           font=dict(family="Raleway", color=INK),
                           margin=dict(l=60, r=60, t=40, b=40))
        st.plotly_chart(fig3, use_container_width=True)

    st.markdown("<div style='height:2.5rem'></div>", unsafe_allow_html=True)

    # ============================ EN QUÉ SE VA (gasto por categoría) ============================
    st.markdown("### En qué se va (gasto por categoría)")
    catg = gastos.groupby("categoria")["valore"].sum().reset_index().sort_values("valore", ascending=False)
    fig2 = px.bar(catg, x="valore", y="categoria", orientation="h",
                  color="categoria", color_discrete_sequence=CAT_SEQ)
    fig2.update_layout(height=340, showlegend=False, plot_bgcolor=SURFACE, paper_bgcolor=PAPER,
                       font=dict(family="Raleway", color=INK),
                       margin=dict(l=10, r=10, t=10, b=10),
                       yaxis=dict(title="", autorange="reversed"),
                       xaxis=dict(title="", gridcolor=BORDER))
    st.plotly_chart(fig2, use_container_width=True)

    # ============================ TABLA EXPLORABLE + EXPORT ============================
    st.markdown("### Detalle de movimientos")
    tabla = df[["giorno", "ciclo", "conto", "tipo", "tipo_i", "categoria", "valore", "commento"]].copy()
    tabla = tabla.rename(columns={
        "giorno": "Fecha", "ciclo": "Ciclo", "conto": "Cuenta", "tipo": "Tipo",
        "tipo_i": "Clase", "categoria": "Categoría", "valore": "Valor", "commento": "Comentario"})
    tabla = tabla.sort_values("Fecha", ascending=False)

    st.dataframe(tabla, use_container_width=True, hide_index=True,
                 column_config={"Valor": st.column_config.NumberColumn(format="$%.0f")})

    csv_bytes = tabla.to_csv(index=False).encode("utf-8-sig")
    st.download_button("Exportar a CSV", data=csv_bytes,
                       file_name="gastos_filtrado.csv", mime="text/csv")

    st.markdown(f"<div class='eyebrow' style='margin-top:2rem'>"
                f"{len(df)} movimientos · {len(sel_ciclos)} ciclos · "
                f"actualizado desde Gold</div>", unsafe_allow_html=True)

# ================================================================================
# PESTAÑA: INVERSIONES
# ================================================================================
with tab_inversiones:
    st.markdown(f"""
    <div class="masthead">
      <div class="eyebrow">Finanzas personales · Portafolio</div>
      <h1 style="margin:.1rem 0 0 0; font-weight:900; font-size:2.4rem;">Inversiones</h1>
    </div>
    """, unsafe_allow_html=True)

    if inv_error:
        st.warning(
            "No se pudieron cargar los datos de inversión. Revisá "
            "`AZURE_INVESTMENTS_CONNECTION_STRING` (o `INVESTMENTS_LOCAL_PATH` en local) "
            f"y que los archivos `Portafolio_Activo.csv` / `Portafolio_Fijo.csv` existan.\n\n{inv_error}")
    elif not TWELVE_DATA_API_KEY:
        st.info("`TWELVE_DATA_API_KEY` no está configurada: los precios y la tasa "
                "USD/COP se muestran con el último valor guardado en el CSV, no en vivo.")

    kpis = investments_data.summarize_investment_kpis(inv_cycle_df)
    capital_fija = inv_fixed_df["capital_invertido"].sum()
    capital_variable_usd = (inv_var_pos_df["cantidad"] * inv_var_pos_df["precio_compra"]).sum()
    capital_variable_cop = capital_variable_usd * inv_fx_rate if inv_fx_rate else 0.0
    capital_total = capital_fija + capital_variable_cop
    tasa_retorno = (kpis["combinado"]["retorno_total"] / capital_total * 100) if capital_total else 0

    # ============================ FILA KPI: PORTAFOLIO COMBINADO ============================
    c1, c2, c3, c4 = st.columns(4, gap="medium")
    with c1:
        sub = "Fija + variable" if inv_fx_rate else "Fija + variable (variable sin convertir: falta tasa USD/COP)"
        st.markdown(f'<div class="kpi"><div class="lbl">Capital invertido</div>'
                    f'<div class="val">{sig_money(capital_total)}</div>'
                    f'<div class="sub">{sub}</div></div>', unsafe_allow_html=True)
    with c2:
        cls = "pos" if kpis["combinado"]["retorno_total"] >= 0 else "neg"
        st.markdown(f'<div class="kpi"><div class="lbl">Recaudo a la fecha</div>'
                    f'<div class="val {cls}">{sig_money(kpis["combinado"]["retorno_total"])}</div>'
                    f'<div class="sub">Renta fija + variable</div></div>', unsafe_allow_html=True)
    with c3:
        cls = "pos" if kpis["combinado"]["mom_delta"] >= 0 else "neg"
        st.markdown(f'<div class="kpi"><div class="lbl">Vs. mes anterior</div>'
                    f'<div class="val {cls}">{sig_money(kpis["combinado"]["mom_delta"])}</div>'
                    f'<div class="sub">Variación mensual</div></div>', unsafe_allow_html=True)
    with c4:
        cls = "pos" if tasa_retorno >= 0 else "neg"
        st.markdown(f'<div class="kpi"><div class="lbl">Tasa de retorno</div>'
                    f'<div class="val {cls}">{sig_pct(tasa_retorno)}</div>'
                    f'<div class="sub">sobre capital invertido</div></div>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ============================ POR TIPO DE CARTERA ============================
    st.markdown("### Por tipo de cartera")
    st.markdown("<div style='height:0.85rem'></div>", unsafe_allow_html=True)
    vista = st.segmented_control(
        "Ver cartera", ["Renta fija", "Renta variable"],
        default="Renta fija", label_visibility="collapsed", key="cartera_view")

    if vista == "Renta fija":

        tea_promedio = (inv_fixed_df["capital_invertido"] * inv_fixed_df["tea"]).sum() / capital_fija * 100 if capital_fija else 0
        vencimientos = inv_fixed_df["fecha_vencimiento"].dropna()
        proximo_vencimiento = vencimientos.min().strftime("%d %b %Y") if not vencimientos.empty else "Sin vencimiento"

        f1, f2, f3 = st.columns(3, gap="medium")
        with f1:
            st.markdown(f'<div class="kpi"><div class="lbl">Capital</div>'
                        f'<div class="val">{sig_money(capital_fija)}</div>'
                        f'<div class="sub">{len(inv_fixed_df)} productos</div></div>', unsafe_allow_html=True)
        with f2:
            st.markdown(f'<div class="kpi"><div class="lbl">Recaudo</div>'
                        f'<div class="val pos">{sig_money(kpis["renta_fija"]["retorno_total"])}</div>'
                        f'<div class="sub">a la fecha</div></div>', unsafe_allow_html=True)
        with f3:
            st.markdown(f'<div class="kpi"><div class="lbl">Vs. mes anterior</div>'
                        f'<div class="val pos">{sig_money(kpis["renta_fija"]["mom_delta"])}</div>'
                        f'<div class="sub">recaudo del mes</div></div>', unsafe_allow_html=True)

        st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)

        f4, f5, f6 = st.columns(3, gap="medium")
        with f4:
            rendimiento_fija = (kpis["renta_fija"]["retorno_total"] / capital_fija * 100) if capital_fija else 0
            st.markdown(f'<div class="kpi"><div class="lbl">Rendimiento</div>'
                        f'<div class="val pos">{sig_pct(rendimiento_fija)}</div>'
                        f'<div class="sub">sobre capital</div></div>', unsafe_allow_html=True)
        with f5:
            st.markdown(f'<div class="kpi"><div class="lbl">TEA promedio</div>'
                        f'<div class="val acc">{sig_pct(tea_promedio)}</div>'
                        f'<div class="sub">ponderada por capital</div></div>', unsafe_allow_html=True)
        with f6:
            st.markdown(f'<div class="kpi"><div class="lbl">Próximo vencimiento</div>'
                        f'<div class="val" style="font-size:1.3rem">{proximo_vencimiento}</div>'
                        f'<div class="sub">producto más cercano</div></div>', unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        detalle_fija = inv_fixed_df.copy()
        detalle_fija["recaudo_a_la_fecha"] = detalle_fija["id_producto"].map(
            lambda pid: investments_data.compute_fixed_income_cycle_returns(
                inv_fixed_df[inv_fixed_df["id_producto"] == pid], ciclos_all
            )["interes_renta_fija"].sum()
        )
        detalle_fija["Capital"] = detalle_fija["capital_invertido"].map(sig_money)
        detalle_fija["TEA"] = (detalle_fija["tea"] * 100).map(sig_pct)
        detalle_fija["Recaudo a la fecha"] = detalle_fija["recaudo_a_la_fecha"].map(sig_money)
        detalle_fija = detalle_fija.rename(columns={
            "id_producto": "Producto", "tipo_producto": "Tipo", "entidad": "Entidad",
            "fecha_apertura": "Apertura", "fecha_vencimiento": "Vencimiento"})
        st.dataframe(
            detalle_fija[["Producto", "Tipo", "Entidad", "Capital", "TEA", "Apertura",
                          "Vencimiento", "Recaudo a la fecha"]],
            use_container_width=True, hide_index=True)

    else:
        precio_actual = {
            pid: inv_var_price_df[inv_var_price_df["id_posicion"] == pid]["precio"].iloc[-1]
            for pid in inv_var_pos_df["id_posicion"]
            if not inv_var_price_df[inv_var_price_df["id_posicion"] == pid].empty
        }
        valor_mercado = sum(
            row["cantidad"] * precio_actual.get(row["id_posicion"], row["precio_compra"])
            for _, row in inv_var_pos_df.iterrows()
        )
        valorizacion_pct = (kpis["renta_variable"]["retorno_total"] / capital_variable_usd * 100) if capital_variable_usd else 0
        _precios_ts = timestamp_label(inv_meta["precios_fetched_at"])
        fuente_precio = f"Twelve Data ({_precios_ts})" if inv_precios_en_vivo and _precios_ts else "último valor del CSV"
        precios_fresh = freshness_badge("Twelve Data" if inv_precios_en_vivo else None, inv_meta["precios_fetched_at"])

        # recaudo_mes = ganancia/pérdida de mercado DE ESTE ciclo (no acumulada);
        # recaudo_mes_anterior se deriva restando el mom_delta (que ya es la
        # diferencia entre ambos) para no tener que volver a tocar inv_cycle_df.
        recaudo_mes = inv_cycle_df.iloc[-1]["valorizacion_renta_variable"] if not inv_cycle_df.empty else 0.0
        mom_variable = kpis["renta_variable"]["mom_delta"]
        recaudo_mes_anterior = recaudo_mes - mom_variable
        aceleracion_pct = (mom_variable / abs(recaudo_mes_anterior) * 100) if recaudo_mes_anterior else None

        v1, v2, v3 = st.columns(3, gap="medium")
        with v1:
            st.markdown(f'<div class="kpi"><div class="lbl">Valor de mercado (USD)</div>'
                        f'<div class="val">{sig_money(valor_mercado)}</div>'
                        f'<div class="sub">{len(inv_var_pos_df)} posición(es) · {fuente_precio} · {precios_fresh}</div></div>',
                        unsafe_allow_html=True)
        with v2:
            cls = "pos" if kpis["renta_variable"]["retorno_total"] >= 0 else "neg"
            st.markdown(f'<div class="kpi"><div class="lbl">Recaudo (USD)</div>'
                        f'<div class="val {cls}">{sig_money(kpis["renta_variable"]["retorno_total"])}</div>'
                        f'<div class="sub">a la fecha</div></div>', unsafe_allow_html=True)
        with v3:
            cls = "pos" if recaudo_mes >= 0 else "neg"
            st.markdown(f'<div class="kpi"><div class="lbl">Recaudo del mes (USD)</div>'
                        f'<div class="val {cls}">{sig_money(recaudo_mes)}</div>'
                        f'<div class="sub">mes anterior: {sig_money(recaudo_mes_anterior)}</div></div>', unsafe_allow_html=True)

        st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)

        v4, v5, v6 = st.columns(3, gap="medium")
        with v4:
            if aceleracion_pct is None:
                st.markdown(f'<div class="kpi"><div class="lbl">Crecimiento Vs. mes anterior</div>'
                            f'<div class="val" style="font-size:1.3rem">—</div>'
                            f'<div class="sub">sin mes previo para comparar</div></div>', unsafe_allow_html=True)
            else:
                cls = "pos" if aceleracion_pct >= 0 else "neg"
                verbo = "aceleró" if aceleracion_pct >= 0 else "desaceleró"
                st.markdown(f'<div class="kpi"><div class="lbl">Crecimiento Vs. mes anterior</div>'
                            f'<div class="val {cls}">{sig_pct(aceleracion_pct)}</div>'
                            f'<div class="sub">{verbo} frente al mes pasado</div></div>', unsafe_allow_html=True)
        with v5:
            cls = "pos" if valorizacion_pct >= 0 else "neg"
            st.markdown(f'<div class="kpi"><div class="lbl">Rendimiento</div>'
                        f'<div class="val {cls}">{sig_pct(valorizacion_pct)}</div>'
                        f'<div class="sub">sobre costo base</div></div>', unsafe_allow_html=True)
        with v6:
            if inv_fx_rate:
                fx_sub = {
                    "Twelve Data": "en vivo (Twelve Data)",
                    "open.er-api.com": "respaldo en vivo (open.er-api.com)",
                    "valor fijo (.env)": "valor fijo configurado (.env)",
                }.get(inv_fx_source, inv_fx_source or "")
                fx_fresh = freshness_badge(inv_fx_source, inv_meta["fx_fetched_at"], stale_after_min=2)
                st.markdown(f'<div class="kpi"><div class="lbl">Tasa USD/COP</div>'
                            f'<div class="val acc">{sig_money(inv_fx_rate)}</div>'
                            f'<div class="sub">{fx_sub} · {fx_fresh}</div></div>', unsafe_allow_html=True)
            else:
                st.markdown(f'<div class="kpi"><div class="lbl">Tasa USD/COP</div>'
                            f'<div class="val" style="font-size:1.3rem">No disponible</div>'
                            f'<div class="sub">Twelve Data y respaldo fallaron · configurá USD_COP_FALLBACK_RATE</div></div>', unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        detalle_var = inv_var_pos_df.copy()
        detalle_var["precio_actual"] = detalle_var["id_posicion"].map(
            lambda pid: precio_actual.get(pid, None))
        detalle_var["valor_mercado"] = detalle_var["cantidad"] * detalle_var["precio_actual"]
        detalle_var["costo_base"] = detalle_var["cantidad"] * detalle_var["precio_compra"]
        detalle_var["pnl"] = detalle_var["valor_mercado"] - detalle_var["costo_base"]
        detalle_var["pnl_pct"] = detalle_var["pnl"] / detalle_var["costo_base"] * 100

        detalle_var["Precio compra (USD)"] = detalle_var["precio_compra"].map(sig_money)
        detalle_var["Precio actual (USD)"] = detalle_var["precio_actual"].map(sig_money)
        detalle_var["Valor de mercado (USD)"] = detalle_var["valor_mercado"].map(sig_money)
        detalle_var["P&L (USD)"] = detalle_var["pnl"].map(sig_money)
        detalle_var["P&L %"] = detalle_var["pnl_pct"].map(sig_pct)
        detalle_var = detalle_var.rename(columns={"ticker": "Ticker", "nombre": "Nombre", "cantidad": "Cantidad"})
        st.dataframe(
            detalle_var[["Ticker", "Nombre", "Cantidad", "Precio compra (USD)", "Precio actual (USD)",
                        "Valor de mercado (USD)", "P&L (USD)", "P&L %"]],
            use_container_width=True, hide_index=True)

    st.markdown("<div style='height:1.5rem'></div>", unsafe_allow_html=True)

    # ============================ EVOLUCIÓN DEL RETORNO ACUMULADO ============================
    st.markdown("### Evolución del retorno acumulado")
    st.markdown('<div class="eyebrow" style="margin-bottom:0.5rem">'
                'renta variable convertida a COP con la tasa USD/COP actual (no histórica)</div>',
                unsafe_allow_html=True)
    def _rgba(hex_color, alpha):
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"rgba({r},{g},{b},{alpha})"

    figi = go.Figure()
    figi.add_trace(go.Scatter(x=inv_cycle_df["ciclo"], y=inv_cycle_df["retorno_acumulado_fija"],
                              name="Renta fija", mode="lines+markers",
                              line=dict(color=SAGE, width=2.5), marker=dict(size=6),
                              fill="tozeroy", fillcolor=_rgba(SAGE, 0.15)))
    figi.add_trace(go.Scatter(x=inv_cycle_df["ciclo"], y=inv_cycle_df["retorno_acumulado_variable_cop"],
                              name="Renta variable", mode="lines+markers",
                              line=dict(color=GOLD_AC, width=2.5), marker=dict(size=6),
                              fill="tozeroy", fillcolor=_rgba(GOLD_AC, 0.15)))
    figi.add_trace(go.Scatter(x=inv_cycle_df["ciclo"], y=inv_cycle_df["retorno_acumulado_total"],
                              name="Total", mode="lines+markers",
                              line=dict(color=CYAN_AC, width=2.5), marker=dict(size=6),
                              fill="tozeroy", fillcolor=_rgba(CYAN_AC, 0.12)))
    figi.update_layout(height=380, plot_bgcolor=SURFACE, paper_bgcolor=PAPER,
                       font=dict(family="Raleway", color=INK),
                       legend=dict(orientation="h", y=1.12, x=0),
                       margin=dict(l=10, r=10, t=30, b=10),
                       yaxis=dict(title="", gridcolor=BORDER, zerolinecolor=INK))
    st.plotly_chart(figi, use_container_width=True)

    _precios_ts_bottom = timestamp_label(inv_meta["precios_fetched_at"])
    fuente_txt = (f"Twelve Data ({_precios_ts_bottom})" if inv_precios_en_vivo and _precios_ts_bottom
                  else "último valor guardado en el CSV")
    st.markdown(f"<div class='eyebrow' style='margin-top:2rem'>"
                f"Posiciones desde {'Blob Storage' if not INVESTMENTS_LOCAL else 'CSV local'} · "
                f"precios: {fuente_txt} · los filtros de la barra lateral no aplican a esta pestaña</div>",
                unsafe_allow_html=True)
