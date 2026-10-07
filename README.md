# Carlos Acevedo Studio

Sitio estático multipágina construido con HTML, CSS y JavaScript. No requiere instalación de dependencias.

## Ejecutarlo localmente

Desde la raíz del proyecto, inicia un servidor HTTP estático con Python:

```bash
python -m http.server 8000
```

Después, abre [http://localhost:8000/](http://localhost:8000/) en el navegador.

Algunas funciones requieren conexión a Internet porque utilizan TidyCal y Formspree.

## Base local del backend

El proyecto incluye una base Flask para el checkout de Custom Song. Los endpoints backend para Create y Capture ya están implementados y usan SQLite durable para persistencia local. PayPal Sandbox/Live no está conectado al frontend todavía: las pruebas automatizadas usan clientes falsos/mock.

Desde la raíz del proyecto, crea un entorno virtual e instala la dependencia:

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Copia `.env.example` a `.env` cuando llegue la integración de PayPal. `.env` no debe incluirse en Git y ningún secreto debe llegar al frontend.

La variable `PUBLIC_SITE_BASE_URL` configura la URL base del sitio público (e.g., `http://127.0.0.1:8000`). Se usa para construir las URLs de retorno de PayPal (`return_url` y `cancel_url`).

Para ejecutar el servidor local Flask (que también puede servir los archivos estáticos existentes), usa:

```bash
python -m backend.app
```

## Preparación para hosting del backend

El runtime Python se fija en `.python-version` con la version validada por la suite de pruebas; Render debe usar ese archivo al crear el servicio.

Para un host WSGI Linux, el entrypoint es `backend.app:app`. El comando de producción previsto es:

```bash
gunicorn --workers 1 --bind 0.0.0.0:$PORT backend.app:app
```

`ORDER_DB_PATH` es opcional. Si se omite, el desarrollo local conserva `instance/orders.sqlite3`. Para una primera instancia de producción con SQLite, configúrala como una ruta del volumen persistente (por ejemplo, `/var/data/orders.sqlite3`) y usa un solo worker. El endpoint de proceso `GET /health` devuelve `{"status":"ok"}` y no consulta PayPal ni SQLite; una readiness que compruebe dependencias queda pendiente.

Netlify permanece como frontend. En la rama de staging `deploy/render-preview`, `netlify.toml` proxyea únicamente `/api/*` al backend Render para conservar fetch same-origin; `/paypal/return` y `/paypal/cancel` siguen siendo paginas estaticas del frontend. Antes de crear ordenes desde el Branch Deploy, configura `PUBLIC_SITE_BASE_URL` en Render con el origin HTTPS exacto que Netlify asigne a esa rama.

## Smoke tests manuales de PayPal Sandbox

Estas herramientas son exclusivamente de desarrollo para PayPal Sandbox; no son un flujo de producción. Nunca copies credenciales al repositorio ni compartas el Client Secret.

Con `PAYPAL_ENVIRONMENT=sandbox`, el runner solicita Client ID y Client Secret solo si no están disponibles como variables del proceso. El secreto se pide sin eco y no se guarda en archivos.

```bash
python -m backend.paypal_sandbox_smoke auth
python -m backend.paypal_sandbox_smoke create --solo none
python -m backend.paypal_sandbox_smoke create --solo guitar-solo
python -m backend.paypal_sandbox_smoke create --solo piano-solo
python -m backend.paypal_sandbox_smoke capture <ORDER_ID> --solo guitar-solo
```

`auth` confirma OAuth sin mostrar el access token. `create --solo` acepta únicamente configuraciones de solo cerradas; `pricing.py` determina el importe y el operador nunca introduce precio. Abre la URL de aprobación con un comprador Personal Sandbox, aprueba la misma orden y conserva su Order ID. Usa la misma opción `--solo` al capturar; el runner solo muestra `PAYMENT CONFIRMED` cuando los estados, importe y moneda esperados coinciden. El runner se niega a ejecutarse con `PAYPAL_ENVIRONMENT=live`.

El backend usa SQLite durable para persistencia de órdenes Custom Song; su archivo de desarrollo vive fuera de Git bajo `instance/`. Los endpoints Flask para Create y Capture ya están implementados y son pruebas automatizadas. El smoke runner Sandbox sigue siendo una herramienta separada para pruebas manuales.

## Backup SQLite local (Sandbox)

La primera herramienta de backup es local/test y solo acepta `sandbox`; no llama a PayPal ni opera Render. Requiere una DB existente y un directorio de destino distinto al de la DB:

```bash
python -m backend.sqlite_backup create --environment sandbox --source-db C:\ruta\temporal\orders.sqlite3 --destination-directory C:\ruta\temporal\backups
```

Genera una copia SQLite y un manifiesto JSON con checksum SHA-256. No es una herramienta de restore ni un mecanismo de almacenamiento externo.

El endpoint `POST /api/paypal/orders/resolve`, con body JSON `{"token":"<paypal_order_id>"}`, permite correlacionar un PayPal Order ID con el `local_order_id` local sin volver a poner el token en un query string. Este endpoint solo realiza lookup en SQLite y no modifica estados ni consulta PayPal. **El token recibido del navegador NO es autoridad**: se valida sintácticamente, debe existir como `paypal_order_id` persistido en SQLite, y solo sirve para correlación. La página de retorno conserva el token únicamente en memoria, elimina de inmediato `token` y `PayerID` de la URL mediante `history.replaceState`, y aplica `Referrer-Policy: no-referrer`. La verificación definitiva de pago ocurre durante Capture.

## Administración segura de órdenes CAPTURING

La CLI administrativa usa `ORDER_DB_PATH`. `list` e `inspect` abren SQLite en modo read-only y no requieren credenciales PayPal:

```bash
python -m backend.order_admin list --status CAPTURING --older-than 2m --limit 50
python -m backend.order_admin inspect <LOCAL_ORDER_ID>
```

Las salidas usan una referencia estable `local_<12 hex>` calculada como los primeros 12 caracteres de `SHA-256("local\0" + local_order_id)`. Para operar desde una alerta sin copiar ni mostrar el ID completo:

```bash
python -m backend.order_admin inspect --ref local_0123456789ab
python -m backend.order_admin reconcile --ref local_0123456789ab
```

La referencia debe ser exacta. La CLI rechaza cero coincidencias y colisiones; nunca muestra el ID completo.

Para una orden `CAPTURING`, `reconcile` consulta PayPal mediante Show Order y es dry-run por defecto. En ese caso requiere `PAYPAL_ENVIRONMENT`, `PAYPAL_CLIENT_ID` y `PAYPAL_CLIENT_SECRET` desde el entorno:

```bash
python -m backend.order_admin reconcile <LOCAL_ORDER_ID>
python -m backend.order_admin reconcile <LOCAL_ORDER_ID> --apply-paid
```

`--apply-paid` solo permite `CAPTURING -> PAID` cuando ya existe un `capture_request_id` y Show Order confirma estrictamente la misma orden, una captura `COMPLETED`, capture ID, importe y moneda. La CLI nunca ejecuta PayPal Capture, no genera request IDs, no ofrece `force`, SQL libre ni cambios arbitrarios de estado. Sus salidas usan referencias hash y excluyen `brief_json`, identificadores PayPal completos, tokens, secretos y payloads remotos.

## Observabilidad mínima de operación Live

El backend escribe una línea JSON por evento. `INFO` va a stdout y `WARNING`/`ERROR` a stderr. El formato común es:

```json
{"environment":"live","event":"capture_started","level":"INFO","local_order_ref":"local_0123456789ab","operation":"capture_order","outcome":"committed","schema_version":1,"service":"backend","source":"api","status_from":"PAYPAL_CREATED","status_to":"CAPTURING","timestamp":"2026-10-06T18:00:00+00:00"}
```

Los eventos implementados son `order_created_local`, `paypal_order_created`, `capture_started`, `capture_ambiguous`, `capture_reconciled`, `paid`, `failed`, `operational_error` y `stale_order_detected`. Los identificadores se convierten en refs separadas por tipo usando `SHA-256(tipo + "\0" + identificador)` y solo 12 caracteres hex; no se hashea PII. Los eventos admiten únicamente campos y valores cerrados. No incluyen brief, nombre/email/teléfono, IDs completos, tokens, `PayerID`, credenciales, headers, bodies, URLs, queries, IP, user agent, referer, SQL, payload PayPal ni excepciones crudas. Un fallo del logger se ignora y no modifica el resultado financiero.

Los `reason_code` admitidos son: `timeout`, `network`, `http_408`, `http_429`, `paypal_5xx`, `invalid_response`, `create_ambiguous`, `sqlite_error`, `configuration_error`, `show_failed`, `stale_warning` y `stale_critical`.

Los umbrales se configuran en segundos:

```text
ORDER_CAPTURING_WARNING_SECONDS=300
ORDER_CAPTURING_CRITICAL_SECONDS=1800
```

Deben ser enteros positivos y critical debe ser mayor que warning. Una configuración inválida impide arrancar de forma segura y no imprime valores ni secretos.

### Auditoría stale read-only

```bash
python -m backend.order_admin audit-stale
```

El comando abre SQLite en modo read-only, no llama PayPal/Capture y no cambia estados. Emite `stale_order_detected` solo para `CAPTURING`/`PENDING`; `PAYPAL_CREATED` viejo aparece únicamente en el summary. Clasifica:

- `CAPTURING`: warning desde 300 s y critical/manual review desde 1800 s;
- `PENDING`: attention desde 300 s y manual review desde 1800 s;
- `PAYPAL_CREATED`: probable checkout abandonado desde 24 h, solo informativo.

Exit codes de `audit-stale`:

- `0`: sin incidencias que requieran atención; un `PAYPAL_CREATED` abandonado por sí solo no eleva el exit code;
- `2`: warning/attention;
- `3`: critical/manual review;
- `4`: error de configuración, SQLite u operación.

La edad solo clasifica. Ninguna orden se marca `FAILED` o `CANCELLED` por antigüedad.

### Endpoint operacional stale

`POST /internal/audit-stale` ejecuta la misma auditoría read-only con autenticación independiente mediante `Authorization: Bearer <OPS_AUDIT_TOKEN>`. Está previsto para un futuro Render Cron a través de red privada; ese Cron aún no se configura.

El endpoint responde `200 {"status":"ok"}`, `200 {"status":"warning"}`, `409 {"status":"critical"}` o `500 {"status":"error"}`. No devuelve findings, IDs ni otros detalles operacionales; la investigación se realiza con `python -m backend.order_admin audit-stale`. Todas sus respuestas usan `Cache-Control: no-store`.

### Runbook

Regla central: **NUNCA hacer Capture manual “para probar”**.

**A. CAPTURING warning/critical**

1. Ejecutar `audit-stale` y copiar la `local_order_ref`.
2. Ejecutar `python -m backend.order_admin inspect --ref <SAFE_REF>`.
3. Ejecutar `python -m backend.order_admin reconcile --ref <SAFE_REF>` sin `--apply-paid`.
4. Solo si devuelve `eligible_for_apply_paid` y todos los checks son `true`, repetir con `--apply-paid`.
5. Si devuelve `retry_requires_existing_capture_request_id`, dejar el reintento al flujo normal de la API, que hace Show primero y conserva el request ID. Si devuelve `manual_review`, no mutar y escalar.

**B. Create ambiguo o PENDING viejo**

1. Inspeccionar la ref y confirmar `PENDING`.
2. No capturar ni marcar `FAILED` manualmente.
3. Como el cliente no recibió una approval URL fiable, conservar el registro y permitir un nuevo checkout solo después de confirmar que no hay evidencia de pago.
4. Si se repite, pausar nuevos checkouts y revisar conectividad/configuración PayPal.

**C. Error SQLite**

1. Pausar nuevos checkouts si fallan escrituras.
2. No reintentar Capture ni editar SQLite a mano.
3. Revisar montaje, permisos, espacio y backup de `ORDER_DB_PATH`.
4. Después de recuperar acceso, ejecutar `audit-stale` y reconciliar cada `CAPTURING` con Show-first.

**D. Show falla**

1. No ejecutar Capture ni aplicar `PAID`.
2. Revisar entorno/credenciales/disponibilidad sin imprimir secretos.
3. Repetir únicamente el dry-run `reconcile`; escalar si la orden es critical o vuelve a fallar.

**E. PayPal remoto COMPLETED y local no PAID**

1. Ejecutar dry-run `reconcile --ref <SAFE_REF>`.
2. Usar `--apply-paid` únicamente con `eligible_for_apply_paid` y todos los checks verdaderos.
3. Confirmar `action=paid`; una segunda ejecución debe ser idempotente.
4. Si la orden está `PAYPAL_CREATED` sin `capture_request_id`, no forzar SQL: escalar.

El frontend todavía no está conectado al backend en esta iteración. La integración de return/cancel pages se realizará en una iteración posterior.
