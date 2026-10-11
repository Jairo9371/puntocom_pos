# PUNTO COM — Sistema POS con Python

Proyecto web para librería con **Python, Flask, PostgreSQL, Bootstrap y Chart.js**. Interfaz en español, moneda **Q**.

## Requisitos

- Windows con Python 3.11 o posterior.
- PostgreSQL 14 o posterior y pgAdmin.
- Acceso a Internet para cargar Bootstrap y Chart.js desde CDN.

## Instalación Windows (VS Code)

1. Descomprimir ZIP y abrir carpeta `punto_com_python` en VS Code.
2. Abrir terminal **CMD** (símbolo del sistema) y ejecutar:

   ```bat
   py -m venv venv
   venv\Scripts\activate.bat
   python -m pip install -r requirements.txt
   ```

3. Copiar `.env.example` con nombre `.env` y actualizar contraseña y otros datos PostgreSQL.
4. Generar una SECRET_KEY segura con:

   ```bat
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

   Copiar el valor dentro del archivo `.env`.
5. En pgAdmin seleccionar BD `punto_com_db` (que ya venías usando). **No vuelvas a crear las tablas si ya existen.** Ejecuta `sql/02_datos_iniciales.sql` para crear roles, cliente Consumidor Final y dos artículos demo. Si partiste de cero, primero crea la base `punto_com_db`, ejecuta `sql/01_esquema.sql` y luego `sql/02_datos_iniciales.sql`.
6. Registrar primer administrador (desde la carpeta del proyecto):

   ```bat
   flask --app app crear-admin
   ```

7. Ejecutar:

   ```bat
   python app.py
   ```

8. Abre http://127.0.0.1:5000/login y entra con el correo y contraseña que acabas de establecer.

## Primer flujo de prueba

1. Abrir sesión como administrador.
2. Abrir `Caja` e introducir el fondo inicial.
3. Entrar a `Punto de Venta`, buscar `DEMO001`, agregar al carrito y cobrar.
4. Ver el ticket imprimible, listado de ventas y stock actualizado.
5. Probar creación/edición de productos, entradas/salidas de inventario y nuevas compras.

## Qué incluye esta entrega

- Inicio de sesión con contraseñas hasheadas, sesiones y permisos básicos por rol.
- Dashboard con tarjetas y gráfico de ventas semanales.
- Catálogo de productos, crear, editar y activar/desactivar.
- Inventario derivado de `movimiento_inventario`, con registro de entradas/salidas e historial.
- CRUD inicial de categorías, clientes y proveedores (alta, consulta y edición).
- Caja: aperturas, cierres y movimientos; venta en efectivo crea ingreso de caja.
- POS con buscador por código/nombre/barras, carrito, cantidades, descuentos, efectivo, tarjeta o transferencia.
- Cobro atómico (venta, detalle y movimiento de stock), validación y bloqueo de existencias.
- Comprobante imprimible de 80 mm (usar Imprimir > Guardar como PDF si se necesita PDF).
- Compras simples (un producto por compra, con entrada automática al inventario).
- Historial de ventas, reportes con gráfico y exportación básica de ventas a Excel.
- Alta de usuarios y activación/desactivación (administradores).

## Limitaciones importantes: versión de desarrollo

- **No es un sistema listo para producción**: faltan pruebas integrales, copias de seguridad, auditoría de operaciones, despliegue HTTPS, límites de intentos de sesión, control tributario y seguridad operativa adicional.
- El ticket **no es factura electrónica FEL** y no contiene validación SAT; no usarlo como sustituto de factura legal.
- Los pagos tarjeta/transferencia se registran como declaración de medio de pago, **sin pasarela ni validación bancaria**.
- Aún no hay pago mixto, devolución/anulación de ventas, compras con varios renglones, carga de imágenes, importación masiva ni configuración persistente; quedan para siguientes versiones.
- El cuadro de caja registra efectivo; pagos con tarjeta/transferencia figuran en ventas y reportes, no como efectivo en caja.
- La cifra de ganancias es **margen bruto estimado**, no utilidad neta.
- Los precios se interpretan como precios finales. La tasa de IVA del detalle es informativa, el sistema no emite documentos fiscales.
- El estado `AJUSTE` está pendiente; el esquema actual solo admite `ENTRADA` y `SALIDA`.
- Las fechas de SQL dependen de zona horaria del servidor PostgreSQL; se recomienda configurarlo para Guatemala antes de producción.
- La base de datos anterior debe tener nombres/columnas idénticos a `sql/01_esquema.sql`. Si fueron alterados, reconciliar con el diagrama ER antes de ejecutar.

## Estructura

- `app.py`: rutas y lógica del servidor.
- `templates/`: pantallas y formularios.
- `static/style.css`: estilos.
- `sql/01_esquema.sql`: esquema nuevo (no ejecutar sobre BD existente).
- `sql/02_datos_iniciales.sql`: datos demo y roles.
- `.env.example`: plantilla de conexión.

Mantén `.env` fuera de repositorios y configura HTTPS, contraseñas robustas y respaldo periódico al poner el sistema en uso.
