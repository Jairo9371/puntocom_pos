"""El comprobante explica el ahorro con importes históricos, sin app ni BD."""

import unittest
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from permisos import tiene_permiso
from validaciones import money


class TextoTicket(HTMLParser):
    def __init__(self):
        super().__init__()
        self.partes = []

    def handle_data(self, data):
        self.partes.append(data)


class TicketDescuentosTests(unittest.TestCase):
    def renderizar(self, detalle, total, rol='Administrador'):
        entorno = Environment(loader=ChoiceLoader([
            DictLoader({'base.html': '{% block content %}{% endblock %}'}),
            FileSystemLoader(Path(__file__).resolve().parents[1] / 'templates'),
        ]), autoescape=True)
        entorno.filters.update(
            q=lambda valor: 'Q {:,.2f}'.format(money(valor)),
            fecha=str,
        )
        entorno.globals.update(
            puede=lambda permiso: tiene_permiso(permiso, rol),
            url_for=lambda endpoint: '/' + endpoint,
        )
        html = entorno.get_template('ticket.html').render(
            venta={
                'numero_ticket': 'V-000001', 'fecha_hora': '21/10/2026',
                'cajero': 'Ana', 'nombre_razon_social': 'Consumidor Final',
                'nit_dpi': 'CF', 'estado': 'COMPLETADA',
                'estado_pago': 'CONFIRMADO', 'metodo_pago': 'EFECTIVO',
                'total': Decimal(total), 'motivo_pago': None,
                'resultado_cobro': None,
            },
            detalle=detalle,
        )
        parser = TextoTicket()
        parser.feed(html)
        return ' '.join(' '.join(parser.partes).split())

    @staticmethod
    def articulo(nombre, precio, cantidad, descuento='0.00'):
        return {
            'nombre': nombre, 'precio_unitario': Decimal(precio),
            'cantidad': cantidad, 'descuento': Decimal(descuento),
        }

    def test_varios_productos_desglosan_descuentos_por_renglon_y_total(self):
        detalle = [
            self.articulo('Cuaderno', '12.50', 3, '4.00'),
            self.articulo('Lápiz', '2.25', 4, '1.00'),
            self.articulo('Marcador', '6.75', 2),
        ]
        # Un cambio posterior en catálogo no modifica los precios históricos.
        detalle[0]['precio_venta'] = Decimal('99.99')
        texto = self.renderizar(detalle, '55.00')

        self.assertIn('Cuaderno Q 12.50 c/u Descuento: -Q 4.00 3 Q 33.50', texto)
        self.assertIn('Lápiz Q 2.25 c/u Descuento: -Q 1.00 4 Q 8.00', texto)
        self.assertIn('Marcador Q 6.75 c/u 2 Q 13.50', texto)
        self.assertIn('Subtotal Q 60.00 Descuento total -Q 5.00 TOTAL Q 55.00', texto)
        self.assertEqual(texto.count('Descuento:'), 2)
        self.assertNotIn('TOTAL Q 50.00', texto)
        self.assertNotIn('99.99', texto)

    def test_descuento_completo_muestra_ahorro_y_total_cero(self):
        texto = self.renderizar([self.articulo('Premio', '8.75', 2, '17.50')], '0.00')

        self.assertIn('Premio Q 8.75 c/u Descuento: -Q 17.50 2 Q 0.00', texto)
        self.assertIn('Subtotal Q 17.50 Descuento total -Q 17.50 TOTAL Q 0.00', texto)

    def test_sin_descuentos_conserva_ticket_simple(self):
        texto = self.renderizar([self.articulo('Carpeta', '4.25', 3)], '12.75')

        self.assertIn('Carpeta Q 4.25 c/u 3 Q 12.75', texto)
        self.assertIn('TOTAL Q 12.75', texto)
        self.assertNotIn('Descuento', texto)
        self.assertNotIn('Subtotal', texto)
        self.assertNotIn('-Q', texto)

    def test_cajero_ve_mismo_descuento_que_admin_sin_permiso_para_aplicarlo(self):
        detalle = [self.articulo('Cuaderno', '15.00', 2, '3.50')]
        self.assertFalse(tiene_permiso('descuentos.modificar', 'Cajero'))
        self.assertTrue(tiene_permiso('descuentos.modificar', 'Administrador'))

        ticket_cajero = self.renderizar(detalle, '26.50', rol='Cajero')
        ticket_admin = self.renderizar(detalle, '26.50', rol='Administrador')

        self.assertEqual(ticket_cajero, ticket_admin)
        self.assertIn('Descuento total -Q 3.50 TOTAL Q 26.50', ticket_cajero)

if __name__ == '__main__':
    unittest.main()
