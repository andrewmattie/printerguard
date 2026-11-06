"""Synthetic bindings shared by tests; never use installation-specific IDs."""
from p1s_guard.config import parse_config


def example_config():
    return {
        'ai_task': 'ai_task.example_vision',
        'notify_service': 'notify.example_phone',
        'printers': [
            {'name': 'Printer One', 'entity_prefix': 'printer_one', 'camera': 'camera.printer_one'},
            {'name': 'Printer Two', 'entity_prefix': 'printer_two', 'camera': 'camera.printer_two'},
        ],
    }


SETTINGS = parse_config(example_config())
