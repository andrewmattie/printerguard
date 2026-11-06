"""Synthetic bindings shared by tests; never use installation-specific IDs."""
from printer_guard.config import parse_config


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


def synthetic_jpeg(quantization=1, *, width=8, height=8):
    """Encode one constant gray 8x8 block; no copied image or private metadata.

    Baseline JPEG defaults to unit quantization with single-symbol Huffman tables:
    a zero DC difference followed by end-of-block, padded with one bits. Changing
    quantization changes the encoding of this same constant gray image.
    """
    def segment(marker, payload):
        return b'\xff' + bytes([marker]) + (len(payload)+2).to_bytes(2,'big') + payload
    quantization = segment(0xdb, b'\x00' + bytes([quantization])*64)
    frame = segment(0xc0, b'\x08' + height.to_bytes(2,'big') + width.to_bytes(2,'big') + b'\x01\x01\x11\x00')
    dc = segment(0xc4, b'\x00\x01' + bytes(15) + b'\x00')
    ac = segment(0xc4, b'\x10\x01' + bytes(15) + b'\x00')
    scan = segment(0xda, b'\x01\x01\x00\x00\x3f\x00')
    return b'\xff\xd8' + quantization + frame + dc + ac + scan + b'\x3f\xff\xd9'
