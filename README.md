# PUNTO COM — Sistema POS con Python

Aplicación web para una librería en Guatemala, construida con **Python, Flask y PostgreSQL**. La interfaz está en español y los importes se expresan en quetzales (Q).

## Requisitos

- Python 3.11 o posterior; instrucciones locales para Windows y despliegue Linux con Docker Compose.
- PostgreSQL 14 o posterior; pgAdmin es opcional para administrar la base.
- Acceso a Internet para cargar Bootstrap y Chart.js desde CDN.

## Instalación local en Windows

Abre una terminal PowerShell en la carpeta `puntocom_pos` y crea el entorno virtual:

```powershell
py -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt -c requirements.lock
Copy-Item .env.example .env
```

Edita `.env` con los datos de tu instalación. La plantilla usa `DB_NAME=puntocom_pos`, `DB_HOST=127.0.0.1` y `DB_PORT=5433`. Cambia el puerto si PostgreSQL escucha en otro, por ejemplo `5432`.

Genera una clave de sesión y copia el resultado en `SECRET_KEY` dentro de `.env`:

```powershell
.\venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

Para una instalación **nueva**, crea la base `puntocom_pos` y ejecuta, en este orden, `sql/01_esquema.sql` y `sql/02_datos_iniciales.sql`. En una base existente, revisa primero su estructura y sus datos: estos archivos son de instalación inicial y no constituyen una migración. No vuelvas a ejecutar el esquema sobre la base que ya utilizas.

Después de la instalación inicial, o al actualizar una instalación existente, crea y verifica un respaldo y aplica las migraciones antes de arrancar esta versión:

```powershell
.\venv\Scripts\python.exe -m deployment.respaldos --env-file .env backup --directory backups --verify-cleanup
.\venv\Scripts\python.exe -m flask --app app migrar-operacion
```

`migrar-operacion` utiliza la conexión configurada en `.env` y ejecuta las migraciones aditivas `04_roles_inventario.sql`, `05_ventas_pagos.sql` y `06_caja_compras.sql`, cada una dentro de su transacción. Puede repetirse y conserva registros e historial. `migrar-accesos` se mantiene para ejecutar únicamente la migración 04. La aplicación no migra automáticamente al arrancar. Consulta el [flujo de pagos y operación](docs/operacion.md) y la [guía de despliegue y recuperación](docs/despliegue.md).

En la instalación validada el **18 de octubre de 2026**, la migración 04 se aplicó correctamente a la base configurada y reutilizó los tres identificadores de roles existentes. Los pasos anteriores siguen siendo necesarios para otras instalaciones o bases nuevas.

Crea el administrador inicial y arranca el servidor local:

```powershell
.\venv\Scripts\python.exe -m flask --app app crear-admin
.\venv\Scripts\python.exe app.py
```

Abre <http://127.0.0.1:5000/login> e inicia sesión con las credenciales que acabas de establecer.

## Configuración

`.env.example` contiene únicamente valores de ejemplo. Mantén `.env` fuera del repositorio.

| Variable | Valor predeterminado | Uso |
| --- | --- | --- |
| `APP_ENV` | `development` | Admite `development`, `testing` y `production`. Con `production`, se exige una `SECRET_KEY` explícita. |
| `APP_DEBUG` | `false` | Activa la depuración solo si se habilita explícitamente en desarrollo o pruebas; se rechaza en producción. |
| `SECRET_KEY` | Generada temporalmente en desarrollo | Firma las sesiones y tokens CSRF. Define una clave estable en `.env`; sin ella, un reinicio invalida las sesiones. |
| `SESSION_COOKIE_SECURE` | `false` en desarrollo; `true` en producción | Restringe el envío de la cookie de sesión a HTTPS cuando está activada. |
| `DB_USER` | `postgres` | Usuario PostgreSQL. |
| `DB_PASSWORD` | Vacío | Contraseña PostgreSQL; configura la de tu instalación. |
| `DB_HOST` | `127.0.0.1` | Servidor PostgreSQL. |
| `DB_PORT` | `5433` | Puerto PostgreSQL. |
| `DB_NAME` | `puntocom_pos` | Nombre de la base de datos. |
| `TRUSTED_HOSTS` | Sin restricción en desarrollo | Obligatorio en producción: hosts separados por comas, sin esquema ni puerto. |
| `DB_CONNECT_TIMEOUT` | `5` | Límite de conexión PostgreSQL, entre 1 y 120 segundos. |
| `DB_SSLMODE` | `prefer` | Verificación TLS para la conexión PostgreSQL; usa `verify-full` y CA con servidor remoto. |

La aplicación utiliza `America/Guatemala` para sus fechas y fija esa zona horaria en las conexiones PostgreSQL que administra. Utiliza aislamiento `READ COMMITTED`, necesario para que una operación que espera un bloqueo consulte después las existencias ya confirmadas por la anterior. Estos ajustes se aplican a las conexiones de la aplicación y no modifican la configuración global del servidor PostgreSQL. Los ajustes de producción no sustituyen la configuración de un servidor WSGI, HTTPS, respaldos y controles operativos.

## Primer flujo de prueba

1. Inicia sesión como administrador y abre `Caja` con un fondo inicial.
2. En `Punto de Venta`, busca un producto con existencias, agrégalo al carrito y cobra.
3. Consulta el ticket, el historial de ventas y el inventario actualizado.
4. Registra una compra simple o una entrada manual de inventario.
5. Consulta reportes para el mismo período y descarga el Excel.

## Funciones y reglas principales

- Inicio de sesión con contraseñas protegidas con hash, sesiones, CSRF y permisos por rol.
- Productos, categorías, clientes, proveedores y administración de usuarios.
- Código interno automático para productos nuevos, por ejemplo `PC-000014`; se asigna al guardar y aparece en el mensaje de confirmación y el listado.
- Desactivación y reactivación de productos, categorías, clientes, proveedores y usuarios, reservadas al Administrador; eliminación definitiva solo de registros sin historial ni relaciones.
- Inventario calculado a partir de entradas y salidas, con historial y alertas de existencias bajas.
- Apertura y cierre de caja, ingresos y egresos de efectivo.
- POS con búsqueda por código, nombre o barras; cantidades y descuento monetario por renglón únicamente para Administrador.
- Cobro transaccional con validación de existencias y precios del servidor. Un pago en efectivo registra el ingreso en caja.
- Ticket imprimible de 80 mm. El navegador puede guardarlo como PDF.
- Compras de varios productos, con entradas y actualización de costo en una transacción, crédito y abonos con origen explícito del efectivo.
- Cobros con clave de operación para recuperar reintentos sin ventas duplicadas y pagos pendientes, confirmados o fallidos.
- Arqueo de caja con esperado, contado, diferencia y motivo, conservado después del cierre.
- Panel general para Administrador; historial y reportes de ventas propios para Cajero, con exportación Excel y comprobantes protegidos.
- Conteos físicos pendientes de aprobación administrativa; aprobación única y movimientos con responsable, motivo y fecha.
- Anulación administrativa de ventas, conservando comprobantes e historial de reversión.

Los reportes consideran ventas `COMPLETADA`. La pantalla y ambas direcciones de descarga (`/reportes/excel` y `/reportes/ventas/excel`) comparten la lógica y los filtros de fecha. Aceptan `desde`/`hasta` o sus alias `inicio`/`fin`; sin filtros usan los últimos 30 días en Guatemala. El intervalo debe estar ordenado y abarcar como máximo 366 días, incluyendo ambas fechas. El Excel incluye todas las ventas del período válido, sin el anterior recorte de 5,000 registros; si excede la capacidad de una hoja, solicita un período menor.

El indicador de inventario bajo cuenta todos los productos activos que alcanzaron su mínimo; el menú de Administrador y Bodeguero muestra una lista de hasta 20 de ellos.

## Roles y acceso

Los perfiles admitidos son Administrador, Cajero y Bodeguero. El rol y el estado se cargan desde PostgreSQL en cada solicitud protegida; una desactivación o cambio de rol afecta la siguiente solicitud de una sesión abierta. Las operaciones de escritura revalidan los permisos dentro de su transacción. Una operación sin permiso responde HTTP 403 con “No tienes permiso para realizar esta acción”.

| Perfil | Inicio y tareas permitidas |
| --- | --- |
| Administrador | Panel general; administración de productos, catálogos, usuarios, roles, costos, descuentos, compras y todas las ventas; movimientos y aprobación de ajustes. |
| Cajero | POS y caja propia; productos y existencias sin costos; consulta y creación de clientes; sus ventas, tickets, reportes y Excel. |
| Bodeguero | Inventario; consulta de productos y proveedores sin costos; entradas, salidas, conteos, solicitudes pendientes, movimientos, stock bajo y Excel de inventario. |

El Cajero no puede editar ni desactivar clientes, aunque los haya creado. El Bodeguero no puede modificar productos, categorías ni proveedores ni aprobar ajustes. Ocultar acciones se complementa con autorización del servidor, incluida la propiedad de cada comprobante y los filtros de exportación.

La [guía de control de acceso](documentacion/Control_acceso_roles.md) contiene la matriz completa, rutas, migración y pasos de prueba por perfil. La edición persistente de configuración general es la única función de la matriz aún pendiente; la página informativa existente queda protegida para Administrador.

## Demostración de 30 operaciones por roles

El **18 de octubre de 2026**, a las **01:52:07 de Guatemala**, se guardaron las 30 operaciones autorizadas en la base actual `puntocom_pos`: diez por perfil, con **35 comprobaciones adicionales correctas**. No se crearon usuarios ni roles. Antes de aplicar el lote se completó una simulación con reversión. Las existencias finales de los productos A, B y C quedaron en **111, 101 y 104 unidades**, respectivamente; los tres ajustes quedaron aprobado, rechazado y pendiente. Los dos turnos de fondo cero creados para la preparación quedaron cerrados y no se crearon movimientos de efectivo.

`demostracion_roles.py` permite ejecutar **10 operaciones como Administrador, 10 como Cajero y 10 como Bodeguero**, reutilizando un usuario activo existente de cada perfil. No crea cuentas ni roles, ni cambia contraseñas. La carga utiliza las rutas, formularios, permisos y tokens CSRF de la aplicación; asigna las sesiones de prueba desde el servidor, por lo que esta demostración no valida contraseñas. El inicio de sesión se comprueba por separado en la suite PostgreSQL.

| Perfil | Operaciones de la demostración |
| --- | --- |
| Administrador | Crear una categoría, un proveedor, tres productos y un cliente; modificar costo y precio de un producto; registrar una venta con descuento; aprobar un ajuste y rechazar otro. |
| Cajero | Registrar dos clientes y ocho ventas sin descuentos. |
| Bodeguero | Registrar cuatro entradas, tres salidas y tres conteos físicos. |

Los productos tienen códigos `PRUEBA-ROLES-A`, `PRUEBA-ROLES-B` y `PRUEBA-ROLES-C`. Categoría, proveedor y clientes también se identifican como PRUEBA. Un conteo queda aprobado, otro rechazado y el tercero pendiente, sin modificar existencias hasta su aprobación. La demostración incluye solicitudes directas a operaciones restringidas y comprobaciones de propiedad de ventas, tickets y exportaciones.

En una instalación que todavía no tenga este lote, ejecuta el comando en una terminal independiente del servidor web. Sin opciones, simula y revierte los registros; `--aplicar` conserva las operaciones ficticias en la base configurada en `.env`. En la base actual, donde ya se aplicó, ambos comandos rechazan una nueva carga para evitar duplicados:

```powershell
.\venv\Scripts\python.exe demostracion_roles.py
.\venv\Scripts\python.exe demostracion_roles.py --aplicar
```

**La carga aplicada aparece en los reportes habituales de ventas e inventario.** Sus nueve ventas utilizan TARJETA o TRANSFERENCIA, sin conexión bancaria y sin movimientos de efectivo. Si Administrador o Cajero ya tienen un turno abierto, se reutiliza conservando su saldo y estado. Si necesitan uno, la preparación abre un turno con fondo cero y lo cierra antes de confirmar el lote; esta preparación se documenta aparte de las 30 operaciones.

Todas las operaciones y comprobaciones se ejecutan dentro de una transacción exterior con puntos de guardado por solicitud. El lote se conserva únicamente si termina completo; ante un error se revierte. Una simulación o un fallo puede consumir números de secuencia de PostgreSQL. La carga rechaza una repetición si ya existen los códigos reservados y no borra automáticamente el historial de prueba.

La ejecución aplicada genera [Prueba_30_operaciones_roles.md](documentacion/Prueba_30_operaciones_roles.md) y [resultado_30_operaciones_roles.json](documentacion/resultado_30_operaciones_roles.json), con responsables, identificadores, tickets, existencias y comprobaciones. La simulación genera archivos con el sufijo `_simulacion`. Las 30 operaciones son una demostración representativa; la suite automatizada conserva la cobertura del resto de reglas.

## Eliminar, desactivar y reactivar

Las listas conservan registros activos e inactivos y muestran su estado. **Desactivar** conserva los datos y su historial; **Reactivar** permite volver a utilizarlos. Las solicitudes indican una acción explícita (`activar` o `desactivar`): repetir una solicitud no alterna el estado por accidente. Crear o modificar productos, categorías y proveedores, y editar o cambiar estados de clientes y usuarios, requiere Administrador.

**Eliminar** requiere administrador y borra definitivamente un registro únicamente cuando no tiene historial ni relaciones. La pantalla deshabilita el botón cuando detecta vínculos e indica el motivo al situar el cursor sobre él. El servidor vuelve a comprobar las relaciones dentro de la transacción antes de eliminar.

| Registro | Relaciones que impiden eliminar |
| --- | --- |
| Producto | Detalles de ventas o compras, movimientos y solicitudes de ajuste. |
| Categoría | Productos asociados, incluidos los inactivos. |
| Cliente | Ventas de cualquier estado. |
| Proveedor | Compras registradas. |
| Usuario | Turnos de caja, ventas, compras, movimientos, solicitudes, aprobaciones o anulaciones, incluido el historial cerrado. |

También se comprueban las claves foráneas adicionales que tenga la instalación, incluidas las que declaren `ON DELETE CASCADE`. La aplicación no elimina registros relacionados en cascada. Un usuario con historial puede desactivarse después de cerrar sus turnos de caja. Un Administrador no puede eliminar ni desactivar su cuenta actual; el último Administrador activo tampoco puede perder su rol. Consumidor Final (`CF`) no puede desactivarse ni eliminarse, y su identidad no puede cambiarse al editarlo.

El POS ofrece clientes activos; compras ofrece proveedores activos; los productos nuevos solo pueden asignarse a categorías activas. El servidor verifica esas condiciones de nuevo al guardar. Desactivar una categoría conserva sus productos y permite seguir vendiendo los productos que estén activos. Al editar un producto se puede conservar su categoría actual aunque esté inactiva, pero no asignar esa categoría a otro producto.

La eliminación bloquea la fila con `FOR UPDATE` y comprueba las referencias reales en `pg_catalog`. Las operaciones que usan un cliente, proveedor o categoría activa bloquean su fila con `FOR SHARE` mientras guardan, para coordinarse con una desactivación o eliminación simultánea. Los usuarios también se verifican activos en las operaciones protegidas.

## Estructura

| Archivo o carpeta | Responsabilidad |
| --- | --- |
| `app.py` | Aplicación Flask, rutas, autenticación y coordinación de las operaciones. |
| `configuracion.py` | Variables de entorno, conexión, cookies, depuración y zona horaria. |
| `datos.py` | Acceso SQL y utilidades de base de datos. |
| `permisos.py` | Matriz central, autorización reutilizable y revalidación transaccional de responsables. |
| `validaciones.py` | Reglas compartidas de entrada y normalización. |
| `ventas_servicio.py` | Cobro y anulación con responsable y permisos. |
| `caja_servicio.py` | Caja con bloqueos, saldo disponible y arqueos persistentes. |
| `compras_servicio.py` | Compras de varios artículos, pagos y saldo a proveedores. |
| `inventario_servicio.py` | Movimientos, conteos y aprobación o rechazo de ajustes. |
| `inventario_rutas.py` | Rutas y exportación de inventario protegidas. |
| `administracion_servicio.py` | Reglas de estado, eliminación sin cascadas, cambios de rol y protección administrativa. |
| `demostracion_roles.py` | Demostración transaccional de 30 operaciones por roles con usuarios existentes; simulación predeterminada y carga explícita con `--aplicar`. |
| `migraciones.py` | Ejecución explícita de migraciones idempotentes. |
| `reportes_excel.py` | Exportación común de ventas a Excel. |
| `templates/` y `static/` | Pantallas, scripts y estilos. |
| `tests/` | Pruebas automatizadas con `unittest`. |
| `tests/postgres/` | Ejecutor explícito, base desechable y pruebas de integración y concurrencia PostgreSQL. |
| `sql/` | Esquema, datos iniciales y migración `04_roles_inventario.sql` para accesos e inventario. |
| `documentacion/Logica_del_proyecto_PUNTO_COM.md` | Descripción funcional y técnica actualizada. |
| `documentacion/Control_acceso_roles.md` | Matriz completa, instalación y pruebas manuales de acceso por perfil. |
| `.env.example` | Plantilla pública de configuración, sin secretos. |
| `docs/operacion.md` | Estados de pago, reintentos, migración y flujos de caja/compras. |
| `docs/despliegue.md` | Servidor Linux, HTTPS, respaldo, recuperación y operación. |
| `deployment/` | Scripts de respaldo, restauración, instalación y permisos de producción. |

## Pruebas

La [validación de operación](docs/validacion_operacion.md) registra los resultados de esta versión y el respaldo verificado. Como antecedente, el **18 de octubre de 2026** aprobaron 90 pruebas aisladas y 104 PostgreSQL. Las pruebas nuevas cubren reintentos, estados de pago, arqueos y compras de varios artículos. Las seis pruebas JavaScript del POS requieren Node.js; sin Node se omiten explícitamente. CI prepara Python, Node y PostgreSQL.

Desde la carpeta del proyecto, ejecuta las pruebas aisladas:

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests
```

