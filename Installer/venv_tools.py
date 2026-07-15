from .core import CAT_ENV, register_command
from .utils import setup_venv


def rebuild_venv_menu():
    """Baut die Python-Umgebung fuer Reparaturfaelle neu auf."""
    return setup_venv(show_header=True)


register_command(
    "21",
    "Python venv neu aufbauen (Reparatur)",
    rebuild_venv_menu,
    sort_order=21,
    category=CAT_ENV,
)
