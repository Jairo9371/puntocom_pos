# Validación de la versión de operación

Comprobación local del **22 de octubre de 2026**, horario de Guatemala.

| Comprobación | Resultado |
| --- | --- |
| Pruebas aisladas, configuración, respaldos y JavaScript POS | 149 aprobadas; 1 comprobación de enlaces simbólicos omitida en Windows por requerir privilegios adicionales. Sin errores ni fallos. |
| Integración y concurrencia PostgreSQL | 150 aprobadas en 69.819 s, sin errores, fallos ni omisiones. |
| Contrato del esquema instalado | Sin diferencias respecto al esquema completo del repositorio. |
| Limpieza de pruebas | Base temporal eliminada. |
| Dependencias | `pip check` correcto. |
| Configuración e impresión | Guardado administrativo, validación, CSRF, permisos revocados, migración repetida, ticket de 58/80 mm y prueba sin crear ventas verificados. |
| Disponibilidad | Servidor local reiniciado con la versión nueva; `/health/live` y `/health/ready` responden HTTP 200. La ruta de prueba de impresión está cargada y exige sesión. |
| Respaldo diario Windows | Tarea registrada a las 02:17, zona de Guatemala, bajo la cuenta real. Primera ejecución desde el Programador: resultado 0, 573 filas restauradas y verificadas. Requiere sesión iniciada. |

Se probaron reintentos simultáneos con confirmación o rollback, conflicto de
claves, recuperación del carrito después de perder una respuesta, sesión/CSRF
vencidos, pestañas duplicadas y recuperación después de cerrar el turno. Los
casos JavaScript ejecutan el script real del POS en Node con DOM simulado; no
constituyen una prueba de extremo a extremo en un navegador real.

Las pruebas PostgreSQL comprueban además confirmaciones simultáneas, pagos
pendientes y fallidos sin movimientos, stock al confirmar, arqueos, compras con
varios productos, abonos y pagos desde caja, rollback, permisos y conservación
del historial. Mantienen los casos anteriores de roles, bajas y demostración.

## Antecedente: actualización local del 21 de octubre

Antes de actualizar se creó y restauró en una base temporal el respaldo
`backups/puntocom_20261022T000902Z_6a7fad2f.dump`. El nombre lleva fecha UTC.
La restauración verificó columnas y conteos: **167 filas**. La base de verificación
se eliminó. El dump y su manifiesto son privados y están excluidos de Git.

Se aplicaron las migraciones 04, 05 y 06 y se repitieron para comprobar su
idempotencia. Las **167 filas de las 14 tablas anteriores conservaron exactamente
sus columnas originales**. Se agregó el esquema nuevo sin cargar ventas,
compras o usuarios ficticios en el negocio.

Después de migrar se creó y verificó también
`backups/puntocom_20261022T000957Z_9b893780.dump`: 167 filas restauradas y base
temporal eliminada. La suite completa posterior comparó el esquema instalado
con el repositorio y no encontró diferencias.

## Límites de esta validación

Docker no está instalado en este equipo. Las imágenes, HTTPS y los límites de
recursos deben construirse y comprobarse en el servidor Linux de destino. No se
publicó el sistema ni se activaron cron, copia remota o monitoreo externo.

Los pagos son registros manuales, sin pasarela bancaria. La suite no sustituye
una prueba de carga ni el ensayo de corte al servidor con los cajeros.
Los pasos pendientes se encuentran en [despliegue.md](despliegue.md).

## Configuración y respaldo automático del 22 de octubre

Antes de aplicar la migración 07 se creó y restauró el respaldo
`backups/puntocom_20261022T235256Z_335b6e7a.dump`, con **572 filas**.
La comparación de todas las filas y columnas de las 15 tablas anteriores,
mediante huellas calculadas sin mostrar los valores, fue idéntica antes y después
de la migración. Se conservaron ventas, compras, inventario, usuarios y caja.
La única fila añadida corresponde a los valores predeterminados de
`configuracion_negocio`. El usuario pidió conservar toda la información actual.

El Administrador puede guardar los datos del negocio y el ancho de 58 u 80 mm
desde **Configuración**. El ticket usa el encabezado y mensaje guardados.
**Probar impresión** genera un comprobante marcado como prueba y no registra
ventas, inventario ni efectivo. La aceptación física sigue pendiente.

Se ejecutó el runner diario después de migrar y se probó también desde la tarea
real de Windows. La segunda ejecución creó y restauró
`backups/puntocom_20261022T235600Z_a8daa37d.dump`, con **573 filas**, y eliminó
la base temporal de verificación. El Programador devolvió **0**. La tarea
**PUNTO COM - respaldo diario** usa `pythonw.exe` del entorno virtual, sin ventana,
a las **02:17, hora de Guatemala**. Necesita que la cuenta tenga sesión iniciada;
si el equipo no estuvo disponible, puede ejecutarse cuando vuelva a estarlo.
Revisa `backups/respaldos.log`, `backups/ultimo_respaldo_verificado.json` y el
último resultado del Programador. Estos archivos y los respaldos son privados.
No se borran copias anteriores. Falta elegir y comprobar el destino de la copia
fuera del equipo.

