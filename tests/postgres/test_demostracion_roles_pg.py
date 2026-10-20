"""Carga demostrativa de treinta operaciones sobre PostgreSQL desechable.

No utiliza la base del negocio: ejecutar mediante ``tests/postgres/ejecutar.py``.
Los snapshots comparan filas existentes sin mostrar datos privados ni hashes de
contraseñas. Las secuencias PostgreSQL pueden consumir identificadores al revertir.
"""

from collections import Counter
from hashlib import sha256
from unittest.mock import patch

from sqlalchemy import text

from demostracion_roles import Demostracion, ErrorDemostracion, ejecutar_demostracion
from entorno_pg import CasoPostgres, TABLAS


class DemostracionRolesPostgres(CasoPostgres):
    def _valor(self, sql, **datos):
        with self.engine.connect() as conexion:
            return conexion.execute(text(sql), datos).scalar_one()

    def _snapshot(self):
        """Representación comparable que evita imprimir información de usuarios."""
        resultado = {}
        with self.engine.connect() as conexion:
            for tabla in TABLAS:
                filas = conexion.execute(text(f'SELECT * FROM {tabla}')).mappings().all()
                resultado[tabla] = {
                    tuple(fila.values())[0]: sha256(repr(tuple(fila.values())).encode()).hexdigest()
                    for fila in filas
                }
        return resultado

    def _filas_previas_intactas(self, antes):
        despues = self._snapshot()
        for tabla, filas in antes.items():
            for clave, firma in filas.items():
                with self.subTest(tabla=tabla, identificador=clave):
                    self.assertEqual(despues[tabla].get(clave), firma)

    def test_aplicar_crea_diez_operaciones_por_rol_y_conserva_los_datos_previos(self):
        antes = self._snapshot()
        usuarios_antes = self._valor('SELECT COUNT(*) FROM usuario')
        stock_antes = self._valor(
            'SELECT SUM(existencia_actual) FROM vista_inventario WHERE id_producto IN (1,2)')

        resultado = ejecutar_demostracion(self.app, self.engine, aplicar=True)

        self.assertTrue(resultado['aplicado'])
        self.assertEqual(len(resultado['operaciones']), 30)
        self.assertEqual(Counter(operacion['rol'] for operacion in resultado['operaciones']), {
            'Administrador': 10, 'Cajero': 10, 'Bodeguero': 10,
        })
        self.assertEqual(resultado['usuarios_creados'], 0)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM usuario'), usuarios_antes)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM rol'), 3)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM venta'), 9)
        self.assertEqual(self._valor("""SELECT COUNT(*) FROM venta v
            JOIN usuario u USING(id_usuario) JOIN rol r USING(id_rol)
            WHERE r.nombre='Cajero'"""), 8)
        self.assertEqual(self._valor("""SELECT COUNT(*) FROM venta v
            JOIN usuario u USING(id_usuario) JOIN rol r USING(id_rol)
            WHERE r.nombre='Administrador'"""), 1)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM solicitud_ajuste'), 3)
        for estado in ('PENDIENTE', 'APROBADA', 'RECHAZADA'):
            with self.subTest(estado=estado):
                self.assertEqual(self._valor(
                    'SELECT COUNT(*) FROM solicitud_ajuste WHERE estado=:estado', estado=estado), 1)
        self.assertEqual(self._valor("SELECT COUNT(*) FROM producto WHERE codigo LIKE 'PRUEBA-ROLES-%'"), 3)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM vista_inventario WHERE existencia_actual<0'), 0)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM movimiento_caja'), 0)
        self.assertEqual(self._valor(
            'SELECT SUM(existencia_actual) FROM vista_inventario WHERE id_producto IN (1,2)'), stock_antes)
        self._filas_previas_intactas(antes)

    def test_simular_recorre_las_operaciones_pero_revierte_toda_la_carga(self):
        antes = self._snapshot()

        resultado = ejecutar_demostracion(self.app, self.engine, aplicar=False)

        self.assertFalse(resultado['aplicado'])
        self.assertEqual(len(resultado['operaciones']), 30)
        self.assertEqual(resultado['usuarios_creados'], 0)
        self.assertEqual(self._snapshot(), antes)

    def test_fallo_despues_de_escrituras_revierte_tambien_las_operaciones_previas(self):
        # La falla sucede al registrar el primer conteo, después de crear los
        # catálogos, las ventas y los movimientos. El error no debe dejar una
        # carga parcial aunque las rutas hayan llamado a session.commit().
        with self.engine.begin() as conexion:
            conexion.exec_driver_sql("""CREATE FUNCTION prueba_impedir_conteo_demo()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'Fallo deliberado de conteo demostrativo'; END $$""")
            conexion.exec_driver_sql("""CREATE TRIGGER prueba_impedir_conteo_demo
                BEFORE INSERT ON solicitud_ajuste FOR EACH ROW
                EXECUTE FUNCTION prueba_impedir_conteo_demo()""")
        antes = self._snapshot()
        try:
            with patch.object(self.app.app.logger, 'exception'):
                with self.assertRaises((RuntimeError, ValueError, AssertionError)):
                    ejecutar_demostracion(self.app, self.engine, aplicar=True)
            self.assertEqual(self._snapshot(), antes)
        finally:
            with self.engine.begin() as conexion:
                conexion.exec_driver_sql(
                    'DROP TRIGGER prueba_impedir_conteo_demo ON solicitud_ajuste')
                conexion.exec_driver_sql('DROP FUNCTION prueba_impedir_conteo_demo()')

    def test_fallo_en_comprobacion_final_revierte_aprobaciones_y_cierre_de_turnos(self):
        antes = self._snapshot()
        verificar_original = Demostracion.verificar
        evidencia = {}

        def verificar_con_fallo(demo, nombre, condicion, **datos):
            verificar_original(demo, nombre, condicion, **datos)
            if nombre == 'No se crearon usuarios ni roles':
                # Se llega aquí después de completar las treinta operaciones,
                # aprobar y rechazar ajustes, y cerrar los turnos preparados.
                evidencia['operaciones'] = len(demo.reporte['operaciones'])
                evidencia['aprobadas'] = demo.valor(
                    "SELECT COUNT(*) FROM solicitud_ajuste WHERE estado='APROBADA'")
                evidencia['rechazadas'] = demo.valor(
                    "SELECT COUNT(*) FROM solicitud_ajuste WHERE estado='RECHAZADA'")
                evidencia['turnos_nuevos'] = [
                    demo.valor('SELECT estado FROM turno_caja WHERE id_turno=:id', id=tid)
                    for _, tid in demo.turnos_nuevos
                ]
                raise ErrorDemostracion('Fallo final deliberado de validacion')

        with patch.object(Demostracion, 'verificar', verificar_con_fallo):
            with self.assertRaisesRegex(ErrorDemostracion, 'Fallo final deliberado'):
                ejecutar_demostracion(self.app, self.engine, aplicar=True)

        self.assertEqual(evidencia['operaciones'], 30)
        self.assertEqual(evidencia['aprobadas'], 1)
        self.assertEqual(evidencia['rechazadas'], 1)
        self.assertTrue(evidencia['turnos_nuevos'])
        self.assertTrue(all(estado == 'CERRADO' for estado in evidencia['turnos_nuevos']))
        self.assertEqual(self._snapshot(), antes)

    def test_segunda_ejecucion_rechaza_duplicar_el_lote_sin_modificar_datos(self):
        ejecutar_demostracion(self.app, self.engine, aplicar=True)
        antes = self._snapshot()

        with self.assertRaises((RuntimeError, ValueError, AssertionError)):
            ejecutar_demostracion(self.app, self.engine, aplicar=True)

        self.assertEqual(self._snapshot(), antes)

    def test_bloqueo_del_lote_persiste_despues_de_las_solicitudes_de_preparacion(self):
        antes = self._snapshot()
        catalogos_original = Demostracion.catalogos_administrador
        evidencia = {}

        def catalogos_con_comprobacion(demo):
            # preparar() ya consultó tokens CSRF y abrió caja mediante Flask.
            # Sus teardowns finalizan savepoints, pero ninguna otra conexión
            # debe adquirir el bloqueo exterior mientras continúa el lote.
            with self.engine.begin() as conexion:
                evidencia['otra_conexion_adquirio_bloqueo'] = conexion.execute(text(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended('puntocom:demo_roles',0))"
                )).scalar_one()
            return catalogos_original(demo)

        with patch.object(Demostracion, 'catalogos_administrador', catalogos_con_comprobacion):
            resultado = ejecutar_demostracion(self.app, self.engine)

        self.assertFalse(evidencia['otra_conexion_adquirio_bloqueo'])
        self.assertEqual(len(resultado['operaciones']), 30)
        self.assertFalse(resultado['aplicado'])
        self.assertEqual(self._snapshot(), antes)
