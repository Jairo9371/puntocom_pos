"""Arqueos, compras y pagos con PostgreSQL real en una base desechable."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import re
from threading import Event
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event, text
from sqlalchemy.orm import Session
from werkzeug.datastructures import MultiDict

from caja_servicio import operar_caja
from compras_servicio import registrar_abono, registrar_compra
from entorno_pg import CasoPostgres


class CajaComprasPostgresTest(CasoPostgres):
    ESPERA = 6

    def escalar(self, sql, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar_one()

    def compra(self, **cambios):
        return {'id_proveedor': 1, 'items': [
            {'id_producto': 2, 'cantidad': 3, 'costo_unitario': '7.50'},
            {'id_producto': 1, 'cantidad': 2, 'costo_unitario': '11.25'}],
            'metodo_pago': 'CRÉDITO', 'clave_operacion': uuid4().hex, **cambios}

    def abono(self, **cambios):
        return {'monto': '10.00', 'metodo_pago': 'TRANSFERENCIA',
                'clave_operacion': uuid4().hex, **cambios}

    def ejecutar(self, funcion, *args):
        with Session(self.engine) as session, session.begin():
            return funcion(session, *args)

    def registrar(self, datos=None):
        return self.ejecutar(registrar_compra, datos or self.compra(), self.ids['admin'])

    def abrir_admin(self, fondo='100.00'):
        return self.ejecutar(operar_caja, {'accion': 'abrir', 'fondo_inicial': fondo}, 1)

    def test_arqueo_conserva_snapshot_y_exige_motivo(self):
        with self.assertRaisesRegex(ValueError, 'motivo'):
            self.ejecutar(operar_caja, {'accion': 'cerrar', 'efectivo_contado': '98'}, 2)
        self.assertEqual(self.escalar('SELECT estado FROM turno_caja WHERE id_turno=1'), 'ABIERTO')
        resultado = self.ejecutar(operar_caja, {'accion': 'cerrar', 'efectivo_contado': '98',
                                              'motivo_diferencia': 'Faltante por conteo'}, 2)
        self.assertEqual(resultado['efectivo_esperado'], Decimal('100.00'))
        self.assertEqual(resultado['diferencia'], Decimal('-2.00'))
        self.assertEqual(self.escalar('SELECT motivo_diferencia FROM turno_caja WHERE id_turno=1'),
                         'Faltante por conteo')
        self.assertEqual(self.escalar('SELECT estado FROM turno_caja WHERE id_turno=2'), 'ABIERTO')

    def test_importes_fuera_de_precision_no_escriben(self):
        for monto in ('10000000000', 'NaN', '-1'):
            with self.subTest(monto=monto), self.assertRaises(ValueError):
                self.ejecutar(operar_caja, {'accion': 'abrir', 'fondo_inicial': monto}, 1)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM turno_caja'), 2)
        with self.assertRaises(ValueError):
            self.registrar(self.compra(items=[{'id_producto': 1, 'cantidad': 2147483648,
                                               'costo_unitario': '0'}]))
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM compra'), 0)

    def test_varios_productos_credito_y_reintento_no_duplican(self):
        datos = self.compra()
        inicial = self.registrar(datos)
        repetida = self.registrar(datos)
        self.assertEqual(inicial['id_compra'], repetida['id_compra'])
        self.assertTrue(repetida['repetida'])
        self.assertEqual(inicial['total'], Decimal('45.00'))
        self.assertEqual(inicial['saldo_pendiente'], Decimal('45.00'))
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM detalle_compra'), 2)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM pago_compra'), 0)
        self.assertEqual(self.escalar('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1'), 12)
        self.assertEqual(self.escalar('SELECT existencia_actual FROM vista_inventario WHERE id_producto=2'), 13)
        self.assertEqual(self.escalar('SELECT costo FROM producto WHERE id_producto=1'), Decimal('11.25'))
        with self.assertRaisesRegex(ValueError, 'diferente'):
            self.registrar({**datos, 'numero_factura': 'OTRA'})
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM compra'), 1)

    def test_formulario_repetido_y_compatibilidad_efectivo_legacy(self):
        datos = MultiDict([('id_proveedor', '1'), ('id_producto', '2'), ('cantidad', '2'),
                          ('costo_unitario', '3.00'), ('id_producto', '1'), ('cantidad', '4'),
                          ('costo_unitario', '2.00'), ('metodo_pago', 'TRANSFERENCIA')])
        self.assertEqual(self.registrar(datos)['total'], Decimal('14.00'))
        legacy = MultiDict({'id_proveedor': '1', 'id_producto': '1', 'cantidad': '1',
                            'costo_unitario': '5.00', 'metodo_pago': 'EFECTIVO'})
        self.registrar(legacy)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM movimiento_caja'), 0)
        self.assertEqual(self.escalar("SELECT origen_efectivo FROM pago_compra WHERE metodo_pago='EFECTIVO'"),
                         'EXTERNO')

    def test_efectivo_exige_origen_actual_y_pago_caja_vinculado(self):
        self.abrir_admin()
        with self.assertRaisesRegex(ValueError, 'origen|efectivo'):
            self.registrar(self.compra(metodo_pago='EFECTIVO'))
        compra = self.registrar(self.compra(metodo_pago='EFECTIVO', origen_efectivo='CAJA'))
        self.assertEqual(compra['saldo_pendiente'], Decimal('0.00'))
        self.assertEqual(self.escalar('''SELECT m.monto FROM pago_compra p
            JOIN movimiento_caja m ON m.id_movimiento_caja=p.id_movimiento_caja'''), Decimal('45.00'))
        self.assertEqual(self.escalar("SELECT tipo FROM movimiento_caja"), 'EGRESO')
        self.assertEqual(self.escalar("SELECT id_turno FROM movimiento_caja"), 3)

    def test_compra_sin_efectivo_y_fallo_tardio_revierten_todo(self):
        self.abrir_admin('1')
        with self.assertRaisesRegex(ValueError, 'insuficiente'):
            self.registrar(self.compra(metodo_pago='EFECTIVO', origen_efectivo='CAJA'))
        with patch('compras_servicio._pago', side_effect=ValueError('fallo tardío')):
            with self.assertRaisesRegex(ValueError, 'tardío'):
                self.registrar(self.compra(metodo_pago='TRANSFERENCIA'))
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM compra'), 0)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM detalle_compra'), 0)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM movimiento_caja'), 0)
        self.assertEqual(self.escalar('SELECT costo FROM producto WHERE id_producto=1'), Decimal('9.00'))
        self.assertEqual(self.escalar('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1'), 10)

    def test_abonos_saldo_reintento_y_limite(self):
        compra = self.registrar()
        datos = self.abono()
        primero = self.ejecutar(registrar_abono, compra['id_compra'], datos, 1)
        repetido = self.ejecutar(registrar_abono, compra['id_compra'], datos, 1)
        self.assertEqual(primero['id_pago_compra'], repetido['id_pago_compra'])
        self.assertTrue(repetido['repetida'])
        self.assertEqual(repetido['saldo_pendiente'], Decimal('35.00'))
        with self.assertRaisesRegex(ValueError, 'supera'):
            self.ejecutar(registrar_abono, compra['id_compra'], self.abono(monto='36'), 1)
        with self.assertRaisesRegex(ValueError, 'diferente'):
            self.ejecutar(registrar_abono, compra['id_compra'], {**datos, 'monto': '5'}, 1)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM pago_compra'), 1)

    def test_http_compra_multiarticulo_y_abono_con_csrf_real(self):
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = '1'
            sesion['_fresh'] = True
        pagina = self.client.get('/compras')
        self.assertEqual(pagina.status_code, 200)
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"',
                          pagina.get_data(as_text=True)).group(1)
        datos = MultiDict([('csrf_token', token), ('clave_operacion', uuid4().hex),
                          ('id_proveedor', '1'), ('id_producto', '1'), ('cantidad', '2'),
                          ('costo_unitario', '11.25'), ('id_producto', '2'), ('cantidad', '3'),
                          ('costo_unitario', '7.50'), ('metodo_pago', 'CRÉDITO')])
        self.assertEqual(self.client.post('/compras/nueva', data=datos).status_code, 302)
        cid = self.escalar('SELECT id_compra FROM compra')
        pagina = self.client.get('/compras')
        self.assertIn('Registrar abono', pagina.get_data(as_text=True))
        abono = {'csrf_token': token, **self.abono()}
        url = f'/compras/{cid}/abonos'
        self.assertEqual(self.client.post(url, data=abono).status_code, 302)
        self.assertEqual(self.client.post(url, data=abono).status_code, 302)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM pago_compra'), 1)
        self.assertEqual(self.escalar('SELECT saldo_pendiente FROM compra'), Decimal('35.00'))

    def test_abono_efectivo_externo_y_caja(self):
        compra = self.registrar()
        self.abrir_admin()
        for origen in ('EXTERNO', 'CAJA'):
            self.ejecutar(registrar_abono, compra['id_compra'],
                         self.abono(metodo_pago='EFECTIVO', origen_efectivo=origen), 1)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM pago_compra'), 2)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM movimiento_caja'), 1)
        self.assertEqual(self.escalar('SELECT saldo_pendiente FROM compra'), Decimal('25.00'))

    def test_historicos_no_se_convierten_en_pagados_ni_en_arqueos(self):
        with self.engine.begin() as conn:
            cid = conn.execute(text("""INSERT INTO compra(id_proveedor,id_usuario,metodo_pago,total)
                VALUES(1,1,'CRÉDITO',90) RETURNING id_compra""")).scalar_one()
            conn.exec_driver_sql("UPDATE turno_caja SET estado='CERRADO',cierre=NOW(),efectivo_contado=100 WHERE id_turno=1")
        from migraciones import aplicar_migracion_caja_compras
        aplicar_migracion_caja_compras(self.engine)
        self.assertIsNone(self.escalar('SELECT saldo_pendiente FROM compra WHERE id_compra=:id', id=cid))
        self.assertIsNone(self.escalar('SELECT efectivo_esperado FROM turno_caja WHERE id_turno=1'))
        with self.assertRaisesRegex(ValueError, 'histórica'):
            self.ejecutar(registrar_abono, cid, self.abono(), 1)

    def test_permiso_se_revalida_en_servicio(self):
        with self.assertRaises(PermissionError):
            self.ejecutar(registrar_compra, self.compra(), 2)
        compra = self.registrar()
        with self.assertRaises(PermissionError):
            self.ejecutar(registrar_abono, compra['id_compra'], self.abono(), 2)
        with self.assertRaises(PermissionError):
            self.ejecutar(operar_caja, {'accion': 'abrir', 'fondo_inicial': '0'}, 4)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM pago_compra'), 0)

    def test_abono_json_nulo_rechazado_sin_escrituras(self):
        with self.assertRaisesRegex(ValueError, 'Solicitud de abono'):
            self.ejecutar(registrar_abono, 1, None, 1)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM pago_compra'), 0)

    def trabajador(self, funcion, args, proceso, preparado=None, liberar=None, observar=None):
        with Session(self.engine) as session:
            conn = session.connection()
            proceso['pid'] = conn.execute(text('SELECT pg_backend_pid()')).scalar_one()
            proceso['listo'].set()
            if observar:
                event.listen(conn, 'after_cursor_execute', observar)
            try:
                resultado = funcion(session, *args)
                if preparado:
                    preparado.set()
                if liberar and not liberar.wait(self.ESPERA):
                    raise AssertionError('No se liberó la operación retenida.')
                session.commit()
                return resultado
            except BaseException:
                session.rollback()
                raise
            finally:
                if observar:
                    event.remove(conn, 'after_cursor_execute', observar)

    def esperar(self, senal, futuro):
        if not senal.wait(self.ESPERA):
            if futuro.done():
                futuro.result()
            self.fail('La operación no alcanzó el punto de coordinación.')

    def bloqueo(self, bloqueado, bloqueador):
        fin = monotonic() + 3
        pausa = Event()
        while monotonic() < fin:
            if bloqueador in self.escalar('SELECT pg_blocking_pids(:pid)', pid=bloqueado):
                return
            pausa.wait(.02)
        self.fail('No se observó el bloqueo real esperado.')

    def carrera(self, f1, a1, f2, a2):
        p1, p2 = {'listo': Event()}, {'listo': Event()}
        preparado, liberar = Event(), Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            primero = pool.submit(self.trabajador, f1, a1, p1, preparado, liberar)
            try:
                self.esperar(preparado, primero)
                segundo = pool.submit(self.trabajador, f2, a2, p2)
                self.esperar(p2['listo'], segundo)
                self.bloqueo(p2['pid'], p1['pid'])
            finally:
                liberar.set()
            resultado = primero.result(timeout=self.ESPERA)
            return resultado, segundo

    def test_abonos_concurrentes_no_superan_saldo(self):
        compra = self.registrar()
        _, segundo = self.carrera(registrar_abono, (compra['id_compra'], self.abono(monto='30'), 1),
                                  registrar_abono, (compra['id_compra'], self.abono(monto='20'), 1))
        with self.assertRaisesRegex(ValueError, 'supera'):
            segundo.result()
        self.assertEqual(self.escalar('SELECT saldo_pendiente FROM compra'), Decimal('15.00'))
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM pago_compra'), 1)

    def test_abonos_concurrentes_misma_clave_solo_pagan_una_vez(self):
        compra = self.registrar()
        datos = self.abono()
        primero, segundo = self.carrera(registrar_abono, (compra['id_compra'], datos, 1),
                                        registrar_abono, (compra['id_compra'], datos, 1))
        repetido = segundo.result()
        self.assertEqual(primero['id_pago_compra'], repetido['id_pago_compra'])
        self.assertTrue(repetido['repetida'])
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM pago_compra'), 1)

    def test_compra_caja_concurrente_con_cierre_incluye_egreso_en_snapshot(self):
        self.abrir_admin()
        compra = self.compra(metodo_pago='EFECTIVO', origen_efectivo='CAJA')
        _, cierre = self.carrera(registrar_compra, (compra, 1), operar_caja,
                                 ({'accion': 'cerrar', 'efectivo_contado': '55'}, 1))
        self.assertEqual(cierre.result()['efectivo_esperado'], Decimal('55.00'))
        self.assertEqual(self.escalar('SELECT diferencia FROM turno_caja WHERE id_turno=3'), Decimal('0.00'))

    def test_compras_inversas_bloquean_productos_en_orden(self):
        datos = self.compra()
        inversa = self.compra(items=list(reversed(datos['items'])))
        observados = []

        def observar(conn, cursor, sql, params, context, many):
            if 'FROM producto' in sql and 'FOR UPDATE' in sql:
                observados.append(params['id'])

        with Session(self.engine) as session:
            conn = session.connection()
            event.listen(conn, 'after_cursor_execute', observar)
            try:
                registrar_compra(session, inversa, 1)
                session.commit()
            finally:
                event.remove(conn, 'after_cursor_execute', observar)
        self.assertEqual(observados, [1, 2])
        _, segunda = self.carrera(registrar_compra, (datos, 1), registrar_compra,
                                  (self.compra(items=list(reversed(datos['items']))), 1))
        self.assertFalse(segunda.result()['repetida'])
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM compra'), 3)
