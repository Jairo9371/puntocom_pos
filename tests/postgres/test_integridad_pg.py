"""Carreras HTTP de caja y documentos sobre la base PostgreSQL desechable.

La primera petición espera tras comprobar la ausencia del registro. La segunda
debe esperar un bloqueo transaccional antes de repetir esa comprobación. Cuando
falta la protección, se permite finalizar a ambas para exponer el duplicado.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread
from time import monotonic

from sqlalchemy import event, text

from entorno_pg import CasoPostgres


class IntegridadConcurrentePostgresTest(CasoPostgres):
    ESPERA = 6
    DOCUMENTO = "8765432-K"

    def escalar(self, sql, **params):
        with self.engine.connect() as conexion:
            return conexion.execute(text(sql), params).scalar_one()

    def cliente_autenticado(self, ruta):
        cliente = self.app.app.test_client()
        with cliente.session_transaction() as sesion:
            sesion["_user_id"] = str(self.ids["admin"])
            sesion["_fresh"] = True
        pagina = cliente.get(ruta)
        self.assertEqual(pagina.status_code, 200)
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"',
                          pagina.get_data(as_text=True))
        self.assertIsNotNone(token, "El formulario debe incluir un token CSRF real.")
        return cliente, token.group(1)

    @staticmethod
    def pid_conexion(conexion):
        driver = conexion.connection.driver_connection
        if hasattr(driver, "get_backend_pid"):
            return driver.get_backend_pid()
        return driver.info.backend_pid

    def esperar_evento(self, senal, futuro):
        if not senal.wait(self.ESPERA):
            if futuro.done():
                futuro.result()
            self.fail("La petición no alcanzó el punto de coordinación.")

    def observar_bloqueo_o_final(self, bloqueado, bloqueador, futuro):
        """Permite que el código vulnerable termine y revele el duplicado."""
        limite = monotonic() + 3
        pausa = Event()
        with self.engine.connect() as conexion:
            while monotonic() < limite:
                bloqueadores = conexion.execute(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": bloqueado}
                ).scalar_one()
                if bloqueador in bloqueadores:
                    return True
                if futuro.done():
                    futuro.result()
                    return False
                pausa.wait(0.02)
        return False

    def peticiones_en_carrera(self, ruta, datos_primera, datos_segunda, consulta):
        """Dos cookies, tokens, conexiones y transacciones independientes."""
        cliente1, token1 = self.cliente_autenticado(ruta)
        cliente2, token2 = self.cliente_autenticado(ruta)
        retenida, liberar, segunda_lista = Event(), Event(), Event()
        procesos = {}

        def antes(conexion, cursor, statement, parameters, context, many):
            if current_thread().ident == procesos.get("segunda_ident"):
                procesos["segunda_pid"] = self.pid_conexion(conexion)
                segunda_lista.set()

        def despues(conexion, cursor, statement, parameters, context, many):
            if (current_thread().ident == procesos.get("primera_ident")
                    and not retenida.is_set() and consulta(statement)):
                procesos["primera_pid"] = self.pid_conexion(conexion)
                retenida.set()
                if not liberar.wait(self.ESPERA):
                    raise AssertionError("No se liberó la primera petición retenida.")

        def enviar(cliente, token, datos, nombre):
            procesos[nombre + "_ident"] = current_thread().ident
            return cliente.post(ruta, data={**datos, "csrf_token": token})

        event.listen(self.engine, "before_cursor_execute", antes)
        event.listen(self.engine, "after_cursor_execute", despues)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                primera = pool.submit(enviar, cliente1, token1, datos_primera, "primera")
                try:
                    self.esperar_evento(retenida, primera)
                    segunda = pool.submit(enviar, cliente2, token2, datos_segunda, "segunda")
                    self.esperar_evento(segunda_lista, segunda)
                    bloqueo = self.observar_bloqueo_o_final(
                        procesos["segunda_pid"], procesos["primera_pid"], segunda
                    )
                finally:
                    liberar.set()
                self.assertEqual(primera.result(timeout=self.ESPERA).status_code, 302)
                self.assertEqual(segunda.result(timeout=self.ESPERA).status_code, 302)
        finally:
            event.remove(self.engine, "before_cursor_execute", antes)
            event.remove(self.engine, "after_cursor_execute", despues)
        with cliente1.session_transaction() as sesion:
            mensajes1 = list(sesion.get("_flashes", []))
        with cliente2.session_transaction() as sesion:
            mensajes2 = list(sesion.get("_flashes", []))
        return bloqueo, mensajes1, mensajes2

    def verificar_mensajes(self, mensajes1, mensajes2, conflicto):
        self.assertTrue(any(tipo == "success" for tipo, _ in mensajes1), mensajes1)
        self.assertTrue(any(tipo == "danger" and conflicto in mensaje
                            for tipo, mensaje in mensajes2), mensajes2)

    def test_dos_aperturas_http_del_mismo_usuario_crean_un_solo_turno(self):
        datos = {"accion": "abrir", "fondo_inicial": "125.00"}
        bloqueo, mensajes1, mensajes2 = self.peticiones_en_carrera(
            "/caja", datos, datos,
            lambda sql: "FROM turno_caja" in sql and "FOR UPDATE" in sql,
        )
        self.assertEqual(self.escalar("""SELECT COUNT(*) FROM turno_caja
            WHERE id_usuario=:usuario AND estado='ABIERTO'""",
            usuario=self.ids["admin"]), 1)
        self.assertEqual(self.escalar("""SELECT fondo_inicial FROM turno_caja
            WHERE id_usuario=:usuario AND estado='ABIERTO'""",
            usuario=self.ids["admin"]), 125)
        self.assertTrue(bloqueo, "La segunda apertura debe esperar a la primera en PostgreSQL.")
        self.verificar_mensajes(mensajes1, mensajes2, "Ya tienes un turno abierto")

    def probar_documentos(self, modulo, editar=False):
        cliente = modulo == "clientes"
        tabla = "cliente" if cliente else "proveedor"
        pk = "id_cliente" if cliente else "id_proveedor"
        campo_nombre = "nombre_razon_social" if cliente else "razon_social"
        campo_documento = "nit_dpi" if cliente else "nit"
        datos1 = {campo_nombre: "Persona Primera", campo_documento: "8765432-k"}
        datos2 = {campo_nombre: "Persona Segunda", campo_documento: " 8765432-K "}
        if editar:
            with self.engine.begin() as conexion:
                for datos, nombre, documento in (
                    (datos1, "Persona Primera", "1111111-1"),
                    (datos2, "Persona Segunda", "2222222-2"),
                ):
                    identificador = conexion.execute(text(f"""INSERT INTO {tabla}
                        ({campo_nombre},{campo_documento}) VALUES (:nombre,:documento)
                        RETURNING {pk}"""),
                        {"nombre": nombre, "documento": documento}).scalar_one()
                    datos["id"] = str(identificador)
        bloqueo, mensajes1, mensajes2 = self.peticiones_en_carrera(
            "/gestion/" + modulo, datos1, datos2,
            lambda sql: f"FROM {tabla}" in sql and f"UPPER({campo_documento})" in sql,
        )
        self.assertEqual(self.escalar(f"""SELECT COUNT(*) FROM {tabla}
            WHERE UPPER({campo_documento})=:documento""", documento=self.DOCUMENTO), 1)
        self.assertEqual(self.escalar(f"SELECT COUNT(*) FROM {tabla}"), 3 if editar else 2)
        if editar:
            # La edición rechazada conserva su documento anterior.
            self.assertEqual(self.escalar(f"""SELECT {campo_documento} FROM {tabla}
                WHERE {pk}=:id""", id=int(datos2["id"])), "2222222-2")
        self.assertTrue(bloqueo, "Los documentos iguales deben serializarse en PostgreSQL.")
        self.verificar_mensajes(mensajes1, mensajes2, "Ya existe un registro con ese NIT o DPI")

    def test_altas_http_clientes_con_documento_igual_crean_un_solo_cliente(self):
        self.probar_documentos("clientes")

    def test_altas_http_proveedores_con_nit_igual_crean_un_solo_proveedor(self):
        self.probar_documentos("proveedores")

    def test_ediciones_http_clientes_no_pueden_converger_al_mismo_documento(self):
        self.probar_documentos("clientes", editar=True)

    def test_ediciones_http_proveedores_no_pueden_converger_al_mismo_nit(self):
        self.probar_documentos("proveedores", editar=True)
