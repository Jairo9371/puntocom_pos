"""Configuración del negocio sobre PostgreSQL desechable, HTTP y CSRF reales."""

from html import unescape
import re
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from configuracion_negocio import NEGOCIO_PREDETERMINADO

from entorno_pg import CasoPostgres
from migraciones import aplicar_migracion_configuracion


class ConfiguracionNegocioPostgres(CasoPostgres):
    def setUp(self):
        super().setUp()
        with self.engine.begin() as conexion:
            conexion.execute(text('INSERT INTO configuracion_negocio(id) VALUES(1)'))

    def _negocio(self):
        with self.engine.connect() as conexion:
            return dict(conexion.execute(text('SELECT * FROM configuracion_negocio WHERE id=1')).mappings().one())

    def _csrf(self, ruta, cliente=None):
        cliente = cliente or self.client
        respuesta = cliente.get(ruta)
        self.assertEqual(respuesta.status_code, 200)
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"', respuesta.get_data(as_text=True))
        self.assertIsNotNone(token, ruta)
        return unescape(token.group(1))

    def _login(self, usuario='admin', cliente=None):
        cliente = cliente or self.client
        respuesta = cliente.post('/login', data={
            'csrf_token': self._csrf('/login', cliente),
            'correo': f'{usuario}@example.test', 'password': 'PruebaPostgres2026!',
        })
        self.assertEqual(respuesta.status_code, 302)

    def _guardar(self, **cambios):
        return self.client.post('/configuracion', data={
            **NEGOCIO_PREDETERMINADO, **cambios, 'csrf_token': self._csrf('/configuracion'),
        })

    def test_datos_persisten_con_responsable_y_ancho_del_ticket(self):
        self._login()
        self.assertEqual(self._guardar(
            nombre_comercial='Librería Central 2026', giro='Papelería y útiles escolares',
            direccion='Avenida 2, zona 1\nGuatemala', telefono='23456789',
            correo='VENTAS@EJEMPLO.COM', nit='12345-k', mensaje_ticket='Gracias, vuelva pronto.',
            ancho_ticket='58',
        ).status_code, 302)
        negocio = self._negocio()
        self.assertEqual(negocio['nombre_comercial'], 'Librería Central 2026')
        self.assertEqual(negocio['correo'], 'ventas@ejemplo.com')
        self.assertEqual(negocio['nit'], '12345-K')
        self.assertEqual(negocio['ancho_ticket'], 58)
        self.assertEqual(negocio['actualizado_por'], self.ids['admin'])
        html = self.client.get('/configuracion').get_data(as_text=True)
        self.assertIn('Librería Central 2026', html)
        self.assertIn('ventas@ejemplo.com', html)

    def test_anonimo_cajero_y_bodeguero_no_modifican_configuracion(self):
        antes = self._negocio()
        self.assertEqual(self.client.get('/configuracion').status_code, 302)
        self.assertEqual(self.client.post('/configuracion', data={}).status_code, 302)
        for nombre in ('cajero1', 'bodeguero'):
            cliente = self.app.app.test_client()
            self._login(nombre, cliente)
            self.assertEqual(cliente.get('/configuracion').status_code, 403)
            self.assertEqual(cliente.post('/configuracion', data=NEGOCIO_PREDETERMINADO).status_code, 403)
        self.assertEqual(self._negocio(), antes)

    def test_csrf_invalido_no_escribe(self):
        self._login()
        antes = self._negocio()
        respuesta = self.client.post('/configuracion', data=NEGOCIO_PREDETERMINADO)
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(self._negocio(), antes)

    def test_ticket_y_prueba_usan_configuracion_y_prueba_no_registra_operaciones(self):
        self._login()
        self.assertEqual(self._guardar(
            nombre_comercial='Librería <Centro>', direccion='Dirección visible, zona 1',
            mensaje_ticket='Mensaje visible & gracias', ancho_ticket='58',
        ).status_code, 302)
        with self.engine.begin() as conexion:
            venta_id = conexion.execute(text('''INSERT INTO venta(
                id_cliente,id_usuario,id_turno,numero_ticket,metodo_pago,total)
                VALUES(1,2,1,'TICKET-CONFIG','EFECTIVO',15) RETURNING id_venta''')).scalar_one()
            conexion.execute(text('''INSERT INTO detalle_venta(
                id_venta,id_producto,cantidad,precio_unitario,costo_unitario_historico)
                VALUES(:id,1,1,15,9)'''), {'id': venta_id})
            antes = {tabla: conexion.execute(text(f'SELECT COUNT(*) FROM {tabla}')).scalar_one()
                     for tabla in ('venta', 'detalle_venta', 'movimiento_caja', 'movimiento_inventario')}
        for ruta in (f'/ventas/{venta_id}/ticket', '/configuracion/prueba-ticket'):
            respuesta = self.client.get(ruta)
            self.assertEqual(respuesta.status_code, 200)
            html = respuesta.get_data(as_text=True)
            self.assertIn('Librería &lt;Centro&gt;', html)
            self.assertIn('Dirección visible, zona 1', html)
            self.assertIn('Mensaje visible &amp; gracias', html)
            self.assertIn('--ancho-ticket: 58mm', html)
        with self.engine.connect() as conexion:
            despues = {tabla: conexion.execute(text(f'SELECT COUNT(*) FROM {tabla}')).scalar_one()
                       for tabla in antes}
        self.assertEqual(despues, antes)
        for nombre in ('cajero1', 'bodeguero'):
            cliente = self.app.app.test_client()
            self._login(nombre, cliente)
            self.assertEqual(cliente.get('/configuracion/prueba-ticket').status_code, 403)

    def test_validacion_rechaza_cambios_completos_sin_guardado_parcial(self):
        self._login()
        antes = self._negocio()
        for campo, valor in (
            ('nombre_comercial', ''), ('nombre_comercial', 'a' * 151),
            ('direccion', 'a' * 301), ('telefono', '123'), ('correo', 'incorrecto'),
            ('nit', 'CF'), ('ancho_ticket', '72'), ('mensaje_ticket', 'a' * 256),
            ('nombre_comercial', 'Texto\u202eoculto'),
        ):
            with self.subTest(campo=campo):
                respuesta = self._guardar(**{campo: valor})
                self.assertEqual(respuesta.status_code, 400)
                self.assertEqual(self._negocio(), antes)
        self.assertEqual(self._guardar(nombre_comercial='Nombre corregido').status_code, 302)

    def test_revalidacion_de_permiso_impide_guardar_si_admin_fue_revocado(self):
        self._login()
        token = self._csrf('/configuracion')
        antes = self._negocio()
        guardar_original = self.app.guardar_negocio

        def revocar_y_guardar(session, datos, usuario_id):
            with self.engine.begin() as conexion:
                conexion.execute(text('UPDATE usuario SET id_rol=2 WHERE id_usuario=:uid'),
                                 {'uid': usuario_id})
            return guardar_original(session, datos, usuario_id)

        with patch.object(self.app, 'guardar_negocio', side_effect=revocar_y_guardar):
            respuesta = self.client.post('/configuracion', data={
                **NEGOCIO_PREDETERMINADO, 'nombre_comercial': 'Cambio prohibido', 'csrf_token': token,
            })
        self.assertEqual(respuesta.status_code, 403)
        self.assertEqual(self._negocio(), antes)

    def test_migracion_repetida_preserva_personalizacion_y_es_singleton(self):
        self._login()
        self.assertEqual(self._guardar(nombre_comercial='Datos propios', ancho_ticket='58').status_code, 302)
        antes = self._negocio()
        for _ in range(2):
            aplicar_migracion_configuracion(self.engine)
            self.assertEqual(self._negocio(), antes)
        for sql in ('INSERT INTO configuracion_negocio(id) VALUES(2)',
                    'UPDATE configuracion_negocio SET ancho_ticket=72 WHERE id=1'):
            with self.subTest(sql=sql), self.assertRaises(IntegrityError):
                with self.engine.begin() as conexion:
                    conexion.execute(text(sql))

    def test_migracion_agrega_tabla_faltante_sin_modificar_datos_existentes(self):
        self.assertRegex(self.engine.url.database, r'^puntocom_test_[a-f0-9]{32}$')
        self.addCleanup(aplicar_migracion_configuracion, self.engine)
        with self.engine.begin() as conexion:
            antes = {tabla: [tuple(fila) for fila in conexion.execute(
                text(f'SELECT * FROM {tabla} ORDER BY 1'))]
                for tabla in ('usuario', 'producto', 'movimiento_inventario', 'turno_caja')}
            conexion.execute(text('DROP TABLE configuracion_negocio'))
        aplicar_migracion_configuracion(self.engine)
        negocio = self._negocio()
        self.assertEqual({campo: negocio[campo] for campo in NEGOCIO_PREDETERMINADO},
                         NEGOCIO_PREDETERMINADO)
        with self.engine.connect() as conexion:
            despues = {tabla: [tuple(fila) for fila in conexion.execute(
                text(f'SELECT * FROM {tabla} ORDER BY 1'))] for tabla in antes}
        self.assertEqual(despues, antes)
