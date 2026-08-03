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
import duckdb
import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from dotenv import load_dotenv

load_dotenv()                        

# ------------------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------------------
st.set_page_config(page_title="Gastos · Balance", page_icon="◐",
                   layout="wide", initial_sidebar_state="expanded")

CONTAINER = "lakehouse"
CONN_STR = os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or \
    (st.secrets.get("AZURE_STORAGE_CONNECTION_STRING") if hasattr(st, "secrets") else None)
GOLD_LOCAL = os.environ.get("GOLD_LOCAL_PATH")   # si está, lee de disco (modo prueba)

# Red de seguridad para el error "Problem with the SSL CA cert" en contenedores
# tipo Streamlit Cloud: le decimos al transporte curl de la extensión azure
# dónde está el bundle de certs del sistema (Debian/Ubuntu), sin pisar un valor
# que ya venga seteado en el entorno.
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
        con.execute("SET azure_transport_option_type = 'curl';")
        con.execute(f"SET azure_storage_connection_string='{CONN_STR}';")
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


def money(x):
    sign = "-" if x < 0 else ""
    absx = abs(x)
    if absx >= 1_000_000:
        v = f"{absx/1_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{sign}${v}M"
    if absx >= 1_000:
        v = f"{absx/1_000:.1f}".rstrip("0").rstrip(".")
        return f"{sign}${v}K"
    return f"{sign}${absx:,.0f}"


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

# --- Masthead ---
st.markdown(f"""
<div class="masthead">
  <div class="eyebrow">Finanzas personales</div>
  <h1 style="margin:.1rem 0 0 0; font-weight:900; font-size:2.4rem;">El Balance</h1>
</div>
""", unsafe_allow_html=True)

# --- Sidebar: filtros ---
st.sidebar.markdown("### Filtros")
ciclos = sorted(data["ciclo"].unique())
sel_ciclos = st.sidebar.multiselect("Ciclo (17 a 17)", ciclos, default=ciclos)
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
c1, c2, c3, c4 = st.columns(4)
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

fig = go.Figure()
fig.add_bar(x=piv["ciclo"], y=piv["Ingreso"], name="Ingreso", marker_color=SAGE)
fig.add_bar(x=piv["ciclo"], y=-piv["Gasto"], name="Gasto", marker_color=RUST)
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