Este comando no ejecuta las pruebas de PostgreSQL. Las pruebas aisladas cubren validaciones, configuración, reportes, rutas HTTP, permisos de plantillas, cobros y reglas de inventario; algunas utilizan SQLite en memoria o sustituyen dependencias para verificar componentes.

Para comprobar la conexión PostgreSQL configurada en `.env`, sin crear una base ni ejecutar las pruebas:

```powershell
.\venv\Scripts\python.exe tests\postgres\ejecutar.py --solo-conexion
```

Para ejecutar la integración HTTP y las operaciones concurrentes con conexiones PostgreSQL reales:

```powershell
.\venv\Scripts\python.exe tests\postgres\ejecutar.py
```

El ejecutor lee la configuración privada sin imprimir contraseñas ni cadenas de conexión. El usuario configurado necesita permiso `CREATEDB` o ser superusuario para la ejecución completa. Crea una base `puntocom_test_<UUID>` desde `template0`, carga allí el esquema, las migraciones y datos exclusivos de prueba, y configura la aplicación para utilizarla. Las pruebas conservan CSRF. Los casos de autenticación e integración comprueban el formulario real de inicio de sesión; los casos del generador de demostración reutilizan sesiones asignadas desde el servidor y no validan contraseñas. Se verifican operaciones, importes, relaciones, inventario, caja, reportes, autorización por rol, ventas ajenas, bajas, ajustes pendientes, rollback y carreras entre transacciones independientes.

