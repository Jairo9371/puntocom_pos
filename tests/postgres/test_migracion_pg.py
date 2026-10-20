"""Comprueba la migración solo en la base desechable creada por el ejecutor."""

import re
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from entorno_pg import CasoPostgres, TABLAS
from migraciones import TABLAS_ESTADO, aplicar_migracion_bajas


class MigracionCatalogosPostgres(CasoPostgres):
    def _verificar_base_desechable(self):
        """Defensa adicional antes de retirar columnas de la instalación de prueba."""
        self.assertRegex(self.engine.url.database, r'^puntocom_test_[a-f0-9]{32}$')
        with self.engine.connect() as conexion:
            marca = conexion.execute(text('''
                SELECT shobj_description(oid,'pg_database')
                FROM pg_database WHERE datname=current_database()
            ''')).scalar_one()
        self.assertIsInstance(marca, str)
        self.assertTrue(re.fullmatch(r'puntocom-pruebas:[a-f0-9]{32}', marca))

    def _historial(self):
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql('''
                INSERT INTO venta(id_cliente,id_usuario,id_turno,numero_ticket,metodo_pago,total)
                VALUES (1,2,1,'MIGRACION-VENTA-001','EFECTIVO',15.00)
            ''')
            conexion.exec_driver_sql('''
                INSERT INTO detalle_venta(id_venta,id_producto,cantidad,precio_unitario,costo_unitario_historico)
                VALUES (1,1,1,15.00,9.00)
            ''')
            conexion.exec_driver_sql('''
                INSERT INTO movimiento_caja(id_turno,tipo,concepto,monto)
                VALUES (1,'INGRESO','Venta anterior a la migración',15.00)
            ''')
            conexion.exec_driver_sql('''
                INSERT INTO compra(id_proveedor,id_usuario,numero_factura,metodo_pago,total)
                VALUES (1,4,'MIGRACION-COMPRA-001','EFECTIVO',22.50)
            ''')
            conexion.exec_driver_sql('''
                INSERT INTO detalle_compra(id_compra,id_producto,cantidad,costo_unitario)
                VALUES (1,2,3,7.50)
            ''')
            conexion.exec_driver_sql('''
                INSERT INTO movimiento_inventario(id_producto,tipo,cantidad,costo_unitario,motivo)
                VALUES (1,'SALIDA',1,9.00,'Venta anterior a la migración'),
                       (2,'ENTRADA',3,7.50,'Compra anterior a la migración')
            ''')

    def _datos(self, excluir_estados=False):
        with self.engine.connect() as conexion:
            resultado = {}
            for tabla in TABLAS:
                filas = [dict(fila) for fila in conexion.execute(
                    text(f'SELECT * FROM public.{tabla} ORDER BY 1')
                ).mappings()]
                if excluir_estados and tabla in TABLAS_ESTADO:
                    for fila in filas:
                        fila.pop('estado', None)
                resultado[tabla] = filas
            return resultado

    def test_agrega_estados_con_default_true_y_conserva_datos_e_historial(self):
        self._verificar_base_desechable()
        self._historial()
        antes = self._datos(excluir_estados=True)
        # Se restaura el esquema incluso si falla una comprobación intermedia.
        self.addCleanup(aplicar_migracion_bajas, self.engine)
        with self.engine.begin() as conexion:
            for tabla in TABLAS_ESTADO:
                conexion.execute(text(f'ALTER TABLE public.{tabla} DROP COLUMN estado'))

        self.assertEqual(aplicar_migracion_bajas(self.engine),
                         ['categoria.estado', 'cliente.estado', 'proveedor.estado'])
        self.assertEqual(self._datos(excluir_estados=True), antes)
        self.assertEqual(antes['venta'][0]['total'], Decimal('15.00'))
        self.assertEqual(antes['compra'][0]['total'], Decimal('22.50'))
        with self.engine.begin() as conexion:
            columnas = conexion.execute(text('''
                SELECT table_name,data_type,is_nullable,column_default
                FROM information_schema.columns
                WHERE table_schema='public' AND column_name='estado'
                  AND table_name IN ('categoria','cliente','proveedor')
                ORDER BY table_name
            ''')).mappings().all()
            self.assertEqual(len(columnas), 3)
            for columna in columnas:
                self.assertEqual((columna['data_type'], columna['is_nullable']), ('boolean', 'NO'))
                self.assertEqual(columna['column_default'], 'true')
            for tabla in TABLAS_ESTADO:
                self.assertEqual(conexion.execute(text(
                    f'SELECT COUNT(*) FROM public.{tabla} WHERE estado IS DISTINCT FROM TRUE'
                )).scalar_one(), 0)
            for tabla, campo, nombre in (
                ('categoria', 'nombre', 'Nueva después de migrar'),
                ('cliente', 'nombre_razon_social', 'Cliente después de migrar'),
                ('proveedor', 'razon_social', 'Proveedor después de migrar'),
            ):
                activo = conexion.execute(text(
                    f'INSERT INTO public.{tabla}({campo}) VALUES (:nombre) RETURNING estado'
                ), {'nombre': nombre}).scalar_one()
                self.assertTrue(activo)
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conexion:
                conexion.execute(text('UPDATE public.cliente SET estado=NULL WHERE id_cliente=1'))

    def test_repetir_migracion_no_cambia_datos_ni_reactiva_registros(self):
        self._verificar_base_desechable()
        self._historial()
        with self.engine.begin() as conexion:
            for tabla in TABLAS_ESTADO:
                conexion.execute(text(f'UPDATE public.{tabla} SET estado=FALSE'))
        antes = self._datos()
        self.assertEqual(aplicar_migracion_bajas(self.engine), [])
        self.assertEqual(aplicar_migracion_bajas(self.engine), [])
        self.assertEqual(self._datos(), antes)
