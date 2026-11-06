"""Configuración con entornos artificiales, sin leer .env ni conectar PostgreSQL."""

import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from flask import Flask
from sqlalchemy.pool import NullPool

from configuracion import TZ, cargar_configuracion


class ConfiguracionTests(unittest.TestCase):
    def produccion(self, **cambios):
        return {"APP_ENV": "production", "SECRET_KEY": "0123456789abcdef" * 4,
                "TRUSTED_HOSTS": "pos.example.invalid,localhost,127.0.0.1", **cambios}

    def vercel(self, **cambios):
        return {"VERCEL": "1", "SECRET_KEY": "0123456789abcdef" * 4,
                "VERCEL_URL": "puntocom-deploy-equipo.vercel.app",
                "VERCEL_BRANCH_URL": "puntocom-git-pruebas-equipo.vercel.app",
                "VERCEL_PROJECT_PRODUCTION_URL": "puntocom.vercel.app", **cambios}

    def test_defaults_desarrollo_sin_debug_y_base_correcta(self):
        configuracion = cargar_configuracion({})
        self.assertEqual(configuracion["APP_ENV"], "development")
        self.assertIs(configuracion["DEBUG"], False)
        self.assertEqual(configuracion["SQLALCHEMY_DATABASE_URI"].database, "puntocom_pos")
        self.assertEqual(configuracion["SQLALCHEMY_DATABASE_URI"].port, 5433)
        self.assertTrue(configuracion["SECRET_KEY"].startswith("dev-only-"))
        self.assertIs(configuracion["SESSION_COOKIE_SECURE"], False)

    def test_entorno_explicito_no_usa_variables_del_proceso(self):
        with patch.dict(os.environ, {"APP_ENV": "production", "APP_DEBUG": "true", "DB_PORT": "incorrecto"}):
            configuracion = cargar_configuracion({})
        self.assertEqual(configuracion["APP_ENV"], "development")
        self.assertIs(configuracion["DEBUG"], False)
        self.assertEqual(configuracion["SQLALCHEMY_DATABASE_URI"].port, 5433)

    def test_vercel_usa_produccion_y_exige_clave_segura_y_hosts(self):
        configuracion = cargar_configuracion(self.vercel())
        self.assertEqual(configuracion["APP_ENV"], "production")
        self.assertIs(configuracion["DEBUG"], False)
        self.assertIs(configuracion["SESSION_COOKIE_SECURE"], True)
        self.assertIs(configuracion["REMEMBER_COOKIE_SECURE"], True)
        for clave in (None, "", "   ", "a" * 64):
            entorno = self.vercel(SECRET_KEY=clave)
            if clave is None:
                del entorno["SECRET_KEY"]
            with self.subTest(clave=clave), self.assertRaisesRegex(ValueError, "SECRET_KEY"):
                cargar_configuracion(entorno)
        with self.assertRaisesRegex(ValueError, "TRUSTED_HOSTS"):
            cargar_configuracion(self.vercel(VERCEL_URL="", VERCEL_BRANCH_URL="",
                                            VERCEL_PROJECT_PRODUCTION_URL=""))

    def test_vercel_combina_hosts_explicitos_y_automaticos_sin_repetirlos(self):
        entorno = self.vercel(
            TRUSTED_HOSTS=" pos.example.invalid, puntocom-deploy-equipo.vercel.app ",
            VERCEL_PROJECT_PRODUCTION_URL="pos.example.invalid",
        )
        self.assertEqual(cargar_configuracion(entorno)["TRUSTED_HOSTS"], [
            "pos.example.invalid", "puntocom-deploy-equipo.vercel.app",
            "puntocom-git-pruebas-equipo.vercel.app",
        ])

    def test_flask_acepta_dominios_del_proyecto_y_rechaza_otro_proyecto_vercel(self):
        aplicacion = Flask(__name__, static_folder=None)
        aplicacion.config.update(cargar_configuracion(self.vercel(TRUSTED_HOSTS="pos.example.invalid")))
        aplicacion.add_url_rule("/", view_func=lambda: "ok")
        cliente = aplicacion.test_client()
        for host in aplicacion.config["TRUSTED_HOSTS"]:
            with self.subTest(host=host):
                self.assertEqual(cliente.get("/", base_url=f"https://{host}").status_code, 200)
        for host in ("otro-proyecto.vercel.app", "otro.puntocom.vercel.app"):
            with self.subTest(host=host):
                self.assertEqual(cliente.get("/", base_url=f"https://{host}").status_code, 400)

    def test_variables_vercel_no_afectan_ejecucion_fuera_de_la_plataforma(self):
        for indicador in (None, "0", "true"):
            entorno = {"VERCEL_URL": "https://dominio-invalido.vercel.app",
                       "VERCEL_BRANCH_URL": "rama.vercel.app",
                       "VERCEL_PROJECT_PRODUCTION_URL": "produccion.vercel.app"}
            if indicador is not None:
                entorno["VERCEL"] = indicador
            with self.subTest(indicador=indicador):
                configuracion = cargar_configuracion(entorno)
                self.assertEqual(configuracion["APP_ENV"], "development")
                self.assertIsNone(configuracion["TRUSTED_HOSTS"])
                self.assertIs(configuracion["SESSION_COOKIE_SECURE"], False)
                self.assertNotIn("poolclass", configuracion["SQLALCHEMY_ENGINE_OPTIONS"])

    def test_vercel_libera_conexiones_sin_cambiar_zona_aislamiento_o_ssl(self):
        opciones = cargar_configuracion(self.vercel(DB_SSLMODE="verify-full"))["SQLALCHEMY_ENGINE_OPTIONS"]
        self.assertIs(opciones["poolclass"], NullPool)
        self.assertIs(opciones["pool_pre_ping"], True)
        self.assertEqual(opciones["isolation_level"], "READ COMMITTED")
        self.assertEqual(opciones["connect_args"]["options"], "-c timezone=America/Guatemala")
        self.assertEqual(opciones["connect_args"]["sslmode"], "verify-full")
        self.assertNotIn("poolclass", cargar_configuracion(self.produccion())["SQLALCHEMY_ENGINE_OPTIONS"])

    def test_hosts_automaticos_malformados_se_rechazan_sin_revelar_valores(self):
        for variable in ("VERCEL_URL", "VERCEL_BRANCH_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
            for host in ("https://privado.vercel.app", "*.vercel.app", "privado.vercel.app:443",
                         "privado.vercel.app/ruta", "privado..vercel.app"):
                with self.subTest(variable=variable, host=host):
                    salida = StringIO()
                    errores = StringIO()
                    with redirect_stdout(salida), redirect_stderr(errores):
                        with self.assertRaisesRegex(ValueError, "TRUSTED_HOSTS") as captura:
                            cargar_configuracion(self.vercel(**{variable: host}))
                    self.assertNotIn(host, str(captura.exception))
                    self.assertEqual(salida.getvalue(), "")
                    self.assertEqual(errores.getvalue(), "")

    def test_produccion_exige_clave_explicita_no_vacia(self):
        for clave in (None, "", "   "):
            entorno = {"APP_ENV": "production"}
            if clave is not None:
                entorno["SECRET_KEY"] = clave
            with self.subTest(clave=clave), self.assertRaisesRegex(ValueError, "SECRET_KEY"):
                cargar_configuracion(entorno)

    def test_produccion_usa_clave_y_cookies_secure_sin_debug(self):
        configuracion = cargar_configuracion(self.produccion())
        self.assertEqual(configuracion["SECRET_KEY"], self.produccion()["SECRET_KEY"])
        self.assertIs(configuracion["DEBUG"], False)
        self.assertIs(configuracion["SESSION_COOKIE_SECURE"], True)
        self.assertIs(configuracion["REMEMBER_COOKIE_SECURE"], True)

    def test_produccion_rechaza_claves_debiles_cookies_http_y_hosts_vacios(self):
        for cambios in ({"SECRET_KEY": "a" * 64}, {"SECRET_KEY": "dev-only-" + "0123456789abcdef" * 4},
                        {"SECRET_KEY": "muy-corta"}, {"SESSION_COOKIE_SECURE": "false"},
                        {"TRUSTED_HOSTS": ""}):
            with self.subTest(cambios=cambios), self.assertRaises(ValueError):
                cargar_configuracion(self.produccion(**cambios))

    def test_hosts_validos_y_rechazo_de_urls_y_comodines(self):
        self.assertEqual(cargar_configuracion(self.produccion())["TRUSTED_HOSTS"],
                         ["pos.example.invalid", "localhost", "127.0.0.1"])
        for hosts in ("https://pos.invalid", "*", "pos.invalid:443", "pos.invalid/path", "dos hosts", "a..invalid"):
            with self.subTest(hosts=hosts), self.assertRaisesRegex(ValueError, "TRUSTED_HOSTS"):
                cargar_configuracion({"TRUSTED_HOSTS": hosts})

    def test_ssl_y_timeout_postgresql(self):
        opciones = cargar_configuracion({"DB_SSLMODE": "verify-full", "DB_SSLROOTCERT": "/certs/ca.pem",
                                         "DB_CONNECT_TIMEOUT": "9"})["SQLALCHEMY_ENGINE_OPTIONS"]["connect_args"]
        self.assertEqual(opciones["sslmode"], "verify-full")
        self.assertEqual(opciones["sslrootcert"], "/certs/ca.pem")
        self.assertEqual(opciones["connect_timeout"], 9)
        for cambios in ({"DB_SSLMODE": "inventado"}, {"DB_CONNECT_TIMEOUT": "0"},
                        {"DB_CONNECT_TIMEOUT": "121"}, {"DB_CONNECT_TIMEOUT": "texto"}):
            with self.subTest(cambios=cambios), self.assertRaises(ValueError):
                cargar_configuracion(cambios)

    def test_produccion_rechaza_debug_habilitado(self):
        for valor in ("true", "1", "YES", " on "):
            with self.subTest(valor=valor), self.assertRaisesRegex(ValueError, "APP_DEBUG"):
                cargar_configuracion({"APP_ENV": "production", "SECRET_KEY": "clave-ficticia", "APP_DEBUG": valor})

    def test_cadenas_false_no_habilitan_debug_ni_cookies_secure(self):
        for valor in ("false", "0", "NO", " off "):
            with self.subTest(valor=valor):
                configuracion = cargar_configuracion({"APP_DEBUG": valor, "SESSION_COOKIE_SECURE": valor})
                self.assertIs(configuracion["DEBUG"], False)
                self.assertIs(configuracion["SESSION_COOKIE_SECURE"], False)
                self.assertIs(configuracion["REMEMBER_COOKIE_SECURE"], False)

    def test_debug_se_habilita_solo_con_valor_booleano_reconocido(self):
        configuracion = cargar_configuracion({"APP_ENV": "testing", "APP_DEBUG": "true", "SESSION_COOKIE_SECURE": "true"})
        self.assertIs(configuracion["DEBUG"], True)
        self.assertIs(configuracion["SESSION_COOKIE_SECURE"], True)
        for variable in ("APP_DEBUG", "SESSION_COOKIE_SECURE"):
            with self.subTest(variable=variable), self.assertRaisesRegex(ValueError, variable):
                cargar_configuracion({variable: "quizás"})

    def test_puertos_invalidos_se_rechazan_y_limites_son_validos(self):
        for puerto in ("", "5433.5", "texto", "0", "-1", "65536", None):
            with self.subTest(puerto=puerto), self.assertRaisesRegex(ValueError, "DB_PORT"):
                cargar_configuracion({"DB_PORT": puerto})
        for puerto in ("1", "65535"):
            with self.subTest(puerto=puerto):
                configuracion = cargar_configuracion({"DB_PORT": puerto})
                self.assertEqual(configuracion["SQLALCHEMY_DATABASE_URI"].port, int(puerto))

    def test_datos_de_conexion_preservan_caracteres_especiales(self):
        entorno = {
            "DB_USER": "usuario@prueba", "DB_PASSWORD": "password-ficticio:@/#!",
            "DB_HOST": "db.example.invalid", "DB_PORT": "5432", "DB_NAME": "puntocom_pruebas",
        }
        uri = cargar_configuracion(entorno)["SQLALCHEMY_DATABASE_URI"]
        self.assertEqual(uri.drivername, "postgresql+psycopg2")
        self.assertEqual(uri.username, entorno["DB_USER"])
        self.assertEqual(uri.password, entorno["DB_PASSWORD"])
        self.assertEqual(uri.host, entorno["DB_HOST"])
        self.assertEqual(uri.database, entorno["DB_NAME"])
        self.assertEqual(uri.port, 5432)
        self.assertNotIn(entorno["DB_PASSWORD"], str(uri))

    def test_postgresql_y_aplicacion_comparten_zona_del_negocio(self):
        configuracion = cargar_configuracion({})
        self.assertEqual(TZ.key, "America/Guatemala")
        self.assertEqual(
            configuracion["SQLALCHEMY_ENGINE_OPTIONS"]["connect_args"]["options"],
            "-c timezone=America/Guatemala",
        )
        self.assertIs(configuracion["SQLALCHEMY_ENGINE_OPTIONS"]["pool_pre_ping"], True)
        self.assertEqual(configuracion["SQLALCHEMY_ENGINE_OPTIONS"]["isolation_level"], "READ COMMITTED")

    def test_cookies_httponly_y_samesite(self):
        configuracion = cargar_configuracion({})
        for prefijo in ("SESSION", "REMEMBER"):
            with self.subTest(prefijo=prefijo):
                self.assertIs(configuracion[f"{prefijo}_COOKIE_HTTPONLY"], True)
                self.assertEqual(configuracion[f"{prefijo}_COOKIE_SAMESITE"], "Lax")

    def test_configuracion_no_imprime_valores_del_entorno(self):
        salida = StringIO()
        errores = StringIO()
        entorno = {"SECRET_KEY": "clave-ficticia-no-imprimir", "DB_PASSWORD": "password-ficticio-no-imprimir"}
        with redirect_stdout(salida), redirect_stderr(errores):
            cargar_configuracion(entorno)
        self.assertEqual(salida.getvalue(), "")
        self.assertEqual(errores.getvalue(), "")

    def test_errores_identifican_variable_sin_repetir_su_valor(self):
        valor = "valor-ficticio-confidencial"
        for variable in ("APP_ENV", "APP_DEBUG", "DB_PORT", "SESSION_COOKIE_SECURE"):
            with self.subTest(variable=variable):
                salida = StringIO()
                errores = StringIO()
                with redirect_stdout(salida), redirect_stderr(errores):
                    with self.assertRaises(ValueError) as captura:
                        cargar_configuracion({variable: valor})
                self.assertIn(variable, str(captura.exception))
                self.assertNotIn(valor, str(captura.exception))
                self.assertEqual(salida.getvalue(), "")
                self.assertEqual(errores.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
