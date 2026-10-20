"""Carreras sobre PostgreSQL real; se ejecutan con el runner dedicado.

Los Event retienen transacciones en puntos concretos. pg_blocking_pids verifica
que la segunda conexión realmente espera un bloqueo antes de liberar la primera.
No se sustituyen sentencias SQL ni se desactiva CSRF en las peticiones de caja.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event, current_thread
from time import monotonic

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from entorno_pg import CasoPostgres
from ventas_servicio import registrar_venta


class ConcurrenciaPostgresTest(CasoPostgres):
    ESPERA = 5

    def solicitud(self, productos=None, cantidad=7):
        productos = productos or [self.ids["producto1"]]
        return {
            "cliente_id": self.ids["cliente"],
            "metodo": "EFECTIVO",
            "recibido": "1000.00",
            "items": [{"id": producto, "cantidad": cantidad} for producto in productos],
        }

    def escalar(self, sql, **params):
        with self.engine.connect() as connection:
            return connection.execute(text(sql), params).scalar_one()

    def saldo(self, producto="producto1"):
        return self.escalar(
            "SELECT existencia_actual FROM vista_inventario WHERE id_producto=:id",
            id=self.ids[producto],
        )

    def esperar_evento(self, senal, futuro=None):
        if not senal.wait(self.ESPERA):
            if futuro is not None and futuro.done():
                futuro.result()  # Expone el error original del trabajador.
            self.fail("El trabajador no alcanzó el punto de coordinación.")

    def esperar_bloqueo(self, bloqueado, bloqueador):
        """Observa un bloqueo real de PostgreSQL, con un límite breve."""
        limite = monotonic() + 3
        pausa = Event()
        with self.engine.connect() as connection:
            while monotonic() < limite:
                bloqueadores = connection.execute(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": bloqueado}
                ).scalar_one()
                if bloqueador in bloqueadores:
                    return
                pausa.wait(0.02)
        self.fail(
            f"PostgreSQL no mostró a {bloqueado} esperando al proceso {bloqueador}."
        )

    def vender(self, usuario, solicitud, proceso, preparada=None, liberar=None,
               revertir=False, al_bloquear_producto=None):
        """Cada trabajador tiene su propia conexión y su propia transacción."""
        with Session(self.engine) as session:
            connection = session.connection()
            proceso["pid"] = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            proceso["listo"].set()
            if al_bloquear_producto is not None:
                event.listen(connection, "after_cursor_execute", al_bloquear_producto)
            try:
                resultado = registrar_venta(session, solicitud, self.ids[usuario])
                if preparada is not None:
                    preparada.set()
                if liberar is not None and not liberar.wait(self.ESPERA):
                    raise AssertionError("No se liberó la transacción retenida de venta.")
                if revertir:
                    session.rollback()
                else:
                    session.commit()
                return resultado
            except BaseException:
                session.rollback()
                raise
            finally:
                if al_bloquear_producto is not None:
                    event.remove(connection, "after_cursor_execute", al_bloquear_producto)

    @staticmethod
    def proceso():
        return {"listo": Event()}

    def verificar_venta_unica(self, usuario):
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM venta"), 1)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM detalle_venta"), 1)
        self.assertEqual(self.escalar("SELECT id_usuario FROM venta"), self.ids[usuario])
        self.assertEqual(self.saldo(), 3)
        self.assertEqual(self.saldo("producto2"), 10)
        self.assertEqual(self.escalar(
            "SELECT SUM(cantidad) FROM movimiento_inventario WHERE tipo='SALIDA'"
        ), 7)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM movimiento_caja"), 1)
        self.assertEqual(self.escalar("SELECT SUM(monto) FROM movimiento_caja"),
                         Decimal("105.00"))

    def test_dos_cajeros_compiten_por_siete_de_diez_unidades(self):
        primera, segunda = self.proceso(), self.proceso()
        preparada, liberar = Event(), Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            ganadora = pool.submit(
                self.vender, "cajero1", self.solicitud(), primera, preparada, liberar
            )
            try:
                self.esperar_evento(preparada, ganadora)
                perdedora = pool.submit(self.vender, "cajero2", self.solicitud(), segunda)
                self.esperar_evento(segunda["listo"], perdedora)
                self.esperar_bloqueo(segunda["pid"], primera["pid"])
                # Otra conexión no puede ver escrituras todavía sin confirmar.
                self.assertEqual(self.saldo(), 10)
                self.assertEqual(self.escalar("SELECT COUNT(*) FROM venta"), 0)
            finally:
                liberar.set()
            self.assertEqual(ganadora.result(timeout=self.ESPERA)["total"], "105.00")
            with self.assertRaisesRegex(ValueError, "Stock insuficiente"):
                perdedora.result(timeout=self.ESPERA)
        self.verificar_venta_unica("cajero1")

    def test_carritos_en_orden_inverso_bloquean_productos_en_orden_comun(self):
        p1, p2 = sorted((self.ids["producto1"], self.ids["producto2"]))
        primera, segunda = self.proceso(), self.proceso()
        primer_producto, liberar = Event(), Event()
        orden_primera, orden_segunda = [], []

        def observar(orden, retener=False):
            def despues(connection, cursor, statement, parameters, context, many):
                if "FROM producto" in statement and "FOR UPDATE" in statement:
                    producto = parameters["id"]
                    orden.append(producto)
                    if retener and len(orden) == 1:
                        primer_producto.set()
                        if not liberar.wait(self.ESPERA):
                            raise AssertionError("No se liberó el primer bloqueo de producto.")
            return despues

        with ThreadPoolExecutor(max_workers=2) as pool:
            venta1 = pool.submit(
                self.vender, "cajero1", self.solicitud([p1, p2], 2), primera,
                al_bloquear_producto=observar(orden_primera, True),
            )
            try:
                self.esperar_evento(primer_producto, venta1)
                venta2 = pool.submit(
                    self.vender, "cajero2", self.solicitud([p2, p1], 2), segunda,
                    al_bloquear_producto=observar(orden_segunda),
                )
                self.esperar_evento(segunda["listo"], venta2)
                self.esperar_bloqueo(segunda["pid"], primera["pid"])
            finally:
                liberar.set()
            self.assertEqual(venta1.result(timeout=self.ESPERA)["total"], "60.00")
            self.assertEqual(venta2.result(timeout=self.ESPERA)["total"], "60.00")
        self.assertEqual(orden_primera, [p1, p2])
        self.assertEqual(orden_segunda, [p1, p2])
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM venta"), 2)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM detalle_venta"), 4)
        self.assertEqual(self.saldo(), 6)
        self.assertEqual(self.saldo("producto2"), 6)
        self.assertEqual(self.escalar("SELECT SUM(monto) FROM movimiento_caja"),
                         Decimal("120.00"))

    def test_rollback_libera_stock_y_no_deja_partes_de_la_venta(self):
        primera, segunda = self.proceso(), self.proceso()
        preparada, liberar = Event(), Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            revertida = pool.submit(
                self.vender, "cajero1", self.solicitud(), primera, preparada, liberar, True
            )
            try:
                self.esperar_evento(preparada, revertida)
                confirmada = pool.submit(self.vender, "cajero2", self.solicitud(), segunda)
                self.esperar_evento(segunda["listo"], confirmada)
                self.esperar_bloqueo(segunda["pid"], primera["pid"])
                self.assertEqual(self.saldo(), 10)
            finally:
                liberar.set()
            descartada = revertida.result(timeout=self.ESPERA)
            guardada = confirmada.result(timeout=self.ESPERA)
        self.assertNotEqual(descartada["id_venta"], guardada["id_venta"])
        self.assertEqual(self.escalar("SELECT id_venta FROM venta"), guardada["id_venta"])
        self.verificar_venta_unica("cajero2")

    def cliente_formulario(self, ruta, usuario="admin"):
        """Sesión autenticada independiente con el token CSRF real del formulario."""
        flask_app = getattr(self.app, "app", self.app)
        cliente = flask_app.test_client()
        with cliente.session_transaction() as session:
            session["_user_id"] = str(self.ids[usuario])
            session["_fresh"] = True
        pagina = cliente.get(ruta)
        self.assertEqual(pagina.status_code, 200)
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"',
                          pagina.get_data(as_text=True))
        self.assertIsNotNone(token, "El formulario debe incluir CSRF.")
        return cliente, token.group(1)

    def cliente_caja(self):
        return self.cliente_formulario("/caja", "cajero1")

    @staticmethod
    def pid_conexion(connection):
        driver = connection.connection.driver_connection
        if hasattr(driver, "get_backend_pid"):
            return driver.get_backend_pid()
        return driver.info.backend_pid

    def test_cierre_http_espera_venta_y_conserva_su_ingreso(self):
        cliente, token = self.cliente_caja()
        primera, cierre = self.proceso(), self.proceso()
        preparada, liberar = Event(), Event()
        nombre_cierre = {}

        def observar(connection, cursor, statement, parameters, context, many):
            if (current_thread().ident == nombre_cierre.get("ident")
                    and "FROM turno_caja" in statement and "FOR UPDATE" in statement):
                cierre["pid"] = self.pid_conexion(connection)
                cierre["listo"].set()

        def cerrar():
            nombre_cierre["ident"] = current_thread().ident
            return cliente.post("/caja", data={
                "accion": "cerrar", "efectivo_contado": "205.00", "csrf_token": token,
            })

        event.listen(self.engine, "before_cursor_execute", observar)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                venta = pool.submit(
                    self.vender, "cajero1", self.solicitud(), primera, preparada, liberar
                )
                try:
                    self.esperar_evento(preparada, venta)
                    respuesta = pool.submit(cerrar)
                    self.esperar_evento(cierre["listo"], respuesta)
                    self.esperar_bloqueo(cierre["pid"], primera["pid"])
                    self.assertEqual(self.escalar(
                        "SELECT estado FROM turno_caja WHERE id_turno=:id",
                        id=self.ids["turno1"],
                    ), "ABIERTO")
                finally:
                    liberar.set()
                venta.result(timeout=self.ESPERA)
                self.assertEqual(respuesta.result(timeout=self.ESPERA).status_code, 302)
        finally:
            event.remove(self.engine, "before_cursor_execute", observar)
        self.verificar_venta_unica("cajero1")
        self.assertEqual(self.escalar(
            "SELECT estado FROM turno_caja WHERE id_turno=:id", id=self.ids["turno1"]
        ), "CERRADO")
        self.assertEqual(self.escalar(
            "SELECT efectivo_contado FROM turno_caja WHERE id_turno=:id",
            id=self.ids["turno1"],
        ), Decimal("205.00"))
        self.assertEqual(self.escalar("SELECT id_turno FROM venta"), self.ids["turno1"])

    def test_venta_espera_cierre_http_y_rechaza_turno_ya_cerrado(self):
        cliente, token = self.cliente_caja()
        cierre, venta = self.proceso(), self.proceso()
        cierre_escrito, liberar = Event(), Event()
        nombre_cierre = {}

        def retener(connection, cursor, statement, parameters, context, many):
            if (current_thread().ident == nombre_cierre.get("ident")
                    and "UPDATE turno_caja SET cierre" in statement):
                cierre["pid"] = self.pid_conexion(connection)
                cierre_escrito.set()
                if not liberar.wait(self.ESPERA):
                    raise AssertionError("No se liberó la transacción retenida de cierre.")

        def cerrar():
            nombre_cierre["ident"] = current_thread().ident
            return cliente.post("/caja", data={
                "accion": "cerrar", "efectivo_contado": "100.00", "csrf_token": token,
            })

        event.listen(self.engine, "after_cursor_execute", retener)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                respuesta = pool.submit(cerrar)
                try:
                    self.esperar_evento(cierre_escrito, respuesta)
                    rechazada = pool.submit(self.vender, "cajero1", self.solicitud(), venta)
                    self.esperar_evento(venta["listo"], rechazada)
                    self.esperar_bloqueo(venta["pid"], cierre["pid"])
                finally:
                    liberar.set()
                self.assertEqual(respuesta.result(timeout=self.ESPERA).status_code, 302)
                with self.assertRaisesRegex(ValueError, "abrir caja"):
                    rechazada.result(timeout=self.ESPERA)
        finally:
            event.remove(self.engine, "after_cursor_execute", retener)
        self.assertEqual(self.escalar(
            "SELECT estado FROM turno_caja WHERE id_turno=:id", id=self.ids["turno1"]
        ), "CERRADO")
        self.assertEqual(self.saldo(), 10)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM venta"), 0)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM detalle_venta"), 0)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM movimiento_caja"), 0)
        self.assertEqual(self.escalar(
            "SELECT COUNT(*) FROM movimiento_inventario WHERE tipo='SALIDA'"
        ), 0)

    def test_salida_http_espera_venta_y_rechaza_stock_ya_consumido(self):
        cliente, token = self.cliente_formulario("/inventario")
        venta, salida = self.proceso(), self.proceso()
        preparada, liberar = Event(), Event()
        nombre_salida = {}

        def observar(connection, cursor, statement, parameters, context, many):
            if (current_thread().ident == nombre_salida.get("ident")
                    and "FROM producto" in statement and "FOR UPDATE" in statement):
                salida["pid"] = self.pid_conexion(connection)
                salida["listo"].set()

        def retirar():
            nombre_salida["ident"] = current_thread().ident
            return cliente.post("/inventario/movimiento", data={
                "id_producto": self.ids["producto1"], "tipo": "SALIDA",
                "cantidad": "7", "motivo": "Ajuste de inventario", "csrf_token": token,
            })

        event.listen(self.engine, "before_cursor_execute", observar)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                confirmada = pool.submit(
                    self.vender, "cajero1", self.solicitud(), venta, preparada, liberar
                )
                try:
                    self.esperar_evento(preparada, confirmada)
                    respuesta = pool.submit(retirar)
                    self.esperar_evento(salida["listo"], respuesta)
                    self.esperar_bloqueo(salida["pid"], venta["pid"])
                    self.assertEqual(self.saldo(), 10)
                finally:
                    liberar.set()
                self.assertEqual(confirmada.result(timeout=self.ESPERA)["total"], "105.00")
                self.assertEqual(respuesta.result(timeout=self.ESPERA).status_code, 302)
        finally:
            event.remove(self.engine, "before_cursor_execute", observar)
        with cliente.session_transaction() as session:
            mensajes = [mensaje for categoria, mensaje in session.get("_flashes", [])]
        self.assertTrue(any("Existencia insuficiente" in mensaje for mensaje in mensajes))
        self.verificar_venta_unica("cajero1")
        self.assertEqual(self.escalar(
            "SELECT COUNT(*) FROM movimiento_inventario WHERE motivo=:motivo",
            motivo="Ajuste de inventario",
        ), 0)

    def test_compra_http_serializa_stock_y_nuevo_costo_historico_con_venta(self):
        cliente, token = self.cliente_formulario("/compras")
        compra, venta = self.proceso(), self.proceso()
        compra_escrita, liberar = Event(), Event()
        nombre_compra = {}

        def retener(connection, cursor, statement, parameters, context, many):
            if (current_thread().ident == nombre_compra.get("ident")
                    and "UPDATE producto SET costo" in statement):
                compra["pid"] = self.pid_conexion(connection)
                compra_escrita.set()
                if not liberar.wait(self.ESPERA):
                    raise AssertionError("No se liberó la transacción retenida de compra.")

        def comprar():
            nombre_compra["ident"] = current_thread().ident
            return cliente.post("/compras/nueva", data={
                "id_proveedor": self.ids["proveedor"],
                "id_producto": self.ids["producto1"], "cantidad": "5",
                "costo_unitario": "11.25", "numero_factura": "PG-CONCURRENCIA",
                "metodo_pago": "TRANSFERENCIA", "csrf_token": token,
            })

        event.listen(self.engine, "after_cursor_execute", retener)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                respuesta = pool.submit(comprar)
                try:
                    self.esperar_evento(compra_escrita, respuesta)
                    # La entrada y el cambio de costo aún pertenecen a la compra pendiente.
                    self.assertEqual(self.saldo(), 10)
                    self.assertEqual(self.escalar(
                        "SELECT costo FROM producto WHERE id_producto=:id",
                        id=self.ids["producto1"],
                    ), Decimal("9.00"))
                    self.assertEqual(self.escalar("SELECT COUNT(*) FROM compra"), 0)
                    confirmada = pool.submit(self.vender, "cajero1", self.solicitud(), venta)
                    self.esperar_evento(venta["listo"], confirmada)
                    self.esperar_bloqueo(venta["pid"], compra["pid"])
                finally:
                    liberar.set()
                self.assertEqual(respuesta.result(timeout=self.ESPERA).status_code, 302)
                self.assertEqual(confirmada.result(timeout=self.ESPERA)["total"], "105.00")
        finally:
            event.remove(self.engine, "after_cursor_execute", retener)
        self.assertEqual(self.saldo(), 8)  # 10 iniciales + 5 comprados - 7 vendidos.
        self.assertEqual(self.saldo("producto2"), 10)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM compra"), 1)
        self.assertEqual(self.escalar("SELECT total FROM compra"), Decimal("56.25"))
        self.assertEqual(self.escalar("SELECT id_usuario FROM compra"), self.ids["admin"])
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM detalle_compra"), 1)
        self.assertEqual(self.escalar("SELECT cantidad FROM detalle_compra"), 5)
        self.assertEqual(self.escalar("SELECT costo_unitario FROM detalle_compra"),
                         Decimal("11.25"))
        self.assertEqual(self.escalar(
            "SELECT costo FROM producto WHERE id_producto=:id", id=self.ids["producto1"]
        ), Decimal("11.25"))
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM venta"), 1)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM detalle_venta"), 1)
        self.assertEqual(self.escalar("SELECT costo_unitario_historico FROM detalle_venta"),
                         Decimal("11.25"))
        self.assertEqual(self.escalar("SELECT precio_unitario FROM detalle_venta"),
                         Decimal("15.00"))
        self.assertEqual(self.escalar(
            "SELECT costo_unitario FROM movimiento_inventario WHERE tipo='SALIDA'"
        ), Decimal("11.25"))
        self.assertEqual(self.escalar(
            "SELECT cantidad FROM movimiento_inventario WHERE motivo LIKE 'Compra #%'"
        ), 5)
        self.assertEqual(self.escalar("SELECT COUNT(*) FROM movimiento_caja"), 1)
        self.assertEqual(self.escalar("SELECT SUM(monto) FROM movimiento_caja"),
                         Decimal("105.00"))
