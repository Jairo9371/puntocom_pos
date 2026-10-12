
import re
from decimal import Decimal, InvalidOperation


def validar_nombre(nombre):
    nombre = nombre.strip()

    if not re.fullmatch(
        r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ ]+",
        nombre
    ):
        raise ValueError(
            "El nombre solo puede contener letras."
        )

    return nombre


def validar_telefono(telefono):
    telefono = telefono.strip()

    if telefono and not re.fullmatch(
        r"[0-9]{8}", telefono
    ):
        raise ValueError(
            "El teléfono debe tener 8 números."
        )

    return telefono


def validar_nit(nit):
    nit = nit.strip().upper()

    if nit and not re.fullmatch(
        r"[0-9]+-?[0-9K]",
        nit
    ):
        raise ValueError(
            "El formato del NIT no es válido."
        )

    return nit


def validar_nit_dpi(valor):
    valor = valor.strip().upper()

    if not valor:
        return valor

    if re.fullmatch(r"[0-9]{13}", valor):
        return valor

    if re.fullmatch(r"[0-9]+-?[0-9K]", valor):
        return valor

    raise ValueError(
        "Ingrese un NIT o DPI con formato válido."
    )


def validar_correo(correo):
    correo = correo.strip()

    if correo and not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
        r"@[A-Za-z0-9-]+"
        r"(\.[A-Za-z0-9-]+)+",
        correo
    ):
        raise ValueError(
            "Ingrese un correo electrónico válido."
        )

    return correo


def validar_precio(valor):
    try:
        precio = Decimal(str(valor))
    except (InvalidOperation, ValueError):
        raise ValueError(
            "Ingrese un precio válido."
        )

    if not precio.is_finite() or precio < 0:
        raise ValueError(
            "El precio no puede ser negativo o inválido."
        )

    if precio.as_tuple().exponent < -2:
        raise ValueError(
            "El precio admite como máximo dos decimales."
        )

    return precio
