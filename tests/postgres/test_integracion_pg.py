"""Integración HTTP y SQL real sobre la base desechable del runner PostgreSQL.

Se conserva la protección CSRF y se autentican usuarios mediante el formulario
real. No se sustituyen consultas, sesiones SQLAlchemy ni servicios de negocio.
"""

import re
from uuid import uuid4
from decimal import Decimal
from html import unescape
from io import BytesIO

from openpyxl import load_workbook
from sqlalchemy import text

from entorno_pg import CasoPostgres


class IntegracionPostgres(CasoPostgres):
    def _uno(self, sql, **parametros):
        with self.engine.connect() as conexion:
            return conexion.execute(text(sql), parametros).mappings().one()

    def _valor(self, sql, **parametros):
        with self.engine.connect() as conexion:
            return conexion.execute(text(sql), parametros).scalar_one()

    def _escribir(self, sql, **parametros):
        with self.engine.begin() as conexion:
            conexion.execute(text(sql), parametros)

    def _csrf(self, pagina):
        respuesta = self.client.get(pagina)
        self.assertEqual(respuesta.status_code, 200, pagina)
        html = respuesta.get_data(as_text=True)
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
        if not token:
            token = re.search(r'data-csrf-token="([^"]+)"', html)
        self.assertIsNotNone(token, f"No se encontró el token CSRF en {pagina}")
        return unescape(token.group(1))

    def _login(self, usuario="admin"):
        respuesta = self.client.post('/login', data={
            'csrf_token': self._csrf('/login'),
            'correo': f'{usuario}@example.test',
            'password': 'PruebaPostgres2026!',
        })
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(respuesta.location, {
            'admin': '/', 'cajero1': '/pos', 'cajero2': '/pos',
            'bodeguero': '/inventario',
        }[usuario])

    def _administrador_con_caja(self):
        """Los descuentos y precios enviados solo los puede operar un admin."""
        self._login('admin')
        self.assertEqual(self._formulario('/caja', {
            'accion': 'abrir', 'fondo_inicial': '100.00',
        }).status_code, 302)
        self.ids['turno_admin'] = self._valor('''SELECT id_turno FROM turno_caja
            WHERE id_usuario=:id AND estado='ABIERTO' ''', id=self.ids['admin'])

    def _formulario(self, ruta, datos, pagina=None):
        return self.client.post(ruta, data={
            **datos, 'csrf_token': self._csrf(pagina or ruta),
        })

    def _payload(self, **cambios):
        datos = {
            'cliente_id': self.ids['cliente'], 'metodo': 'EFECTIVO',
            'recibido': '100.00',
            'items': [{'id': self.ids['producto1'], 'cantidad': 1, 'descuento': '0'}],
        }
        datos.update(cambios)
        return datos

    def _cobrar(self, **cambios):
        return self.client.post('/pos/cobrar', json=self._payload(**cambios),
                                headers={'X-CSRFToken': self._csrf('/pos'), 'Idempotency-Key': uuid4().hex})

    def _stock(self, producto='producto1'):
        return self._valor('SELECT existencia_actual FROM vista_inventario WHERE id_producto=:id',
                           id=self.ids[producto])

    def _estado_transaccional(self):
        tablas = ('venta', 'detalle_venta', 'compra', 'detalle_compra',
                  'movimiento_inventario', 'movimiento_caja', 'producto')
        conteos = {tabla: self._valor(f'SELECT COUNT(*) FROM {tabla}') for tabla in tablas}
        conteos['stock1'] = self._stock()
        conteos['stock2'] = self._stock('producto2')
        conteos['costo1'] = self._valor('SELECT costo FROM producto WHERE id_producto=:id',
                                      id=self.ids['producto1'])
        return conteos

    def test_autenticacion_csrf_y_permisos_con_usuarios_reales(self):
        self.assertEqual(self.client.get('/productos').status_code, 302)
        self.assertEqual(self.client.post('/login', data={
            'correo': 'admin@example.test', 'password': 'PruebaPostgres2026!',
        }).status_code, 400)
        respuesta = self.client.post('/login', data={
            'csrf_token': self._csrf('/login'), 'correo': 'admin@example.test',
            'password': 'incorrecta',
        })
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn('incorrectos', respuesta.get_data(as_text=True))
        self.assertEqual(self.client.get('/productos').status_code, 302)
        self._login('cajero1')
        self.assertEqual(self.client.get('/productos/nuevo').status_code, 403)
        self.assertEqual(self.client.get('/usuarios').status_code, 403)
        antes = self._estado_transaccional()
        self.assertEqual(self.client.post('/pos/cobrar', json=self._payload()).status_code, 400)
        respuesta = self._formulario('/compras/nueva', {}, '/productos')
        self.assertEqual(respuesta.status_code, 403)
        self.assertEqual(self._estado_transaccional(), antes)
        respuesta = self._formulario('/logout', {}, '/caja')
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(self.client.get('/pos').status_code, 302)

    def test_paginas_consultan_vistas_joins_y_agregados_postgresql(self):
        self._login()
        for ruta in ('/', '/productos', '/productos/nuevo', '/inventario',
                     '/gestion/categorias', '/gestion/clientes', '/gestion/proveedores',
                     '/pos', '/ventas', '/compras', '/caja', '/reportes',
                     '/usuarios', '/configuracion'):
            with self.subTest(ruta=ruta):
                self.assertEqual(self.client.get(ruta).status_code, 200)
        self.assertEqual(self.client.get('/productos/999999/editar').status_code, 404)
        self.assertEqual(self.client.get('/ventas/999999/ticket').status_code, 404)
        self.assertEqual(self.client.get('/gestion/desconocido').status_code, 404)

    def test_producto_alta_edicion_estado_y_movimiento_inicial(self):
        self._login()
        datos = {
            'id_categoria': self.ids['categoria'], 'codigo': 'INTEGRACION-NUEVO',
            'codigo_barras': '1234567890123', 'nombre': 'Cuaderno de integración',
            'marca': 'Escolar', 'unidad': 'unidad', 'costo': '8.45',
            'precio_venta': '14.95', 'stock_minimo': '4', 'estado': 'on',
            'stock_inicial': '7',
        }
        self.assertEqual(self._formulario('/productos/nuevo', datos).status_code, 302)
        producto = self._uno('SELECT * FROM producto WHERE codigo=:codigo', codigo=datos['codigo'])
        pid = producto['id_producto']
        self.assertEqual(producto['costo'], Decimal('8.45'))
        self.assertEqual(producto['precio_venta'], Decimal('14.95'))
        self.assertEqual(producto['id_categoria'], self.ids['categoria'])
        movimiento = self._uno('SELECT * FROM movimiento_inventario WHERE id_producto=:id', id=pid)
        self.assertEqual((movimiento['tipo'], movimiento['cantidad'], movimiento['costo_unitario']),
                         ('ENTRADA', 7, Decimal('8.45')))
        ruta = f'/productos/{pid}/editar'
        self.assertEqual(self._formulario(ruta, {**datos, 'precio_venta': '16.50',
                                               'stock_inicial': '99'}).status_code, 302)
        self.assertEqual(self._valor('SELECT precio_venta FROM producto WHERE id_producto=:id', id=pid),
                         Decimal('16.50'))
        self.assertEqual(self._valor('SELECT existencia_actual FROM vista_inventario WHERE id_producto=:id',
                                     id=pid), 7)
        self.assertEqual(self._formulario(f'/productos/{pid}/estado',
                                         {'estado': 'desactivar'}, '/productos').status_code, 302)
        self.assertFalse(self._valor('SELECT estado FROM producto WHERE id_producto=:id', id=pid))

    def test_producto_revierte_insert_si_stock_inicial_es_invalido(self):
        self._login()
        antes = self._estado_transaccional()
        respuesta = self._formulario('/productos/nuevo', {
            'id_categoria': self.ids['categoria'], 'codigo': 'DEBE-REVERTIRSE',
            'nombre': 'Producto inválido', 'marca': '', 'unidad': 'unidad',
            'costo': '2.50', 'precio_venta': '5', 'stock_minimo': '1',
            'stock_inicial': '-1', 'estado': 'on',
        })
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(self._estado_transaccional(), antes)
        self.assertEqual(self._valor("SELECT COUNT(*) FROM producto WHERE codigo='DEBE-REVERTIRSE'"), 0)

    def test_gestion_crea_edita_normaliza_y_rechaza_duplicados(self):
        self._login()
        casos = (
            ('categorias', 'categoria', 'id_categoria', 'nombre',
             {'nombre': 'Papelería nueva', 'descripcion': 'Material escolar'},
             {'nombre': 'Papelería editada', 'descripcion': 'Material escolar'}),
            ('clientes', 'cliente', 'id_cliente', 'nombre_razon_social',
             {'nombre_razon_social': 'Ana Pérez', 'nit_dpi': '1234567-k',
              'telefono': '55551234', 'correo': 'ANA@EXAMPLE.TEST', 'direccion_fiscal': 'Zona 1'},
             {'nombre_razon_social': 'Ana López', 'nit_dpi': '1234567-K',
              'telefono': '55551234', 'correo': 'ana@example.test', 'direccion_fiscal': 'Zona 2'}),
            ('proveedores', 'proveedor', 'id_proveedor', 'razon_social',
             {'razon_social': 'Distribuidora 123, S.A.', 'nit': '7654321-k',
              'contacto': 'Luis Pérez', 'telefono': '55554321', 'correo': 'VENTAS@EXAMPLE.TEST'},
             {'razon_social': 'Distribuidora 456, S.A.', 'nit': '7654321-K',
              'contacto': 'Luis Pérez', 'telefono': '55554321', 'correo': 'ventas@example.test'}),
        )
        for modulo, tabla, pk, campo, nuevo, editado in casos:
            with self.subTest(modulo=modulo):
                ruta = f'/gestion/{modulo}'
                self.assertEqual(self._formulario(ruta, nuevo).status_code, 302)
                registro = self._uno(f'SELECT * FROM {tabla} WHERE {campo}=:nombre', nombre=nuevo[campo])
                if modulo != 'categorias':
                    self.assertEqual(registro['correo'], nuevo['correo'].lower())
                self.assertEqual(self._formulario(ruta, {**editado, 'id': registro[pk]}).status_code, 302)
                self.assertEqual(self._valor(f'SELECT {campo} FROM {tabla} WHERE {pk}=:id', id=registro[pk]),
                                 editado[campo])
                cantidad = self._valor(f'SELECT COUNT(*) FROM {tabla}')
                self.assertEqual(self._formulario(ruta, editado).status_code, 302)
                self.assertEqual(self._valor(f'SELECT COUNT(*) FROM {tabla}'), cantidad)

    def test_compra_actualiza_relaciones_stock_y_costo(self):
        self._login()
        respuesta = self._formulario('/compras/nueva', {
            'id_proveedor': self.ids['proveedor'], 'id_producto': self.ids['producto1'],
            'cantidad': '3', 'costo_unitario': '11.25', 'metodo_pago': 'CRÉDITO',
            'numero_factura': 'FAC-INTEGRACION-001',
        }, '/compras')
        self.assertEqual(respuesta.status_code, 302)
        compra = self._uno("SELECT * FROM compra WHERE numero_factura='FAC-INTEGRACION-001'")
        self.assertEqual((compra['id_usuario'], compra['id_proveedor'], compra['total']),
                         (self.ids['admin'], self.ids['proveedor'], Decimal('33.75')))
        detalle = self._uno('SELECT * FROM detalle_compra WHERE id_compra=:id', id=compra['id_compra'])
        self.assertEqual((detalle['id_producto'], detalle['cantidad'], detalle['costo_unitario']),
                         (self.ids['producto1'], 3, Decimal('11.25')))
        movimiento = self._uno('SELECT * FROM movimiento_inventario WHERE motivo=:motivo',
                               motivo=f"Compra #{compra['id_compra']}")
        self.assertEqual((movimiento['tipo'], movimiento['cantidad'], movimiento['costo_unitario']),
                         ('ENTRADA', 3, Decimal('11.25')))
        self.assertEqual(self._stock(), 13)
        self.assertEqual(self._valor('SELECT costo FROM producto WHERE id_producto=:id',
                                     id=self.ids['producto1']), Decimal('11.25'))
        antes = self._estado_transaccional()
        self.assertEqual(self._formulario('/compras/nueva', {
            'id_proveedor': '999999', 'id_producto': self.ids['producto1'],
            'cantidad': '3', 'costo_unitario': '4.00', 'metodo_pago': 'EFECTIVO',
        }, '/compras').status_code, 302)
        self.assertEqual(self._estado_transaccional(), antes)

    def test_movimientos_inventario_y_salida_insuficiente(self):
        self._login()
        for tipo, cantidad, esperado in (('ENTRADA', 4, 14), ('SALIDA', 3, 11)):
            respuesta = self._formulario('/inventario/movimiento', {
                'id_producto': self.ids['producto1'], 'tipo': tipo,
                'cantidad': cantidad, 'motivo': 'Ajuste de inventario',
            }, '/inventario')
            self.assertEqual(respuesta.status_code, 302)
            self.assertEqual(self._stock(), esperado)
        antes = self._estado_transaccional()
        respuesta = self._formulario('/inventario/movimiento', {
            'id_producto': self.ids['producto1'], 'tipo': 'SALIDA',
            'cantidad': '12', 'motivo': 'Salida excesiva',
        }, '/inventario')
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(self._estado_transaccional(), antes)

    def test_cobro_efectivo_recalcula_importes_y_conserva_relaciones(self):
        self._administrador_con_caja()
        respuesta = self._cobrar(recibido='50.00', total='0.01', items=[{
            'id': self.ids['producto1'], 'cantidad': 2, 'descuento': '1.25',
            'precio': '0.01', 'precio_unitario': '0.01', 'costo': '0.01',
        }])
        self.assertEqual(respuesta.status_code, 200)
        resultado = respuesta.get_json()
        self.assertTrue(resultado['ok'])
        self.assertEqual((resultado['total'], resultado['cambio']), ('28.75', '21.25'))
        venta = self._uno('SELECT * FROM venta WHERE id_venta=:id', id=resultado['id_venta'])
        self.assertEqual((venta['id_cliente'], venta['id_usuario'], venta['id_turno']),
                         (self.ids['cliente'], self.ids['admin'], self.ids['turno_admin']))
        self.assertEqual((venta['estado'], venta['metodo_pago'], venta['total']),
                         ('COMPLETADA', 'EFECTIVO', Decimal('28.75')))
        detalle = self._uno('SELECT * FROM detalle_venta WHERE id_venta=:id', id=venta['id_venta'])
        self.assertEqual((detalle['cantidad'], detalle['precio_unitario'],
                          detalle['costo_unitario_historico'], detalle['descuento']),
                         (2, Decimal('15'), Decimal('9'), Decimal('1.25')))
        caja = self._uno('SELECT * FROM movimiento_caja WHERE concepto=:concepto',
                         concepto=f"Venta {resultado['numero']}")
        self.assertEqual((caja['id_turno'], caja['tipo'], caja['monto']),
                         (self.ids['turno_admin'], 'INGRESO', Decimal('28.75')))
        salida = self._uno('SELECT * FROM movimiento_inventario WHERE motivo=:motivo',
                           motivo=f"Venta {resultado['numero']}")
        self.assertEqual((salida['tipo'], salida['cantidad'], salida['costo_unitario']),
                         ('SALIDA', 2, Decimal('9')))
        self.assertEqual((self._stock(), self._stock('producto2')), (8, 10))
        ticket = self.client.get(f"/ventas/{venta['id_venta']}/ticket")
        self.assertEqual(ticket.status_code, 200)
        self.assertIn(resultado['numero'], ticket.get_data(as_text=True))
        self.assertIn('Q 28.75', ticket.get_data(as_text=True))
        nombre = self._valor('SELECT nombre FROM producto WHERE id_producto=:id', id=self.ids['producto1'])
        self.assertIn(nombre, ticket.get_data(as_text=True))

    def test_tarjeta_transferencia_y_descuento_total_sin_ingreso_de_caja(self):
        self._administrador_con_caja()
        for metodo in ('TARJETA', 'TRANSFERENCIA'):
            with self.subTest(metodo=metodo):
                respuesta = self._cobrar(metodo=metodo, recibido='0')
                self.assertEqual(respuesta.status_code, 200)
                self.assertEqual(respuesta.get_json()['cambio'], '0.00')
        respuesta = self._cobrar(recibido='0', items=[{
            'id': self.ids['producto1'], 'cantidad': 1, 'descuento': '15.00',
        }])
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.get_json()['total'], '0.00')
        self.assertEqual(self._valor('SELECT COUNT(*) FROM venta'), 3)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM movimiento_caja'), 0)
        self.assertEqual(self._stock(), 7)

    def test_cobros_invalidos_no_dejan_ventas_detalles_movimientos_ni_caja(self):
        self._administrador_con_caja()
        pid = self.ids['producto1']
        casos = (
            {'cliente_id': 999999}, {'metodo': 'CRÉDITO'}, {'recibido': '0'},
            {'items': []}, {'items': [{'id': pid, 'cantidad': 11}]},
            {'items': [{'id': pid, 'cantidad': 1.5}]},
            {'items': [{'id': pid, 'cantidad': 1, 'descuento': '15.01'}]},
            {'items': [{'id': pid, 'cantidad': 1}, {'id': pid, 'cantidad': 1}]},
            {'items': [{'id': pid, 'cantidad': 1},
                       {'id': self.ids['producto2'], 'cantidad': 11}]},
            {'recibido': 'NaN'},
        )
        antes = self._estado_transaccional()
        for cambios in casos:
            with self.subTest(cambios=cambios):
                respuesta = self._cobrar(**cambios)
                self.assertEqual(respuesta.status_code, 400)
                self.assertFalse(respuesta.get_json()['ok'])
                self.assertEqual(self._estado_transaccional(), antes)
        self._escribir('UPDATE producto SET estado=FALSE WHERE id_producto=:id', id=pid)
        self.assertEqual(self._cobrar().status_code, 400)
        self.assertEqual(self._estado_transaccional(), antes)

    def test_error_postgresql_tardio_revierte_toda_la_venta(self):
        self._login('cajero1')
        antes = self._estado_transaccional()
        # Falla la última escritura, cuando venta, detalles y salidas ya existen
        # dentro de la transacción. El fallo viene del servidor PostgreSQL real.
        self._escribir("""CREATE FUNCTION pc_prueba_rechazar_ingreso() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                RAISE EXCEPTION 'Fallo intencional de integración';
            END $$""")
        try:
            self._escribir('''CREATE TRIGGER pc_prueba_fallo_caja BEFORE INSERT
                ON movimiento_caja FOR EACH ROW EXECUTE FUNCTION pc_prueba_rechazar_ingreso()''')
            try:
                with self.assertLogs(self.app.app.logger, level='ERROR'):
                    respuesta = self._cobrar()
                self.assertEqual(respuesta.status_code, 500)
                self.assertFalse(respuesta.get_json()['ok'])
                self.assertEqual(self._estado_transaccional(), antes)
            finally:
                self._escribir('DROP TRIGGER pc_prueba_fallo_caja ON movimiento_caja')
        finally:
            self._escribir('DROP FUNCTION pc_prueba_rechazar_ingreso()')
        # Una sesión que sufrió una excepción puede volver a cobrar después
        # del rollback y no conserva ninguna escritura de la venta fallida.
        self.assertEqual(self._cobrar().status_code, 200)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM venta'), 1)

    def test_caja_movimientos_cierre_reapertura_y_bloqueo_de_cobro(self):
        self._login('cajero1')
        for tipo, monto in (('INGRESO', '12.50'), ('EGRESO', '4.25')):
            self.assertEqual(self._formulario('/caja', {
                'accion': 'movimiento', 'tipo': tipo, 'monto': monto,
                'concepto': 'Movimiento de integración',
            }).status_code, 302)
        self.assertIn('Q 108.25', self.client.get('/caja').get_data(as_text=True))
        self._formulario('/caja', {'accion': 'abrir', 'fondo_inicial': '50'})
        self.assertEqual(self._valor("SELECT COUNT(*) FROM turno_caja WHERE id_usuario=:id AND estado='ABIERTO'",
                                     id=self.ids['cajero1']), 1)
        self.assertEqual(self._formulario('/caja', {
            'accion': 'cerrar', 'efectivo_contado': '108.25',
        }).status_code, 302)
        turno = self._uno('SELECT * FROM turno_caja WHERE id_turno=:id', id=self.ids['turno1'])
        self.assertEqual((turno['estado'], turno['efectivo_contado']), ('CERRADO', Decimal('108.25')))
        self.assertIsNotNone(turno['cierre'])
        antes = self._estado_transaccional()
        self.assertEqual(self._cobrar().status_code, 400)
        self.assertEqual(self._estado_transaccional(), antes)
        self.assertEqual(self._formulario('/caja', {
            'accion': 'abrir', 'fondo_inicial': '50.00',
        }).status_code, 302)
        nuevo = self._uno("SELECT * FROM turno_caja WHERE id_usuario=:id AND estado='ABIERTO'",
                          id=self.ids['cajero1'])
        self.assertNotEqual(nuevo['id_turno'], self.ids['turno1'])
        self.assertEqual(nuevo['fondo_inicial'], Decimal('50.00'))
        resultado = self._cobrar().get_json()
        self.assertTrue(resultado['ok'])
        self.assertEqual(self._valor('SELECT id_turno FROM venta WHERE id_venta=:id', id=resultado['id_venta']),
                         nuevo['id_turno'])

    def test_reportes_y_excel_respetan_limites_de_fecha_y_estado(self):
        self._login('cajero1')
        fechas = ('2026-01-14 23:59:59', '2026-01-15 00:00:00',
                  '2026-01-15 23:59:59', '2026-01-16 00:00:00', '2026-01-15 12:00:00')
        tickets_incluidos = []
        for posicion, fecha in enumerate(fechas):
            respuesta = self._cobrar(metodo='TARJETA', recibido='0')
            self.assertEqual(respuesta.status_code, 200)
            venta = respuesta.get_json()
            self._escribir('UPDATE venta SET fecha_hora=:fecha,fecha_confirmacion=:fecha,estado=:estado WHERE id_venta=:id',
                           fecha=fecha, estado='ANULADA' if posicion == 4 else 'COMPLETADA',
                           id=venta['id_venta'])
            if posicion in (1, 2):
                tickets_incluidos.append(venta['numero'])
        reporte = self.client.get('/reportes?inicio=2026-01-15&fin=2026-01-15')
        self.assertEqual(reporte.status_code, 200)
        html = reporte.get_data(as_text=True)
        self.assertRegex(html, r'Ventas en el período</small>\s*<strong>2</strong>')
        self.assertRegex(html, r'Total vendido</small>\s*<strong>Q 30\.00</strong>')
        contenidos = []
        for ruta in ('/reportes/excel?desde=2026-01-15&hasta=2026-01-15',
                     '/reportes/ventas/excel?inicio=2026-01-15&fin=2026-01-15'):
            respuesta = self.client.get(ruta)
            self.assertEqual(respuesta.status_code, 200)
            self.assertIn('spreadsheetml.sheet', respuesta.mimetype)
            with BytesIO(respuesta.data) as archivo:
                libro = load_workbook(archivo)
                try:
                    filas = list(libro.active.values)
                    self.assertEqual(len(filas), 3)
                    self.assertEqual([fila[0] for fila in filas[1:]], list(reversed(tickets_incluidos)))
                    self.assertEqual([fila[5] for fila in filas[1:]], [15, 15])
                    self.assertTrue(all(fila[1].date().isoformat() == '2026-01-15' for fila in filas[1:]))
                    contenidos.append(filas)
                finally:
                    libro.close()
        self.assertEqual(contenidos[0], contenidos[1])
        self.assertEqual(self.client.get('/reportes/excel?inicio=2026-01-16&fin=2026-01-15').status_code, 302)

    def test_dashboard_calcula_ganancia_y_alertas_sin_limitar_contador_a_veinte(self):
        self._administrador_con_caja()
        resultado = self._cobrar(items=[{
            'id': self.ids['producto1'], 'cantidad': 2, 'descuento': '1.25',
        }]).get_json()
        self.assertTrue(resultado['ok'])
        self._escribir('UPDATE venta SET fecha_hora=CURRENT_TIMESTAMP WHERE id_venta=:id',
                       id=resultado['id_venta'])
        self._escribir('''INSERT INTO producto(id_categoria,codigo,nombre,unidad,costo,precio_venta,stock_minimo,estado)
            SELECT :categoria,'ALERTA-' || numero,'Producto alerta ' || numero,'Unidad',1,2,5,TRUE
            FROM generate_series(1,25) numero''', categoria=self.ids['categoria'])
        respuesta = self.client.get('/')
        self.assertEqual(respuesta.status_code, 200)
        html = respuesta.get_data(as_text=True)
        self.assertRegex(html, r'Ventas de hoy</small>\s*<strong>Q 28\.75</strong>')
        self.assertRegex(html, r'Ganancia bruta estimada</small>\s*<strong>Q 10\.75</strong>')
        self.assertRegex(html, r'Stock bajo o agotado</small>\s*<strong>25</strong>')
        self.assertIn(resultado['numero'], html)
