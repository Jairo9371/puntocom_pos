"""Publica los recursos estáticos durante el build, sin cargar la app ni la BD."""

from pathlib import Path
from shutil import copytree


def preparar_estaticos():
    raiz = Path(__file__).resolve().parents[1]
    # Conserva /static/... y url_for('static', ...) en las plantillas existentes.
    destino = raiz / 'public' / 'static'
    copytree(raiz / 'static', destino, dirs_exist_ok=True)


if __name__ == '__main__':
    preparar_estaticos()
    print('Recursos estáticos preparados en public/static/.')