Al terminar, elimina únicamente la base desechable que creó: comprueba su nombre y una marca exclusiva antes de `DROP DATABASE ... WITH (FORCE)`. Esa operación cierra las conexiones de esa base temporal. **Este ejecutor** consulta la base del negocio en modo de solo lectura para inspeccionar conexión y metadatos; no inserta allí ventas, compras ni movimientos. La carga explícita `demostracion_roles.py --aplicar` es una operación independiente, autorizada para conservar la demostración en la base actual.

La comparación con la instalación comprueba presencia de tablas y columnas, tipos, nulabilidad, longitudes, precisión de importes y claves foráneas. No compara ni valida el contenido de negocio, ni constituye una comprobación completa de índices, restricciones, funciones o permisos. El resultado se guarda en `documentacion/resultado_postgresql.json`; un fallo de pruebas, una diferencia en el contrato o un error de infraestructura hace que el comando termine con código distinto de cero.

Estas pruebas no sustituyen la validación del despliegue, los respaldos ni una prueba de carga representativa de la operación real.

## Alcance y límites

La versión incluye preparación para despliegue. La instalación requiere `migrar-operacion` antes de iniciar; no migra automáticamente al arrancar. Docker, HTTPS, cron y la copia remota deben activarse y comprobarse en el servidor de destino.

