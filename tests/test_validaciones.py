"""Regresiones de reglas comerciales y entradas numéricas del servidor."""

import unittest
from decimal import Decimal

from validaciones import (
    entero,
    money,
    solo_letras,
    validar_correo,
    validar_datos_gestion,
    validar_nit,
    validar_nit_dpi,
    validar_nombre,
    validar_precio,
    validar_telefono,
)


class ImportesTest(unittest.TestCase):
    def test_redondea_a_centavos_con_half_up(self):
        self.assertEqual(money("10.005"), Decimal("10.01"))
        self.assertEqual(money(Decimal("-0.005")), Decimal("-0.01"))
        self.assertEqual(money(None), Decimal("0.00"))
        self.assertEqual(money(""), Decimal("0.00"))

    def test_no_impone_limite_comercial_a_saldos(self):
        self.assertEqual(money("10000000000"), Decimal("10000000000.00"))
        self.assertEqual(money("-12.50"), Decimal("-12.50"))

    def test_rechaza_no_finitos_y_booleanos(self):
        for valor in ("NaN", "sNaN", "Infinity", "-Infinity", float("nan"),
                      float("inf"), Decimal("NaN"), True, False):
            with self.subTest(valor=repr(valor)):
                with self.assertRaises(ValueError):
                    money(valor)
                with self.assertRaises(ValueError):
                    validar_precio(valor)

    def test_precio_no_redondea_entradas_con_mas_de_dos_decimales(self):
        self.assertEqual(validar_precio("12.50"), Decimal("12.50"))
        self.assertEqual(validar_precio("0"), Decimal("0"))
        for valor in ("-0.01", "1.005", "1.000", None, "", "texto"):
            with self.subTest(valor=valor):
                with self.assertRaises(ValueError):
                    validar_precio(valor)

    def test_precision_excesiva_produce_un_error_de_validacion(self):
        with self.assertRaises(ValueError):
            money("1e1000")


class CantidadesTest(unittest.TestCase):
    def test_admite_enteros_del_formulario_y_json(self):
        for valor in (" 2 ", "+2", 2, 2.0, Decimal("2.0")):
            with self.subTest(valor=valor):
                self.assertEqual(entero(valor, minimo=1), 2)
        self.assertEqual(entero("0"), 0)

    def test_no_trunca_fracciones_ni_admite_no_finitos(self):
        for valor in (1.9, Decimal("1.1"), "1.0", "1.9", "1e2", "1_000",
                      True, False, None, [], {}, float("inf"), float("nan")):
            with self.subTest(valor=repr(valor)):
                with self.assertRaisesRegex(ValueError, "Cantidad inválida para producto"):
                    entero(valor, minimo=1, etiqueta="producto")

    def test_respeta_minimo(self):
        with self.assertRaises(ValueError):
            entero(0, minimo=1)
        with self.assertRaises(ValueError):
            entero(-1)


