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