- El ticket es un comprobante imprimible; no se ha agregado facturación electrónica FEL.
- Tarjeta y transferencia son medios de pago declarados, sin pasarela ni validación bancaria.
- La anulación administrativa conserva motivo, comprobante y movimientos de reversión; exige efectivo suficiente en el turno abierto del Administrador. No hay devoluciones parciales ni pago mixto.
- Las compras de varios artículos admiten crédito y abonos. Un pago en efectivo sale de caja solo cuando se elige ese origen; fondos externos no alteran caja. Las compras anteriores sin saldo registrado requieren conciliación.
- El margen mostrado es bruto estimado y no descuenta gastos generales.
- Los precios son finales; la tasa de IVA del detalle es informativa.
- Los ajustes se solicitan mediante conteos pendientes y se aplican una sola vez tras aprobación del Administrador, generando movimientos `ENTRADA` o `SALIDA`.
- La configuración general tiene una página informativa protegida; su edición persistente está pendiente.
- Se incluyen respaldo con snapshot y restauración verificable, despliegue Docker/HTTPS y plantilla de cron. Su activación, copia remota y comprobación en el servidor requieren configurar el destino. Continúan pendientes auditoría general, límite de intentos de acceso y pruebas de carga.

La documentación fuente está en [Markdown](documentacion/Logica_del_proyecto_PUNTO_COM.md). Su [PDF](documentacion/Logica_del_proyecto_PUNTO_COM.pdf) se genera a partir del Markdown; para actualizarlo después de editar el documento, ejecuta:

```powershell
.\venv\Scripts\python.exe documentacion\generar_pdf.py
```
