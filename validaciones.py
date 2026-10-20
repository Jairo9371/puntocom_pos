"""Reglas de entrada compartidas por ventas y formularios administrativos.

Las funciones no dependen de Flask ni de la base de datos: reciben valores,
devuelven valores normalizados y comunican los errores mediante ValueError.
"""

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


RE_SOLO_LETRAS = re.compile(
    r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?: [A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)*"
)
RE_TELEFONO_GT = re.compile(r"[0-9]{8}")
RE_DPI = re.compile(r"[0-9]{13}")
RE_NIT = re.compile(r"[0-9]{1,12}-?[0-9K]")
RE_CORREO = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"
)
LIMITES_GESTION = {
    "categorias": {"nombre": 100},
    "clientes": {
        "nombre_razon_social": 150, "nit_dpi": 30,
        "telefono": 20, "correo": 150,
    },
    "proveedores": {
        "razon_social": 150, "nit": 30, "contacto": 100,
        "telefono": 20, "correo": 150,
    },
}


def _texto(valor):
    if valor is None:
        return ""
    if not isinstance(valor, str):
        raise ValueError("Ingrese un texto válido.")
    return valor.strip()


def _decimal_finito(valor):
    # bool es una subclase de int, pero no representa un importe o una cantidad.
    if isinstance(valor, bool):
        raise ValueError("Ingrese un número válido.")
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Ingrese un número válido.") from error
    if not numero.is_finite():
        raise ValueError("Ingrese un número finito válido.")
    return numero


def money(valor):
    """Redondea a centavos; admite importes negativos para saldos y diferencias."""
    if valor is None or valor == "":
        valor = 0
    numero = _decimal_finito(valor)
    try:
        return numero.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as error:
        raise ValueError("El importe excede la precisión admitida.") from error


def entero(valor, minimo=0, etiqueta="cantidad"):
    """Valida enteros sin truncar las cantidades recibidas como números JSON."""
    mensaje = f"Cantidad inválida para {etiqueta}."
    try:
        if isinstance(valor, str):
            # Conservar la sintaxis de los campos enteros del formulario.
            if not re.fullmatch(r"[+-]?[0-9]+", valor.strip()):
                raise ValueError(mensaje)
            numero = int(valor)
        else:
            decimal = _decimal_finito(valor)
            if decimal != decimal.to_integral_value():
                raise ValueError(mensaje)
            numero = int(decimal)
        if numero < minimo:
            raise ValueError(mensaje)
        return numero
    except (InvalidOperation, TypeError, ValueError, OverflowError) as error:
        raise ValueError(mensaje) from error


def solo_letras(valor, etiqueta, obligatorio=True):
    valor = _texto(valor)
    if not valor and not obligatorio:
        return valor
    if not RE_SOLO_LETRAS.fullmatch(valor):
        raise ValueError(f"{etiqueta}: solo se permiten letras y espacios.")
    return valor


def validar_nombre(nombre):
    return solo_letras(nombre, "Nombre")


def validar_telefono(telefono):
    telefono = _texto(telefono)
    if telefono and not RE_TELEFONO_GT.fullmatch(telefono):
        raise ValueError("El teléfono debe contener exactamente 8 números.")
    return telefono


def validar_nit(nit):
    nit = _texto(nit).upper().replace(" ", "")
    if nit and not RE_NIT.fullmatch(nit):
        raise ValueError("El formato del NIT no es válido.")
    return nit


def validar_nit_dpi(valor):
    valor = _texto(valor).upper().replace(" ", "")
    if not valor or RE_DPI.fullmatch(valor) or RE_NIT.fullmatch(valor):
        return valor
    raise ValueError("Ingrese un NIT o DPI con formato válido.")


def validar_correo(correo):
    correo = _texto(correo).lower()
    if correo and not RE_CORREO.fullmatch(correo):
        raise ValueError("Introduce un correo electrónico válido.")
    return correo


def validar_precio(valor):
    """Valida un precio sin redondearlo ni admitir decimales adicionales."""
    precio = _decimal_finito(valor)
    if precio < 0:
        raise ValueError("El precio no puede ser negativo o inválido.")
    if precio.as_tuple().exponent < -2:
        raise ValueError("El precio admite como máximo dos decimales.")
    return precio


def validar_datos_gestion(modulo, data):
    """Aplica las reglas de cada entidad antes de guardar, sin modificar data."""
    if modulo not in LIMITES_GESTION:
        raise ValueError("Módulo de gestión inválido.")
    datos = {campo: _texto(valor) or None for campo, valor in data.items()}
    for campo, limite in LIMITES_GESTION[modulo].items():
        valor = datos.get(campo)
        if valor is not None and len(valor) > limite:
            raise ValueError(
                f"El campo {campo.replace('_', ' ')} supera {limite} caracteres."
            )

    if modulo == "categorias":
        datos["nombre"] = solo_letras(datos.get("nombre"), "Nombre de categoría")
        datos["descripcion"] = solo_letras(
            datos.get("descripcion"), "Descripción de categoría", obligatorio=False
        )
        return datos

    es_cliente = modulo == "clientes"
    campo_nombre = "nombre_razon_social" if es_cliente else "razon_social"
    nombre = datos.get(campo_nombre)
    if not nombre:
        raise ValueError("El nombre o razón social es obligatorio.")
    if es_cliente:
        datos[campo_nombre] = solo_letras(nombre, "Nombre de cliente")
    else:
        # Las razones sociales de proveedores sí admiten números y signos.
        datos["contacto"] = solo_letras(
            datos.get("contacto"), "Nombre del contacto", obligatorio=False
        )

    datos["telefono"] = validar_telefono(datos.get("telefono")) or None
    datos["correo"] = validar_correo(datos.get("correo")) or None
    campo_nit = "nit_dpi" if es_cliente else "nit"
    nit = _texto(datos.get(campo_nit)).upper().replace(" ", "")
    if es_cliente and nit == "CF":
        if nombre.casefold() != "consumidor final":
            raise ValueError("El identificador CF está reservado para Consumidor Final.")
    elif nit:
        nit = validar_nit_dpi(nit) if es_cliente else validar_nit(nit)
    datos[campo_nit] = nit or None
    return datos
