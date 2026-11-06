"""Fail-closed print monitoring engine with injected service boundaries."""
from __future__ import annotations
import asyncio
import hashlib
import math
from dataclasses import dataclass
from collections import deque
from typing import Any
from .frame_validation import verify_jpeg

MAX_FRAME_BYTES = 8_000_000
CAPTURE_TIMEOUT = 15

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
    # Monotonic local receipt time; never a claim about source capture time.
    received_at: float
    # Only a trusted capture-source sequence can set this. A hash isn't freshness proof.
    source_frame_id: str | None = None

FRAME_FAILURE_REASONS = frozenset({
    'missing_frame', 'invalid_frame_bytes', 'frame_too_small', 'frame_too_large',
    'invalid_jpeg_markers', 'invalid_receipt_timestamp', 'future_receipt',
    'stale_receipt', 'duplicate_frame', 'unsupported_image_format',
    'invalid_jpeg_data', 'frame_dimensions_exceeded', 'frame_pixel_limit_exceeded',
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
                 capture_timeout=CAPTURE_TIMEOUT, inference_timeout=40.0, max_frame_bytes=MAX_FRAME_BYTES,
                 ai_phase=lambda: 'ai_service', notification_timeout=10.0, on_started=lambda new_job: None):
        self.capture, self.screen, self.compare, self.context = capture, screen, compare, context
        self.reserve, self.pause, self.notify = reserve, pause, notify
        self.clock, self.sleep, self.allow_pause, self.interval = clock, sleep, allow_pause, interval
        self.capture_timeout, self.inference_timeout, self.max_frame_bytes = capture_timeout, inference_timeout, max_frame_bytes
        self.lock = asyncio.Lock()
        self.ai_phase = ai_phase
        self.last_error = None
        self.notification_timeout = notification_timeout
        self.recent_frames = deque(maxlen=8)
        self.history_job = None
        self.last_freshness = 'not_checked'
        self.last_assessment = 'not_checked'
        self.last_unjudgeable_reasons = ()
        self.assessment_usable = False
        self.last_notification = 'not_requested'
        self.on_started = on_started

    def remember_frame(self, frame):
        """Track possible repeated input without persisting pixels or hashes."""
        digest = hashlib.sha256(frame.content).digest()
        repeated = digest in self.recent_frames
        if not repeated:
            self.recent_frames.append(digest)
        if repeated:
            self.last_freshness = 'suspected_stale'
        return repeated

    async def send_notice(self, message, context):
        try:
            result = await asyncio.wait_for(self.notify(message, context), self.notification_timeout)
            self.last_notification = result if result in ('submitted','suppressed_cooldown','suppressed_session') else 'unknown'
            return result
        except asyncio.CancelledError:
            self.last_notification = 'cancelled'
            raise
        except Exception as error:
            self.last_notification = 'timed_out' if isinstance(error,TimeoutError) else 'failed'
            raise NotificationFailure(error) from None
        finally:
            try:
                same_session = self.same_session(context)
            except Exception:
                same_session = False
            if not same_session or self.last_notification == 'suppressed_session':
                self.assessment_usable = False

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
        if type(f.received_at) not in (int, float) or not math.isfinite(f.received_at):
            raise FrameValidationError('invalid_receipt_timestamp')
        age = self.clock() - f.received_at
        if age < 0:
            raise FrameValidationError('future_receipt')
        if age > 5:
            raise FrameValidationError('stale_receipt')
        reason = await asyncio.to_thread(verify_jpeg, f.content)
        if reason is not None:
            raise FrameValidationError(reason)
        if self.clock() - f.received_at > 5:
            raise FrameValidationError('stale_receipt')
        digest = hashlib.sha256(f.content).hexdigest()
        if digest in seen:
            self.last_freshness = 'suspected_stale'
            raise FrameValidationError('duplicate_frame')
        seen.add(digest)
        return f

    async def run(self) -> str:
        if self.lock.locked():
            return 'busy'
        async with self.lock:
            frames: list[Capture] = []
            self.last_error = None
            self.last_freshness = 'not_checked'
            self.last_assessment = 'not_checked'
            self.last_unjudgeable_reasons = ()
            self.assessment_usable = False
            self.last_notification = 'not_requested'
            phase = 'context'
            try:
                initial = self.context()
                if not initial.eligible:
                    return 'ineligible'
                self.last_freshness = 'unknown'
                new_job = initial.job != self.history_job
                if new_job:
                    self.recent_frames.clear()
                    self.history_job = initial.job
                self.on_started(new_job)
                seen: set[str] = set()
                phase = 'camera_capture'
                f = await self.get_frame(seen); frames.append(f)
                phase = 'session_check'
                if not self.same_session(initial): return 'session_changed'
                repeated = self.remember_frame(f)
                phase = 'screen_ai'
                first_raw = await asyncio.wait_for(self.screen(f, initial.telemetry), self.inference_timeout)
                phase = 'screen_verdict'
                first = verdict(first_raw)
                phase = 'session_check'
                if not self.same_session(initial): return 'session_changed'
                if self.clock() - f.received_at > self.inference_timeout + 5: return 'stale_result'
                # Static scenes still receive an advisory assessment. Repeated
                # bytes cannot refresh usable health or escalate to an alert.
                self.last_unjudgeable_reasons = tuple(label for label, applies in (
                    ('model_reported', not first.judgeable),
                    ('missing_evidence', first.score >= 0.8 and not first.evidence.strip()),
                ) if applies)
                judgeable = not self.last_unjudgeable_reasons
                self.last_assessment = 'unjudgeable' if not judgeable else 'suspicious' if suspicious(first) else 'clear'
                if repeated: return 'repeated_view'
                if not judgeable: return 'screen_unjudgeable'
                if not suspicious(first):
                    self.assessment_usable = True
                    return 'screen_clear'
                self.last_assessment = 'pending_comparison'
                for _ in range(2):
                    await self.sleep(self.interval)
                    if not self.same_session(initial): return 'session_changed'
                    phase = 'camera_capture'
                    f = await self.get_frame(seen)
                    phase = 'session_check'
                    if not 15 <= f.received_at - frames[-1].received_at <= 75: return 'stale_gap'
                    if self.remember_frame(f): return 'repeated_view'
                    frames.append(f)
                if not self.same_session(initial): return 'session_changed'
                phase = 'comparison_ai'
                raw = await asyncio.wait_for(self.compare(frames, initial.telemetry), self.inference_timeout)
                phase = 'comparison_verdict'
                if not self.same_session(initial): return 'session_changed'
                if self.clock() - frames[-1].received_at > self.inference_timeout + 5: return 'stale_result'
                if not isinstance(raw, list) or len(raw) != 3:
                    raise ValueError('invalid comparison shape')
                assessments = [verdict(x) for x in raw]
                self.last_unjudgeable_reasons = tuple(label for label, applies in (
                    ('model_reported', any(not item.judgeable for item in assessments)),
                    ('missing_evidence', any(item.score >= 0.8 and not item.evidence.strip() for item in assessments)),
                ) if applies)
                if self.last_unjudgeable_reasons:
                    self.last_assessment = 'unjudgeable'
                    return 'comparison_unjudgeable'
                self.assessment_usable = True
                if not all(suspicious(item) for item in assessments):
                    self.last_assessment = 'not_confirmed'
                    return 'comparison_not_confirmed'
                self.last_assessment = 'suspicious'
                # Exact-byte dedup is not proof of a live camera. Never pause without
                # trustworthy source frame IDs, and never arm by default.
                ids = [x.source_frame_id for x in frames]
                freshness_verified = all(ids) and len(set(ids)) == 3
                if not self.allow_pause or not freshness_verified:
                    phase = 'notification'
                    await self.send_notice('Possible print failure; three assessments were suspicious. No pause was sent; frame freshness or automatic pause is not verified.', initial)
                    if self.last_notification == 'suppressed_session' or not self.same_session(initial):
                        return 'session_changed'
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
