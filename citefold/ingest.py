from __future__ import annotations

import json
import mimetypes
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .models import IngestResult, MemoryScope
from .openrouter import ModelResponseError, OpenRouterClient, OpenRouterRequestError
from .store import LedgerStore


class MediaProcessingError(RuntimeError):
    pass


class FFmpegProcessor:
    DEFAULT_AUDIO_CHUNK_MS = 300_000

    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe

    def probe_duration_ms(self, path: Path) -> int:
        completed = self._run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ]
        )
        try:
            return max(0, round(float(completed.stdout.strip()) * 1000))
        except ValueError as exc:
            raise MediaProcessingError("FFprobe returned an invalid duration") from exc

    def standardize_audio(self, source: Path, target: Path) -> None:
        self._run(
            [
                self.ffmpeg,
                "-y",
                "-v",
                "error",
                "-i",
                str(source),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(target),
            ]
        )

    def split_audio(
        self,
        source: Path,
        target_dir: Path,
        duration_ms: int,
        chunk_ms: int = DEFAULT_AUDIO_CHUNK_MS,
    ) -> list[tuple[int, int, Path]]:
        if duration_ms <= 0:
            raise MediaProcessingError("Audio duration must be positive")
        if chunk_ms <= 0:
            raise ValueError("chunk_ms must be positive")
        if duration_ms <= chunk_ms:
            return [(0, duration_ms, source)]
        chunks: list[tuple[int, int, Path]] = []
        for index, start_ms in enumerate(range(0, duration_ms, chunk_ms)):
            end_ms = min(duration_ms, start_ms + chunk_ms)
            target = target_dir / f"audio-{index:04d}.wav"
            self._run(
                [
                    self.ffmpeg,
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    f"{start_ms / 1000:.3f}",
                    "-t",
                    f"{(end_ms - start_ms) / 1000:.3f}",
                    "-i",
                    str(source),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(target),
                ]
            )
            chunks.append((start_ms, end_ms, target))
        return chunks

    def extract_audio(self, source: Path, target: Path) -> bool:
        try:
            self._run(
                [
                    self.ffmpeg,
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(source),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(target),
                ]
            )
        except MediaProcessingError:
            return False
        return target.exists() and target.stat().st_size > 44

    def scene_times_ms(self, source: Path, max_frames: int = 12) -> list[int]:
        completed = self._run(
            [
                self.ffmpeg,
                "-v",
                "info",
                "-i",
                str(source),
                "-filter:v",
                "select=gt(scene\\,0.30),showinfo",
                "-an",
                "-f",
                "null",
                "-",
            ],
            allow_nonzero=True,
        )
        combined = completed.stderr
        times = [0]
        for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", combined):
            timestamp = round(float(value) * 1000)
            if timestamp not in times:
                times.append(timestamp)
            if len(times) >= max_frames:
                break
        return sorted(times)

    def extract_frame(self, source: Path, timestamp_ms: int, target: Path) -> None:
        self._run(
            [
                self.ffmpeg,
                "-y",
                "-v",
                "error",
                "-ss",
                f"{timestamp_ms / 1000:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(target),
            ]
        )

    def extract_clip(self, source: Path, start_ms: int, end_ms: int, target: Path) -> None:
        if start_ms < 0 or end_ms <= start_ms:
            raise ValueError("clip range must be positive")
        self._run(
            [
                self.ffmpeg,
                "-y",
                "-v",
                "error",
                "-ss",
                f"{start_ms / 1000:.3f}",
                "-t",
                f"{(end_ms - start_ms) / 1000:.3f}",
                "-i",
                str(source),
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(target),
            ]
        )

    def subtitle_segments(self, source: Path, target_dir: Path) -> list[dict[str, Any]]:
        target = target_dir / "subtitles.vtt"
        completed = self._run(
            [
                self.ffmpeg,
                "-y",
                "-v",
                "error",
                "-i",
                str(source),
                "-map",
                "0:s:0?",
                "-f",
                "webvtt",
                str(target),
            ],
            allow_nonzero=True,
        )
        if completed.returncode != 0 or not target.exists():
            return []
        text = target.read_text(encoding="utf-8", errors="replace")
        pattern = re.compile(
            r"(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s+-->\s+"
            r"(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})[^\n]*\n(?P<text>.*?)(?=\n\n|\Z)",
            re.DOTALL,
        )
        return [
            {
                "start_ms": self._vtt_time_ms(match.group("start")),
                "end_ms": self._vtt_time_ms(match.group("end")),
                "text": " ".join(line.strip() for line in match.group("text").splitlines() if line.strip()),
                "confidence": 1.0,
            }
            for match in pattern.finditer(text)
            if match.group("text").strip()
        ]

    @staticmethod
    def _vtt_time_ms(value: str) -> int:
        hours, minutes, seconds = value.split(":")
        return round((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)

    @staticmethod
    def _run(command: list[str], allow_nonzero: bool = False) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaProcessingError(f"Media tool failed: {type(exc).__name__}") from exc
        if completed.returncode != 0 and not allow_nonzero:
            raise MediaProcessingError(f"Media tool exited with status {completed.returncode}")
        return completed


class MultiModalIngestor:
    def __init__(
        self,
        store: LedgerStore,
        model_client: OpenRouterClient | None = None,
        media: FFmpegProcessor | None = None,
    ) -> None:
        self.store = store
        self.model_client = model_client
        self.media = media or FFmpegProcessor()

    def ingest_image(
        self,
        scope: MemoryScope,
        media_input: str | Path | bytes,
        source: str,
        observations: list[dict[str, Any]] | None = None,
        mime_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> IngestResult:
        data, detected_mime, name, _path = self._read_input(media_input, mime_type, "image")
        asset, _created = self.store.register_asset(
            scope,
            data,
            detected_mime,
            source,
            occurred_at=(metadata or {}).get("occurred_at"),
            privacy_policy=(metadata or {}).get("privacy_policy"),
            original_name=name,
            metadata=metadata,
        )
        result = IngestResult(asset_ids=[asset.asset_id])
        supplied = observations
        if supplied is None and self.model_client is not None:
            try:
                supplied = self.model_client.observe_image(data, detected_mime, asset.asset_id).get("observations", [])
            except (OpenRouterRequestError, ModelResponseError) as exc:
                result.errors.append(type(exc).__name__)
        if not supplied:
            supplied = [{"content": "Image asset registered; visual observation pending.", "confidence": 0.0, "locator": {}}]
            result.pending.append(asset.asset_id)

        for item in supplied:
            observation, _created = self.store.append_observation(
                scope=scope,
                asset_id=asset.asset_id,
                modality="image",
                locator=dict(item.get("locator", {})),
                content=str(item.get("content", "")),
                producer_type="model" if observations is None else "recorded_model",
                producer_model=self.model_client.models.observation if observations is None and self.model_client else None,
                confidence=float(item.get("confidence", 0.0)),
                source_origin="model_observation",
                metadata={**(metadata or {}), "untrusted_media": True},
            )
            result.observation_ids.append(observation.observation_id)
        episode = self._episode(scope, result, "image", source, metadata)
        result.episode_ids.append(episode["episode_id"])
        return result

    def ingest_audio(
        self,
        scope: MemoryScope,
        media_input: str | Path | bytes,
        source: str,
        transcript_segments: list[dict[str, Any]] | None = None,
        mime_type: str | None = None,
        duration_ms: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> IngestResult:
        data, detected_mime, name, path = self._read_input(media_input, mime_type, "audio")
        asset, _created = self.store.register_asset(
            scope,
            data,
            detected_mime,
            source,
            occurred_at=(metadata or {}).get("occurred_at"),
            privacy_policy=(metadata or {}).get("privacy_policy"),
            original_name=name,
            metadata=metadata,
        )
        result = IngestResult(asset_ids=[asset.asset_id])
        segments = transcript_segments
        with tempfile.TemporaryDirectory() as tmp:
            source_path = path or self._temporary_input(Path(tmp), data, name, detected_mime)
            actual_duration = duration_ms if duration_ms is not None else self._duration_or_none(source_path)
            standardized = Path(tmp) / "standardized.wav"
            standardized_ready = False
            try:
                self.media.standardize_audio(source_path, standardized)
                standardized_ready = True
                standardized_asset, _created = self.store.register_asset(
                    scope=scope,
                    data=standardized.read_bytes(),
                    mime_type="audio/wav",
                    source="ffmpeg_standardized",
                    occurred_at=asset.occurred_at,
                    privacy_policy=asset.privacy_policy,
                    original_name=f"{Path(name).stem if name else asset.asset_id}-16k-mono.wav",
                    metadata={"derived_from": asset.asset_id, "sample_rate": 16000, "channels": 1},
                )
                result.asset_ids.append(standardized_asset.asset_id)
                if actual_duration is None:
                    duration = actual_duration or self.media.probe_duration_ms(standardized)
                    actual_duration = duration
            except MediaProcessingError as exc:
                result.errors.append(type(exc).__name__)
            if segments is None and self.model_client is not None and standardized_ready:
                try:
                    duration = actual_duration or self.media.probe_duration_ms(standardized)
                    segments = []
                    for chunk_start, chunk_end, chunk_path in self.media.split_audio(
                        standardized,
                        Path(tmp),
                        duration,
                    ):
                        segments.extend(
                            self.model_client.transcribe_audio(
                                chunk_path.read_bytes(),
                                "wav",
                                asset.asset_id,
                                chunk_start,
                                chunk_end,
                            ).get("segments", [])
                        )
                except (MediaProcessingError, OpenRouterRequestError, ModelResponseError) as exc:
                    result.errors.append(type(exc).__name__)
            if not segments:
                end = max(1, actual_duration or 1)
                segments = [
                    {
                        "start_ms": 0,
                        "end_ms": end,
                        "text": "Audio asset registered; transcription pending.",
                        "confidence": 0.0,
                    }
                ]
                result.pending.append(asset.asset_id)
            self._append_timed_observations(
                scope,
                asset.asset_id,
                "audio",
                segments,
                actual_duration,
                result,
                metadata,
                recorded=transcript_segments is not None,
            )
        episode = self._episode(scope, result, "audio", source, metadata)
        result.episode_ids.append(episode["episode_id"])
        return result

    def ingest_video(
        self,
        scope: MemoryScope,
        media_input: str | Path | bytes,
        source: str,
        transcript_segments: list[dict[str, Any]] | None = None,
        frame_observations: list[dict[str, Any]] | None = None,
        mime_type: str | None = None,
        duration_ms: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> IngestResult:
        data, detected_mime, name, path = self._read_input(media_input, mime_type, "video")
        asset, _created = self.store.register_asset(
            scope,
            data,
            detected_mime,
            source,
            occurred_at=(metadata or {}).get("occurred_at"),
            privacy_policy=(metadata or {}).get("privacy_policy"),
            original_name=name,
            metadata=metadata,
        )
        result = IngestResult(asset_ids=[asset.asset_id])
        with tempfile.TemporaryDirectory() as tmp:
            source_path = path or self._temporary_input(Path(tmp), data, name, detected_mime)
            actual_duration = duration_ms if duration_ms is not None else self._duration_or_none(source_path)
            segments = transcript_segments
            frames = frame_observations
            subtitles: list[dict[str, Any]] = []
            if hasattr(self.media, "subtitle_segments"):
                try:
                    subtitles = self.media.subtitle_segments(source_path, Path(tmp))
                except MediaProcessingError as exc:
                    result.errors.append(type(exc).__name__)
            if segments is None:
                audio_path = Path(tmp) / "audio.wav"
                if self.model_client is not None and self.media.extract_audio(source_path, audio_path):
                    try:
                        duration = actual_duration or self.media.probe_duration_ms(audio_path)
                        segments = []
                        for chunk_start, chunk_end, chunk_path in self.media.split_audio(
                            audio_path,
                            Path(tmp),
                            duration,
                        ):
                            segments.extend(
                                self.model_client.transcribe_audio(
                                    chunk_path.read_bytes(),
                                    "wav",
                                    asset.asset_id,
                                    chunk_start,
                                    chunk_end,
                                ).get("segments", [])
                            )
                    except (MediaProcessingError, OpenRouterRequestError, ModelResponseError) as exc:
                        result.errors.append(type(exc).__name__)
            if frames is None and self.model_client is not None:
                frames = []
                try:
                    for index, timestamp_ms in enumerate(self.media.scene_times_ms(source_path)):
                        frame_path = Path(tmp) / f"frame-{index:03d}.jpg"
                        self.media.extract_frame(source_path, timestamp_ms, frame_path)
                        batch = self.model_client.observe_image(
                            frame_path.read_bytes(), "image/jpeg", asset.asset_id
                        ).get("observations", [])
                        frames.extend({**item, "timestamp_ms": timestamp_ms} for item in batch)
                except (MediaProcessingError, OpenRouterRequestError, ModelResponseError) as exc:
                    result.errors.append(type(exc).__name__)
                if not frames and hasattr(self.model_client, "observe_video") and hasattr(self.media, "extract_clip"):
                    try:
                        clip_end = min(actual_duration or 5000, 5000)
                        if clip_end > 0:
                            clip_path = Path(tmp) / "dynamic-fallback.mp4"
                            self.media.extract_clip(source_path, 0, clip_end, clip_path)
                            dynamic = self.model_client.observe_video(
                                clip_path.read_bytes(), "video/mp4", asset.asset_id
                            ).get("observations", [])
                            for item in dynamic:
                                locator = dict(item.get("locator", {}))
                                start_ms = int(locator.get("start_ms", 0))
                                end_ms = int(locator.get("end_ms", clip_end))
                                self._validate_range(start_ms, end_ms, actual_duration)
                                observation, _created = self.store.append_observation(
                                    scope,
                                    asset.asset_id,
                                    "video_clip",
                                    {"start_ms": start_ms, "end_ms": end_ms},
                                    str(item.get("content", "")),
                                    "model",
                                    self.model_client.models.observation,
                                    float(item.get("confidence", 0.0)),
                                    "model_observation",
                                    {**(metadata or {}), "untrusted_media": True, "channel": "dynamic_visual"},
                                )
                                result.observation_ids.append(observation.observation_id)
                    except (MediaProcessingError, OpenRouterRequestError, ModelResponseError, ValueError) as exc:
                        result.errors.append(type(exc).__name__)
            segments = segments or []
            frames = frames or []
            self._append_timed_observations(
                scope,
                asset.asset_id,
                "audio",
                segments,
                actual_duration,
                result,
                metadata,
                recorded=transcript_segments is not None,
            )
            self._append_timed_observations(
                scope,
                asset.asset_id,
                "subtitle",
                subtitles,
                actual_duration,
                result,
                metadata,
                recorded=True,
                channel="subtitle",
            )
            for item in frames:
                timestamp = int(item.get("timestamp_ms", 0))
                if actual_duration is not None and timestamp >= actual_duration:
                    timestamp = max(0, actual_duration - 1)
                frame_end = min(timestamp + 1, actual_duration or timestamp + 1)
                self._validate_range(timestamp, frame_end, actual_duration)
                observation, _created = self.store.append_observation(
                    scope=scope,
                    asset_id=asset.asset_id,
                    modality="video_frame",
                    locator={"start_ms": timestamp, "end_ms": frame_end},
                    content=str(item.get("content", "")),
                    producer_type="recorded_model" if frame_observations is not None else "model",
                    producer_model=self.model_client.models.observation if frame_observations is None and self.model_client else None,
                    confidence=float(item.get("confidence", 0.0)),
                    source_origin="model_observation",
                    metadata={**(metadata or {}), "untrusted_media": True, "channel": "visual"},
                )
                result.observation_ids.append(observation.observation_id)
            result.observation_ids.sort(
                key=lambda observation_id: self.store.observations(scope)[observation_id]["locator"].get("start_ms", 0)
            )
            if not result.observation_ids:
                observation, _created = self.store.append_observation(
                    scope,
                    asset.asset_id,
                    "video",
                    {"start_ms": 0, "end_ms": max(1, actual_duration or 1)},
                    "Video asset registered; processing pending.",
                    "system",
                    None,
                    0.0,
                    "external_content",
                    {**(metadata or {}), "untrusted_media": True},
                )
                result.observation_ids.append(observation.observation_id)
                result.pending.append(asset.asset_id)
        episode = self._episode(scope, result, "video", source, metadata)
        result.episode_ids.append(episode["episode_id"])
        return result

    def _episode(
        self,
        scope: MemoryScope,
        result: IngestResult,
        modality: str,
        source: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        observations = self.store.observations(scope)
        ordered = [observations[item] for item in result.observation_ids]
        summary = "\n".join(item["content"] for item in ordered if item.get("content")).strip()
        locators = [item.get("locator", {}) for item in ordered]
        start_values = [item["start_ms"] for item in locators if "start_ms" in item]
        end_values = [item["end_ms"] for item in locators if "end_ms" in item]
        now = self.store.now_iso()
        episode, _created = self.store.append_episode(
            scope=scope,
            observation_ids=result.observation_ids,
            summary=summary or f"{modality.title()} evidence captured; processing pending.",
            source_origin="multimodal_capture",
            start_at=now,
            end_at=now,
            participants=[],
            scene=modality,
            topics=[],
            metadata={
                **(metadata or {}),
                "multimodal": True,
                "modality": modality,
                "source": source,
                "timeline_start_ms": min(start_values) if start_values else None,
                "timeline_end_ms": max(end_values) if end_values else None,
            },
            idempotency_key=f"{modality}:{result.asset_ids[0]}:{'|'.join(result.observation_ids)}",
        )
        return episode.as_record()

    def _append_timed_observations(
        self,
        scope: MemoryScope,
        asset_id: str,
        modality: str,
        segments: list[dict[str, Any]],
        duration_ms: int | None,
        result: IngestResult,
        metadata: dict[str, Any] | None,
        recorded: bool,
        channel: str = "audio",
    ) -> None:
        ordered_segments = sorted(
            segments,
            key=lambda item: (
                int(item.get("start_ms", 0)),
                int(item.get("end_ms", item.get("start_ms", 0))),
                str(item.get("text", item.get("content", ""))),
            ),
        )
        seen: set[tuple[int, int, str]] = set()
        for segment in ordered_segments:
            start_ms = int(segment.get("start_ms", 0))
            end_ms = int(segment.get("end_ms", start_ms))
            content = str(segment.get("text", segment.get("content", "")))
            key = (start_ms, end_ms, content)
            if key in seen:
                continue
            seen.add(key)
            self._validate_range(start_ms, end_ms, duration_ms)
            observation, _created = self.store.append_observation(
                scope=scope,
                asset_id=asset_id,
                modality=modality,
                locator={"start_ms": start_ms, "end_ms": end_ms},
                content=content,
                producer_type="recorded_model" if recorded else "model",
                producer_model=(
                    getattr(self.model_client.models, "asr", self.model_client.models.observation)
                    if not recorded and self.model_client
                    else None
                ),
                confidence=float(segment.get("confidence", 0.0)),
                source_origin="model_observation",
                metadata={**(metadata or {}), "untrusted_media": True, "channel": channel},
            )
            result.observation_ids.append(observation.observation_id)

    @staticmethod
    def _validate_range(start_ms: int, end_ms: int, duration_ms: int | None) -> None:
        if start_ms < 0 or end_ms <= start_ms:
            raise ValueError(f"Invalid media time range: {start_ms}-{end_ms}")
        if duration_ms is not None and end_ms > duration_ms + 50:
            raise ValueError(f"Media time range exceeds duration: {start_ms}-{end_ms} > {duration_ms}")

    def _duration_or_none(self, path: Path) -> int | None:
        try:
            return self.media.probe_duration_ms(path)
        except MediaProcessingError:
            return None

    @staticmethod
    def _read_input(
        media_input: str | Path | bytes,
        mime_type: str | None,
        expected_kind: str,
    ) -> tuple[bytes, str, str | None, Path | None]:
        if isinstance(media_input, bytes):
            if not mime_type:
                raise ValueError("mime_type is required when media input is bytes")
            data = media_input
            name = None
            path = None
            detected = mime_type
        else:
            path = Path(media_input)
            data = path.read_bytes()
            name = path.name
            detected = mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not detected.startswith(f"{expected_kind}/"):
            raise ValueError(f"Expected {expected_kind} media, got {detected}")
        return data, detected, name, path

    @staticmethod
    def _temporary_input(root: Path, data: bytes, name: str | None, mime_type: str) -> Path:
        suffix = Path(name).suffix if name else mimetypes.guess_extension(mime_type) or ".bin"
        path = root / f"input{suffix}"
        path.write_bytes(data)
        return path
