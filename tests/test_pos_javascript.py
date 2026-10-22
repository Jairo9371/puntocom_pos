"""Ejecuta el JavaScript real del POS sin navegador ni conexiones de red."""

import json
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader


class ScriptsPOS(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.actual = None

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.actual = {'attrs': dict(attrs), 'codigo': ''}

    def handle_data(self, data):
        if self.actual is not None:
            self.actual['codigo'] += data

    def handle_endtag(self, tag):
        if tag == 'script' and self.actual is not None:
            self.scripts.append(self.actual)
            self.actual = None


@unittest.skipUnless(shutil.which('node'), 'Requiere Node.js para probar el POS')
class JavaScriptPOSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raiz = Path(__file__).resolve().parents[1]
        entorno = Environment(loader=ChoiceLoader([
            DictLoader({'base.html': '{% block content %}{% endblock %}{% block scripts %}{% endblock %}'}),
            FileSystemLoader(raiz / 'templates'),
        ]), autoescape=True)
        entorno.globals.update(
            puede=lambda permiso: True,
            current_user=SimpleNamespace(id='1'),
            csrf_token=lambda: 'csrf-de-prueba',
            url_for=lambda endpoint: '/' + endpoint,
        )
        cls.fixtures = {}
        for abierto in (True, False):
            html = entorno.get_template('pos.html').render(
                turno=SimpleNamespace(id_turno=7) if abierto else None,
                clientes=[
                    {'id_cliente': 1, 'nombre_razon_social': 'Consumidor Final'},
                    {'id_cliente': 2, 'nombre_razon_social': 'Cliente habitual'},
                ],
                products=[{
                    'id_producto': 1, 'codigo': 'P1', 'codigo_barras': '',
                    'nombre': 'Cuaderno', 'precio_venta': 10, 'existencia_actual': 8,
                }],
            )
            parser = ScriptsPOS()
            parser.feed(html)
            cls.fixtures['abierto' if abierto else 'cerrado'] = {
                'html': html,
                'script': next(s['codigo'] for s in parser.scripts if not s['attrs']),
                'productos': next(s['codigo'] for s in parser.scripts if s['attrs'].get('id') == 'pos-productos'),
            }
        cls.harness = raiz / 'tests' / 'pos_harness.js'

    def comprobar(self, caso):
        resultado = subprocess.run(
            [shutil.which('node'), str(self.harness), caso],
            input=json.dumps(self.fixtures), text=True, encoding='utf-8',
            capture_output=True, timeout=15, check=False,
        )
        self.assertEqual(resultado.returncode, 0, resultado.stdout + resultado.stderr)

    def test_respuesta_perdida_recarga_misma_venta(self):
        self.comprobar('respuesta_perdida')

    def test_segunda_venta_reinicia_cobro_y_usa_otra_clave(self):
        self.comprobar('segunda_venta')

    def test_reabrir_cobro_actualiza_importe_si_cambio_carrito(self):
        self.comprobar('carrito_modificado')

    def test_reabrir_mismo_carrito_conserva_importe_manual(self):
        self.comprobar('reabrir_mismo_carrito')

    def test_datos_guardados_antes_de_correccion_no_contaminan_nueva_venta(self):
        self.comprobar('datos_venta_anterior')

    def test_volver_desde_ticket_no_muestra_venta_anterior(self):
        self.comprobar('regreso_ticket')

    def test_producto_ausente_se_recupera_solo_en_carrito(self):
        self.comprobar('producto_ausente')

    def test_sesion_y_csrf_conservan_intento(self):
        self.comprobar('sesion_csrf')

    def test_rechazo_definitivo_permite_corregir_con_misma_clave(self):
        self.comprobar('rechazo_definitivo')

    def test_duplicar_pestana_no_clona_el_cobro(self):
        self.comprobar('pestana_duplicada')

    def test_reintento_recuperado_funciona_con_caja_cerrada(self):
        self.comprobar('turno_cerrado')


if __name__ == '__main__':
    unittest.main()
