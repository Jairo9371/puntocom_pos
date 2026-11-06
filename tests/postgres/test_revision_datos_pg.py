"""Auditoría de inicio en PostgreSQL, sin alterar la instalación del negocio."""

import json

from sqlalchemy import text

from deployment.revisar_datos import revisar_datos
from entorno_pg import CasoPostgres


class RevisionDatosPostgresTest(CasoPostgres):
    def revisar(self):
        with self.engine.raw_connection() as conexion:
            return revisar_datos(conexion)

    def test_revision_acotada_sin_datos_personales_y_sin_escrituras(self):
        antes = self.revisar()
        despues = self.revisar()
        self.assertTrue(antes['solo_lectura'])
        self.assertFalse(antes['requiere_revision'])
        self.assertFalse(antes['datos_personales_incluidos'])
        self.assertEqual(antes['conteos']['producto'], 2)
        self.assertEqual(antes['conteos'], despues['conteos'])
        self.assertEqual(antes['movimientos_con_palabras_prueba_o_demo'], 2)
        serializado = json.dumps(antes)
        for dato_privado in ('admin@example.test', 'Cajero Uno', 'Consumidor Final', 'Cuaderno Uno'):
            self.assertNotIn(dato_privado, serializado)

    def test_ceros_y_marcas_de_prueba_son_informativos(self):
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql('UPDATE producto SET precio_venta=0,costo=0 WHERE id_producto=1')
            conexion.exec_driver_sql('''INSERT INTO movimiento_inventario
                (id_producto,tipo,cantidad,costo_unitario,motivo)
                VALUES(1,'SALIDA',10,0,'Inventario prueba')''')
        reporte = self.revisar()
        self.assertFalse(reporte['requiere_revision'])
        for nombre in ('productos_activos_sin_existencia', 'productos_activos_precio_cero',
                       'productos_activos_costo_cero'):
            self.assertEqual(reporte['informacion_para_revision'][nombre],
                             {'cantidad': 1, 'primeros_ids': [1]})

    def test_invariantes_muestran_identificadores_para_revision_sin_corregir_datos(self):
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql('''INSERT INTO movimiento_inventario
                (id_producto,tipo,cantidad,costo_unitario,motivo)
                VALUES(1,'SALIDA',11,9,'Inconsistencia deliberada de prueba')''')
            conexion.exec_driver_sql('''INSERT INTO compra
                (id_proveedor,id_usuario,metodo_pago,total,saldo_pendiente)
                VALUES(1,1,'CRÉDITO',10,9)''')
        reporte = self.revisar()
        self.assertTrue(reporte['requiere_revision'])
        for nombre in ('existencias_negativas', 'compras_total_distinto_detalle',
                       'compras_saldo_distinto_abonos'):
            self.assertEqual(reporte['inconsistencias'][nombre], {'cantidad': 1, 'primeros_ids': [1]})
        with self.engine.connect() as conexion:
            stock = conexion.execute(text('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1')).scalar_one()
        self.assertEqual(stock, -1)
