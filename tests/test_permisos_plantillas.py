"""Renderizado por rol sin importar app ni conectar a PostgreSQL."""

import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from permisos import tiene_permiso


class PlantillasPorRolTests(unittest.TestCase):
    def renderizar(self, plantilla, rol, **contexto):
        entorno = Environment(
            loader=FileSystemLoader(Path(__file__).resolve().parents[1] / 'templates'),
            autoescape=select_autoescape(),
        )
        entorno.filters.update(q=lambda valor: f'Q {valor}', fecha=str)
        entorno.globals.update(
            puede=lambda permiso: tiene_permiso(permiso, rol),
            current_user=SimpleNamespace(
                is_authenticated=True, rol=rol, nombre='Ana', id='1',
            ),
            request=SimpleNamespace(path='', args={}),
            url_for=lambda endpoint, **datos: '/' + endpoint + (
                '/' + datos['modulo'] if 'modulo' in datos else ''
            ),
            csrf_token=lambda: 'csrf-prueba',
            get_flashed_messages=lambda **datos: [],
            total_alertas=0, alertas=[], clock_today='18/10/2026',
        )
        return entorno.get_template(plantilla).render(**contexto)

    @staticmethod
    def producto():
        return {
            'id_producto': 1, 'codigo': 'PC1', 'codigo_barras': '',
            'nombre': 'Cuaderno', 'categoria': 'Papel', 'marca': '',
            'costo': '73429.81', 'precio_venta': 10, 'existencia_actual': 8,
            'stock_minimo': 2, 'estado': True, 'puede_eliminar': True,
        }

    def test_productos_no_incluyen_costos_ni_acciones_para_cajero_y_bodeguero(self):
        for rol in ('Cajero', 'Bodeguero'):
            with self.subTest(rol=rol):
                html = self.renderizar('productos.html', rol, data=[self.producto()], categorias=[])
                self.assertIn('Cuaderno', html)
                self.assertIn('Q 10', html)
                self.assertNotIn('73429.81', html)
                self.assertNotIn('<th>Costo</th>', html)
                self.assertNotIn('/producto_form', html)
                self.assertNotIn('/producto_estado', html)
                self.assertNotIn('/producto_eliminar', html)

    def test_administrador_ve_costos_y_acciones_del_producto(self):
        html = self.renderizar('productos.html', 'Administrador', data=[self.producto()], categorias=[])
        self.assertIn('73429.81', html)
        self.assertIn('/producto_form', html)
        self.assertIn('/producto_estado', html)
        self.assertIn('/producto_eliminar', html)

    def test_menu_del_cajero_excluye_administracion_e_inventario(self):
        html = self.renderizar('base.html', 'Cajero')
        for endpoint in ('/pos', '/productos', '/gestion/clientes', '/ventas', '/caja', '/reportes'):
            self.assertIn(f'href="{endpoint}"', html)
        for endpoint in ('/inventario', '/gestion/categorias', '/gestion/proveedores', '/compras', '/usuarios', '/configuracion'):
            self.assertNotIn(f'href="{endpoint}"', html)
        self.assertNotIn('Alertas de inventario', html)

    def test_menu_del_bodeguero_excluye_ventas_y_administracion(self):
        html = self.renderizar('base.html', 'Bodeguero')
        for endpoint in ('/productos', '/inventario', '/gestion/proveedores'):
            self.assertIn(f'href="{endpoint}"', html)
        for endpoint in ('/pos', '/gestion/clientes', '/gestion/categorias', '/ventas', '/compras', '/usuarios', '/configuracion', '/reportes', '/caja'):
            self.assertNotIn(f'href="{endpoint}"', html)

    def test_descuento_editable_solo_se_renderiza_para_administrador(self):
        for rol in ('Administrador', 'Cajero'):
            with self.subTest(rol=rol):
                html = self.renderizar('pos.html', rol, turno=None, clientes=[], products=[])
                control = "'descuento', this.value)"
                if rol == 'Administrador':
                    self.assertIn(control, html)
                    self.assertIn('<th>Desc. Q</th>', html)
                else:
                    self.assertNotIn(control, html)
                    self.assertNotIn('<th>Desc. Q</th>', html)

    def test_cajero_puede_registrar_clientes_sin_controles_de_edicion(self):
        config = {
            'titulo': 'Clientes', 'campos': ['nombre_razon_social'], 'pk': 'id_cliente',
            'permiso_crear': 'clientes.crear', 'permiso_editar': 'clientes.gestionar',
        }
        html = self.renderizar('gestion.html', 'Cajero', config=config, modulo='clientes', data=[{
            'id_cliente': 2, 'nombre_razon_social': 'Cliente Prueba', 'estado': True,
        }])
        self.assertIn('+ Nuevo registro', html)
        self.assertIn('id="gestion-form"', html)
        self.assertNotIn('>Editar</button>', html)
        self.assertNotIn('/gestion_estado', html)
        self.assertNotIn('/gestion_eliminar', html)

    def test_proveedores_del_bodeguero_son_solo_consulta(self):
        config = {
            'titulo': 'Proveedores', 'campos': ['razon_social'], 'pk': 'id_proveedor',
            'permiso_crear': 'proveedores.gestionar', 'permiso_editar': 'proveedores.gestionar',
        }
        html = self.renderizar('gestion.html', 'Bodeguero', config=config, modulo='proveedores', data=[{
            'id_proveedor': 2, 'razon_social': 'Proveedor Prueba', 'estado': True,
        }])
        self.assertIn('Proveedor Prueba', html)
        self.assertNotIn('+ Nuevo registro', html)
        self.assertNotIn('id="gestion-form"', html)
        self.assertNotIn('/gestion_estado', html)
        self.assertNotIn('/gestion_eliminar', html)


if __name__ == '__main__':
    unittest.main()
