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

El proyecto incluye una base Flask para el futuro checkout de Custom Song. PayPal todavía no está conectado: las rutas actuales solo validan la configuración y calculan el precio del lado servidor.

Desde la raíz del proyecto, crea un entorno virtual e instala la dependencia:

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Copia `.env.example` a `.env` cuando llegue la integración de PayPal. `.env` no debe incluirse en Git y ningún secreto debe llegar al frontend.

Para ejecutar el servidor local Flask (que también puede servir los archivos estáticos existentes), usa:

```bash
python -m backend.app
```

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