La construcción y comprobación de imágenes quedó preparada como un trabajo de
GitHub Actions; todavía no se ejecutó ese trabajo ni se publicó el sistema.

## Jornada y revisión de datos del 22 de octubre de 2026

Se agregó `tests/postgres/test_jornada_pg.py`, una jornada encadenada que utiliza
los formularios de login, los permisos y CSRF reales de Flask contra PostgreSQL
en una base desechable. La ejecución focalizada aprobó **1 caso en 0.961 s** y
eliminó esa base. La prueba enlaza estas operaciones y comprueba sus saldos e
inventarios después de cada etapa:

1. Apertura de caja del administrador y del cajero con **Q 0.00**.
2. Compra de dos productos a crédito y dos abonos por transferencia; los
   reenvíos recuperan la compra y los pagos originales.
3. Descuento permitido al administrador, rechazado al cajero y visible en el
   ticket con subtotal, descuento y total.
4. Dos cobros de efectivo consecutivos con importes y cambios distintos;
   reintento del segundo sin duplicarlo y rechazo de una clave reutilizada
   con otro importe.
5. Transferencia pendiente que se confirma una sola vez y otra que queda
   fallida, sin descontar existencias por el pago fallido.
6. Anulación administrativa con devolución de inventario y efectivo una sola
   vez, y cierres con arqueo: administrador Q 0.00, cajero esperado Q 45.00,
   contado Q 44.00, diferencia -Q 1.00 y motivo registrado.
7. Rechazo de un cobro nuevo con caja cerrada y recuperación de un cobro
   anterior después del cierre.

La revisión reutilizable se ejecuta sin modificar datos:

```powershell
.\venv\Scripts\python.exe -m deployment.revisar_datos --env-file .env
```

PostgreSQL impone una transacción **REPEATABLE READ, READ ONLY** y al terminar
se hace rollback. La salida contiene conteos e identificadores técnicos; omite
nombres, correos, documentos, direcciones y credenciales. Devuelve código 0 si
las comprobaciones son consistentes, 2 si hay inconsistencias para revisar y 1
si no pudo completar la lectura. Los precios o existencias cero son información
para revisión, sin clasificarlos automáticamente como errores.

La instalación local revisada a las **17:53, horario de Guatemala**, conservó
sus datos. Se contaron **572 filas en las 15 tablas revisadas**, incluidos 15
productos, 15 categorías, 37 ventas y 6 compras. Las cinco comprobaciones no
encontraron existencias negativas, diferencias entre totales y detalles de
ventas o compras, diferencias entre saldos y abonos de compras nuevas, ni varios
turnos abiertos para un mismo usuario. Esto verifica coherencia interna; el
inventario físico y los precios comerciales requieren revisión del negocio.

| Información encontrada | Tratamiento |
| --- | --- |
| Productos 18, 19 y 20 sin código de barras | Se conservan; pueden venderse con su código interno. Revisar el código del fabricante si se utilizará lector. |
| Compras históricas 1 y 2 con saldo sin conciliar | Se conservan; su deuda debe confirmarse con los comprobantes antes de registrar abonos. |
| Turno 10 abierto | Se conserva; el responsable debe realizar el arqueo y cierre normal cuando corresponda. |
| 10 movimientos con las palabras «prueba» o «demo» | Coincidencia textual informativa. El usuario decidió conservar todos los datos; no se clasificaron ni eliminaron registros. |

El auditor también aprobó **3 casos PostgreSQL en 0.488 s**: lectura sin datos
personales ni modificaciones, ceros informativos y detección de inconsistencias
deliberadas dentro de su base desechable, que se eliminó al terminar.

### Aceptación con equipos reales

La jornada automatizada usa el cliente HTTP de Flask; falta completar el ensayo
en el navegador con quienes operarán el negocio y con los equipos físicos.
Registra responsable, fecha, resultado y observación al realizar cada paso:

| Paso | Resultado pendiente de comprobar |
| --- | --- |
| Lector | Leer el código de un producto real, confirmar que se selecciona el producto correcto y que la cantidad coincide. |
| Impresora | Imprimir un ticket con descuento, comprobar ancho, márgenes, importes y legibilidad. |
| Navegador y sesión | Cerrar sesión, entrar como cajero y comprobar la recuperación de una respuesta incierta desde la misma pestaña. |
| Inventario físico | Comparar existencias de una muestra de productos y confirmar precios y costos antes del primer día de operación. |
| Cierre de jornada | Contar efectivo real y comprobar esperado, contado, diferencia y motivo. |

Todavía no hay servidor de destino. La instalación de HTTPS, el arranque después
de reiniciar, los respaldos programados y la copia externa deben verificarse
allí antes de publicar el POS.
