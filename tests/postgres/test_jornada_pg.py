"""Una jornada encadenada por HTTP y CSRF sobre la base PostgreSQL desechable."""

from decimal import Decimal
from html import unescape
import re
from uuid import uuid4

from sqlalchemy import text


from entorno_pg import CasoPostgres


class JornadaPostgresTest(CasoPostgres):
    def valor(self, consulta, **parametros):
        with self.engine.connect() as conexion:
            return conexion.execute(text(consulta), parametros).scalar_one()

    def fila(self, consulta, **parametros):
        with self.engine.connect() as conexion:
            return conexion.execute(text(consulta), parametros).mappings().one()

    def csrf(self, cliente, ruta):
        respuesta = cliente.get(ruta)
        self.assertEqual(respuesta.status_code, 200, ruta)
        token = re.search(r'(?:name="csrf_token"\s+value|data-csrf-token)="([^"]+)"',
                          respuesta.get_data(as_text=True))
        self.assertIsNotNone(token, ruta)
        return unescape(token.group(1))

    def formulario(self, cliente, ruta, datos, pagina='/caja'):
        return cliente.post(ruta, data={**datos, 'csrf_token': self.csrf(cliente, pagina)})

    def json(self, cliente, ruta, datos, pagina='/pos', clave=None, estado=200):
        cabeceras = {'X-CSRFToken': self.csrf(cliente, pagina)}
        if clave:
            cabeceras['Idempotency-Key'] = clave
        respuesta = cliente.post(ruta, json=datos, headers=cabeceras)
        self.assertEqual(respuesta.status_code, estado, respuesta.get_data(as_text=True))
        return respuesta.get_json()

    def login(self, cliente, correo):
        respuesta = self.formulario(cliente, '/login', {
            'correo': correo, 'password': 'PruebaPostgres2026!',
        }, '/login')
        self.assertEqual(respuesta.status_code, 302)
        self.assertNotIn('/login', respuesta.location)

    def stock(self, producto):
        return self.valor('SELECT existencia_actual FROM vista_inventario WHERE id_producto=:p',
                          p=producto)

    def venta(self, producto, cantidad=1, recibido='20.00', **cambios):
        return {'cliente_id': 1, 'metodo': 'EFECTIVO', 'recibido': recibido,
                'items': [{'id': producto, 'cantidad': cantidad}], **cambios}

    def test_jornada_caja_cero_compras_cobros_pagos_anulacion_y_arqueo(self):
        administrador = self.app.app.test_client()
        cajero = self.app.app.test_client()
        self.login(administrador, 'admin@example.test')
        self.login(cajero, 'cajero1@example.test')

        # La caja inicial de la fixture se liquida antes de empezar la jornada.
        self.assertEqual(self.formulario(cajero, '/caja', {
            'accion': 'cerrar', 'efectivo_contado': '100.00',
        }).status_code, 302)
        for cliente, usuario in ((administrador, 1), (cajero, 2)):
            self.assertEqual(self.formulario(cliente, '/caja', {
                'accion': 'abrir', 'fondo_inicial': '0',
            }).status_code, 302)
            self.assertEqual(self.valor("SELECT fondo_inicial FROM turno_caja WHERE id_usuario=:u AND estado='ABIERTO'",
                                        u=usuario), Decimal('0.00'))

        compra_datos = {
            'id_proveedor': 1, 'metodo_pago': 'CRÉDITO', 'clave_operacion': uuid4().hex,
            'numero_factura': 'JORNADA-001', 'items': [
                {'id_producto': 1, 'cantidad': 4, 'costo_unitario': '8.00'},
                {'id_producto': 2, 'cantidad': 3, 'costo_unitario': '10.00'},
            ],
        }
        compra = self.json(administrador, '/compras/nueva', compra_datos, '/compras')
        repetida = self.json(administrador, '/compras/nueva', compra_datos, '/compras')
        self.assertEqual(compra['id_compra'], repetida['id_compra'])
        self.assertTrue(repetida['repetida'])
        self.assertEqual((self.stock(1), self.stock(2)), (14, 13))
        cid = compra['id_compra']
        for importe in ('12.00', '50.00'):
            abono = {'monto': importe, 'metodo_pago': 'TRANSFERENCIA',
                     'clave_operacion': uuid4().hex}
            primero = self.json(administrador, f'/compras/{cid}/abonos', abono, '/compras')
            segundo = self.json(administrador, f'/compras/{cid}/abonos', abono, '/compras')
            self.assertEqual(primero['id_pago_compra'], segundo['id_pago_compra'])
        self.assertEqual(self.valor('SELECT saldo_pendiente FROM compra WHERE id_compra=:c', c=cid),
                         Decimal('0.00'))
        self.assertEqual(self.valor('SELECT COUNT(*) FROM pago_compra'), 2)
        self.assertEqual(self.valor('SELECT COUNT(*) FROM movimiento_caja'), 0)

        descuento = self.venta(1, 2, '50.00', items=[{'id': 1, 'cantidad': 2, 'descuento': '3.00'}])
        # El cajero no puede aplicar el descuento del administrador.
        self.json(cajero, '/pos/cobrar', descuento, clave=uuid4().hex, estado=403)
        clave_descuento = uuid4().hex
        descontada = self.json(administrador, '/pos/cobrar', descuento, clave=clave_descuento)
        self.assertEqual((descontada['total'], descontada['cambio']), ('27.00', '23.00'))
        self.assertEqual(descontada, self.json(administrador, '/pos/cobrar', descuento,
                                               clave=clave_descuento))
        ticket = administrador.get(f"/ventas/{descontada['id_venta']}/ticket")
        self.assertEqual(ticket.status_code, 200)
        html = ticket.get_data(as_text=True)
        self.assertIn('Descuento total', html)
        self.assertIn('-Q 3.00', html)
        self.assertIn('Q 27.00', html)

        primera = self.json(cajero, '/pos/cobrar', self.venta(1), clave=uuid4().hex)
        clave_segunda = uuid4().hex
        datos_segunda = self.venta(2, 2, '50.00')
        segunda = self.json(cajero, '/pos/cobrar', datos_segunda, clave=clave_segunda)
        self.assertEqual((primera['total'], primera['cambio']), ('15.00', '5.00'))
        self.assertEqual((segunda['total'], segunda['cambio']), ('30.00', '20.00'))
        self.assertNotEqual(primera['id_venta'], segunda['id_venta'])
        self.assertEqual(segunda, self.json(cajero, '/pos/cobrar', datos_segunda, clave=clave_segunda))
        conflicto = self.json(cajero, '/pos/cobrar', {**datos_segunda, 'recibido': '100.00'},
                              clave=clave_segunda, estado=409)
        self.assertFalse(conflicto['ok'])
        self.assertEqual((self.stock(1), self.stock(2)), (11, 11))

        pendiente = self.json(cajero, '/pos/cobrar', self.venta(
            1, 3, '0', metodo='TRANSFERENCIA', estado_pago='PENDIENTE'), clave=uuid4().hex)
        self.assertEqual(pendiente['estado_pago'], 'PENDIENTE')
        self.assertEqual(self.stock(1), 11)
        ruta_pago = f"/ventas/{pendiente['id_venta']}/pago"
        confirmado = self.json(cajero, ruta_pago, {'estado_pago': 'CONFIRMADO'})
        self.assertEqual(confirmado['estado'], 'COMPLETADA')
        self.assertEqual(confirmado, self.json(cajero, ruta_pago, {'estado_pago': 'CONFIRMADO'}))
        self.assertEqual(self.stock(1), 8)

        fallida = self.json(cajero, '/pos/cobrar', self.venta(
            2, recibido='0', metodo='TRANSFERENCIA', estado_pago='PENDIENTE'), clave=uuid4().hex)
        ruta_fallo = f"/ventas/{fallida['id_venta']}/pago"
        fallo = {'estado_pago': 'FALLIDO', 'motivo': 'Transferencia no recibida'}
        resuelta = self.json(cajero, ruta_fallo, fallo)
        self.assertEqual(resuelta['estado'], 'FALLIDA')
        self.assertEqual(resuelta, self.json(cajero, ruta_fallo, fallo))
        self.json(cajero, ruta_fallo, {'estado_pago': 'CONFIRMADO'}, estado=400)
        self.assertEqual((self.stock(1), self.stock(2)), (8, 11))
        self.assertEqual(self.valor('SELECT COUNT(*) FROM venta'), 5)

        ruta_anular = f"/ventas/{descontada['id_venta']}/anular"
        for _ in range(2):
            self.assertEqual(self.formulario(administrador, ruta_anular, {
                'motivo': 'Devolución durante jornada de prueba',
            }, '/ventas').status_code, 302)
        self.assertEqual(self.valor('SELECT estado FROM venta WHERE id_venta=:v',
                                    v=descontada['id_venta']), 'ANULADA')
        self.assertEqual((self.stock(1), self.stock(2)), (10, 11))
        self.assertEqual(self.valor('SELECT COUNT(*) FROM movimiento_caja'), 4)
        self.assertEqual(self.valor("SELECT SUM(total) FROM venta WHERE estado='COMPLETADA'"),
                         Decimal('90.00'))

        for cliente, usuario, contado, esperado, diferencia in (
            (administrador, 1, '0.00', '0.00', '0.00'),
            (cajero, 2, '44.00', '45.00', '-1.00'),
        ):
            self.assertEqual(self.formulario(cliente, '/caja', {
                'accion': 'cerrar', 'efectivo_contado': contado,
                'motivo_diferencia': 'Faltante identificado en arqueo' if diferencia != '0.00' else '',
            }).status_code, 302)
            cierre = self.fila('SELECT * FROM turno_caja WHERE id_usuario=:u ORDER BY id_turno DESC LIMIT 1',
                               u=usuario)
            self.assertEqual(cierre['estado'], 'CERRADO')
            self.assertEqual(cierre['efectivo_esperado'], Decimal(esperado))
            self.assertEqual(cierre['efectivo_contado'], Decimal(contado))
            self.assertEqual(cierre['diferencia'], Decimal(diferencia))
        self.json(cajero, '/pos/cobrar', self.venta(1), clave=uuid4().hex, estado=400)
        # La recuperación de un cobro confirmado conserva su resultado aun con caja cerrada.
        self.assertEqual(segunda, self.json(cajero, '/pos/cobrar', datos_segunda, clave=clave_segunda))
        self.assertEqual(self.valor('SELECT COUNT(*) FROM venta'), 5)
        self.assertEqual((self.stock(1), self.stock(2)), (10, 11))