class GestionTest(unittest.TestCase):
    def test_normaliza_cliente_sin_modificar_entrada(self):
        entrada = {
            "nombre_razon_social": "  José Pérez  ", "nit_dpi": " 123456-k ",
            "telefono": " 12345678 ", "correo": " Persona@Ejemplo.COM ",
            "direccion_fiscal": " Centro ",
        }
        salida = validar_datos_gestion("clientes", entrada)
        self.assertEqual(salida["nombre_razon_social"], "José Pérez")
        self.assertEqual(salida["nit_dpi"], "123456-K")
        self.assertEqual(salida["telefono"], "12345678")
        self.assertEqual(salida["correo"], "persona@ejemplo.com")
        self.assertEqual(salida["direccion_fiscal"], "Centro")
        self.assertEqual(entrada["nit_dpi"], " 123456-k ")

    def test_cf_solo_pertenece_a_consumidor_final(self):
        salida = validar_datos_gestion("clientes", {
            "nombre_razon_social": " Consumidor Final ", "nit_dpi": "cf",
        })
        self.assertEqual(salida["nit_dpi"], "CF")
        with self.assertRaisesRegex(ValueError, "reservado"):
            validar_datos_gestion("clientes", {
                "nombre_razon_social": "María López", "nit_dpi": "CF",
            })

    def test_opcionales_vacios_no_invalidan_registro(self):
        salida = validar_datos_gestion("clientes", {"nombre_razon_social": "Ana"})
        for campo in ("nit_dpi", "telefono", "correo"):
            self.assertIsNone(salida[campo])

    def test_proveedor_admite_razon_social_con_numeros_y_signos(self):
        salida = validar_datos_gestion("proveedores", {
            "razon_social": " Papelería 24, S.A. ", "contacto": " José Pérez ",
        })
        self.assertEqual(salida["razon_social"], "Papelería 24, S.A.")
        self.assertEqual(salida["contacto"], "José Pérez")
        with self.assertRaises(ValueError):
            validar_datos_gestion("proveedores", {
                "razon_social": "Papelería", "contacto": "Persona 2",
            })

    def test_categoria_y_cliente_conservan_regla_de_solo_letras(self):
        self.assertEqual(validar_datos_gestion("categorias", {
            "nombre": " Útiles escolares ", "descripcion": " Artículos básicos ",
        })["descripcion"], "Artículos básicos")
        for modulo, campos in (
            ("categorias", {"nombre": "Útiles 2"}),
            ("categorias", {"nombre": "Útiles", "descripcion": "Serie 2"}),
            ("clientes", {"nombre_razon_social": "María-López"}),
            ("clientes", {"nombre_razon_social": "María  López"}),
        ):
            with self.subTest(modulo=modulo, campos=campos):
                with self.assertRaises(ValueError):
                    validar_datos_gestion(modulo, campos)

    def test_rechaza_nombre_ausente_y_limites_del_esquema(self):
        for modulo, campos in (
            ("categorias", {}),
            ("clientes", {"nombre_razon_social": " "}),
            ("proveedores", {}),
            ("categorias", {"nombre": "A" * 101}),
            ("clientes", {"nombre_razon_social": "A" * 151}),
            ("proveedores", {"razon_social": "Empresa", "contacto": "A" * 101}),
        ):
            with self.subTest(modulo=modulo, campos=campos):
                with self.assertRaises(ValueError):
                    validar_datos_gestion(modulo, campos)

    def test_rechaza_contactos_invalidos_en_gestion(self):
        for campo, valor in (("correo", "ana@-dominio.com"),
                             ("telefono", "1234567"), ("nit_dpi", "CFX")):
            with self.subTest(campo=campo):
                with self.assertRaises(ValueError):
                    validar_datos_gestion("clientes", {
                        "nombre_razon_social": "Ana", campo: valor,
                    })

    def test_modulo_desconocido_produce_error_de_validacion(self):
        with self.assertRaises(ValueError):
            validar_datos_gestion("otro", {})


class ValidadoresCompatiblesTest(unittest.TestCase):
    def test_nombre_y_campos_opcionales(self):
        self.assertEqual(validar_nombre("  María José Ñúñez  "), "María José Ñúñez")
        self.assertEqual(solo_letras(None, "Marca", obligatorio=False), "")
        self.assertEqual(validar_telefono(None), "")
        with self.assertRaises(ValueError):
            validar_nombre("   ")

    def test_nit_y_dpi_conservan_sus_formatos(self):
        self.assertEqual(validar_nit(" 123456-k "), "123456-K")
        self.assertEqual(validar_nit_dpi("1234567890123"), "1234567890123")
        self.assertEqual(validar_nit(None), "")
        with self.assertRaises(ValueError):
            validar_nit("12345678901234")

    def test_correo_rechaza_guiones_en_extremos_del_dominio(self):
        self.assertEqual(validar_correo(" Ana@Ejemplo.COM "), "ana@ejemplo.com")
        for correo in ("ana@-ejemplo.com", "ana@ejemplo-.com", "ana@ejemplo", "ana@@ejemplo.com"):
            with self.subTest(correo=correo):
                with self.assertRaises(ValueError):
                    validar_correo(correo)


if __name__ == "__main__":
    unittest.main()
