"""Pagos y reintentos contra PostgreSQL real, exclusivamente en base desechable."""

import copy
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Event
from time import monotonic

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from entorno_pg import CasoPostgres
from ventas_servicio import IdempotenciaError, anular_venta, registrar_venta, resolver_pago


class PagosIdempotenciaPostgresTest(CasoPostgres):
    ESPERA = 6

    def solicitud(self, clave='cobro-prueba-00000000000000000001', **cambios):
        return dict(cliente_id=1, metodo='EFECTIVO', recibido='100.00',
                    items=[{'id': 1, 'cantidad': 2}], clave_operacion=clave, **cambios)

    def escalar(self, sql, **params):
        with self.engine.connect() as conexion:
            return conexion.execute(text(sql), params).scalar_one()

    def vender(self, data, usuario=2):
        with Session(self.engine) as session, session.begin():
            return registrar_venta(session, data, usuario)

    def resolver(self, vid, pago='CONFIRMADO', usuario=2, motivo=''):
        with Session(self.engine) as session, session.begin():
            return resolver_pago(session, vid, usuario, pago, motivo)

    def pendiente(self):
        data = self.solicitud()
        data.update(metodo='TRANSFERENCIA', estado_pago='PENDIENTE', recibido='0')
        return data, self.vender(data)

    def retenida(self, operacion, proceso, preparada=None, liberar=None, rollback=False):
        with Session(self.engine) as session:
            proceso['pid'] = session.execute(text('SELECT pg_backend_pid()')).scalar_one()
            proceso['listo'].set()
            try:
                resultado = operacion(session)
                if preparada:
                    preparada.set()
                if liberar and not liberar.wait(self.ESPERA):
                    raise AssertionError('No se liberó la transacción de prueba.')
                if rollback:
                    session.rollback()
                else:
                    session.commit()
                return resultado
            except BaseException:
                session.rollback()
                raise

    def esperar(self, senal, futuro):
        if not senal.wait(self.ESPERA):
            if futuro.done():
                futuro.result()
            self.fail('El trabajador no llegó al punto de coordinación.')

    def esperar_bloqueo(self, segunda, primera):
        limite = monotonic() + 3
        pausa = Event()
        with self.engine.connect() as conexion:
            while monotonic() < limite:
                if primera in conexion.execute(text('SELECT pg_blocking_pids(:p)'), {'p': segunda}).scalar_one():
                    return
                pausa.wait(0.02)
        self.fail('No se observó el bloqueo real entre las operaciones.')

    def carrera(self, primera, segunda, rollback=False, error_segunda=None):
        p1, p2 = {'listo': Event()}, {'listo': Event()}
        preparada, liberar = Event(), Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(self.retenida, primera, p1, preparada, liberar, rollback)
            try:
                self.esperar(preparada, f1)
                f2 = pool.submit(self.retenida, segunda, p2)
                self.esperar(p2['listo'], f2)
                self.esperar_bloqueo(p2['pid'], p1['pid'])
            finally:
                liberar.set()
            r1 = f1.result(timeout=self.ESPERA)
            if error_segunda:
                with self.assertRaises(error_segunda):
                    f2.result(timeout=self.ESPERA)
                return r1, None
            return r1, f2.result(timeout=self.ESPERA)

    def verificar_unica(self):
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM venta'), 1)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM detalle_venta'), 1)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM movimiento_inventario WHERE tipo='SALIDA'"), 1)
        self.assertEqual(self.escalar('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1'), 8)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM movimiento_caja'), 1)
        self.assertEqual(self.escalar('SELECT SUM(monto) FROM movimiento_caja'), Decimal('30.00'))

    def test_misma_clave_simultanea_recupera_misma_respuesta_despues_commit(self):
        solicitud = self.solicitud()
        operacion = lambda s: registrar_venta(s, solicitud, 2)
        primera, repetida = self.carrera(operacion, operacion)
        self.assertEqual(primera, repetida)
        self.assertEqual(repetida['cambio'], '70.00')
        self.verificar_unica()

    def test_misma_clave_simultanea_tras_rollback_solo_crea_segunda_venta(self):
        solicitud = self.solicitud()
        operacion = lambda s: registrar_venta(s, solicitud, 2)
        revertida, confirmada = self.carrera(operacion, operacion, rollback=True)
        self.assertNotEqual(revertida['id_venta'], confirmada['id_venta'])
        self.assertEqual(self.escalar('SELECT id_venta FROM venta'), confirmada['id_venta'])
        self.assertEqual(self.vender(solicitud), confirmada)
        self.verificar_unica()

    def test_payload_distinto_misma_clave_en_carrera_es_conflicto(self):
        original = self.solicitud()
        distinta = dict(original, recibido='200.00')
        self.carrera(lambda s: registrar_venta(s, original, 2),
                     lambda s: registrar_venta(s, distinta, 2), error_segunda=IdempotenciaError)
        self.verificar_unica()

    def test_confirmaciones_simultaneas_solo_entregan_stock_una_vez(self):
        data, venta = self.pendiente()
        operacion = lambda s: resolver_pago(s, venta['id_venta'], 2, 'CONFIRMADO')
        primera, repetida = self.carrera(operacion, operacion)
        self.assertEqual(primera, repetida)
        self.assertEqual(primera['estado'], 'COMPLETADA')
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM movimiento_inventario WHERE tipo='SALIDA'"), 1)
        self.assertEqual(self.escalar('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1'), 8)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM movimiento_caja'), 0)
        self.assertEqual(self.vender(data), venta)
        with self.assertRaisesRegex(ValueError, 'definitivo'):
            self.resolver(venta['id_venta'], 'FALLIDO', motivo='No llegó')

    def test_dos_pendientes_compiten_por_stock_al_confirmar(self):
        data, _ = self.pendiente()
        # El primer pendiente se creó por 2 y el segundo por 7; un tercero
        # compite por 7 también. Ningún pendiente reserva inventario.
        data.update(items=[{'id': 1, 'cantidad': 7}], clave_operacion='pendiente-000000000000000000002')
        primera = self.vender(data)
        data2 = copy.deepcopy(data)
        data2['clave_operacion'] = 'pendiente-000000000000000000003'
        segunda = self.vender(data2, usuario=3)
        self.assertEqual(self.escalar('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1'), 10)
        self.carrera(lambda s: resolver_pago(s, primera['id_venta'], 2, 'CONFIRMADO'),
                     lambda s: resolver_pago(s, segunda['id_venta'], 3, 'CONFIRMADO'), error_segunda=ValueError)
        self.assertEqual(self.escalar('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1'), 3)
        self.assertEqual(self.escalar('SELECT estado_pago FROM venta WHERE id_venta=:v', v=segunda['id_venta']), 'PENDIENTE')

    def test_fallo_transaccional_confirmacion_revierte_stock_costo_y_pago(self):
        data, venta = self.pendiente()
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql('UPDATE producto SET costo=11 WHERE id_producto=1')
            conexion.exec_driver_sql('''CREATE FUNCTION fallo_pago_prueba() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'Fallo controlado de pago' USING ERRCODE='23514'; END $$''')
            conexion.exec_driver_sql('''CREATE TRIGGER fallo_pago_prueba BEFORE UPDATE OF estado_pago ON venta
                FOR EACH ROW EXECUTE FUNCTION fallo_pago_prueba()''')
        try:
            with self.assertRaises(IntegrityError):
                self.resolver(venta['id_venta'])
            self.assertEqual(self.escalar('SELECT estado_pago FROM venta'), 'PENDIENTE')
            self.assertEqual(self.escalar('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1'), 10)
            self.assertEqual(self.escalar('SELECT costo_unitario_historico FROM detalle_venta'), Decimal('9'))
            self.assertEqual(self.escalar('SELECT COUNT(*) FROM movimiento_caja'), 0)
            self.assertEqual(self.vender(data), venta)
        finally:
            with self.engine.begin() as conexion:
                conexion.exec_driver_sql('DROP TRIGGER fallo_pago_prueba ON venta')
                conexion.exec_driver_sql('DROP FUNCTION fallo_pago_prueba()')
        self.assertEqual(self.resolver(venta['id_venta'])['estado_pago'], 'CONFIRMADO')

    def test_confirmacion_revalida_propiedad_roles_turno_y_fallido_no_mueve_stock(self):
        _data, venta = self.pendiente()
        with self.assertRaises(PermissionError):
            self.resolver(venta['id_venta'], usuario=3)
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql('UPDATE usuario SET id_rol=3 WHERE id_usuario=2')
        with self.assertRaises(PermissionError):
            self.resolver(venta['id_venta'])
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql('UPDATE usuario SET id_rol=2 WHERE id_usuario=2')
            conexion.exec_driver_sql("UPDATE turno_caja SET estado='CERRADO' WHERE id_turno=1")
        with self.assertRaisesRegex(ValueError, 'turno original'):
            self.resolver(venta['id_venta'])
        final = self.resolver(venta['id_venta'], 'FALLIDO', motivo='Transferencia no recibida')
        self.assertEqual(final['estado'], 'FALLIDA')
        self.assertEqual(self.resolver(venta['id_venta'], 'FALLIDO', motivo='Reintento'), final)
        with self.assertRaises(ValueError):
            self.resolver(venta['id_venta'])
        self.assertEqual(self.escalar('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1'), 10)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM movimiento_caja'), 0)

    def test_pendiente_se_reporta_solo_al_confirmar_y_con_fecha_de_confirmacion(self):
        _data, venta = self.pendiente()
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql("UPDATE venta SET fecha_hora=CURRENT_DATE-INTERVAL '1 day'")
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM venta WHERE estado='COMPLETADA'"), 0)
        self.resolver(venta['id_venta'])
        self.assertEqual(self.escalar("""SELECT COUNT(*) FROM venta WHERE estado='COMPLETADA'
            AND COALESCE(fecha_confirmacion,fecha_hora)::date=CURRENT_DATE"""), 1)
        self.assertEqual(self.escalar("""SELECT COUNT(*) FROM venta WHERE estado='COMPLETADA'
            AND COALESCE(fecha_confirmacion,fecha_hora)::date=CURRENT_DATE-1"""), 0)

    def test_anulacion_sin_efectivo_disponible_revierte_todos_los_movimientos(self):
        venta = self.vender(self.solicitud())
        # El administrador usa su propia caja para el reembolso.
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql('INSERT INTO turno_caja(id_usuario,fondo_inicial) VALUES(1,0)')
        with self.assertRaisesRegex(ValueError, 'Efectivo insuficiente'):
            with Session(self.engine) as session, session.begin():
                anular_venta(session, venta['id_venta'], 1, 'Devolución')
        self.assertEqual(self.escalar('SELECT estado FROM venta'), 'COMPLETADA')
        self.assertEqual(self.escalar('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1'), 8)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM movimiento_caja WHERE tipo='EGRESO'"), 0)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM movimiento_inventario WHERE tipo='ENTRADA'"), 2)

    def test_http_exige_clave_y_recupera_venta_sin_duplicar_y_conflicto_409(self):
        with self.client.session_transaction() as session:
            session['_user_id'], session['_fresh'] = '2', True
        html = self.client.get('/pos').get_data(as_text=True)
        token = re.search(r'data-csrf-token="([^"]+)"', html).group(1)
        data = self.solicitud()
        clave = data.pop('clave_operacion')
        headers = {'X-CSRFToken': token}
        self.assertEqual(self.client.post('/pos/cobrar', json=data, headers=headers).status_code, 400)
        headers['Idempotency-Key'] = clave
        original = self.client.post('/pos/cobrar', json=data, headers=headers)
        repetida = self.client.post('/pos/cobrar', json=data, headers=headers)
        self.assertEqual(original.status_code, 200)
        self.assertEqual(original.json, repetida.json)
        conflicto = self.client.post('/pos/cobrar', json=dict(data, recibido='200.00'), headers=headers)
        self.assertEqual(conflicto.status_code, 409)
        self.verificar_unica()

    def test_migracion_repetida_conserva_ventas_historicas_sin_inventar_verificacion(self):
        self.assertRegex(self.engine.url.database, r'^puntocom_test_[a-f0-9]{32}$')
        esquema = 'pagos_legacy_' + uuid.uuid4().hex
        script = (Path(__file__).resolve().parents[2] / 'sql' / '05_ventas_pagos.sql').read_text(encoding='utf-8')
        with self.engine.connect() as conexion:
            try:
                conexion.exec_driver_sql(f'CREATE SCHEMA {esquema}')
                conexion.exec_driver_sql(f'SET search_path TO {esquema}')
                conexion.exec_driver_sql('CREATE TABLE usuario(id_usuario INTEGER PRIMARY KEY)')
                conexion.exec_driver_sql('''CREATE TABLE venta(id_venta INTEGER PRIMARY KEY,
                    id_usuario INTEGER REFERENCES usuario(id_usuario),numero_ticket TEXT,
                    fecha_hora TIMESTAMP,metodo_pago TEXT,estado TEXT,total NUMERIC(12,2))''')
                conexion.exec_driver_sql('INSERT INTO usuario VALUES(7)')
                conexion.exec_driver_sql("""INSERT INTO venta VALUES
                    (10,7,'ANTERIOR','2025-01-15 10:30:00','TARJETA','COMPLETADA',25),
                    (11,7,'ANULADA','2025-01-15 10:40:00','EFECTIVO','ANULADA',15)""")
                conexion.commit()
                conexion.exec_driver_sql(script)
                conexion.commit()
                antes = conexion.execute(text('SELECT * FROM venta ORDER BY id_venta')).mappings().all()
                conexion.commit()
                conexion.exec_driver_sql(script)
                conexion.commit()
                despues = conexion.execute(text('SELECT * FROM venta ORDER BY id_venta')).mappings().all()
                self.assertEqual(antes, despues)
                self.assertEqual([v['estado'] for v in despues], ['COMPLETADA', 'ANULADA'])
                for venta in despues:
                    self.assertEqual(venta['estado_pago'], 'CONFIRMADO')
                    self.assertIsNone(venta['fecha_confirmacion'])
                    self.assertIsNone(venta['id_confirmador'])
                    self.assertIsNone(venta['clave_operacion'])
                    self.assertIsNone(venta['resultado_cobro'])
            finally:
                conexion.rollback()
                conexion.exec_driver_sql('SET search_path TO public')
                conexion.exec_driver_sql(f'DROP SCHEMA IF EXISTS {esquema} CASCADE')
                conexion.commit()
