"""Home Assistant camera and state adapters for configured printers."""
from __future__ import annotations
import asyncio
import time
from .config import Printer
from .engine import CAPTURE_TIMEOUT, Capture, Context, FrameValidationError
from .frame_validation import MAX_FRAME_BYTES

def context_from_states(states, printer: Printer) -> Context:
    def val(state):
        return state.state if state is not None else 'unavailable'

    guard_states = tuple(states.get(entity_id) for entity_id in (
        printer.print_status,
        printer.current_stage,
        printer.online,
        printer.print_error,
        printer.camera,
        printer.task_name,
        printer.start_time,
    ))
    status, stage, online, error, camera, task, start = map(val, guard_states)
    guard_epoch = tuple(
        state.last_changed.isoformat() if state is not None else ''
        for state in guard_states
    )
    telemetry = {}
    for key, suffix in (
        ('layer', 'current_layer'),
        ('total_layers', 'total_layer_count'),
        ('progress', 'print_progress'),
        ('nozzle_temperature', 'nozzle_temperature'),
        ('bed_temperature', 'bed_temperature'),
    ):
        entity_id = getattr(printer, suffix)
        value = val(states.get(entity_id)) if entity_id is not None else 'unavailable'
        telemetry[key] = None if value in ('unavailable', 'unknown') else value
    return Context(
        status=status, stage=stage, online=online == 'on',
        camera_available=camera not in ('unavailable', 'unknown', 'off'),
        print_error=error, task=task, start=start, running_epoch=guard_epoch[0],
        telemetry=telemetry, guard_epoch=guard_epoch,
    )

async def camera_capture(hass, printer: Printer) -> Capture:
    """Fetch this printer's image. This is not proof of source freshness."""
    from homeassistant.components.camera import async_get_image
    task = asyncio.current_task()
    cancelling = task.cancelling()
    try:
        image = await async_get_image(hass, printer.camera, timeout=CAPTURE_TIMEOUT)
    except Exception:
        # HA can replace caller cancellation with a generic camera error.
        if task.cancelling() > cancelling:
            raise asyncio.CancelledError from None
        raise
    if image.content_type != 'image/jpeg': raise FrameValidationError('unsupported_image_format')
    content=image.content
    # Bambu's camera returns a mutable bytearray. Freeze that known buffer type
    # at the adapter boundary; the engine still validates the complete JPEG.
    if type(content) is bytearray:
        if len(content) > MAX_FRAME_BYTES: raise FrameValidationError('frame_too_large')
        content=bytes(content)
    return Capture(content,time.monotonic(),None)
