"""Informe de inicio de operación, exclusivamente de lectura y sin datos personales.

Uso: python -m deployment.revisar_datos --env-file .env
Devuelve 2 si encuentra inconsistencias, 1 si no pudo revisar y 0 en otro caso.
Los importes cero y las marcas de prueba son informativos; nunca elimina datos.
"""

import argparse
from datetime import datetime
import json
import sys

from configuracion import TZ
from deployment.respaldos import conectar, entorno_conexion


TABLAS = (
    'rol', 'usuario', 'turno_caja', 'movimiento_caja', 'cliente', 'categoria',
    'producto', 'proveedor', 'venta', 'detalle_venta', 'compra', 'detalle_compra',
    'movimiento_inventario', 'solicitud_ajuste', 'pago_compra',
)

# Las consultas proyectan exclusivamente identificadores técnicos. Los nombres,
# correos, documentos, direcciones, credenciales y textos de motivos no salen.
COMPROBACIONES = {
    'existencias_negativas': '''SELECT id_producto AS id FROM vista_inventario
        WHERE existencia_actual < 0 ORDER BY id_producto''',
    'ventas_total_distinto_detalle': '''SELECT v.id_venta AS id FROM venta v
        LEFT JOIN detalle_venta d USING(id_venta) GROUP BY v.id_venta,v.total
        HAVING v.total <> COALESCE(SUM(d.cantidad*d.precio_unitario-d.descuento),0)
        ORDER BY v.id_venta''',
    'compras_total_distinto_detalle': '''SELECT c.id_compra AS id FROM compra c
        LEFT JOIN detalle_compra d USING(id_compra) GROUP BY c.id_compra,c.total
        HAVING c.total <> COALESCE(SUM(d.cantidad*d.costo_unitario),0)
        ORDER BY c.id_compra''',
    'compras_saldo_distinto_abonos': '''SELECT c.id_compra AS id FROM compra c
        LEFT JOIN pago_compra p USING(id_compra)
        WHERE c.saldo_pendiente IS NOT NULL GROUP BY c.id_compra,c.total,c.saldo_pendiente
        HAVING c.saldo_pendiente <> c.total-COALESCE(SUM(p.monto),0)
        ORDER BY c.id_compra''',
    'usuarios_con_varias_cajas_abiertas': '''SELECT id_usuario AS id FROM turno_caja
        WHERE estado='ABIERTO' GROUP BY id_usuario HAVING COUNT(*)>1 ORDER BY id_usuario''',
}

INFORMATIVAS = {
    'productos_activos_sin_existencia': '''SELECT p.id_producto AS id FROM producto p
        JOIN vista_inventario v USING(id_producto) WHERE p.estado AND v.existencia_actual=0
        ORDER BY p.id_producto''',
    'productos_activos_bajo_minimo': '''SELECT p.id_producto AS id FROM producto p
        JOIN vista_inventario v USING(id_producto) WHERE p.estado AND v.existencia_actual<p.stock_minimo
        ORDER BY p.id_producto''',
    'productos_activos_precio_cero': '''SELECT id_producto AS id FROM producto
        WHERE estado AND precio_venta=0 ORDER BY id_producto''',
    'productos_activos_costo_cero': '''SELECT id_producto AS id FROM producto
        WHERE estado AND costo=0 ORDER BY id_producto''',
    'productos_activos_precio_menor_costo': '''SELECT id_producto AS id FROM producto
        WHERE estado AND precio_venta<costo ORDER BY id_producto''',
    'productos_activos_sin_codigo_barras': '''SELECT id_producto AS id FROM producto
        WHERE estado AND NULLIF(BTRIM(codigo_barras),'') IS NULL ORDER BY id_producto''',
    'compras_historicas_saldo_sin_conciliar': '''SELECT id_compra AS id FROM compra
        WHERE saldo_pendiente IS NULL ORDER BY id_compra''',
    'cajas_abiertas': '''SELECT id_turno AS id FROM turno_caja
        WHERE estado='ABIERTO' ORDER BY id_turno''',
    'ventas_pago_pendiente': '''SELECT id_venta AS id FROM venta
        WHERE estado_pago='PENDIENTE' ORDER BY id_venta''',
}


def _hallazgo(cursor, consulta):
    # El resultado siempre tiene un tamaño acotado aunque el catálogo sea grande.
    cursor.execute('SELECT COUNT(*) FROM (' + consulta + ') AS revision')
    cantidad = cursor.fetchone()[0]
    cursor.execute('SELECT id FROM (' + consulta + ') AS revision LIMIT 25')
    return {'cantidad': cantidad, 'primeros_ids': [fila[0] for fila in cursor.fetchall()]}


def revisar_datos(conexion):
    """Requiere conexión nueva; la transacción rechaza escrituras en PostgreSQL."""
    try:
        with conexion.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            cursor.execute("SET LOCAL statement_timeout='10s'")
            cursor.execute("SET LOCAL lock_timeout='2s'")
            cursor.execute("SELECT current_setting('transaction_read_only')")
            if cursor.fetchone()[0] != 'on':
                raise RuntimeError('No se confirmó el modo de solo lectura.')
            conteos = {}
            for tabla in TABLAS:
                # Los nombres son constantes del repositorio, no entrada del usuario.
                cursor.execute('SELECT COUNT(*) FROM ' + tabla)
                conteos[tabla] = cursor.fetchone()[0]
            inconsistencias = {nombre: _hallazgo(cursor, consulta)
                               for nombre, consulta in COMPROBACIONES.items()}
            informativas = {nombre: _hallazgo(cursor, consulta)
                            for nombre, consulta in INFORMATIVAS.items()}
            cursor.execute('''SELECT COUNT(*) FROM movimiento_inventario
                WHERE motivo ILIKE '%prueba%' OR motivo ILIKE '%demo%' ''')
            movimientos_con_marcas = cursor.fetchone()[0]
            return {
                'fecha_guatemala': datetime.now(TZ).isoformat(),
                'solo_lectura': True,
                'datos_personales_incluidos': False,
                'conteos': conteos,
                'inconsistencias': inconsistencias,
                'informacion_para_revision': informativas,
                'movimientos_con_palabras_prueba_o_demo': movimientos_con_marcas,
                'criterio_marcas': 'Coincidencia textual informativa; no clasifica ni elimina registros.',
                'requiere_revision': any(valor['cantidad'] for valor in inconsistencias.values()),
            }
    finally:
        # No confirma ni conserva una transacción, incluso cuando una consulta falla.
        conexion.rollback()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', required=True, help='Archivo privado con la conexión que se revisará.')
    argumentos = parser.parse_args(argv)
    try:
        entorno = entorno_conexion(argumentos.env_file)
        with conectar(entorno) as conexion:
            reporte = revisar_datos(conexion)
        print(json.dumps(reporte, ensure_ascii=False, indent=2))
        return 2 if reporte['requiere_revision'] else 0
    except Exception as error:
        # Los errores de conexión SQL pueden contener credenciales o datos privados.
        print('No se completó la revisión de datos (' + type(error).__name__ + ').', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
