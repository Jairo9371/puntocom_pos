# Ventas, pagos, caja y compras

Los pagos se confirman por un responsable dentro del POS. No hay validación
bancaria ni pasarela conectada. Se conservan los permisos por rol existentes.

## Actualizar una instalación

Detén los cobros durante la actualización, conserva la versión anterior, crea
un respaldo verificado y aplica las migraciones antes de reiniciar la aplicación:

```powershell
.\venv\Scripts\python.exe -m deployment.respaldos --env-file .env backup --directory backups --verify-cleanup
.\venv\Scripts\python.exe -m flask --app app migrar-operacion
```

`pg_dump` y `pg_restore` deben estar en PATH o en la carpeta indicada por `PG_BIN`.
Conserva el `.dump` y su `.dump.manifest.json` con acceso privado. La verificación
compara columnas y conteos del mismo snapshot exportado y elimina únicamente su
base temporal. Nunca restaura sobre la base del negocio.

Las migraciones 04, 05 y 06 son aditivas e idempotentes. No ejecutes el esquema
inicial sobre una base existente. Las ventas históricas conservan su estado y
se consideran pagos registrados manualmente, sin inventar confirmaciones
bancarias, fechas o responsables. Los arqueos anteriores conservan sus datos;
las compras anteriores muestran saldo **Sin conciliar**.

## Código interno de productos

En Nuevo producto no es necesario escribir el código interno: se asigna al
guardar, por ejemplo `PC-000014`. La secuencia existente de identificadores y la
restricción única de PostgreSQL protegen las altas simultáneas y evitan códigos
ya ocupados por productos activos o inactivos. No se reserva un código al abrir
el formulario. Los saltos de numeración después de una operación fallida son
normales y los números grandes no se truncan.

Los productos anteriores conservan su código. Editar sin enviar ese campo
conserva el existente; las cargas anteriores con códigos explícitos siguen
siendo compatibles. El código de barras continúa siendo un campo independiente.
Esta mejora utiliza el esquema actual y no requiere otra migración.

## Cobros y reintentos

El POS genera una clave por cobro y guarda el carrito y la solicitud en la
pestaña. Si se pierde la respuesta, reintenta el mismo cobro: el servidor devuelve
su resultado original sin repetir ventas, stock o ingresos, incluso después de
cerrar el turno. La misma clave con otra solicitud devuelve HTTP 409.

No cambies el carrito cuando el resultado sea incierto. Si el servidor rechaza
la operación y confirma su reversión, puedes corregirla y reenviarla. Si vence
la sesión o CSRF, inicia sesión o recarga y recupera el intento guardado. Usa un
navegador actual con almacenamiento de sesión habilitado. Cerrar la pestaña
pierde la recuperación local: consulta el historial antes de crear otra venta.

`POST /pos/cobrar` exige `Idempotency-Key` o `clave_operacion` en JSON, de 16 a
100 caracteres alfanuméricos, guiones o guiones bajos. Si envías ambos deben
coincidir. Se mantienen cookies de sesión, CSRF y permisos. El servicio interno
conserva compatibilidad con llamadas antiguas sin clave; la ruta HTTP la exige.

## Estados de pago

| Pago | Venta | Efectos |
| --- | --- | --- |
| PENDIENTE | PENDIENTE | Conserva precios; no descuenta stock, no mueve efectivo ni cuenta como venta completada. |
| CONFIRMADO | COMPLETADA | Comprueba turno y stock, registra salidas e ingreso de efectivo cuando corresponde. |
| FALLIDO | FALLIDA | Cierra el pendiente con motivo, sin afectar stock ni caja. |

El efectivo exige confirmación al cobrar. Tarjeta y transferencia permiten
pendiente o confirmado manualmente. No entregues productos con pago pendiente:
no se reservan existencias y otro cajero puede venderlas antes de confirmar.

En Ventas y Ticket, el cajero resuelve sus pendientes y el Administrador puede
resolver cualquiera. Confirmar comprueba de nuevo cliente y producto activos,
stock y turno original abierto. Conserva el precio acordado y toma el costo
histórico al entregar inventario. Un pendiente de turno cerrado solo admite
cierre fallido; revisa el pago antes de registrar una venta nueva en otro turno.

Repetir una resolución no repite movimientos. Los estados finales no se pueden
intercambiar. Para revertir una venta completada usa la anulación administrativa
con motivo y efectivo suficiente para reembolsar. La venta ANULADA conserva el
pago originalmente registrado; no se ejecutan reembolsos bancarios.

Reportes, Excel y panel incluyen únicamente ventas COMPLETADA y usan la fecha
de confirmación. Las ventas históricas sin esa fecha conservan la original.

## Caja y compras

El cierre guarda esperado, contado, diferencia (`contado - esperado`) y motivo;
un faltante o sobrante exige explicación. Los egresos, pagos y reembolsos exigen
efectivo suficiente. El bloqueo del turno coordina cobros y cierres simultáneos.
El historial conserva el arqueo después del cierre.

Una compra admite hasta 80 productos distintos. Detalles, entradas, cambios de
costo y pago se guardan en una sola transacción; un fallo revierte todo. Las
claves también protegen compras y abonos contra reenvíos de formulario.

CRÉDITO deja el total pendiente para abonar después. El desglose muestra
artículos, pagos y saldo. Cada abono identifica responsable y método, sin exceder
la deuda. El efectivo exige elegir **Mi caja abierta** (egreso vinculado con
saldo suficiente) o **Fondos externos**. Tarjeta y transferencia no mueven caja.
Los formularios antiguos de un artículo sin clave ni origen conservan su
tratamiento previo de fondos externos.

Las compras antiguas sin saldo registrado requieren conciliación antes de
admitir abonos; no se infieren deudas a partir de registros incompletos.

## Verificación y servidor

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests
.\venv\Scripts\python.exe tests\postgres\ejecutar.py
```

Antes de migrar la instalación, puedes probar el esquema del repositorio con
`tests\postgres\ejecutar.py --sin-comparar-instalacion`. Las pruebas crean y
eliminan exclusivamente su base UUID; no migran el negocio. Sin esa opción se
compara además el esquema instalado y el comando falla si hay diferencias.

La [guía de despliegue](despliegue.md) contiene Docker Compose, HTTPS, permisos
limitados, respaldo diario con cron, restauración y copia fuera del servidor.
Cron es una plantilla para instalar y verificar en el servidor. `/health/live`
comprueba el proceso y `/health/ready` la base y las columnas requeridas.
