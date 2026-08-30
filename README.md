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

Para un host WSGI Linux, el entrypoint es `backend.app:app`. El comando de producción previsto es:

```bash
gunicorn --workers 1 --bind 0.0.0.0:$PORT backend.app:app
```

`ORDER_DB_PATH` es opcional. Si se omite, el desarrollo local conserva `instance/orders.sqlite3`. Para una primera instancia de producción con SQLite, configúrala como una ruta del volumen persistente (por ejemplo, `/var/data/orders.sqlite3`) y usa un solo worker. El endpoint de proceso `GET /health` devuelve `{"status":"ok"}` y no consulta PayPal ni SQLite; una readiness que compruebe dependencias queda pendiente.

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

El endpoint `GET /api/paypal/orders/resolve?token=<paypal_order_id>` permite correlacionar un PayPal Order ID con el `local_order_id` local. Este endpoint solo realiza lookup en SQLite y no modifica estados ni consulta PayPal. **El token recibido del navegador NO es autoridad**: se valida sintácticamente, debe existir como `paypal_order_id` persistido en SQLite, y solo sirve para correlación. La verificación definitiva de pago ocurre durante Capture. La correlación server-side permite al frontend obtener el identificador local necesario para llamar al endpoint Capture.

El frontend todavía no está conectado al backend en esta iteración. La integración de return/cancel pages se realizará en una iteración posterior.
