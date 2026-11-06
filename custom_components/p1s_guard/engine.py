"""Dependency-free, fail-closed print monitoring engine."""
from __future__ import annotations
import asyncio
import hashlib
import math
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class Context:
    status: str
    stage: str
    online: bool
    camera_available: bool
    print_error: str
    task: str
    start: str
    running_epoch: str
    telemetry: dict[str, Any]
    guard_epoch: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        missing = ('', 'unknown', 'unavailable', 'none')
        return (self.status == 'running' and self.stage == 'printing' and self.online
                and self.camera_available and self.print_error == 'off'
                and self.task.lower() not in missing and self.start.lower() not in missing
                and bool(self.running_epoch))

    @property
    def job(self) -> tuple[str, str]:
        """Stable identity for cooldowns across transient guard-state changes."""
        return self.task, self.start

    @property
    def session(self) -> tuple:
        return self.task, self.start, self.running_epoch, self.guard_epoch

@dataclass(frozen=True)
class Capture:
    content: bytes
    captured_at: float
    # Only a trusted capture-source sequence can set this. A hash isn't freshness proof.
    source_frame_id: str | None = None

FRAME_FAILURE_REASONS = frozenset({
    'missing_frame', 'invalid_frame_bytes', 'frame_too_small', 'frame_too_large',
    'invalid_jpeg_markers', 'invalid_capture_timestamp', 'future_capture',
    'stale_capture', 'duplicate_frame', 'unsupported_image_format',
})


class FrameValidationError(ValueError):
    """A fixed diagnostic reason, without image bytes, URLs or timestamps."""
    def __init__(self, reason: str):
        if reason not in FRAME_FAILURE_REASONS:
            raise ValueError('invalid frame failure reason')
        self.reason = reason
        super().__init__(reason)

@dataclass(frozen=True)
class Verdict:
    judgeable: bool
    score: float
    evidence: str

def verdict(obj: Any) -> Verdict:
    if not isinstance(obj, dict) or set(obj) != {'judgeable','score','evidence'}:
        raise ValueError('invalid verdict shape')
    score = obj['score']
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError('invalid score')
    if type(obj['judgeable']) is not bool or not isinstance(obj['evidence'], str) or len(obj['evidence']) > 500:
        raise ValueError('invalid assessment fields')
    return Verdict(obj['judgeable'], float(score), obj['evidence'])

def suspicious(v: Verdict) -> bool:
    return v.judgeable and v.score >= 0.8 and bool(v.evidence.strip())

def safe_error_category(error: Exception) -> str:
    """Return only fixed labels, never exception text or arbitrary class names."""
    if isinstance(error, TimeoutError): return 'timeout'
    if isinstance(error, PermissionError): return 'permission'
    if isinstance(error, FileNotFoundError): return 'not_found'
    if isinstance(error, OSError): return 'os_error'
    if type(error).__name__ in ('Invalid', 'MultipleInvalid', 'ServiceValidationError'):
        return 'validation'
    if isinstance(error, ValueError): return 'invalid_data'
    return 'other'

class NotificationFailure(Exception):
    """Keep the notification boundary identifiable without exposing its payload."""
    def __init__(self, error):
        super().__init__('notification failed')
        self.error = error


