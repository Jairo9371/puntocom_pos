"""Códigos internos automáticos sobre HTTP, CSRF y PostgreSQL reales."""

import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from html import unescape
from threading import Barrier

from sqlalchemy import text

from entorno_pg import CasoPostgres


class CodigoProductoPostgres(CasoPostgres):
    def _valor(self, sql, **parametros):
        with self.engine.connect() as conexion:
            return conexion.execute(text(sql), parametros).scalar_one()

    def _uno(self, sql, **parametros):
        with self.engine.connect() as conexion:
            return dict(conexion.execute(text(sql), parametros).mappings().one())

    def _escribir(self, sql, **parametros):
        with self.engine.begin() as conexion:
            conexion.execute(text(sql), parametros)

    def _csrf(self, ruta, cliente=None):
        cliente = cliente or self.client
        respuesta = cliente.get(ruta)
        self.assertEqual(respuesta.status_code, 200, ruta)
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"',
                          respuesta.get_data(as_text=True))
        self.assertIsNotNone(token, ruta)
        return unescape(token.group(1))

    def _login(self, usuario='admin', cliente=None):
        cliente = cliente or self.client
        respuesta = cliente.post('/login', data={
            'csrf_token': self._csrf('/login', cliente),
            'correo': f'{usuario}@example.test', 'password': 'PruebaPostgres2026!',
        })
        self.assertEqual(respuesta.status_code, 302)

    def _datos(self, **cambios):
        datos = {
            'id_categoria': self.ids['categoria'], 'nombre': 'Producto automático',
            'codigo_barras': '7501234567890', 'marca': 'Escolar', 'unidad': 'Unidad',
            'costo': '8.45', 'precio_venta': '14.95', 'stock_minimo': '4',
            'stock_inicial': '7', 'estado': 'on',
        }
        datos.update(cambios)
        return datos

    def _guardar(self, datos, ruta='/productos/nuevo', cliente=None):
        cliente = cliente or self.client
        return cliente.post(ruta, data={**datos, 'csrf_token': self._csrf(ruta, cliente)})

    def _reiniciar_serial(self, siguiente):
        # Solo el motor de CasoPostgres, cuya base desechable crea el runner.
        self.assertRegex(self.engine.url.database, r'^puntocom_test_[a-f0-9]{32}$')
        self._escribir("SELECT setval(pg_get_serial_sequence('public.producto',"
                       "'id_producto'), :siguiente, false)", siguiente=siguiente)

    def _estado_serial(self):
        # El identificador procede de metadatos del esquema de pruebas.
        secuencia = self._valor("SELECT pg_get_serial_sequence('public.producto','id_producto')")
        with self.engine.connect() as conexion:
            return tuple(conexion.exec_driver_sql(f'SELECT last_value,is_called FROM {secuencia}').one())

    def test_alta_omite_o_deja_codigo_vacio_y_conserva_codigos_explicitos(self):
        self._login()
        for indice, codigo in enumerate((None, '', '   ', 'LEGADO-001')):
            with self.subTest(codigo=codigo):
                datos = self._datos(nombre=f'Nuevo automático {indice}',
                                    codigo_barras=f'750123456789{indice}')
                if codigo is not None:
                    datos['codigo'] = codigo
                self.assertEqual(self._guardar(datos).status_code, 302)
                producto = self._uno('SELECT * FROM producto WHERE nombre=:nombre',
                                     nombre=datos['nombre'])
                if codigo == 'LEGADO-001':
                    self.assertEqual(producto['codigo'], codigo)
                else:
                    self.assertRegex(producto['codigo'], r'^PC-\d{6,}$')
                self.assertEqual(producto['codigo_barras'], datos['codigo_barras'])
                self.assertEqual(producto['precio_venta'], Decimal('14.95'))
                movimiento = self._uno('''SELECT tipo,cantidad,costo_unitario
                    FROM movimiento_inventario WHERE id_producto=:id''', id=producto['id_producto'])
                self.assertEqual(movimiento, {'tipo': 'ENTRADA', 'cantidad': 7,
                                             'costo_unitario': Decimal('8.45')})
        self.assertEqual(self._valor('SELECT COUNT(DISTINCT codigo) FROM producto'), 6)

    def test_visitar_formulario_nuevo_no_reserva_ni_consume_codigos(self):
        self._login()
        antes = self._estado_serial()
        for _ in range(2):
            respuesta = self.client.get('/productos/nuevo')
            self.assertEqual(respuesta.status_code, 200)
            self.assertNotRegex(respuesta.get_data(as_text=True),
                                r'<input\b[^>]*name="codigo"[^>]*\brequired\b')
        self.assertEqual(self._estado_serial(), antes)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM producto'), 2)

    def test_editar_sin_codigo_conserva_el_existente_y_actualiza_datos(self):
        self._login()
        self._escribir("UPDATE producto SET codigo_barras='1234567890123' WHERE id_producto=1")
        self.assertEqual(self._guardar(self._datos(
            nombre='Cuaderno editado', precio_venta='19.50', codigo_barras='1234567890123',
        ), '/productos/1/editar').status_code, 302)
        producto = self._uno('SELECT codigo,codigo_barras,nombre,precio_venta FROM producto WHERE id_producto=1')
        self.assertEqual(producto, {'codigo': 'PG001', 'codigo_barras': '1234567890123',
                                    'nombre': 'Cuaderno editado', 'precio_venta': Decimal('19.50')})
        self.assertEqual(self._valor('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1'), 10)

    def test_salta_colisiones_con_codigos_activos_e_inactivos(self):
        self._login()
        self._escribir("UPDATE producto SET codigo='PC-000003' WHERE id_producto=1")
        self._escribir("UPDATE producto SET codigo='PC-000004',estado=FALSE WHERE id_producto=2")
        self._reiniciar_serial(3)
        self.assertEqual(self._guardar(self._datos()).status_code, 302)
        producto = self._uno('SELECT id_producto,codigo FROM producto WHERE nombre=:nombre',
                             nombre='Producto automático')
        self.assertEqual(producto, {'id_producto': 5, 'codigo': 'PC-000005'})
        self.assertEqual(self._valor('SELECT codigo FROM producto WHERE id_producto=1'), 'PC-000003')
        self.assertEqual(self._valor('SELECT codigo FROM producto WHERE id_producto=2'), 'PC-000004')
        self.assertFalse(self._valor('SELECT estado FROM producto WHERE id_producto=2'))
        self.assertEqual(self._valor('SELECT COUNT(*) FROM movimiento_inventario WHERE id_producto=5'), 1)

    def test_codigo_no_trunca_numeros_mayores_a_seis_digitos(self):
        self._login()
        self._reiniciar_serial(1000000)
        self.assertEqual(self._guardar(self._datos()).status_code, 302)
        producto = self._uno('SELECT id_producto,codigo FROM producto WHERE nombre=:nombre',
                             nombre='Producto automático')
        self.assertEqual(producto, {'id_producto': 1000000, 'codigo': 'PC-1000000'})

    def test_dos_altas_concurrentes_tienen_codigos_distintos(self):
        # Dos administradores evitan que el bloqueo del usuario serialice las altas.
        self._escribir('UPDATE usuario SET id_rol=1 WHERE id_usuario=2')
        clientes = [self.client, self.app.app.test_client()]
        for usuario, cliente in zip(('admin', 'cajero1'), clientes):
            self._login(usuario, cliente)
        tokens = [self._csrf('/productos/nuevo', cliente) for cliente in clientes]
        inicio = Barrier(2, timeout=5)

        def guardar(indice):
            inicio.wait()
            return clientes[indice].post('/productos/nuevo', data={
                **self._datos(nombre=f'Concurrente {indice}', codigo_barras=f'CONC-{indice}'),
                'csrf_token': tokens[indice],
            })

        with ThreadPoolExecutor(max_workers=2) as pool:
            futuros = [pool.submit(guardar, indice) for indice in range(2)]
            respuestas = [futuro.result(timeout=15) for futuro in futuros]
        self.assertEqual([respuesta.status_code for respuesta in respuestas], [302, 302])
        with self.engine.connect() as conexion:
            productos = conexion.execute(text('''SELECT id_producto,codigo FROM producto
                WHERE nombre LIKE 'Concurrente %' ORDER BY id_producto''')).mappings().all()
        self.assertEqual(len(productos), 2)
        self.assertEqual(len({producto['codigo'] for producto in productos}), 2)
        for producto in productos:
            self.assertRegex(producto['codigo'], r'^PC-\d{6,}$')
            self.assertEqual(self._valor('''SELECT existencia_actual FROM vista_inventario
                WHERE id_producto=:id''', id=producto['id_producto']), 7)

    def test_stock_inicial_invalido_revierte_producto_y_movimientos(self):
        self._login()
        productos = self._valor('SELECT COUNT(*) FROM producto')
        movimientos = self._valor('SELECT COUNT(*) FROM movimiento_inventario')
        respuesta = self._guardar(self._datos(nombre='Alta inválida', stock_inicial='-1'))
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM producto'), productos)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM movimiento_inventario'), movimientos)
        self.assertEqual(self._valor("SELECT COUNT(*) FROM producto WHERE nombre='Alta inválida'"), 0)
        # El intento posterior funciona aunque la secuencia haya consumido un número.
        self.assertEqual(self._guardar(self._datos()).status_code, 302)
        self.assertRegex(self._valor("SELECT codigo FROM producto WHERE nombre='Producto automático'"),
                         r'^PC-\d{6,}$')
