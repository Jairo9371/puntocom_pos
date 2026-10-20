"""Migración de roles e inventario exclusivamente sobre la base UUID de pruebas.

La simulación de instalaciones antiguas retira el trigger de roles solo después
de comprobar el nombre y la marca de la base desechable. La limpieza restaura
siempre los roles originales y vuelve a instalar el trigger con la migración.
"""

import re

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from entorno_pg import CasoPostgres, TABLAS
from migraciones import aplicar_migracion_roles


class MigracionRolesPostgres(CasoPostgres):
    def _verificar_base_desechable(self):
        self.assertRegex(self.engine.url.database, r'^puntocom_test_[a-f0-9]{32}$')
        with self.engine.connect() as conexion:
            nombre, marca = conexion.execute(text('''SELECT current_database(),
                shobj_description(oid,'pg_database') FROM pg_database
                WHERE datname=current_database()''')).one()
        self.assertEqual(nombre, self.engine.url.database)
        self.assertIsInstance(marca, str)
        self.assertIsNotNone(re.fullmatch(r'puntocom-pruebas:[a-f0-9]{32}', marca))

    def _datos(self):
        with self.engine.connect() as conexion:
            return {tabla: [dict(fila) for fila in conexion.execute(
                text(f'SELECT * FROM public.{tabla} ORDER BY 1')).mappings()]
                    for tabla in TABLAS}

    def _restaurar_roles_legacy(self, roles, usuarios):
        self._verificar_base_desechable()
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql('DROP TRIGGER IF EXISTS usuario_rol_permitido ON public.usuario')
            for rol in roles:
                conexion.execute(text('UPDATE rol SET nombre=:nombre WHERE id_rol=:id_rol'), rol)
            for usuario in usuarios:
                conexion.execute(text('UPDATE usuario SET id_rol=:id_rol WHERE id_usuario=:id_usuario'), usuario)
        aplicar_migracion_roles(self.engine)

    def _simular_instalacion_legacy(self):
        self._verificar_base_desechable()
        with self.engine.connect() as conexion:
            roles = [dict(fila) for fila in conexion.execute(text('SELECT id_rol,nombre FROM rol')).mappings()]
            usuarios = [dict(fila) for fila in conexion.execute(text('SELECT id_usuario,id_rol FROM usuario')).mappings()]
        self.addCleanup(self._restaurar_roles_legacy, roles, usuarios)
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql('DROP TRIGGER IF EXISTS usuario_rol_permitido ON public.usuario')

    def _historial(self):
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql('''INSERT INTO venta
                (id_cliente,id_usuario,id_turno,numero_ticket,metodo_pago,total)
                VALUES(1,2,1,'MIGRACION-ROLES-001','EFECTIVO',15)''')
            conexion.exec_driver_sql('''INSERT INTO detalle_venta
                (id_venta,id_producto,cantidad,precio_unitario,costo_unitario_historico)
                VALUES(1,1,1,15,9)''')
            conexion.exec_driver_sql('''INSERT INTO movimiento_caja(id_turno,tipo,concepto,monto)
                VALUES(1,'INGRESO','Venta anterior a roles',15)''')
            conexion.exec_driver_sql('''INSERT INTO compra
                (id_proveedor,id_usuario,numero_factura,metodo_pago,total)
                VALUES(1,4,'MIGRACION-ROLES-COMPRA','TARJETA',18)''')
            conexion.exec_driver_sql('''INSERT INTO detalle_compra(id_compra,id_producto,cantidad,costo_unitario)
                VALUES(1,2,2,9)''')

    def test_migracion_idempotente_conserva_ids_datos_inactivos_e_historial(self):
        self._verificar_base_desechable()
        self._historial()
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql('UPDATE usuario SET estado=FALSE WHERE id_usuario=4')
            for tabla in ('categoria', 'cliente', 'proveedor', 'producto'):
                conexion.exec_driver_sql(f'UPDATE {tabla} SET estado=FALSE')
        antes = self._datos()
        aplicar_migracion_roles(self.engine)
        aplicar_migracion_roles(self.engine)
        self.assertEqual(self._datos(), antes)
        self.assertEqual([(rol['id_rol'], rol['nombre']) for rol in antes['rol']],
                         [(1, 'Administrador'), (2, 'Cajero'), (3, 'Bodeguero')])
        self.assertFalse(antes['usuario'][3]['estado'])
        self.assertEqual(len(antes['venta']), 1)
        self.assertEqual(len(antes['compra']), 1)

    def test_reutiliza_ids_variantes_y_reasigna_encargado_sin_crear_roles_duplicados(self):
        self._simular_instalacion_legacy()
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql("UPDATE rol SET nombre=' cajero ' WHERE id_rol=2")
            variante = conexion.execute(text("INSERT INTO rol(nombre) VALUES('CAJERO') RETURNING id_rol")).scalar_one()
            encargado = conexion.execute(text("INSERT INTO rol(nombre) VALUES('Encargado de inventario') RETURNING id_rol")).scalar_one()
            conexion.execute(text('UPDATE usuario SET id_rol=:id WHERE id_usuario=2'), {'id': variante})
            conexion.execute(text('UPDATE usuario SET id_rol=:id WHERE id_usuario=4'), {'id': encargado})
        antes = self._datos()
        aplicar_migracion_roles(self.engine)
        despues = self._datos()
        self.assertEqual([fila['id_rol'] for fila in despues['rol']], [fila['id_rol'] for fila in antes['rol']])
        self.assertEqual(len(despues['rol']), 5)
        nombres = {fila['id_rol']: fila['nombre'] for fila in despues['rol']}
        self.assertEqual((nombres[1], nombres[2], nombres[3]), ('Administrador', 'Cajero', 'Bodeguero'))
        roles_usuario = {fila['id_usuario']: fila['id_rol'] for fila in despues['usuario']}
        self.assertEqual(roles_usuario, {1: 1, 2: 2, 3: 2, 4: 3})
        for tabla in TABLAS:
            if tabla not in ('rol', 'usuario'):
                self.assertEqual(despues[tabla], antes[tabla], tabla)
        for vieja, nueva in zip(antes['usuario'], despues['usuario']):
            self.assertEqual({k: v for k, v in vieja.items() if k != 'id_rol'},
                             {k: v for k, v in nueva.items() if k != 'id_rol'})
        aplicar_migracion_roles(self.engine)
        self.assertEqual(self._datos(), despues)

    def test_rol_desconocido_asignado_revierte_todo_sin_otorgar_administracion(self):
        self._simular_instalacion_legacy()
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql("UPDATE rol SET nombre=' administrador ' WHERE id_rol=1")
            encargado = conexion.execute(text("INSERT INTO rol(nombre) VALUES('Encargado de inventario') RETURNING id_rol")).scalar_one()
            desconocido = conexion.execute(text("INSERT INTO rol(nombre) VALUES('Auditor legado') RETURNING id_rol")).scalar_one()
            conexion.execute(text('UPDATE usuario SET id_rol=:id WHERE id_usuario=4'), {'id': encargado})
            conexion.execute(text('UPDATE usuario SET id_rol=:id WHERE id_usuario=3'), {'id': desconocido})
        antes = self._datos()
        with self.assertRaises(DBAPIError):
            aplicar_migracion_roles(self.engine)
        despues = self._datos()
        self.assertEqual(despues, antes)
        usuario = next(fila for fila in despues['usuario'] if fila['id_usuario'] == 3)
        self.assertEqual(usuario['id_rol'], desconocido)
        self.assertNotEqual(usuario['id_rol'], 1)
        # La normalización y reasignación previas al error también se revierten.
        self.assertEqual(despues['rol'][0]['nombre'], ' administrador ')
        self.assertEqual(despues['usuario'][3]['id_rol'], encargado)

    def test_movimientos_anteriores_conservan_responsable_nulo_y_nuevas_fk_protegen_historial(self):
        self._verificar_base_desechable()
        antes = self._datos()['movimiento_inventario']
        self.assertTrue(antes)
        self.assertTrue(all(fila['id_usuario'] is None for fila in antes))
        aplicar_migracion_roles(self.engine)
        self.assertEqual(self._datos()['movimiento_inventario'], antes)
        with self.engine.begin() as conexion:
            movimiento = conexion.execute(text('''INSERT INTO movimiento_inventario
                (id_producto,tipo,cantidad,costo_unitario,motivo,id_usuario)
                VALUES(1,'ENTRADA',1,9,'Entrada con responsable',4)
                RETURNING id_movimiento_inventario''')).scalar_one()
            conexion.execute(text('''INSERT INTO solicitud_ajuste
                (id_producto,id_solicitante,existencia_sistema,cantidad_contada,diferencia,
                 motivo,estado,id_aprobador,fecha_resolucion,id_movimiento)
                VALUES(1,4,10,11,1,'Conteo previo','APROBADA',1,CURRENT_TIMESTAMP,:id)'''),
                             {'id': movimiento})
        historial = self._datos()
        for sentencia in (
            'DELETE FROM usuario WHERE id_usuario=4',
            f'DELETE FROM movimiento_inventario WHERE id_movimiento_inventario={movimiento}',
        ):
            with self.subTest(sentencia=sentencia), self.assertRaises(IntegrityError):
                with self.engine.begin() as conexion:
                    conexion.exec_driver_sql(sentencia)
            self.assertEqual(self._datos(), historial)
        with self.engine.connect() as conexion:
            cascadas = conexion.execute(text('''SELECT COUNT(*) FROM pg_constraint
                WHERE contype='f' AND confdeltype='c'
                  AND conrelid IN ('solicitud_ajuste'::regclass,'movimiento_inventario'::regclass)''')).scalar_one()
        self.assertEqual(cascadas, 0)
