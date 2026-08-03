# Dashboard de Gastos — Balance ingreso vs. gasto

Dashboard en Streamlit + DuckDB que lee el esquema estrella (Gold) desde Azure
Blob. Foco en **balance ingreso vs. gasto**, con las 4 vistas pedidas, filtros,
tabla explorable y export CSV.

## Vistas
- **KPIs de balance**: ingresos, gastos, balance (superávit/déficit) y tasa de ahorro.
- **Ingreso vs. gasto por ciclo**: barras divergentes + línea de balance (foco principal).
- **Gasto por categoría**: barras horizontales ordenadas.
- **Necesario · Extra · Ahorro**: dona por `tipo_i`.
- **Uso por cuenta/tarjeta**: barras coloreadas por crédito/débito.
- **Detalle de movimientos**: tabla filtrable + botón de export CSV.

Filtros (barra lateral): ciclo (17→17), cuenta/tarjeta, categoría.

## Probar localmente (con datos de muestra)
```bash
pip install -r requirements_dashboard.txt
export GOLD_LOCAL_PATH=./sample_gold      # lee los parquet de la carpeta local
streamlit run dashboard.py
```

## Conectar a tu Blob real
```bash
# NO definas GOLD_LOCAL_PATH; en su lugar la connection string:
export AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=stgastospersonales;AccountKey=<TU_KEY>;EndpointSuffix=core.windows.net"
streamlit run dashboard.py
```

## Desplegar gratis en Streamlit Community Cloud
1. Subí `dashboard.py` y `requirements_dashboard.txt` a un repo de GitHub
   (NO subas la connection string).
2. En share.streamlit.io conectá el repo.
3. En **Settings -> Secrets** de la app pegá:
   ```
   AZURE_STORAGE_CONNECTION_STRING = "DefaultEndpointsProtocol=https;AccountName=...;EndpointSuffix=core.windows.net"
   ```
4. Deploy. La app se duerme por inactividad pero **despierta sola** al abrirla
   (unos segundos), sin reactivar nada — a diferencia del warehouse de Databricks.

## Notas
- El cache (`ttl=600`) evita releer Blob en cada interacción; refresca cada 10 min.
- `sample_gold/` son datos de tu Silver real para que pruebes el look antes de
  conectar Blob. En producción borralo o ignoralo.