class Monitor:
    """One screen followed by a bounded three-frame comparison.

    capture, screen, compare, context, reserve, pause and notify are injected.
    reserve must atomically and durably latch the job before issuing pause.
    Deployment MUST leave allow_pause False until end-to-end verification.
    """
    def __init__(self, *, capture, screen, compare, context, reserve, pause, notify,
                 clock, sleep=asyncio.sleep, allow_pause=False, interval=30.0,
                 capture_timeout=15.0, inference_timeout=40.0, max_frame_bytes=8_000_000,
                 ai_phase=lambda: 'ai_service', notification_timeout=10.0):
        self.capture, self.screen, self.compare, self.context = capture, screen, compare, context
        self.reserve, self.pause, self.notify = reserve, pause, notify
        self.clock, self.sleep, self.allow_pause, self.interval = clock, sleep, allow_pause, interval
        self.capture_timeout, self.inference_timeout, self.max_frame_bytes = capture_timeout, inference_timeout, max_frame_bytes
        self.lock = asyncio.Lock()
        self.ai_phase = ai_phase
        self.last_error = None
        self.notification_timeout = notification_timeout

    async def send_notice(self, message, context):
        try:
            return await asyncio.wait_for(self.notify(message, context), self.notification_timeout)
        except Exception as error:
            raise NotificationFailure(error) from None

    def same_session(self, original: Context) -> bool:
        current = self.context()
        return current.eligible and current.session == original.session

    async def get_frame(self, seen: set[str]) -> Capture:
        self.capture_phase = 'camera_capture'
        f = await asyncio.wait_for(self.capture(), self.capture_timeout)
        self.capture_phase = 'frame_validation'
        if not isinstance(f, Capture):
            raise FrameValidationError('missing_frame')
        if not isinstance(f.content, bytes):
            raise FrameValidationError('invalid_frame_bytes')
        if len(f.content) < 4:
            raise FrameValidationError('frame_too_small')
        if len(f.content) > self.max_frame_bytes:
            raise FrameValidationError('frame_too_large')
        if not (f.content.startswith(b'\xff\xd8') and f.content.endswith(b'\xff\xd9')):
            raise FrameValidationError('invalid_jpeg_markers')
        if type(f.captured_at) not in (int, float) or not math.isfinite(f.captured_at):
            raise FrameValidationError('invalid_capture_timestamp')
        age = self.clock() - f.captured_at
        if age < 0:
            raise FrameValidationError('future_capture')
        if age > 5:
            raise FrameValidationError('stale_capture')
        digest = hashlib.sha256(f.content).hexdigest()
        if digest in seen:
            raise FrameValidationError('duplicate_frame')
        seen.add(digest)
        return f

    async def run(self) -> str:
        if self.lock.locked():
            return 'busy'
        async with self.lock:
            frames: list[Capture] = []
            self.last_error = None
            phase = 'context'
            try:
                initial = self.context()
                if not initial.eligible:
                    return 'ineligible'
                seen: set[str] = set()
                phase = 'camera_capture'
                f = await self.get_frame(seen); frames.append(f)
                phase = 'session_check'
                if not self.same_session(initial): return 'session_changed'
                phase = 'screen_ai'
                first_raw = await asyncio.wait_for(self.screen(f, initial.telemetry), self.inference_timeout)
                phase = 'screen_verdict'
                first = verdict(first_raw)
                phase = 'session_check'
                if not self.same_session(initial): return 'session_changed'
                if self.clock() - f.captured_at > self.inference_timeout + 5: return 'stale_result'
                if not suspicious(first): return 'screen_clear_or_unjudgeable'
                for _ in range(2):
                    await self.sleep(self.interval)
                    if not self.same_session(initial): return 'session_changed'
                    phase = 'camera_capture'
                    f = await self.get_frame(seen)
                    phase = 'session_check'
                    if not 15 <= f.captured_at - frames[-1].captured_at <= 75: return 'stale_gap'
                    frames.append(f)
                if not self.same_session(initial): return 'session_changed'
                phase = 'comparison_ai'
                raw = await asyncio.wait_for(self.compare(frames, initial.telemetry), self.inference_timeout)
                phase = 'comparison_verdict'
                if not self.same_session(initial): return 'session_changed'
                if self.clock() - frames[-1].captured_at > self.inference_timeout + 5: return 'stale_result'
                if not isinstance(raw, list) or len(raw) != 3:
                    raise ValueError('invalid comparison shape')
                assessments = [verdict(x) for x in raw]
                if not all(suspicious(item) for item in assessments):
                    return 'comparison_clear_or_unjudgeable'
                # Exact-byte dedup is not proof of a live camera. Never pause without
                # trustworthy source frame IDs, and never arm by default.
                ids = [x.source_frame_id for x in frames]
                freshness_verified = all(ids) and len(set(ids)) == 3
                if not self.allow_pause or not freshness_verified:
                    phase = 'notification'
                    await self.send_notice('Possible print failure; three assessments were suspicious. No pause was sent; frame freshness or automatic pause is not verified.', initial)
                    return 'monitor_only'
                if not self.same_session(initial): return 'session_changed'
                try:
                    reserved = await self.reserve(initial.task, initial.start)
                except Exception:
                    await self.send_notice('Possible print failure. Could not safely reserve a pause attempt; no pause was sent. Check the printer.', initial)
                    return 'reservation_failed'
                if not reserved: return 'already_attempted'
                if not self.same_session(initial): return 'session_changed'
                try:
                    await self.pause()
                except Exception:
                    await self.send_notice('Possible print failure. The pause request failed or its result is uncertain; check the printer.', initial)
                    return 'pause_uncertain'
                # Confirm actual reported state; do not claim success merely from service return.
                for _ in range(5):
                    if self.context().status == 'pause':
                        await self.send_notice('Possible print failure. The printer reports paused.', initial)
                        return 'paused_confirmed'
                    await self.sleep(2)
                await self.send_notice('Possible print failure. A pause was requested, but paused status was not confirmed; check the printer.', initial)
                return 'pause_unconfirmed'
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if isinstance(error, NotificationFailure):
                    phase = 'notification'
                    error = error.error
                elif phase == 'camera_capture':
                    phase = self.capture_phase
                if phase in ('screen_ai', 'comparison_ai'):
                    candidate = self.ai_phase()
                    phase = candidate if candidate in ('ai_prepare', 'media_prepare', 'ai_request_build', 'ai_service', 'ai_response', 'media_cleanup') else 'ai_other'
                self.last_error = {'phase': phase, 'category': safe_error_category(error)}
                if isinstance(error, FrameValidationError) and error.reason in FRAME_FAILURE_REASONS:
                    self.last_error['reason'] = error.reason
                return 'unavailable_or_error'
            finally:
                frames.clear()
