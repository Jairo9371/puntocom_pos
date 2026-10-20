"""Reglas y transacciones de inventario contra SQL real de SQLite.

SQLite no implementa los bloqueos PostgreSQL; el ejecutor PostgreSQL verifica
esa parte y los permisos de las rutas HTTP con conexiones independientes.
"""

import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from inventario_servicio import aprobar_ajuste, rechazar_ajuste, registrar_conteo, registrar_movimiento


class SQLSQLite:
    def __init__(self, session):
        self.session = session

    def execute(self, statement, params=None):
        sql = str(statement).replace(' FOR SHARE OF u', '').replace(' FOR UPDATE', '')
        return self.session.execute(text(sql), params or {})


ESQUEMA = (
    'CREATE TABLE rol (id_rol INTEGER PRIMARY KEY,nombre TEXT NOT NULL)',
    '''CREATE TABLE usuario (id_usuario INTEGER PRIMARY KEY,id_rol INTEGER NOT NULL
       REFERENCES rol(id_rol),estado BOOLEAN NOT NULL)''',
    '''CREATE TABLE producto (id_producto INTEGER PRIMARY KEY,costo INTEGER NOT NULL,
       estado BOOLEAN NOT NULL)''',
    '''CREATE TABLE movimiento_inventario (id_movimiento_inventario INTEGER PRIMARY KEY,
       id_producto INTEGER NOT NULL REFERENCES producto(id_producto),tipo TEXT NOT NULL,
       cantidad INTEGER NOT NULL CHECK(cantidad>0),costo_unitario INTEGER NOT NULL,
       motivo TEXT NOT NULL,id_usuario INTEGER REFERENCES usuario(id_usuario))''',
    '''CREATE VIEW vista_inventario AS SELECT p.id_producto,COALESCE(SUM(CASE
       WHEN mi.tipo='ENTRADA' THEN mi.cantidad ELSE -mi.cantidad END),0) AS existencia_actual
       FROM producto p LEFT JOIN movimiento_inventario mi USING(id_producto) GROUP BY p.id_producto''',
    '''CREATE TABLE solicitud_ajuste (id_solicitud INTEGER PRIMARY KEY,
       id_producto INTEGER NOT NULL REFERENCES producto(id_producto),
       id_solicitante INTEGER NOT NULL REFERENCES usuario(id_usuario),
       existencia_sistema INTEGER NOT NULL CHECK(existencia_sistema>=0),
       cantidad_contada INTEGER NOT NULL CHECK(cantidad_contada>=0),diferencia INTEGER NOT NULL,
       motivo TEXT NOT NULL,estado TEXT NOT NULL DEFAULT 'PENDIENTE',
       id_aprobador INTEGER REFERENCES usuario(id_usuario),fecha_resolucion TIMESTAMP,
       motivo_resolucion TEXT,id_movimiento INTEGER UNIQUE
       REFERENCES movimiento_inventario(id_movimiento_inventario))''',
)


class InventarioServicioTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite+pysqlite:///:memory:')
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql('PRAGMA foreign_keys=ON')
            for sentencia in ESQUEMA:
                conexion.exec_driver_sql(sentencia)
            conexion.exec_driver_sql("INSERT INTO rol VALUES (1,'Administrador'),(2,'Cajero'),(3,'Bodeguero')")
            conexion.exec_driver_sql('INSERT INTO usuario VALUES (1,1,TRUE),(2,2,TRUE),(3,3,TRUE)')
            conexion.exec_driver_sql('INSERT INTO producto VALUES (1,7,TRUE)')
            conexion.exec_driver_sql("INSERT INTO movimiento_inventario VALUES (1,1,'ENTRADA',10,7,'Saldo inicial',1)")
        self.session = Session(self.engine)
        self.sql = SQLSQLite(self.session)
        self.conteo = {'id_producto': '1', 'cantidad_contada': '8', 'motivo': 'Conteo general'}
        self.movimiento = {'id_producto': '1', 'tipo': 'SALIDA', 'cantidad': '3', 'motivo': 'Producto dañado'}

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def saldo(self):
        return self.session.scalar(text('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1'))

    def solicitud(self, aid):
        return self.session.execute(text('SELECT * FROM solicitud_ajuste WHERE id_solicitud=:id'), {'id': aid}).mappings().one()

    def test_conteo_pendiente_conserva_existencias_y_responsable_real(self):
        aid = registrar_conteo(self.sql, {**self.conteo, 'id_solicitante': '1'}, 3)
        self.session.commit()
        solicitud = self.solicitud(aid)
        self.assertEqual(self.saldo(), 10)
        self.assertEqual((solicitud['existencia_sistema'], solicitud['cantidad_contada'], solicitud['diferencia']), (10, 8, -2))
        self.assertEqual((solicitud['estado'], solicitud['id_solicitante']), ('PENDIENTE', 3))

    def test_aprobacion_aplica_diferencia_una_sola_vez_y_guarda_aprobador(self):
        aid = registrar_conteo(self.sql, self.conteo, 3)
        self.session.commit()
        mid = aprobar_ajuste(self.sql, aid, 1)
        self.session.commit()
        self.assertEqual(self.saldo(), 8)
        self.assertEqual((self.solicitud(aid)['estado'], self.solicitud(aid)['id_aprobador']), ('APROBADA', 1))
        movimiento = self.session.execute(text('SELECT * FROM movimiento_inventario WHERE id_movimiento_inventario=:id'), {'id': mid}).mappings().one()
        self.assertEqual((movimiento['tipo'], movimiento['cantidad'], movimiento['id_usuario']), ('SALIDA', 2, 1))
        with self.assertRaisesRegex(ValueError, 'ya fue resuelta'):
            aprobar_ajuste(self.sql, aid, 1)
        self.session.rollback()
        self.assertEqual(self.saldo(), 8)
        self.assertEqual(self.session.scalar(text('SELECT COUNT(*) FROM movimiento_inventario')), 2)

    def test_bodeguero_y_cajero_no_pueden_aprobar(self):
        aid = registrar_conteo(self.sql, self.conteo, 3)
        self.session.commit()
        for uid in (2, 3):
            with self.subTest(usuario=uid), self.assertRaises(PermissionError):
                aprobar_ajuste(self.sql, aid, uid)
            self.session.rollback()
        self.assertEqual(self.saldo(), 10)
        self.assertEqual(self.solicitud(aid)['estado'], 'PENDIENTE')

    def test_cajero_no_puede_contar_ni_mover_inventario(self):
        for operacion, datos in ((registrar_conteo, self.conteo), (registrar_movimiento, self.movimiento)):
            with self.subTest(operacion=operacion.__name__), self.assertRaises(PermissionError):
                operacion(self.sql, datos, 2)
            self.session.rollback()
        self.assertEqual(self.saldo(), 10)

    def test_cambio_de_existencias_exige_nuevo_conteo(self):
        aid = registrar_conteo(self.sql, self.conteo, 3)
        self.session.commit()
        registrar_movimiento(self.sql, self.movimiento, 3)
        self.session.commit()
        with self.assertRaisesRegex(ValueError, 'cambiaron después del conteo'):
            aprobar_ajuste(self.sql, aid, 1)
        self.session.rollback()
        self.assertEqual(self.saldo(), 7)
        self.assertEqual(self.solicitud(aid)['estado'], 'PENDIENTE')

    def test_rechazo_no_modifica_stock_y_no_permite_aprobacion_posterior(self):
        aid = registrar_conteo(self.sql, self.conteo, 3)
        self.session.commit()
        rechazar_ajuste(self.sql, aid, 1, 'Repetir conteo')
        self.session.commit()
        self.assertEqual(self.saldo(), 10)
        self.assertEqual(self.solicitud(aid)['motivo_resolucion'], 'Repetir conteo')
        with self.assertRaisesRegex(ValueError, 'ya fue resuelta'):
            aprobar_ajuste(self.sql, aid, 1)

    def test_conteo_cero_puede_aprobarse_sin_stock_negativo(self):
        aid = registrar_conteo(self.sql, {**self.conteo, 'cantidad_contada': '0'}, 3)
        aprobar_ajuste(self.sql, aid, 1)
        self.session.commit()
        self.assertEqual(self.saldo(), 0)

    def test_conteo_sin_diferencias_conserva_historial_sin_crear_movimiento(self):
        aid = registrar_conteo(self.sql, {**self.conteo, 'cantidad_contada': '10'}, 3)
        self.assertIsNone(aprobar_ajuste(self.sql, aid, 1))
        self.session.commit()
        self.assertEqual(self.solicitud(aid)['estado'], 'APROBADA')
        self.assertEqual(self.session.scalar(text('SELECT COUNT(*) FROM movimiento_inventario')), 1)

    def test_no_se_aceptan_conteos_negativos_fraccionarios_ni_fuera_de_rango(self):
        for cantidad in ('-1', '1.5', '2147483648'):
            with self.subTest(cantidad=cantidad), self.assertRaises(ValueError):
                registrar_conteo(self.sql, {**self.conteo, 'cantidad_contada': cantidad}, 3)
            self.session.rollback()
        self.assertEqual(self.session.scalar(text('SELECT COUNT(*) FROM solicitud_ajuste')), 0)

    def test_salida_superior_al_saldo_no_se_guarda(self):
        with self.assertRaisesRegex(ValueError, 'Existencia insuficiente'):
            registrar_movimiento(self.sql, {**self.movimiento, 'cantidad': '11'}, 3)
        self.session.rollback()
        self.assertEqual(self.saldo(), 10)

    def test_movimiento_usa_responsable_del_servidor(self):
        mid = registrar_movimiento(self.sql, {**self.movimiento, 'id_usuario': '1'}, 3)
        self.session.commit()
        responsable = self.session.scalar(text('SELECT id_usuario FROM movimiento_inventario WHERE id_movimiento_inventario=:id'), {'id': mid})
        self.assertEqual(responsable, 3)
        self.assertEqual(self.saldo(), 7)

    def test_revocacion_usuario_impide_operacion_en_servicio(self):
        self.session.execute(text('UPDATE usuario SET estado=FALSE WHERE id_usuario=3'))
        self.session.commit()
        with self.assertRaises(PermissionError):
            registrar_movimiento(self.sql, self.movimiento, 3)
        self.assertEqual(self.saldo(), 10)

    def test_rollback_revierte_movimiento_y_aprobacion_juntos(self):
        aid = registrar_conteo(self.sql, self.conteo, 3)
        self.session.commit()
        aprobar_ajuste(self.sql, aid, 1)
        self.assertEqual(self.saldo(), 8)
        self.session.rollback()
        self.assertEqual(self.saldo(), 10)
        self.assertEqual(self.solicitud(aid)['estado'], 'PENDIENTE')


if __name__ == '__main__':
    unittest.main()
