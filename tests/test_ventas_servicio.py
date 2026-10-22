"""Prueba el cobro contra SQL real en SQLite, sin .env ni base de datos externa.

El adaptador elimina FOR UPDATE porque SQLite no implementa esos bloqueos.
Estas pruebas validan reglas y transacciones; no prueban concurrencia PostgreSQL.
"""

import copy
import unittest
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ventas_servicio import IdempotenciaError, anular_venta, registrar_venta, resolver_pago


class SessionSQLite:
    """Adapta SQL PostgreSQL y parámetros Decimal al controlador SQLite."""

    def __init__(self, session):
        self.session = session
        self.ejecutadas = []

    def execute(self, statement, params=None):
        sql = str(statement).replace("FOR SHARE OF u", "").replace(" FOR SHARE", "").replace(" FOR UPDATE", "")
        if 'pg_advisory_xact_lock' in sql:
            # Solo PostgreSQL prueba la exclusión real; aquí no hay concurrencia.
            return self.session.execute(text('SELECT 1'))
        sql = sql.replace(' AS JSONB', ' AS TEXT')
        params = {
            clave: str(valor) if isinstance(valor, Decimal) else valor
            for clave, valor in (params or {}).items()
        }
        resultado = self.session.execute(text(sql), params)
        self.ejecutadas.append(sql)
        return resultado


ESQUEMA = (
    "CREATE TABLE rol (id_rol INTEGER PRIMARY KEY, nombre TEXT NOT NULL)",
    "CREATE TABLE usuario (id_usuario INTEGER PRIMARY KEY, id_rol INTEGER NOT NULL DEFAULT 1 REFERENCES rol(id_rol), estado BOOLEAN NOT NULL DEFAULT TRUE)",
    "CREATE TABLE cliente (id_cliente INTEGER PRIMARY KEY, estado BOOLEAN NOT NULL DEFAULT TRUE)",
    """CREATE TABLE turno_caja (
        id_turno INTEGER PRIMARY KEY,
        id_usuario INTEGER NOT NULL REFERENCES usuario(id_usuario),
        estado TEXT NOT NULL,
        fondo_inicial NUMERIC NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE producto (
        id_producto INTEGER PRIMARY KEY,
        nombre TEXT NOT NULL,
        precio_venta NUMERIC NOT NULL CHECK(precio_venta >= 0),
        costo NUMERIC NOT NULL CHECK(costo >= 0),
        estado BOOLEAN NOT NULL
    )""",
    """CREATE TABLE venta (
        id_venta INTEGER PRIMARY KEY,
        id_cliente INTEGER NOT NULL REFERENCES cliente(id_cliente),
        id_usuario INTEGER NOT NULL REFERENCES usuario(id_usuario),
        id_turno INTEGER NOT NULL REFERENCES turno_caja(id_turno),
        numero_ticket TEXT UNIQUE NOT NULL,
        metodo_pago TEXT NOT NULL,
        estado TEXT NOT NULL,
        total NUMERIC NOT NULL CHECK(total >= 0),
        estado_pago TEXT NOT NULL DEFAULT 'CONFIRMADO',
        clave_operacion TEXT,
        hash_solicitud TEXT,
        resultado_cobro TEXT,
        recibido NUMERIC NOT NULL DEFAULT 0,
        cambio NUMERIC NOT NULL DEFAULT 0,
        id_confirmador INTEGER REFERENCES usuario(id_usuario),
        fecha_confirmacion TEXT,
        id_resolutor INTEGER REFERENCES usuario(id_usuario),
        fecha_resolucion TEXT,
        motivo_pago TEXT,
        id_anulador INTEGER REFERENCES usuario(id_usuario),
        fecha_anulacion TEXT,
        motivo_anulacion TEXT,
        UNIQUE(id_usuario,clave_operacion)
    )""",
    """CREATE TABLE detalle_venta (
        id_detalle_venta INTEGER PRIMARY KEY,
        id_venta INTEGER NOT NULL REFERENCES venta(id_venta),
        id_producto INTEGER NOT NULL REFERENCES producto(id_producto),
        cantidad INTEGER NOT NULL CHECK(cantidad > 0),
        precio_unitario NUMERIC NOT NULL CHECK(precio_unitario >= 0),
        costo_unitario_historico NUMERIC NOT NULL CHECK(costo_unitario_historico >= 0),
        descuento NUMERIC NOT NULL CHECK(descuento >= 0),
        tasa_iva NUMERIC NOT NULL CHECK(tasa_iva >= 0)
    )""",
    """CREATE TABLE movimiento_inventario (
        id_movimiento_inventario INTEGER PRIMARY KEY,
        id_producto INTEGER NOT NULL REFERENCES producto(id_producto),
        tipo TEXT NOT NULL CHECK(tipo IN ('ENTRADA', 'SALIDA')),
        cantidad INTEGER NOT NULL CHECK(cantidad > 0),
        costo_unitario NUMERIC NOT NULL CHECK(costo_unitario >= 0),
        motivo TEXT,
        id_usuario INTEGER REFERENCES usuario(id_usuario)
    )""",
    """CREATE TABLE movimiento_caja (
        id_movimiento_caja INTEGER PRIMARY KEY,
        id_turno INTEGER NOT NULL REFERENCES turno_caja(id_turno),
        tipo TEXT NOT NULL CHECK(tipo IN ('INGRESO', 'EGRESO')),
        concepto TEXT NOT NULL,
        monto NUMERIC NOT NULL CHECK(monto > 0)
    )""",
    """CREATE VIEW vista_inventario AS
        SELECT p.id_producto,
               COALESCE(SUM(CASE WHEN m.tipo = 'ENTRADA'
                                THEN m.cantidad ELSE -m.cantidad END), 0)
                   AS existencia_actual
        FROM producto p LEFT JOIN movimiento_inventario m USING(id_producto)
        GROUP BY p.id_producto""",
)


class RegistrarVentaTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        with self.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            for sentencia in ESQUEMA:
                connection.execute(text(sentencia))
            connection.execute(text("INSERT INTO rol VALUES (1, 'Administrador')"))
            connection.execute(text("INSERT INTO usuario(id_usuario) VALUES (7), (8)"))
            connection.execute(text("INSERT INTO cliente(id_cliente) VALUES (1)"))
            connection.execute(text("INSERT INTO turno_caja(id_turno,id_usuario,estado) VALUES (1, 7, 'ABIERTO')"))
            connection.execute(text("""INSERT INTO producto VALUES
                (1, 'Cuaderno', 12.50, 7.25, TRUE),
                (2, 'Lápiz', 3.75, 1.10, TRUE),
                (3, 'Producto inactivo', 5.00, 2.00, FALSE)"""))
            connection.execute(text("""INSERT INTO movimiento_inventario
                (id_producto, tipo, cantidad, costo_unitario, motivo) VALUES
                (1, 'ENTRADA', 10, 7.25, 'Inventario inicial'),
                (2, 'ENTRADA', 20, 1.10, 'Inventario inicial'),
                (3, 'ENTRADA', 5, 2.00, 'Inventario inicial')"""))
        self.session = Session(self.engine)
        self.adaptada = SessionSQLite(self.session)
        self.solicitud = {
            "cliente_id": 1, "metodo": "EFECTIVO", "recibido": "100.00",
            # El cliente intenta enviar precios distintos al catálogo.
            "items": [
                {"id": 2, "cantidad": 3, "descuento": 0, "precio_venta": "0.01"},
                {"id": 1, "cantidad": 2, "descuento": "1.25", "precio_venta": "999.00"},
            ],
        }

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def cobrar(self, solicitud=None, usuario_id=7):
        with self.session.begin():
            return registrar_venta(
                self.adaptada,
                self.solicitud if solicitud is None else solicitud,
                usuario_id,
            )

    def filas(self, sql):
        with self.engine.connect() as connection:
            return [dict(fila) for fila in connection.execute(text(sql)).mappings()]

    def escalar(self, sql):
        with self.engine.connect() as connection:
            return connection.execute(text(sql)).scalar_one()

    def assert_sin_venta(self):
        for tabla in ("venta", "detalle_venta", "movimiento_caja"):
            self.assertEqual(self.escalar(f"SELECT COUNT(*) FROM {tabla}"), 0)
        self.assertEqual(self.escalar(
            "SELECT COUNT(*) FROM movimiento_inventario WHERE tipo='SALIDA'"
        ), 0)
        saldos = self.filas("SELECT * FROM vista_inventario ORDER BY id_producto")
        self.assertEqual([fila["existencia_actual"] for fila in saldos], [10, 20, 5])

    def test_efectivo_recalcula_precios_y_registra_stock_caja_y_costos_historicos(self):
        resultado = self.cobrar()
        # 2 * 12.50 - 1.25 + 3 * 3.75 = 35; recibido 100, cambio 65.
        self.assertEqual(resultado["total"], "35.00")
        self.assertEqual(resultado["cambio"], "65.00")
        self.assertRegex(resultado["numero"], r"^PC-\d{8}-[0-9A-F]{8}$")

        venta = self.filas("SELECT * FROM venta")[0]
        self.assertEqual(venta["id_venta"], resultado["id_venta"])
        self.assertEqual(venta["numero_ticket"], resultado["numero"])
        self.assertEqual(venta["id_cliente"], 1)
        self.assertEqual(venta["id_usuario"], 7)
        self.assertEqual(venta["id_turno"], 1)
        self.assertEqual(venta["estado"], "COMPLETADA")
        self.assertEqual(Decimal(str(venta["total"])), Decimal("35.00"))

        detalles = self.filas("SELECT * FROM detalle_venta ORDER BY id_producto")
        self.assertEqual(len(detalles), 2)
        for detalle, cantidad, precio, costo, descuento in (
            (detalles[0], 2, "12.50", "7.25", "1.25"),
            (detalles[1], 3, "3.75", "1.10", "0"),
        ):
            self.assertEqual(detalle["id_venta"], resultado["id_venta"])
            self.assertEqual(detalle["cantidad"], cantidad)
            self.assertEqual(Decimal(str(detalle["precio_unitario"])), Decimal(precio))
            self.assertEqual(Decimal(str(detalle["costo_unitario_historico"])), Decimal(costo))
            self.assertEqual(Decimal(str(detalle["descuento"])), Decimal(descuento))
            self.assertEqual(detalle["tasa_iva"], 12)

        salidas = self.filas("""SELECT * FROM movimiento_inventario
            WHERE tipo='SALIDA' ORDER BY id_producto""")
        self.assertEqual([fila["cantidad"] for fila in salidas], [2, 3])
        self.assertEqual([Decimal(str(fila["costo_unitario"])) for fila in salidas],
                         [Decimal("7.25"), Decimal("1.10")])
        self.assertTrue(all(fila["motivo"] == f'Venta {resultado["numero"]}'
                            for fila in salidas))
        self.assertEqual([fila["existencia_actual"] for fila in self.filas(
            "SELECT * FROM vista_inventario ORDER BY id_producto"
        )], [8, 17, 5])
        caja = self.filas("SELECT * FROM movimiento_caja")
        self.assertEqual(len(caja), 1)
        self.assertEqual(caja[0]["tipo"], "INGRESO")
        self.assertEqual(caja[0]["id_turno"], 1)
        self.assertEqual(Decimal(str(caja[0]["monto"])), Decimal("35.00"))
        self.assertEqual(caja[0]["concepto"], f'Venta {resultado["numero"]}')

        with self.session.begin():
            self.session.execute(text("UPDATE producto SET costo=99, precio_venta=150"))
        self.assertEqual(self.filas("SELECT * FROM detalle_venta ORDER BY id_producto"), detalles)

    def test_tarjeta_registra_venta_sin_movimiento_de_efectivo(self):
        self.solicitud.update(metodo="TARJETA", recibido=0)
        resultado = self.cobrar()
        self.assertEqual(resultado["total"], "35.00")
        self.assertEqual(resultado["cambio"], "0.00")
        self.assertEqual(self.escalar("SELECT metodo_pago FROM venta"), "TARJETA")
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM movimiento_caja"), 0)
        self.assertEqual(self.escalar(
            "SELECT existencia_actual FROM vista_inventario WHERE id_producto=1"
        ), 8)

    def test_transferencia_tambien_desconta_stock_sin_ingreso_de_caja(self):
        self.solicitud.update(metodo="TRANSFERENCIA", recibido=0)
        self.assertEqual(self.cobrar()["cambio"], "0.00")
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM venta"), 1)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM movimiento_caja"), 0)
        self.assertEqual(self.escalar(
            "SELECT existencia_actual FROM vista_inventario WHERE id_producto=2"
        ), 17)

    def test_stock_insuficiente_no_crea_ninguna_parte_de_la_venta(self):
        self.solicitud["items"] = [{"id": 1, "cantidad": 11}]
        with self.assertRaisesRegex(ValueError, "Stock insuficiente"):
            self.cobrar()
        self.assert_sin_venta()

    def test_descuentos_invalidos_no_generan_movimientos(self):
        for descuento in ("-0.01", "NaN", "Infinity", True, "12.51", "10000000000"):
            with self.subTest(descuento=descuento):
                self.solicitud["items"] = [{"id": 1, "cantidad": 1, "descuento": descuento}]
                with self.assertRaises(ValueError):
                    self.cobrar()
                self.assert_sin_venta()

    def test_cantidades_json_no_se_truncan_ni_admiten_booleanos(self):
        for cantidad in (1.5, True, False, 0, -1, None):
            with self.subTest(cantidad=cantidad):
                self.solicitud["items"] = [{"id": 1, "cantidad": cantidad}]
                with self.assertRaises(ValueError):
                    self.cobrar()
                self.assert_sin_venta()

    def test_solicitudes_y_carritos_invalidos(self):
        solicitudes = [None, [], {}, {**self.solicitud, "items": []},
            {**self.solicitud, "items": "inválido"},
            {**self.solicitud, "items": [None]},
            {**self.solicitud, "items": [{"id": 1, "cantidad": 1}] * 81},
            {**self.solicitud, "metodo": "CREDITO"},
            {**self.solicitud, "cliente_id": None},
            {**self.solicitud, "cliente_id": True},
            {**self.solicitud, "cliente_id": 1.5},
            {**self.solicitud, "items": [{"id": True, "cantidad": 1}]},
            {**self.solicitud, "items": [{"id": 1.5, "cantidad": 1}]},
            {**self.solicitud, "items": [{"id": 1, "cantidad": 1}] * 2},
        ]
        for solicitud in solicitudes:
            with self.subTest(solicitud=solicitud):
                with self.assertRaises(ValueError):
                    # Llamada directa para poder probar None, sin el valor por defecto de cobrar.
                    with self.session.begin():
                        registrar_venta(self.adaptada, solicitud, 7)
                self.assert_sin_venta()

    def test_cliente_y_productos_deben_existir_y_producto_debe_estar_activo(self):
        solicitudes = [
            {**self.solicitud, "cliente_id": 999},
            {**self.solicitud, "items": [{"id": 999, "cantidad": 1}]},
            {**self.solicitud, "items": [{"id": 3, "cantidad": 1}]},
        ]
        for solicitud in solicitudes:
            with self.subTest(solicitud=solicitud):
                with self.assertRaises(ValueError):
                    self.cobrar(solicitud)
                self.assert_sin_venta()

    def test_requiere_turno_abierto_del_mismo_usuario(self):
        with self.assertRaisesRegex(ValueError, "abrir caja"):
            self.cobrar(usuario_id=8)
        self.assert_sin_venta()
        with self.session.begin():
            self.session.execute(text("UPDATE turno_caja SET estado='CERRADO'"))
        with self.assertRaisesRegex(ValueError, "abrir caja"):
            self.cobrar()
        self.assert_sin_venta()

    def test_efectivo_insuficiente_o_invalido_no_registra_venta(self):
        for recibido in ("34.99", "-0.01", "NaN", "Infinity", True, "10000000000"):
            with self.subTest(recibido=recibido):
                solicitud = {**self.solicitud, "recibido": recibido}
                with self.assertRaises(ValueError):
                    self.cobrar(solicitud)
                self.assert_sin_venta()

    def test_descuento_total_registra_venta_y_salida_sin_ingreso_cero(self):
        self.solicitud.update(recibido=0, items=[
            {"id": 1, "cantidad": 1, "descuento": "12.50"},
        ])
        resultado = self.cobrar()
        self.assertEqual(resultado["total"], "0.00")
        self.assertEqual(resultado["cambio"], "0.00")
        self.assertEqual(self.escalar("SELECT total FROM venta"), 0)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM detalle_venta"), 1)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM movimiento_caja"), 0)
        self.assertEqual(self.escalar(
            "SELECT existencia_actual FROM vista_inventario WHERE id_producto=1"
        ), 9)

    def test_total_superior_a_capacidad_del_esquema_no_se_guarda(self):
        with self.session.begin():
            self.session.execute(text("UPDATE producto SET precio_venta=9999999999.99 WHERE id_producto=1"))
        self.solicitud.update(metodo="TARJETA", recibido=0, items=[{"id": 1, "cantidad": 2}])
        with self.assertRaisesRegex(ValueError, "total supera"):
            self.cobrar()
        self.assert_sin_venta()

    def test_error_durante_escrituras_revierte_venta_detalles_stock_y_caja(self):
        with self.engine.begin() as connection:
            connection.execute(text("""CREATE TRIGGER rechazar_segunda_salida
                BEFORE INSERT ON movimiento_inventario
                WHEN NEW.tipo='SALIDA' AND NEW.id_producto=2
                BEGIN SELECT RAISE(ABORT, 'Fallo de inventario de prueba'); END"""))
        with self.assertRaises(IntegrityError):
            self.cobrar()
        # Confirma que el error ocurrió después de insertar parte de la venta.
        inserciones = [sql for sql in self.adaptada.ejecutadas if sql.startswith("INSERT INTO")]
        self.assertEqual(sum(sql.startswith("INSERT INTO venta(") for sql in inserciones), 1)
        self.assertEqual(sum(sql.startswith("INSERT INTO detalle_venta(") for sql in inserciones), 2)
        self.assertEqual(sum(sql.startswith("INSERT INTO movimiento_inventario(") for sql in inserciones), 1)
        self.assert_sin_venta()

    def test_servicio_deja_commit_y_rollback_en_manos_del_llamador(self):
        self.session.begin()
        registrar_venta(self.adaptada, copy.deepcopy(self.solicitud), 7)
        self.assertEqual(self.session.execute(text("SELECT COUNT(*) FROM venta")).scalar_one(), 1)
        self.session.rollback()
        self.assert_sin_venta()

    def test_reintento_devuelve_respuesta_original_aun_con_turno_cerrado(self):
        self.solicitud['clave_operacion'] = 'a' * 32
        resultado = self.cobrar()
        with self.session.begin():
            self.session.execute(text("UPDATE turno_caja SET estado='CERRADO'"))
        self.assertEqual(self.cobrar(copy.deepcopy(self.solicitud)), resultado)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM venta'), 1)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM detalle_venta'), 2)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM movimiento_caja'), 1)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM movimiento_inventario WHERE tipo='SALIDA'"), 2)

    def test_clave_reutilizada_con_payload_distinto_se_rechaza(self):
        self.solicitud['clave_operacion'] = 'b' * 32
        original = self.cobrar()
        cambiada = copy.deepcopy(self.solicitud)
        cambiada['recibido'] = '200.00'
        with self.assertRaises(IdempotenciaError):
            self.cobrar(cambiada)
        self.assertEqual(self.cobrar(), original)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM venta'), 1)

    def test_rollback_no_consume_clave(self):
        self.solicitud['clave_operacion'] = 'c' * 32
        with self.assertRaisesRegex(RuntimeError, 'Fallo externo'):
            with self.session.begin():
                registrar_venta(self.adaptada, self.solicitud, 7)
                raise RuntimeError('Fallo externo')
        self.assert_sin_venta()
        self.assertEqual(self.cobrar()['total'], '35.00')

    def pendiente(self):
        self.solicitud.update(metodo='TRANSFERENCIA', estado_pago='PENDIENTE', recibido=0,
                              clave_operacion='p' * 32)
        return self.cobrar()

    def resolver(self, resultado, pago, motivo='', actor_id=7):
        with self.session.begin():
            return resolver_pago(self.adaptada, resultado['id_venta'], actor_id, pago, motivo)

    def test_pendiente_no_mueve_stock_ni_caja_y_confirmacion_es_unica(self):
        original = self.pendiente()
        self.assertEqual(original['estado_pago'], 'PENDIENTE')
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM venta WHERE estado='COMPLETADA'"), 0)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM movimiento_caja'), 0)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM movimiento_inventario WHERE tipo='SALIDA'"), 0)
        with self.session.begin():
            self.session.execute(text('UPDATE producto SET costo=8,precio_venta=99 WHERE id_producto=1'))
        confirmada = self.resolver(original, 'CONFIRMADO')
        self.assertEqual(self.resolver(original, 'CONFIRMADO'), confirmada)
        self.assertEqual(confirmada['estado'], 'COMPLETADA')
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM movimiento_inventario WHERE tipo='SALIDA'"), 2)
        self.assertEqual(self.escalar('SELECT COUNT(*) FROM movimiento_caja'), 0)
        self.assertEqual(self.escalar('SELECT total FROM venta'), 35)
        self.assertEqual(Decimal(str(self.escalar('SELECT precio_unitario FROM detalle_venta WHERE id_producto=1'))), Decimal('12.5'))
        self.assertEqual(self.escalar('SELECT costo_unitario_historico FROM detalle_venta WHERE id_producto=1'), 8)
        # La confirmación posterior no cambia la respuesta persistida del alta.
        self.assertEqual(self.cobrar(), original)
        with self.assertRaisesRegex(ValueError, 'definitivo'):
            self.resolver(original, 'FALLIDO', 'No se recibió')

    def test_fallido_no_mueve_stock_ni_puede_confirmarse_despues(self):
        resultado = self.pendiente()
        with self.assertRaisesRegex(ValueError, 'motivo'):
            self.resolver(resultado, 'FALLIDO')
        final = self.resolver(resultado, 'FALLIDO', 'Transferencia rechazada')
        self.assertEqual(final['estado'], 'FALLIDA')
        self.assertEqual(self.resolver(resultado, 'FALLIDO', 'Reintento'), final)
        self.assertEqual(self.escalar('SELECT motivo_pago FROM venta'), 'Transferencia rechazada')
        with self.assertRaisesRegex(ValueError, 'definitivo'):
            self.resolver(resultado, 'CONFIRMADO')
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM movimiento_inventario WHERE tipo='SALIDA'"), 0)

    def test_efectivo_no_admite_pendiente_o_fallido_al_crear(self):
        for estado in ('PENDIENTE', 'FALLIDO', '', None):
            with self.subTest(estado=estado), self.assertRaises(ValueError):
                self.cobrar({**self.solicitud, 'estado_pago': estado})
        self.assert_sin_venta()

    def test_confirmar_revalida_stock_turno_y_permisos(self):
        resultado = self.pendiente()
        with self.session.begin():
            self.session.execute(text("UPDATE turno_caja SET estado='CERRADO'"))
        with self.assertRaisesRegex(ValueError, 'turno original'):
            self.resolver(resultado, 'CONFIRMADO')
        with self.session.begin():
            self.session.execute(text("UPDATE turno_caja SET estado='ABIERTO'"))
            self.session.execute(text("INSERT INTO movimiento_inventario(id_producto,tipo,cantidad,costo_unitario) VALUES(2,'SALIDA',19,1.1)"))
        with self.assertRaisesRegex(ValueError, 'Stock insuficiente'):
            self.resolver(resultado, 'CONFIRMADO')
        # La salida previa de producto 1 durante el intento se revierte.
        self.assertEqual(self.escalar('SELECT existencia_actual FROM vista_inventario WHERE id_producto=1'), 10)
        self.assertEqual(self.escalar('SELECT estado_pago FROM venta'), 'PENDIENTE')
        with self.session.begin():
            self.session.execute(text('UPDATE usuario SET estado=FALSE WHERE id_usuario=7'))
        with self.assertRaises(PermissionError):
            self.resolver(resultado, 'FALLIDO', 'No acreditado')

    def test_otro_cajero_no_puede_resolver_pendiente_y_admin_si(self):
        resultado = self.pendiente()
        with self.session.begin():
            self.session.execute(text("INSERT INTO rol VALUES(2,'Cajero')"))
            self.session.execute(text('UPDATE usuario SET id_rol=2 WHERE id_usuario=8'))
        with self.assertRaises(PermissionError):
            self.resolver(resultado, 'CONFIRMADO', actor_id=8)
        with self.session.begin():
            self.session.execute(text('UPDATE usuario SET id_rol=1 WHERE id_usuario=8'))
        self.assertEqual(self.resolver(resultado, 'CONFIRMADO', actor_id=8)['estado_pago'], 'CONFIRMADO')

    def test_anulacion_conserva_historial_pago_y_respuesta_original(self):
        self.solicitud['clave_operacion'] = 'n' * 32
        resultado = self.cobrar()
        with self.session.begin():
            anular_venta(self.adaptada, resultado['id_venta'], 7, 'Cambio solicitado')
        self.assertEqual(self.escalar('SELECT estado_pago FROM venta'), 'CONFIRMADO')
        self.assertEqual(self.escalar('SELECT estado FROM venta'), 'ANULADA')
        self.assertEqual(self.cobrar(), resultado)
        with self.assertRaisesRegex(ValueError, 'anulada'):
            self.resolver(resultado, 'CONFIRMADO')

    def test_claves_invalidas_no_crean_venta(self):
        for clave in ('', 'corta', 'a' * 101, 123, 'x' * 15 + '!'):
            with self.subTest(clave=clave), self.assertRaises(ValueError):
                self.cobrar({**self.solicitud, 'clave_operacion': clave})
        self.assert_sin_venta()

    def test_ids_y_cantidades_fuera_de_integer32_se_rechazan_sin_escrituras(self):
        solicitudes = [dict(self.solicitud, cliente_id=2147483648),
                       dict(self.solicitud, items=[{'id': 2147483648, 'cantidad': 1}]),
                       dict(self.solicitud, items=[{'id': 1, 'cantidad': 2147483648}])]
        for solicitud in solicitudes:
            with self.subTest(solicitud=solicitud), self.assertRaises(ValueError):
                self.cobrar(solicitud)
        self.assert_sin_venta()

    def test_motivo_pago_json_no_textual_se_rechaza(self):
        venta = self.pendiente()
        for motivo in (123, True, [], {}):
            with self.subTest(motivo=motivo), self.assertRaises(ValueError):
                self.resolver(venta, 'FALLIDO', motivo)
        self.assertEqual(self.escalar('SELECT estado_pago FROM venta'), 'PENDIENTE')


if __name__ == "__main__":
    unittest.main()
