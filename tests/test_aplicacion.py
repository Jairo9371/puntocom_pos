"""Pruebas HTTP aisladas: jamás se abre conexión con PostgreSQL."""

import importlib
import os
import unittest
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from unittest.mock import MagicMock, patch

from openpyxl import load_workbook


# Evitar la lectura del .env privado y cualquier configuración de producción.
with patch('dotenv.load_dotenv'), patch.dict(os.environ, {
    'APP_ENV': 'testing', 'APP_DEBUG': 'false', 'SECRET_KEY': 'clave-solo-para-pruebas',
    'DB_HOST': '127.0.0.1', 'DB_PORT': '5433', 'DB_NAME': 'no_conectar',
    'SESSION_COOKIE_SECURE': 'false',
}):
    modulo = importlib.import_module('app')


class AplicacionTests(unittest.TestCase):
    def setUp(self):
        modulo.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = modulo.app.test_client()
        self.usuario = {
            'id_usuario': 1, 'nombre': 'Ana', 'correo': 'ana@example.com', 'rol': 'Administrador',
        }
        self.consultas = []
        self.venta = {
            'numero_ticket': 'PC-PRUEBA', 'fecha_hora': datetime(2026, 10, 17, 14),
            'cliente': 'Consumidor Final', 'cajero': 'Ana', 'metodo_pago': 'EFECTIVO',
            'total': Decimal('28.00'), 'estado': 'COMPLETADA',
        }
        self.session = MagicMock()
        for parche in (
            patch.object(modulo.db, 'session', self.session),
            patch.object(modulo, 'one', side_effect=self.uno),
            patch.object(modulo, 'rows', side_effect=self.filas),
            patch.object(modulo, 'value', return_value=35),
        ):
            parche.start()
            self.addCleanup(parche.stop)

    def autenticar(self):
        with self.client.session_transaction() as session:
            session['_user_id'] = '1'
            session['_fresh'] = True

    def uno(self, sql, **params):
        self.consultas.append((sql, params))
        if 'FROM usuario' in sql:
            return self.usuario
        if 'COUNT(*) AS cantidad' in sql:
            return {'cantidad': 1, 'total': Decimal('28.00')}
        return None

    def filas(self, sql, **params):
        self.consultas.append((sql, params))
        if 'AS cliente' in sql:
            return [self.venta]
        return []

    def test_login_y_exportacion_exigen_sesion(self):
        self.assertEqual(self.client.get('/login').status_code, 200)
        respuesta = self.client.get('/reportes/excel')
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn('/login', respuesta.location)
        self.assertEqual(self.consultas, [])

    def test_formulario_filtra_estadisticas_y_ofrece_exportacion(self):
        self.autenticar()
        respuesta = self.client.get('/reportes?inicio=2026-10-01&fin=2026-10-17')
        self.assertEqual(respuesta.status_code, 200)
        html = respuesta.get_data(as_text=True)
        self.assertIn('value="2026-10-01"', html)
        self.assertIn('value="2026-10-17"', html)
        self.assertIn('Aplicar filtro', html)
        self.assertIn('action="/reportes"', html)
        self.assertIn('formaction="/reportes/excel"', html)
        estadisticas = [(sql, p) for sql, p in self.consultas if 'inicio' in p]
        self.assertEqual(len(estadisticas), 3)
        for sql, parametros in estadisticas:
            self.assertEqual(parametros, {'inicio': date(2026, 10, 1), 'fin': date(2026, 10, 18)})
            self.assertIn("estado='COMPLETADA'", sql)

    def test_exportaciones_comparten_periodo_contenido_y_estado(self):
        self.autenticar()
        for url in ('/reportes/excel?desde=2026-10-01&hasta=2026-10-17',
                    '/reportes/ventas/excel?inicio=2026-10-01&fin=2026-10-17'):
            with self.subTest(url=url):
                respuesta = self.client.get(url)
                self.assertEqual(respuesta.status_code, 200)
                self.assertIn('spreadsheetml.sheet', respuesta.mimetype)
                libro = load_workbook(BytesIO(respuesta.data))
                self.assertEqual(libro.active['A2'].value, 'PC-PRUEBA')
                self.assertEqual(libro.active['F2'].value, 28)
                libro.close()
        exportaciones = [(sql, p) for sql, p in self.consultas if 'AS cliente' in sql]
        self.assertEqual(len(exportaciones), 2)
        self.assertEqual(exportaciones[0], exportaciones[1])
        self.assertNotIn('LIMIT 5000', exportaciones[0][0])

    def test_rango_invalido_no_consulta_ventas(self):
        self.autenticar()
        respuesta = self.client.get('/reportes/excel?inicio=2026-10-17&fin=2026-10-01')
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(respuesta.location, '/reportes')
        self.assertFalse(any('AS cliente' in sql for sql, _ in self.consultas))

    def test_cobro_rechaza_json_incorrecto_con_400(self):
        self.autenticar()
        for payload in ([], [1], {'items': [None]}, {'items': [], 'cliente_id': 1}):
            with self.subTest(payload=payload):
                respuesta = self.client.post('/pos/cobrar', json=payload)
                self.assertEqual(respuesta.status_code, 400)
                self.assertFalse(respuesta.json['ok'])
        self.assertEqual(self.session.rollback.call_count, 4)
        self.session.commit.assert_not_called()

    def test_cobro_confirma_solo_si_servicio_tiene_exito(self):
        self.autenticar()
        resultado = {'id_venta': 1, 'numero': 'PC-PRUEBA', 'total': '28.00', 'cambio': '2.00'}
        with patch.object(modulo, 'registrar_venta', return_value=resultado):
            respuesta = self.client.post('/pos/cobrar', json={'items': []})
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.json['ok'])
        self.session.commit.assert_called_once()
        self.session.rollback.assert_not_called()

    def test_cobro_error_de_escritura_revierte_y_oculta_detalle(self):
        self.autenticar()
        with patch.object(modulo, 'registrar_venta', side_effect=RuntimeError('detalle privado')), \
                patch.object(modulo.app.logger, 'exception'):
            respuesta = self.client.post('/pos/cobrar', json={})
        self.assertEqual(respuesta.status_code, 500)
        self.assertNotIn('detalle privado', respuesta.get_data(as_text=True))
        self.session.rollback.assert_called_once()
        self.session.commit.assert_not_called()

    def test_bodeguero_no_puede_cobrar(self):
        self.usuario['rol'] = 'Bodeguero'
        self.autenticar()
        respuesta = self.client.post('/pos/cobrar', json={})
        self.assertEqual(respuesta.status_code, 403)
        self.session.commit.assert_not_called()

    def test_cobro_exige_token_csrf(self):
        self.autenticar()
        with patch.dict(modulo.app.config, {'WTF_CSRF_ENABLED': True}), \
                patch.object(modulo, 'registrar_venta') as registrar:
            respuesta = self.client.post('/pos/cobrar', json={})
        self.assertEqual(respuesta.status_code, 400)
        registrar.assert_not_called()
        self.session.commit.assert_not_called()

    def test_compra_admite_credito_y_rechaza_metodo_desconocido(self):
        self.autenticar()

        def uno_compra(sql, **params):
            if 'FROM producto' in sql:
                return {'id_producto': 1}
            if 'FROM proveedor' in sql:
                return {'id_proveedor': 1}
            return self.uno(sql, **params)

        datos = {'id_proveedor': '1', 'id_producto': '1', 'cantidad': '2',
                 'costo_unitario': '9', 'metodo_pago': 'CRÉDITO'}
        with patch.object(modulo, 'one', side_effect=uno_compra), \
                patch.object(modulo, 'usuario_con_permiso', return_value={'id_usuario': 1, 'rol': 'Administrador'}):
            respuesta = self.client.post('/compras/nueva', data=datos)
        self.assertEqual(respuesta.status_code, 302)
        self.session.commit.assert_called_once()
        self.session.reset_mock()
        datos['metodo_pago'] = 'DESCONOCIDO'
        respuesta = self.client.post('/compras/nueva', data=datos)
        self.assertEqual(respuesta.status_code, 302)
        self.session.commit.assert_not_called()
        self.session.rollback.assert_called_once()

    def test_plantillas_compilan(self):
        for nombre in modulo.app.jinja_env.list_templates():
            with self.subTest(plantilla=nombre):
                modulo.app.jinja_env.get_template(nombre)

    def test_notificaciones_muestran_lista_y_contador_total(self):
        self.autenticar()
        alertas = [{'codigo': f'P{i}', 'nombre': f'Producto {i}',
                    'existencia_actual': 0, 'stock_minimo': 5} for i in range(20)]
        original = self.filas

        def filas(sql, **params):
            if 'LIMIT 20' in sql and 'vista_inventario' in sql:
                return alertas
            return original(sql, **params)

        with patch.object(modulo, 'rows', side_effect=filas):
            respuesta = self.client.get('/reportes')
        self.assertEqual(respuesta.status_code, 200)
        html = respuesta.get_data(as_text=True)
        self.assertIn('Producto 0', html)
        self.assertIn('Producto 19', html)
        self.assertNotIn('No hay alertas de inventario.', html)
        self.assertRegex(html, r'bg-danger">\s*35\s*</span>')


if __name__ == '__main__':
    unittest.main()
