"""Home Assistant camera and state adapters for configured printers."""
from __future__ import annotations
import asyncio
import time
from .config import Printer
from .engine import CAPTURE_TIMEOUT, MAX_FRAME_BYTES, Capture, Context, FrameValidationError

def context_from_states(states, printer: Printer) -> Context:
    def entity(domain, suffix): return printer.entity(domain, suffix)
    def val(eid):
        s=states.get(eid)
        return s.state if s is not None else 'unavailable'
    status_id=entity('sensor','print_status')
    stage_id=entity('sensor','current_stage')
    online_id=entity('binary_sensor','online')
    error_id=entity('binary_sensor','print_error')
    task_id=entity('sensor','task_name')
    start_id=entity('sensor','start_time')
    status=states.get(status_id)
    camera=val(printer.camera)
    guard_epoch=tuple(s.last_changed.isoformat() if (s:=states.get(eid)) is not None else ''
                      for eid in (status_id, stage_id, online_id, error_id, printer.camera, task_id, start_id))
    telemetry={k:(None if (v:=val(entity('sensor',suffix))) in ('unavailable','unknown') else v)
               for k,suffix in {'layer':'current_layer', 'total_layers':'total_layer_count',
                   'progress':'print_progress','nozzle_temperature':'nozzle_temperature',
                   'bed_temperature':'bed_temperature'}.items()}
    return Context(status=val(status_id),stage=val(stage_id),online=val(online_id)=='on',
                   camera_available=camera not in ('unavailable','unknown','off'),
                   print_error=val(error_id),task=val(task_id),start=val(start_id),
                   running_epoch=status.last_changed.isoformat() if status is not None else '',
                   telemetry=telemetry, guard_epoch=guard_epoch)

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
