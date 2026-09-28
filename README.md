# El Balance — Dashboard de Gastos Personales

Dashboard en [Streamlit](https://streamlit.io/) + [DuckDB](https://duckdb.org/) para el seguimiento de finanzas personales. Lee un esquema estrella (capa **Gold**) directamente desde Azure Blob Storage con `read_parquet` — sin base de datos ni warehouse intermedio — y lo presenta con foco en el **balance ingreso vs. gasto**.

Identidad visual inspirada en [mi portfolio](https://aviles17.github.io/My_react_resume/): navy profundo, acento verde menta, tipografía Raleway.

## Vistas

- **KPIs de balance**: ingresos, gastos, balance (superávit/déficit) y tasa de ahorro. Montos abreviados (`$36.5K`, `$1.2M`) y tarjetas de altura uniforme.
- **Ingreso vs. gasto por ciclo**: barras divergentes (ingreso arriba, gasto abajo) + línea de balance. Vista principal, a todo el ancho.
- **Uso por cuenta/tarjeta**: torta con el gasto (no ingresos) distribuido por cuenta.
- **Necesario · Extra · Ahorro**: torta por `tipo_i`, al lado de la anterior.
- **Gasto por categoría**: barras horizontales ordenadas de mayor a menor.
- **Detalle de movimientos**: tabla filtrable + botón de export a CSV.

### Filtros (barra lateral)
- **Ciclo** (17 → 17, multiselección — todos seleccionados por defecto)
- **Cuenta / tarjeta**
- **Categoría**

## Cómo funciona

`dashboard.py` abre una conexión DuckDB en memoria, carga la extensión `azure` y lee los archivos Parquet de Gold directamente desde el contenedor Blob con `read_parquet(...)`, uniendo el fact con sus tres dimensiones. El resultado se cachea 10 minutos (`st.cache_data(ttl=600)`) para no releer el Blob en cada interacción con los filtros.

### Esquema de datos esperado (Gold)

| Archivo | Columnas clave |
|---|---|
| `fact_gastos` (local) / `fact_fatture` (Blob, particionado) | `sk_categoria`, `sk_conto`, `sk_fecha`, `giorno`, `ciclo`, `valore`, `es_ingreso`, `commento` |
| `dim_categoria` | `sk_categoria`, `categoria`, `tipo_i` (Necesario/Extra/Ahorro/Reddito), `tipo` (Gasto/Ingreso) |
| `dim_conto` | `sk_conto`, `conto`, `tipo_cuenta` (Credito/Debito) |
| `dim_fecha` (local) / `dim_giorno` (Blob) | `sk_fecha`, `quincena`, `dia_semana` |

En Blob, la app espera el layout `azure://<container>/gold/<tabla>/...` (contenedor por defecto: `lakehouse`, definido en `CONTAINER` dentro de `dashboard.py`).

### Inversiones

La pestaña **Inversiones** lee sus posiciones desde un **Blob Storage aparte** del de gastos (`INVESTMENTS_CONTAINER`/`INVESTMENTS_PATH` en `dashboard.py`, por defecto `datalake/Dashboard_Finanzas_Personales`), en dos archivos CSV:

| Archivo (Blob: `<container>/<path>/`) | Columnas |
|---|---|
| `Portafolio_Fijo.csv` (CDT / Bolsillos / Alcancías) | `Producto`, `Tipo`, `Entidad`, `Capital Invertido (COP)`, `TEA (%)`, `Moneda`, `Fecha Apertura`, `Fecha Vencimiento` (vacío si no aplica), `Notas` |
| `Portafolio_Activo.csv` (acciones / ETF) | `Ticker`, `Nombre`, `Cantidad`, `Precio Entrada (USD)`, `Precio Actual (USD)`, `Fecha Entrada` (+ otras columnas del tracker de tesis que la app ignora: `Componente`, `Stop-Loss`, `Tesis Vinculada`, etc.) |

`investments_data.py` normaliza ambos a un esquema interno agnóstico de la fuente (pensado para poder reemplazar el CSV por tablas Gold reales sin tocar `dashboard.py`), y trae precios en vivo + la tasa USD/COP desde [Twelve Data](https://twelvedata.com/) (`/price`, `/exchange_rate`, `/time_series`). Si `TWELVE_DATA_API_KEY` no está seteada, o si Twelve Data cae, la app cae de vuelta al último precio guardado en el CSV — nunca rompe la pestaña.

La tasa USD/COP en particular tiene una cadena de 3 respaldos, porque toda la conversión de la cartera variable depende de ella: (1) Twelve Data, (2) [open.er-api.com](https://www.exchangerate-api.com/docs/free) (fallback gratuito, sin API key), (3) un valor fijo configurable por `USD_COP_FALLBACK_RATE`. Solo si los 3 fallan la tarjeta muestra "No disponible".

La renta variable se muestra en USD (su moneda nativa); para las tarjetas y gráficos que la combinan con la renta fija (en COP) se convierte con la tasa USD/COP del momento — si esa tasa no está disponible, la renta variable se excluye de esos totales combinados en vez de sumar montos en monedas distintas por error.

## Instalación local

```bash
git clone git@github.com:Aviles17/Dashboard_Gastos_Personales.git
cd Dashboard_Gastos_Personales
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements_dashboard.txt
```

## Configuración

La app soporta dos modos, controlados por variables de entorno (via `.env` local o `st.secrets` en la nube — **nunca hardcodeadas en el código**).

### Modo local / prueba — `GOLD_LOCAL_PATH`

Apuntá a una carpeta con los 4 Parquet del esquema de arriba (`fact_gastos.parquet`, `dim_categoria.parquet`, `dim_conto.parquet`, `dim_fecha.parquet`):

```bash
export GOLD_LOCAL_PATH=./sample_gold      # Windows: set GOLD_LOCAL_PATH=./sample_gold
streamlit run dashboard.py
```

### Modo producción — `AZURE_STORAGE_CONNECTION_STRING`

No definas `GOLD_LOCAL_PATH`; en su lugar, creá un archivo `.env` (ya está en `.gitignore`, nunca se sube al repo) con:

```bash
AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=stgastospersonales;AccountKey=<TU_KEY>;EndpointSuffix=core.windows.net"
```

`python-dotenv` lo carga automáticamente al arrancar. Después:

```bash
streamlit run dashboard.py
```

### Inversiones — `AZURE_INVESTMENTS_CONNECTION_STRING` / `INVESTMENTS_LOCAL_PATH` / `TWELVE_DATA_API_KEY`

Mismo patrón que arriba, pero para la pestaña Inversiones (Blob separado del de gastos):

```bash
# Local / prueba: carpeta con Portafolio_Fijo.csv y Portafolio_Activo.csv
INVESTMENTS_LOCAL_PATH=./sample_investments

# Producción (en vez de INVESTMENTS_LOCAL_PATH)
AZURE_INVESTMENTS_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=...;EndpointSuffix=core.windows.net"

# En ambos modos, para precios en vivo y tasa USD/COP (si falta, cae al CSV)
TWELVE_DATA_API_KEY="<tu-api-key>"

# Opcional: último respaldo para la tasa USD/COP si Twelve Data Y
# open.er-api.com fallan los dos (ej. ambos caídos o sin internet saliente)
USD_COP_FALLBACK_RATE=4000
```

`sample_investments/` está en `.gitignore` porque normalmente va a contener datos reales de portafolio — nunca se commitea.

## Tema visual

`.streamlit/config.toml` ya trae el tema oscuro (navy + verde menta) configurado a nivel de Streamlit, así que los widgets nativos (multiselect, botones, dataframe) heredan la paleta sin tocar nada. El resto del estilo (tarjetas KPI, tipografía Raleway, subrayados de sección) se inyecta como CSS dentro de `dashboard.py`.

## Desplegar en Streamlit Community Cloud (gratis)

1. En [share.streamlit.io](https://share.streamlit.io) → **New app** → elegí este repo y la rama.
2. **Main file path**: `dashboard.py`.
3. ⚠️ Streamlit Cloud busca `requirements.txt` por defecto. Como este repo usa `requirements_dashboard.txt`, especificá la ruta en **Advanced settings → Python dependencies file**, o renombralo antes de desplegar.
4. En **Settings → Secrets** de la app pegá (formato TOML):
   ```toml
   AZURE_STORAGE_CONNECTION_STRING = "DefaultEndpointsProtocol=https;AccountName=...;EndpointSuffix=core.windows.net"
   ```
   El secret queda cifrado del lado de Streamlit Cloud — nunca toca GitHub.
5. **Deploy**. La app se duerme por inactividad pero despierta sola al abrirla (unos segundos).

### Troubleshooting: `Problem with the SSL CA cert (path? access rights?)`

Si al desplegar ves este error al leer de Blob, **no es tu connection string ni permisos de Azure**: es el mensaje de error de libcurl (`CURLE_SSL_CACERT`). Tiene dos partes:

1. **El contenedor necesita el bundle de certs instalado.** El repo incluye un `packages.txt` con `ca-certificates`, que Streamlit Cloud instala automáticamente vía `apt-get` antes de las dependencias de Python (igual que un Aptfile de Heroku).
2. **DuckDB necesita que se le diga que use ese bundle.** Por defecto, la extensión `azure` usa el transporte HTTP propio del Azure SDK, que en algunos contenedores (incluido el de Streamlit Cloud) no ubica el bundle de certs del sistema aunque esté instalado — el paso 1 solo no alcanza. `dashboard.py` fuerza el transporte `curl` (que sí lo respeta) y además apunta explícitamente a la ruta estándar de Debian/Ubuntu como red de seguridad:
   ```python
   con.execute("SET azure_transport_option_type = 'curl';")
   os.environ.setdefault("CURL_CA_INFO", "/etc/ssl/certs/ca-certificates.crt")
   ```

Si volvés a ver este error después de tocar `packages.txt` o `dashboard.py`, hacé un **Reboot app** desde el menú de la app en Streamlit Cloud para forzar un rebuild del contenedor.

## Seguridad

- El connection string **nunca está en el código**: se lee de `AZURE_STORAGE_CONNECTION_STRING` (env var local o `st.secrets` en la nube). `dashboard.py` no tiene ningún secreto hardcodeado.
- `.env` está en `.gitignore` — no se commitea.
- Por defecto, cualquiera con la URL de la app desplegada puede verla. Si querés restringir el acceso:
  - **Recomendado**: marcá la app como privada en Streamlit Community Cloud y restringí los viewers por email (sin código, lo maneja la plataforma).
  - Alternativa liviana: un gate de contraseña dentro de la app (`st.text_input(type="password")` contra un secret) si necesitás que la URL quede tapada para cualquiera, incluso sin invitación.

## Estructura del repo

```
.
├── dashboard.py                 # app de Streamlit (UI + carga de datos de gastos)
├── investments_data.py          # carga/normaliza CSVs de inversión + Twelve Data (sin Streamlit)
├── requirements_dashboard.txt   # dependencias de Python
├── packages.txt                 # dependencias de sistema (apt) — ca-certificates para DuckDB+azure
├── .streamlit/
│   └── config.toml              # tema oscuro nativo de Streamlit
└── .gitignore                   # excluye .env y sample_investments/
```

## Stack

- [Streamlit](https://streamlit.io/) — UI y despliegue
- [DuckDB](https://duckdb.org/) (+ extensión `azure`) — lectura de Parquet/CSV directo desde Blob
- [Plotly](https://plotly.com/python/) — gráficos
- [pandas](https://pandas.pydata.org/)
- [python-dotenv](https://pypi.org/project/python-dotenv/)
- [Twelve Data](https://twelvedata.com/) (vía `requests`) — precios en vivo y tasa USD/COP para la pestaña Inversiones

---

Hecho por [Santiago Avilés](https://aviles17.github.io/My_react_resume/).
