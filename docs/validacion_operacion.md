# Validación de la versión de operación

Comprobación local del **21 de octubre de 2026**, horario de Guatemala.

| Comprobación | Resultado |
| --- | --- |
| Pruebas aisladas, configuración, respaldos y JavaScript POS | 122 aprobadas, sin errores, fallos ni omisiones. |
| Integración y concurrencia PostgreSQL | 131 aprobadas en 57.847 s, sin errores, fallos ni omisiones. |
| Contrato del esquema instalado | Sin diferencias respecto al esquema completo del repositorio. |
| Limpieza de pruebas | Base temporal eliminada. |
| Dependencias | `pip check` correcto. |
| Rutas locales | `/login`, `/health/live` y `/health/ready` responden HTTP 200. |

Se probaron reintentos simultáneos con confirmación o rollback, conflicto de
claves, recuperación del carrito después de perder una respuesta, sesión/CSRF
vencidos, pestañas duplicadas y recuperación después de cerrar el turno. Los
casos JavaScript ejecutan el script real del POS en Node con DOM simulado; no
constituyen una prueba de extremo a extremo en un navegador real.

Las pruebas PostgreSQL comprueban además confirmaciones simultáneas, pagos
pendientes y fallidos sin movimientos, stock al confirmar, arqueos, compras con
varios productos, abonos y pagos desde caja, rollback, permisos y conservación
del historial. Mantienen los casos anteriores de roles, bajas y demostración.

## Actualización de la instalación local

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
