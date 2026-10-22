"""Bajas físicas y lógicas con PostgreSQL real, HTTP, permisos y CSRF.

Solo se ejecuta mediante ejecutar.py y la base desechable del runner. Las
relaciones y bloqueos son los de PostgreSQL: no se sustituyen consultas SQL.
"""

import re
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from threading import Event, current_thread
from time import monotonic

from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from entorno_pg import CasoPostgres, HASH_PRUEBA


class BajasPostgres(CasoPostgres):
    MODULOS = {
        'categorias': ('categoria', 'id_categoria'),
        'clientes': ('cliente', 'id_cliente'),
        'proveedores': ('proveedor', 'id_proveedor'),
        'usuarios': ('usuario', 'id_usuario'),
    }
    ESPERA = 5

    def _valor(self, sql, **parametros):
        with self.engine.connect() as conexion:
            return conexion.execute(text(sql), parametros).scalar_one()

    def _escribir(self, sql, **parametros):
        with self.engine.begin() as conexion:
            conexion.execute(text(sql), parametros)

    def _insertar(self, sql, **parametros):
        with self.engine.begin() as conexion:
            return conexion.execute(text(sql), parametros).scalar_one()

    def _csrf(self, pagina, cliente=None):
        cliente = cliente or self.client
        respuesta = cliente.get(pagina)
        self.assertEqual(respuesta.status_code, 200, pagina)
        html = respuesta.get_data(as_text=True)
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
        if not token:
            token = re.search(r'data-csrf-token="([^"]+)"', html)
        self.assertIsNotNone(token, f'No se encontró CSRF en {pagina}.')
        return unescape(token.group(1))

    def _login(self, usuario='admin', cliente=None):
        cliente = cliente or self.client
        respuesta = cliente.post('/login', data={
            'csrf_token': self._csrf('/login', cliente),
            'correo': f'{usuario}@example.test', 'password': 'PruebaPostgres2026!',
        })
        self.assertEqual(respuesta.status_code, 302)
        rol = self._valor('''SELECT r.nombre FROM usuario u JOIN rol r USING(id_rol)
            WHERE u.correo=:correo''', correo=f'{usuario}@example.test')
        self.assertEqual(respuesta.location, {
            'Administrador': '/', 'Cajero': '/pos', 'Bodeguero': '/inventario',
        }[rol])

    def _pagina(self, modulo):
        return '/usuarios' if modulo == 'usuarios' else f'/gestion/{modulo}'

    def _ruta(self, modulo, rid, accion):
        prefijo = '/usuarios' if modulo == 'usuarios' else f'/gestion/{modulo}'
        return f'{prefijo}/{rid}/{accion}'

    def _accion(self, modulo, rid, accion, estado=None):
        datos = {'csrf_token': self._csrf(self._pagina(modulo))}
        if estado is not None:
            datos['estado'] = estado
        return self.client.post(self._ruta(modulo, rid, accion), data=datos)

    def _estado(self, modulo, rid):
        tabla, pk = self.MODULOS[modulo]
        return self._valor(f'SELECT estado FROM {tabla} WHERE {pk}=:id', id=rid)

    def _existe(self, modulo, rid):
        tabla, pk = self.MODULOS[modulo]
        return self._valor(f'SELECT COUNT(*) FROM {tabla} WHERE {pk}=:id', id=rid)

    def _nuevo(self, modulo, activo=True):
        tabla, pk = self.MODULOS[modulo]
        numero = self._valor(f'SELECT COALESCE(MAX({pk}),0)+1 FROM {tabla}')
        if modulo == 'categorias':
            return self._insertar('''INSERT INTO categoria(nombre,estado)
                VALUES(:nombre,:estado) RETURNING id_categoria''',
                nombre=f'Categoría sin relaciones {numero}', estado=activo)
        if modulo == 'clientes':
            return self._insertar('''INSERT INTO cliente(nombre_razon_social,nit_dpi,estado)
                VALUES(:nombre,:nit,:estado) RETURNING id_cliente''',
                nombre=f'Cliente sin relaciones {numero}', nit=f'{numero}1234567-K', estado=activo)
        if modulo == 'proveedores':
            return self._insertar('''INSERT INTO proveedor(razon_social,nit,estado)
                VALUES(:nombre,:nit,:estado) RETURNING id_proveedor''',
                nombre=f'Proveedor sin relaciones {numero}', nit=f'{numero}7654321-K', estado=activo)
        return self._insertar('''INSERT INTO usuario(id_rol,nombre,correo,hash_contrasena,estado)
            VALUES(2,:nombre,:correo,:hash,:estado) RETURNING id_usuario''',
            nombre=f'Usuario sin historial {numero}', correo=f'nuevo{numero}@example.test',
            hash=HASH_PRUEBA, estado=activo)

    def _venta_sql(self, cliente=None, usuario=None, estado='COMPLETADA'):
        numero = self._valor('SELECT COALESCE(MAX(id_venta),0)+1 FROM venta')
        return self._insertar('''INSERT INTO venta(id_cliente,id_usuario,id_turno,
            numero_ticket,metodo_pago,estado,total)
            VALUES(:cliente,:usuario,1,:ticket,'TARJETA',:estado,15) RETURNING id_venta''',
            cliente=cliente or self.ids['cliente'], usuario=usuario or self.ids['cajero1'],
            ticket=f'BAJAS-{numero}', estado=estado)

    def _compra_sql(self, proveedor=None, usuario=None):
        return self._insertar('''INSERT INTO compra(id_proveedor,id_usuario,numero_factura,
            metodo_pago,total) VALUES(:proveedor,:usuario,'BAJAS-FAC','CRÉDITO',9)
            RETURNING id_compra''', proveedor=proveedor or self.ids['proveedor'],
            usuario=usuario or self.ids['admin'])

    def _cobrar(self, cliente):
        return self.client.post('/pos/cobrar', json={
            'cliente_id': cliente, 'metodo': 'EFECTIVO', 'recibido': '100.00',
            'items': [{'id': self.ids['producto1'], 'cantidad': 1, 'descuento': '0'}],
        }, headers={'X-CSRFToken': self._csrf('/pos'), 'Idempotency-Key': uuid4().hex})

    def _comprar(self, proveedor):
        return self.client.post('/compras/nueva', data={
            'csrf_token': self._csrf('/compras'), 'id_proveedor': proveedor,
            'id_producto': self.ids['producto1'], 'cantidad': '2',
            'costo_unitario': '9.50', 'metodo_pago': 'CRÉDITO',
            'numero_factura': 'BAJAS-PROVEEDOR',
        })

    def _producto_datos(self, categoria, codigo='BAJAS-NUEVO'):
        return {
            'id_categoria': categoria, 'codigo': codigo, 'nombre': 'Producto de prueba',
            'unidad': 'Unidad', 'costo': '9', 'precio_venta': '15',
            'stock_minimo': '2', 'stock_inicial': '0', 'estado': 'on',
        }

    def _formulario(self, ruta, datos, pagina=None):
        return self.client.post(ruta, data={
            **datos, 'csrf_token': self._csrf(pagina or ruta),
        })

    def _select(self, pagina, nombre):
        respuesta = self.client.get(pagina)
        self.assertEqual(respuesta.status_code, 200)
        elemento = re.search(
            rf'<select\b(?=[^>]*\b(?:name|id)="{re.escape(nombre)}")[^>]*>(.*?)</select\s*>',
            respuesta.get_data(as_text=True), flags=re.S,
        )
        self.assertIsNotNone(elemento, f'No se encontró el selector {nombre} en {pagina}.')
        return elemento.group(1)

    def _ids_select(self, pagina, nombre):
        return {int(rid) for rid in re.findall(r'<option\b[^>]*\bvalue="(\d+)"',
                                              self._select(pagina, nombre))}

    def test_estado_repetido_es_explicito_idempotente_y_reversible(self):
        self._login()
        for modulo in self.MODULOS:
            with self.subTest(modulo=modulo):
                rid = self._nuevo(modulo)
                for accion, esperado in (('desactivar', False), ('desactivar', False),
                                         ('activar', True), ('activar', True)):
                    respuesta = self._accion(modulo, rid, 'estado', accion)
                    self.assertEqual(respuesta.status_code, 302)
                    self.assertIs(self._estado(modulo, rid), esperado)
                    self.assertEqual(self._existe(modulo, rid), 1)

    def test_elimina_solo_registros_libres_activos_o_inactivos(self):
        self._login()
        for modulo in self.MODULOS:
            for activo in (True, False):
                with self.subTest(modulo=modulo, activo=activo):
                    rid = self._nuevo(modulo, activo=activo)
                    respuesta = self._accion(modulo, rid, 'eliminar')
                    self.assertEqual(respuesta.status_code, 302)
                    self.assertEqual(self._existe(modulo, rid), 0)
        # Eliminar maestros libres conserva todos los datos precargados.
        self.assertEqual(self._valor('SELECT COUNT(*) FROM producto'), 2)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM turno_caja'), 2)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM movimiento_inventario'), 2)

    def test_categorias_con_productos_activos_o_inactivos_no_se_eliminan(self):
        self._login()
        for activo in (True, False):
            with self.subTest(productos_activos=activo):
                self._escribir('UPDATE producto SET estado=:estado', estado=activo)
                respuesta = self._accion('categorias', self.ids['categoria'], 'eliminar')
                self.assertEqual(respuesta.status_code, 302)
                self.assertEqual(self._existe('categorias', self.ids['categoria']), 1)
                self.assertEqual(self._valor('SELECT COUNT(*) FROM producto'), 2)
        self.assertEqual(self._accion('categorias', 1, 'estado', 'desactivar').status_code, 302)
        self.assertFalse(self._estado('categorias', 1))
        self.assertEqual(self._valor('SELECT COUNT(*) FROM producto'), 2)

    def test_cliente_con_venta_completada_o_anulada_conserva_historial(self):
        self._login()
        for estado in ('COMPLETADA', 'ANULADA'):
            with self.subTest(estado_venta=estado):
                rid = self._nuevo('clientes')
                venta = self._venta_sql(cliente=rid, estado=estado)
                self.assertEqual(self._accion('clientes', rid, 'eliminar').status_code, 302)
                self.assertEqual(self._existe('clientes', rid), 1)
                self.assertEqual(self._accion('clientes', rid, 'estado', 'desactivar').status_code, 302)
                self.assertFalse(self._estado('clientes', rid))
                self.assertEqual(self._valor('SELECT id_cliente FROM venta WHERE id_venta=:id',
                                             id=venta), rid)

    def test_proveedor_con_compra_conserva_historial_y_puede_desactivarse(self):
        self._login()
        rid = self._nuevo('proveedores')
        compra = self._compra_sql(proveedor=rid)
        self.assertEqual(self._accion('proveedores', rid, 'eliminar').status_code, 302)
        self.assertEqual(self._existe('proveedores', rid), 1)
        self.assertEqual(self._accion('proveedores', rid, 'estado', 'desactivar').status_code, 302)
        self.assertFalse(self._estado('proveedores', rid))
        self.assertEqual(self._valor('SELECT id_proveedor FROM compra WHERE id_compra=:id', id=compra), rid)

    def test_relacion_adicional_con_cascade_tambien_impide_eliminacion(self):
        self._login()
        rid = self._nuevo('categorias')
        self._escribir('''CREATE TABLE relacion_externa_bajas(
            id SERIAL PRIMARY KEY,
            id_categoria INTEGER NOT NULL REFERENCES categoria(id_categoria) ON DELETE CASCADE,
            nota TEXT NOT NULL)''')
        try:
            self._escribir('''INSERT INTO relacion_externa_bajas(id_categoria,nota)
                VALUES(:categoria,'Historial externo que debe conservarse')''', categoria=rid)
            self.assertEqual(self._accion('categorias', rid, 'eliminar').status_code, 302)
            self.assertEqual(self._existe('categorias', rid), 1)
            self.assertEqual(self._valor('SELECT COUNT(*) FROM relacion_externa_bajas'), 1)
        finally:
            self._escribir('DROP TABLE relacion_externa_bajas')

    def test_usuario_con_venta_compra_o_turno_cerrado_no_se_elimina(self):
        self._login()
        for relacion in ('venta', 'compra', 'turno_caja'):
            with self.subTest(relacion=relacion):
                rid = self._nuevo('usuarios')
                if relacion == 'venta':
                    self._venta_sql(usuario=rid)
                elif relacion == 'compra':
                    self._compra_sql(usuario=rid)
                else:
                    self._escribir('''INSERT INTO turno_caja(id_usuario,fondo_inicial,estado,cierre)
                        VALUES(:usuario,100,'CERRADO',NOW())''', usuario=rid)
                antes = self._valor(f'SELECT COUNT(*) FROM {relacion} WHERE id_usuario=:id', id=rid)
                self.assertEqual(self._accion('usuarios', rid, 'eliminar').status_code, 302)
                self.assertEqual(self._existe('usuarios', rid), 1)
                self.assertEqual(self._accion('usuarios', rid, 'estado', 'desactivar').status_code, 302)
                self.assertFalse(self._estado('usuarios', rid))
                self.assertEqual(self._valor(f'SELECT COUNT(*) FROM {relacion} WHERE id_usuario=:id',
                                             id=rid), antes)

    def test_consumidor_final_y_cuenta_actual_no_admiten_bajas(self):
        self._login()
        for modulo, rid in (('clientes', self.ids['cliente']), ('usuarios', self.ids['admin'])):
            with self.subTest(modulo=modulo):
                self.assertEqual(self._accion(modulo, rid, 'estado', 'desactivar').status_code, 302)
                self.assertTrue(self._estado(modulo, rid))
                self.assertEqual(self._accion(modulo, rid, 'eliminar').status_code, 302)
                self.assertEqual(self._existe(modulo, rid), 1)

    def test_consumidor_final_no_puede_perder_identidad_al_editarse(self):
        self._login()
        respuesta = self._formulario('/gestion/clientes', {
            'id': self.ids['cliente'], 'nombre_razon_social': 'Cliente reemplazado',
            'nit_dpi': '1234567-K', 'telefono': '', 'correo': '', 'direccion_fiscal': '',
        })
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(self._valor('SELECT nit_dpi FROM cliente WHERE id_cliente=1'), 'CF')
        self.assertEqual(self._valor('SELECT nombre_razon_social FROM cliente WHERE id_cliente=1'),
                         'Consumidor Final')

    def test_usuario_con_turno_abierto_exige_cierre_antes_de_desactivarse(self):
        self._login()
        rid = self.ids['cajero1']
        self.assertEqual(self._accion('usuarios', rid, 'estado', 'desactivar').status_code, 302)
        self.assertTrue(self._estado('usuarios', rid))
        self.assertEqual(self._valor('SELECT estado FROM turno_caja WHERE id_turno=1'), 'ABIERTO')
        self._escribir("UPDATE turno_caja SET estado='CERRADO',cierre=NOW() WHERE id_usuario=:id", id=rid)
        self.assertEqual(self._accion('usuarios', rid, 'estado', 'desactivar').status_code, 302)
        self.assertFalse(self._estado('usuarios', rid))
        self.assertEqual(self._accion('usuarios', rid, 'eliminar').status_code, 302)
        self.assertEqual(self._existe('usuarios', rid), 1)

    def test_cuenta_desactivada_pierde_sesion_y_no_puede_iniciar_sesion(self):
        usuario = self.app.app.test_client()
        self._login('bodeguero', usuario)
        self._login()
        self.assertEqual(self._accion('usuarios', self.ids['bodeguero'], 'estado', 'desactivar').status_code, 302)
        self.assertEqual(usuario.get('/productos').status_code, 302)
        otro = self.app.app.test_client()
        respuesta = otro.post('/login', data={
            'csrf_token': self._csrf('/login', otro), 'correo': 'bodeguero@example.test',
            'password': 'PruebaPostgres2026!',
        })
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(otro.get('/productos').status_code, 302)
        self.assertEqual(self._accion('usuarios', self.ids['bodeguero'], 'estado', 'activar').status_code, 302)
        self._login('bodeguero', otro)
        self.assertEqual(otro.get('/productos').status_code, 200)

    def test_bajas_son_post_y_exigen_csrf_autenticado(self):
        self._login()
        for modulo in self.MODULOS:
            rid = self._nuevo(modulo)
            for accion in ('estado', 'eliminar'):
                with self.subTest(modulo=modulo, accion=accion):
                    ruta = self._ruta(modulo, rid, accion)
                    self.assertEqual(self.client.get(ruta).status_code, 405)
                    self.assertEqual(self.client.post(ruta, data={'estado': 'desactivar'}).status_code, 400)
                    self.assertEqual(self._existe(modulo, rid), 1)
                    self.assertTrue(self._estado(modulo, rid))

    def test_cajero_no_cambia_estado_clientes_ni_elimina_maestros(self):
        self._login('cajero1')
        for modulo in self.MODULOS:
            rid = self._nuevo(modulo)
            token = self._csrf('/pos')
            with self.subTest(modulo=modulo):
                respuesta = self.client.post(self._ruta(modulo, rid, 'estado'), data={
                    'csrf_token': token, 'estado': 'desactivar',
                })
                self.assertEqual(respuesta.status_code, 403)
                self.assertTrue(self._estado(modulo, rid))
                self.assertEqual(self.client.post(self._ruta(modulo, rid, 'eliminar'),
                                                 data={'csrf_token': token}).status_code, 403)
                self.assertEqual(self._existe(modulo, rid), 1)

    def test_bodeguero_no_cambia_estado_ni_elimina_maestros(self):
        self._login('bodeguero')
        for modulo in self.MODULOS:
            rid = self._nuevo(modulo)
            token = self._csrf('/productos')
            with self.subTest(modulo=modulo):
                respuesta = self.client.post(self._ruta(modulo, rid, 'estado'), data={
                    'csrf_token': token, 'estado': 'desactivar',
                })
                self.assertEqual(respuesta.status_code, 403)
                self.assertTrue(self._estado(modulo, rid))
                self.assertEqual(self.client.post(self._ruta(modulo, rid, 'eliminar'),
                                                 data={'csrf_token': token}).status_code, 403)
                self.assertEqual(self._existe(modulo, rid), 1)

    def test_accion_estado_invalida_no_modifica_ni_invierte_registro(self):
        self._login()
        for modulo in self.MODULOS:
            rid = self._nuevo(modulo)
            for invalido in ('', 'toggle', 'FALSE', 'borrar'):
                with self.subTest(modulo=modulo, estado=invalido):
                    respuesta = self._accion(modulo, rid, 'estado', invalido)
                    self.assertEqual(respuesta.status_code, 302)
                    self.assertTrue(self._estado(modulo, rid))
                    self.assertEqual(self._existe(modulo, rid), 1)

    def test_bajas_de_id_inexistente_y_modulo_desconocido_responden_404(self):
        self._login()
        for modulo in self.MODULOS:
            for accion in ('estado', 'eliminar'):
                with self.subTest(modulo=modulo, accion=accion):
                    respuesta = self._accion(modulo, 999999, accion, 'desactivar')
                    self.assertEqual(respuesta.status_code, 404)
        token = self._csrf('/usuarios')
        for accion in ('estado', 'eliminar'):
            self.assertEqual(self.client.post(f'/gestion/noexiste/1/{accion}', data={
                'csrf_token': token, 'estado': 'desactivar',
            }).status_code, 404)

    def test_inactivos_se_muestran_en_gestion_y_salen_de_selectores(self):
        self._login()
        casos = (
            ('categorias', '/productos/nuevo', 'id_categoria'),
            ('clientes', '/pos', 'cliente'),
            ('proveedores', '/compras', 'id_proveedor'),
        )
        for modulo, pagina, selector in casos:
            with self.subTest(modulo=modulo):
                rid = self._nuevo(modulo)
                self.assertIn(rid, self._ids_select(pagina, selector))
                self.assertEqual(self._accion(modulo, rid, 'estado', 'desactivar').status_code, 302)
                self.assertNotIn(rid, self._ids_select(pagina, selector))
                listado = self.client.get(self._pagina(modulo)).get_data(as_text=True)
                self.assertIn(self._ruta(modulo, rid, 'estado'), listado)
                self.assertIn('Inactivo', listado)
                self.assertEqual(self._accion(modulo, rid, 'estado', 'activar').status_code, 302)
                self.assertIn(rid, self._ids_select(pagina, selector))

    def test_cliente_inactivo_rechaza_cobro_sin_escrituras_y_reactivado_permite(self):
        self._login()
        rid = self._nuevo('clientes', activo=False)
        self._escribir('INSERT INTO turno_caja(id_usuario,fondo_inicial) VALUES(1,100)')
        movimientos = self._valor('SELECT COUNT(*) FROM movimiento_inventario')
        respuesta = self._cobrar(rid)
        self.assertEqual(respuesta.status_code, 400)
        self.assertFalse(respuesta.get_json()['ok'])
        self.assertEqual(self._valor('SELECT COUNT(*) FROM venta'), 0)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM detalle_venta'), 0)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM movimiento_caja'), 0)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM movimiento_inventario'), movimientos)
        self.assertEqual(self._accion('clientes', rid, 'estado', 'activar').status_code, 302)
        respuesta = self._cobrar(rid)
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.get_json()['ok'])
        self.assertEqual(self._valor('SELECT id_cliente FROM venta'), rid)

    def test_proveedor_inactivo_rechaza_compra_sin_escrituras_y_reactivado_permite(self):
        self._login()
        rid = self._nuevo('proveedores', activo=False)
        movimientos = self._valor('SELECT COUNT(*) FROM movimiento_inventario')
        self.assertEqual(self._comprar(rid).status_code, 302)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM compra'), 0)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM detalle_compra'), 0)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM movimiento_inventario'), movimientos)
        self.assertEqual(self._valor('SELECT costo FROM producto WHERE id_producto=1'), 9)
        self.assertEqual(self._accion('proveedores', rid, 'estado', 'activar').status_code, 302)
        self.assertEqual(self._comprar(rid).status_code, 302)
        self.assertEqual(self._valor('SELECT id_proveedor FROM compra'), rid)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM detalle_compra'), 1)

    def test_categoria_inactiva_rechaza_producto_nuevo_y_reactivada_permite(self):
        self._login()
        rid = self._nuevo('categorias', activo=False)
        datos = self._producto_datos(rid)
        self.assertEqual(self._formulario('/productos/nuevo', datos).status_code, 200)
        self.assertEqual(self._valor("SELECT COUNT(*) FROM producto WHERE codigo='BAJAS-NUEVO'"), 0)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM movimiento_inventario'), 2)
        self.assertEqual(self._accion('categorias', rid, 'estado', 'activar').status_code, 302)
        self.assertEqual(self._formulario('/productos/nuevo', datos).status_code, 302)
        self.assertEqual(self._valor("SELECT id_categoria FROM producto WHERE codigo='BAJAS-NUEVO'"), rid)

    def test_categoria_inactiva_se_conserva_al_editar_pero_no_se_asigna_a_otro_producto(self):
        self._login()
        categoria_inactiva = self._nuevo('categorias', activo=False)
        self._escribir('UPDATE producto SET id_categoria=:categoria WHERE id_producto=1',
                       categoria=categoria_inactiva)
        datos = self._producto_datos(categoria_inactiva, codigo='PG001')
        datos['precio_venta'] = '17.50'
        self.assertEqual(self._formulario('/productos/1/editar', datos).status_code, 302)
        self.assertEqual(self._valor('SELECT id_categoria FROM producto WHERE id_producto=1'),
                         categoria_inactiva)
        self.assertEqual(str(self._valor('SELECT precio_venta FROM producto WHERE id_producto=1')), '17.50')
        datos_otro = self._producto_datos(categoria_inactiva, codigo='PG002')
        self.assertEqual(self._formulario('/productos/2/editar', datos_otro).status_code, 200)
        self.assertEqual(self._valor('SELECT id_categoria FROM producto WHERE id_producto=2'), 1)

    def test_desactivar_categoria_no_desactiva_productos_ni_impide_vender_existencias(self):
        self._login()
        self.assertEqual(self._accion('categorias', 1, 'estado', 'desactivar').status_code, 302)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM producto WHERE estado=TRUE'), 2)
        self._escribir('INSERT INTO turno_caja(id_usuario,fondo_inicial) VALUES(1,100)')
        respuesta = self._cobrar(self.ids['cliente'])
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.get_json()['ok'])

    def test_historial_ventas_compras_y_ticket_siguen_visibles_tras_desactivar(self):
        self._login()
        cliente = self._nuevo('clientes')
        proveedor = self._nuevo('proveedores')
        usuario = self._nuevo('usuarios')
        venta = self._venta_sql(cliente=cliente, usuario=usuario)
        compra = self._compra_sql(proveedor=proveedor, usuario=usuario)
        self._escribir('''INSERT INTO detalle_venta(id_venta,id_producto,cantidad,
            precio_unitario,costo_unitario_historico) VALUES(:venta,1,1,15,9)''', venta=venta)
        self._escribir('''INSERT INTO detalle_compra(id_compra,id_producto,cantidad,costo_unitario)
            VALUES(:compra,1,1,9)''', compra=compra)
        for modulo, rid in (('clientes', cliente), ('proveedores', proveedor), ('usuarios', usuario)):
            self.assertEqual(self._accion(modulo, rid, 'estado', 'desactivar').status_code, 302)
        self.assertEqual(self.client.get(f'/ventas/{venta}/ticket').status_code, 200)
        self.assertIn('Cliente sin relaciones', self.client.get('/ventas').get_data(as_text=True))
        self.assertIn('Proveedor sin relaciones', self.client.get('/compras').get_data(as_text=True))
        self.assertIn('Usuario sin historial', self.client.get(f'/ventas/{venta}/ticket').get_data(as_text=True))
        self.assertEqual(self._valor('SELECT COUNT(*) FROM detalle_venta WHERE id_venta=:id', id=venta), 1)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM detalle_compra WHERE id_compra=:id', id=compra), 1)

    def _esperar_bloqueo(self, bloqueado, bloqueador):
        limite = monotonic() + 3
        pausa = Event()
        with self.engine.connect() as conexion:
            while monotonic() < limite:
                bloqueadores = conexion.execute(text('SELECT pg_blocking_pids(:pid)'),
                                                {'pid': bloqueado}).scalar_one()
                if bloqueador in bloqueadores:
                    return
                pausa.wait(0.02)
        self.fail(f'No se observó el bloqueo PostgreSQL de {bloqueado} por {bloqueador}.')

    def _insertar_producto_carrera(self, categoria, proceso):
        with self.engine.begin() as conexion:
            proceso['pid'] = conexion.execute(text('SELECT pg_backend_pid()')).scalar_one()
            proceso['listo'].set()
            conexion.execute(text('''INSERT INTO producto(id_categoria,codigo,nombre,unidad,
                costo,precio_venta) VALUES(:categoria,'BAJAS-CARRERA','Producto concurrente',
                'Unidad',9,15)'''), {'categoria': categoria})

    def test_eliminar_primero_bloquea_nueva_relacion_y_fk_impide_huerfanos(self):
        self._login()
        rid = self._nuevo('categorias')
        ruta = self._ruta('categorias', rid, 'eliminar')
        token = self._csrf('/gestion/categorias')
        preparada, liberar = Event(), Event()
        proceso_baja, proceso_alta = {}, {'listo': Event()}

        def retener_borrado(conexion, cursor, sentencia, parametros, contexto, varios):
            if (current_thread().name.startswith('bajas-delete')
                    and re.match(r'\s*DELETE\s+FROM\s+categoria\b', sentencia, re.I)):
                proceso_baja['pid'] = cursor.connection.get_backend_pid()
                preparada.set()
                if not liberar.wait(self.ESPERA):
                    raise AssertionError('No se liberó la baja retenida.')

        event.listen(self.engine, 'after_cursor_execute', retener_borrado)
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix='bajas-delete') as pool:
                baja = pool.submit(self.client.post, ruta, data={'csrf_token': token})
                try:
                    self.assertTrue(preparada.wait(self.ESPERA), 'El borrado no alcanzó la coordinación.')
                    alta = pool.submit(self._insertar_producto_carrera, rid, proceso_alta)
                    self.assertTrue(proceso_alta['listo'].wait(self.ESPERA))
                    self._esperar_bloqueo(proceso_alta['pid'], proceso_baja['pid'])
                finally:
                    liberar.set()
                self.assertEqual(baja.result(timeout=self.ESPERA).status_code, 302)
                with self.assertRaises(IntegrityError):
                    alta.result(timeout=self.ESPERA)
        finally:
            liberar.set()
            event.remove(self.engine, 'after_cursor_execute', retener_borrado)
        self.assertEqual(self._existe('categorias', rid), 0)
        self.assertEqual(self._valor("SELECT COUNT(*) FROM producto WHERE codigo='BAJAS-CARRERA'"), 0)

    def test_nueva_relacion_primero_hace_esperar_y_rechaza_eliminacion(self):
        self._login()
        rid = self._nuevo('categorias')
        ruta = self._ruta('categorias', rid, 'eliminar')
        token = self._csrf('/gestion/categorias')
        iniciada, proceso = Event(), {}

        def observar_peticion(conexion, cursor, sentencia, parametros, contexto, varios):
            if current_thread().name.startswith('bajas-insert'):
                proceso['pid'] = cursor.connection.get_backend_pid()
                iniciada.set()

        conexion = self.engine.connect()
        transaccion = conexion.begin()
        event.listen(self.engine, 'before_cursor_execute', observar_peticion)
        try:
            pid_alta = conexion.execute(text('SELECT pg_backend_pid()')).scalar_one()
            conexion.execute(text('''INSERT INTO producto(id_categoria,codigo,nombre,unidad,
                costo,precio_venta) VALUES(:categoria,'BAJAS-CARRERA','Producto concurrente',
                'Unidad',9,15)'''), {'categoria': rid})
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix='bajas-insert') as pool:
                baja = pool.submit(self.client.post, ruta, data={'csrf_token': token})
                try:
                    self.assertTrue(iniciada.wait(self.ESPERA), 'No comenzó la petición de baja.')
                    self._esperar_bloqueo(proceso['pid'], pid_alta)
                finally:
                    transaccion.commit()
                self.assertEqual(baja.result(timeout=self.ESPERA).status_code, 302)
        finally:
            if transaccion.is_active:
                transaccion.rollback()
            conexion.close()
            event.remove(self.engine, 'before_cursor_execute', observar_peticion)
        self.assertEqual(self._existe('categorias', rid), 1)
        self.assertEqual(self._valor("SELECT id_categoria FROM producto WHERE codigo='BAJAS-CARRERA'"), rid)

    def _administradores_compiten(self, accion):
        """Ambas sesiones se autentican antes de que una dé de baja a la otra."""
        self._login()
        otro_id = self._insertar('''INSERT INTO usuario(id_rol,nombre,correo,hash_contrasena)
            VALUES(1,'Otro Administrador','admin2@example.test',:hash) RETURNING id_usuario''',
            hash=HASH_PRUEBA)
        otro_cliente = self.app.app.test_client()
        self._login('admin2', otro_cliente)
        token1 = self._csrf('/usuarios')
        token2 = self._csrf('/usuarios', otro_cliente)
        preparada, liberar, segunda_iniciada = Event(), Event(), Event()
        primera, segunda = {}, {}

        def solicitar(cliente, destino, token, proceso):
            proceso['hilo'] = current_thread().name
            return cliente.post(self._ruta('usuarios', destino, accion), data={
                'csrf_token': token, 'estado': 'desactivar',
            })

        def retener_primera(conexion, cursor, sentencia, parametros, contexto, varios):
            if (current_thread().name == primera.get('hilo')
                    and re.match(r'\s*(?:UPDATE\s+usuario|DELETE\s+FROM\s+usuario)\b', sentencia, re.I)):
                primera['pid'] = cursor.connection.get_backend_pid()
                preparada.set()
                if not liberar.wait(self.ESPERA):
                    raise AssertionError('No se liberó la baja del segundo administrador.')

        def observar_segunda(conexion, cursor, sentencia, parametros, contexto, varios):
            if current_thread().name == segunda.get('hilo'):
                segunda['pid'] = cursor.connection.get_backend_pid()
                segunda_iniciada.set()

        event.listen(self.engine, 'after_cursor_execute', retener_primera)
        event.listen(self.engine, 'before_cursor_execute', observar_segunda)
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix='bajas-admin') as pool:
                gana = pool.submit(solicitar, self.client, otro_id, token1, primera)
                try:
                    self.assertTrue(preparada.wait(self.ESPERA), 'La primera baja no se coordinó.')
                    pierde = pool.submit(solicitar, otro_cliente, self.ids['admin'], token2, segunda)
                    self.assertTrue(segunda_iniciada.wait(self.ESPERA), 'No comenzó la segunda baja.')
                    self._esperar_bloqueo(segunda['pid'], primera['pid'])
                finally:
                    liberar.set()
                self.assertEqual(gana.result(timeout=self.ESPERA).status_code, 302)
                respuesta = pierde.result(timeout=self.ESPERA)
                self.assertEqual(respuesta.status_code, 403)
                self.assertIn('No tienes permiso para realizar esta acción',
                              respuesta.get_data(as_text=True))
        finally:
            liberar.set()
            event.remove(self.engine, 'after_cursor_execute', retener_primera)
            event.remove(self.engine, 'before_cursor_execute', observar_segunda)
        self.assertEqual(self._existe('usuarios', self.ids['admin']), 1)
        self.assertTrue(self._estado('usuarios', self.ids['admin']))
        self.assertEqual(self._valor('''SELECT COUNT(*) FROM usuario u JOIN rol r USING(id_rol)
            WHERE u.estado=TRUE AND r.nombre='Administrador' '''), 1)
        if accion == 'estado':
            self.assertEqual(self._existe('usuarios', otro_id), 1)
            self.assertFalse(self._estado('usuarios', otro_id))
        else:
            self.assertEqual(self._existe('usuarios', otro_id), 0)

    def test_dos_administradores_no_pueden_desactivarse_mutuamente_y_dejar_cero_activos(self):
        self._administradores_compiten('estado')

    def test_dos_administradores_no_pueden_eliminarse_mutuamente_y_dejar_cero_activos(self):
        self._administradores_compiten('eliminar')

    def _solicitudes_serializadas(self, solicitar_primera, punto_primera, solicitar_segunda):
        """Retiene SQL real de una ruta y observa a la otra esperando en PostgreSQL."""
        preparada, liberar, segunda_iniciada = Event(), Event(), Event()
        primera, segunda = {}, {}

        def solicitar(funcion, proceso):
            proceso['hilo'] = current_thread().name
            return funcion()

        def retener_primera(conexion, cursor, sentencia, parametros, contexto, varios):
            compacta = ' '.join(sentencia.split())
            if (current_thread().name == primera.get('hilo')
                    and not primera.get('retenida') and re.search(punto_primera, compacta, re.I)):
                primera['retenida'] = True
                primera['pid'] = cursor.connection.get_backend_pid()
                preparada.set()
                if not liberar.wait(self.ESPERA):
                    raise AssertionError('No se liberó la operación retenida.')

        def observar_segunda(conexion, cursor, sentencia, parametros, contexto, varios):
            if current_thread().name == segunda.get('hilo'):
                segunda['pid'] = cursor.connection.get_backend_pid()
                segunda_iniciada.set()

        event.listen(self.engine, 'after_cursor_execute', retener_primera)
        event.listen(self.engine, 'before_cursor_execute', observar_segunda)
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix='bajas-operacion') as pool:
                gana = pool.submit(solicitar, solicitar_primera, primera)
                try:
                    if not preparada.wait(self.ESPERA):
                        if gana.done():
                            gana.result()  # Expone errores originales de la petición.
                        self.fail('La primera operación no alcanzó el punto de coordinación.')
                    sigue = pool.submit(solicitar, solicitar_segunda, segunda)
                    self.assertTrue(segunda_iniciada.wait(self.ESPERA), 'No comenzó la segunda petición.')
                    self._esperar_bloqueo(segunda['pid'], primera['pid'])
                finally:
                    liberar.set()
                return gana.result(timeout=self.ESPERA), sigue.result(timeout=self.ESPERA)
        finally:
            liberar.set()
            event.remove(self.engine, 'after_cursor_execute', retener_primera)
            event.remove(self.engine, 'before_cursor_execute', observar_segunda)

    def test_desactivar_cliente_y_cobrar_se_serializan_en_ambos_ordenes(self):
        self._login()
        cajero = self.app.app.test_client()
        self._login('cajero1', cajero)
        for desactivar_primero in (True, False):
            with self.subTest(desactivar_primero=desactivar_primero):
                rid = self._nuevo('clientes')
                token_baja = self._csrf('/gestion/clientes')
                token_cobro = self._csrf('/pos', cajero)
                stock = self._valor('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1')
                movimientos = self._valor('SELECT COUNT(*) FROM movimiento_inventario')
                efectivo = self._valor('SELECT COUNT(*) FROM movimiento_caja')

                def desactivar():
                    return self.client.post(self._ruta('clientes', rid, 'estado'), data={
                        'csrf_token': token_baja, 'estado': 'desactivar',
                    })

                def cobrar():
                    return cajero.post('/pos/cobrar', json={
                        'cliente_id': rid, 'metodo': 'EFECTIVO', 'recibido': '100',
                        'items': [{'id': self.ids['producto1'], 'cantidad': 1, 'descuento': '0'}],
                    }, headers={'X-CSRFToken': token_cobro, 'Idempotency-Key': uuid4().hex})

                if desactivar_primero:
                    baja, venta = self._solicitudes_serializadas(
                        desactivar, r'^UPDATE cliente SET estado=', cobrar)
                else:
                    venta, baja = self._solicitudes_serializadas(
                        cobrar, r'^SELECT id_cliente FROM cliente .* FOR SHARE$', desactivar)
                self.assertEqual(baja.status_code, 302)
                self.assertFalse(self._estado('clientes', rid))
                esperado = 0 if desactivar_primero else 1
                self.assertEqual(venta.status_code, 400 if desactivar_primero else 200)
                self.assertIs(venta.get_json()['ok'], not desactivar_primero)
                self.assertEqual(self._valor('SELECT COUNT(*) FROM venta WHERE id_cliente=:id', id=rid), esperado)
                self.assertEqual(self._valor('''SELECT COUNT(*) FROM detalle_venta d JOIN venta v USING(id_venta)
                    WHERE v.id_cliente=:id''', id=rid), esperado)
                self.assertEqual(self._valor('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1'),
                                 stock - esperado)
                self.assertEqual(self._valor('SELECT COUNT(*) FROM movimiento_inventario'), movimientos + esperado)
                self.assertEqual(self._valor('SELECT COUNT(*) FROM movimiento_caja'), efectivo + esperado)

    def test_desactivar_proveedor_y_comprar_se_serializan_en_ambos_ordenes(self):
        self._login()
        # Las compras modifican costos y requieren un administrador.
        comprador = self.app.app.test_client()
        self._login('admin', comprador)
        for desactivar_primero in (True, False):
            with self.subTest(desactivar_primero=desactivar_primero):
                rid = self._nuevo('proveedores')
                token_baja = self._csrf('/gestion/proveedores')
                token_compra = self._csrf('/compras', comprador)
                stock = self._valor('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1')
                movimientos = self._valor('SELECT COUNT(*) FROM movimiento_inventario')

                def desactivar():
                    return self.client.post(self._ruta('proveedores', rid, 'estado'), data={
                        'csrf_token': token_baja, 'estado': 'desactivar',
                    })

                def comprar():
                    return comprador.post('/compras/nueva', data={
                        'csrf_token': token_compra, 'id_proveedor': rid, 'id_producto': self.ids['producto1'],
                        'cantidad': '2', 'costo_unitario': '9.50', 'metodo_pago': 'CRÉDITO',
                        'numero_factura': f'CONCURRENCIA-{rid}',
                    })

                if desactivar_primero:
                    baja, compra = self._solicitudes_serializadas(
                        desactivar, r'^UPDATE proveedor SET estado=', comprar)
                else:
                    compra, baja = self._solicitudes_serializadas(
                        comprar, r'^SELECT id_proveedor FROM proveedor .* FOR SHARE$', desactivar)
                self.assertEqual(baja.status_code, 302)
                self.assertEqual(compra.status_code, 302)
                self.assertFalse(self._estado('proveedores', rid))
                esperado = 0 if desactivar_primero else 1
                self.assertEqual(self._valor('SELECT COUNT(*) FROM compra WHERE id_proveedor=:id', id=rid), esperado)
                self.assertEqual(self._valor('''SELECT COUNT(*) FROM detalle_compra d JOIN compra c USING(id_compra)
                    WHERE c.id_proveedor=:id''', id=rid), esperado)
                self.assertEqual(self._valor('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1'),
                                 stock + esperado * 2)
                self.assertEqual(self._valor('SELECT COUNT(*) FROM movimiento_inventario'), movimientos + esperado)

    def test_desactivar_usuario_y_abrir_caja_se_serializan_en_ambos_ordenes(self):
        self._login()
        for desactivar_primero in (True, False):
            with self.subTest(desactivar_primero=desactivar_primero):
                rid = self._nuevo('usuarios')
                cajero = self.app.app.test_client()
                self._login(f'nuevo{rid}', cajero)
                token_baja = self._csrf('/usuarios')
                token_caja = self._csrf('/caja', cajero)

                def desactivar():
                    return self.client.post(self._ruta('usuarios', rid, 'estado'), data={
                        'csrf_token': token_baja, 'estado': 'desactivar',
                    })

                def abrir():
                    return cajero.post('/caja', data={
                        'csrf_token': token_caja, 'accion': 'abrir', 'fondo_inicial': '100',
                    })

                if desactivar_primero:
                    baja, caja = self._solicitudes_serializadas(
                        desactivar, r'^UPDATE usuario SET estado=', abrir)
                else:
                    caja, baja = self._solicitudes_serializadas(
                        abrir, r'^SELECT u.id_usuario, r.nombre AS rol FROM usuario .* FOR NO KEY UPDATE OF u$', desactivar)
                self.assertEqual(baja.status_code, 302)
                self.assertEqual(caja.status_code, 403 if desactivar_primero else 302)
                # La primera operación decide: usuario inactivo sin turno, o
                # usuario activo con turno y desactivación rechazada.
                self.assertIs(self._estado('usuarios', rid), not desactivar_primero)
                self.assertEqual(self._valor("SELECT COUNT(*) FROM turno_caja WHERE id_usuario=:id AND estado='ABIERTO'",
                                             id=rid), 0 if desactivar_primero else 1)
