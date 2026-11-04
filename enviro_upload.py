#!/usr/bin/env python3
"""Uploader data sensor ke Enviro Monitor.

Hanya memakai pustaka standar Python. Dipakai oleh skrip Raspberry Pi untuk
mengirim hasil pembacaan ke endpoint /enviro/api/ingest.

Contoh pemakaian:

    from enviro_upload import Uploader

    uploader = Uploader(
        url="https://devel-ai.ub.ac.id/enviro/api/ingest",
        api_key="...",
        device_id="node-01",
    )
    uploader.upload(data)   # data = dict hasil pembacaan sensor
"""

from __future__ import annotations

import json
import logging
import math
import os
import ssl
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import error, request

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

LOG = logging.getLogger("enviro-upload")

DEVICE_TZ = os.getenv("ENVIRO_DEVICE_TZ", "Asia/Jakarta")

DEFAULT_URL = "https://devel-ai.ub.ac.id/enviro/api/ingest"

# Pemetaan field CSV program sensor -> field API Enviro Monitor.
# (grup, nama field)
FIELD_MAP: Dict[str, tuple] = {
    # SPS30
    "pm1_0": ("sps30", "pm1"),
    "pm2_5": ("sps30", "pm25"),
    "pm4_0": ("sps30", "pm4"),
    "pm10_0": ("sps30", "pm10"),
    # MiCS-4514
    "co_ppm": ("gases", "co"),
    "ch4_ppm": ("gases", "ch4"),
    "ethanol_ppm": ("gases", "c2h5oh"),
    "h2_ppm": ("gases", "h2"),
    "nh3_ppm": ("gases", "nh3"),
    "no2_ppm": ("gases", "no2"),
    # MQ-136
    "mq136_raw": ("extra", "h2s_raw"),
    "mq136_voltage": ("extra", "h2s_voltage"),
    # SHT41
    "sht41_temperature": ("extra", "temp_sht"),
    "sht41_humidity": ("extra", "humidity"),
    # BMP280
    "bmp280_temperature": ("extra", "temp_bmp"),
    "bmp280_pressure": ("extra", "pressure"),
    "bmp280_altitude": ("extra", "altitude"),
}

EXTRA_KEYS = (
    "h2s_raw",
    "h2s_voltage",
    "temp_sht",
    "humidity",
    "temp_bmp",
    "pressure",
    "altitude",
)


def _number(value: Any) -> Optional[float]:
    """Ubah ke float, bulatkan maksimal 2 desimal, tolak nilai tidak valid."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return round(number, 2)


def _device_zone():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(DEVICE_TZ)
        except Exception:
            pass
    return timezone(timedelta(hours=7))


def _timestamp(value: Any) -> str:
    """ISO 8601 UTC. Timestamp tanpa zona dianggap waktu perangkat (WIB)."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            moment = datetime.now()
    else:
        moment = datetime.now()

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_device_zone())
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_payload(row: Dict[str, Any], device_id: str = "node-01") -> Optional[Dict[str, Any]]:
    """Ubah satu baris data sensor menjadi payload API, semua nilai 2 desimal."""
    gases: Dict[str, float] = {}
    sps30: Dict[str, float] = {}
    extra: Dict[str, float] = {}

    for source, (group, target) in FIELD_MAP.items():
        value = _number(row.get(source))
        if value is None:
            continue
        if group == "gases":
            gases[target] = value
        elif group == "sps30":
            sps30[target] = value
        else:
            extra[target] = value

    if not gases and not sps30 and not extra:
        return None

    payload: Dict[str, Any] = {
        "device_id": device_id,
        "timestamp": _timestamp(row.get("timestamp")),
        "gases": gases,
        "sps30": sps30,
    }
    payload.update(extra)
    return payload


class UploadError(RuntimeError):
    def __init__(self, message: str, drop: bool = False, fatal: bool = False) -> None:
        super().__init__(message)
        self.drop = drop
        self.fatal = fatal


class Uploader:
    """Kirim payload ke API dengan antrean offline (JSON lines)."""

    def __init__(
        self,
        url: str = DEFAULT_URL,
        api_key: str = "",
        device_id: str = "node-01",
        queue_path: str = "enviro_queue.jsonl",
        timeout: float = 10.0,
        verify: bool = True,
        max_queue: int = 5000,
    ) -> None:
        self.url = url or DEFAULT_URL
        self.api_key = api_key or os.getenv("ENVIRO_API_KEY", "")
        self.device_id = device_id or os.getenv("ENVIRO_DEVICE_ID", "node-01")
        self.queue_path = Path(queue_path)
        self.timeout = timeout
        self.context = None if verify else ssl._create_unverified_context()
        self.max_queue = max_queue
        self._pending: List[Dict[str, Any]] = []
        self._backoff = 0.0
        self._retry_at = 0.0
        self._load_queue()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------ queue
    def _load_queue(self) -> None:
        if not self.queue_path.exists():
            return
        try:
            with self.queue_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        self._pending.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("gagal membaca antrean %s: %s", self.queue_path, exc)
        if self._pending:
            LOG.info("memuat %d data tertunda dari antrean", len(self._pending))

    def _save_queue(self) -> None:
        try:
            self.queue_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.queue_path.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as handle:
                for item in self._pending:
                    handle.write(json.dumps(item, separators=(",", ":")) + "\n")
            os.replace(temp, self.queue_path)
        except OSError as exc:
            LOG.warning("gagal menyimpan antrean: %s", exc)

    # ----------------------------------------------------------------- upload
    def upload(self, row: Dict[str, Any], force: bool = False) -> bool:
        """Tambahkan satu baris ke antrean lalu coba kirim."""
        if not self.enabled:
            return False

        payload = build_payload(row, self.device_id)
        if payload is None:
            LOG.warning("tidak ada field sensor yang bisa dikirim")
            return False

        self._pending.append(payload)
        if len(self._pending) > self.max_queue:
            dropped = len(self._pending) - self.max_queue
            del self._pending[:dropped]
            LOG.warning("antrean penuh, %d data terlama dibuang", dropped)
        self._save_queue()

        return self.flush(force=force)

    def flush(self, force: bool = False) -> bool:
        """Kirim seluruh antrean dalam batch 200. False bila masih ada sisa."""
        if not self.enabled or not self._pending:
            return True

        if not force and time.monotonic() < self._retry_at:
            return False

        try:
            while self._pending:
                batch = self._pending[:200]
                try:
                    self._send(batch)
                except UploadError as exc:
                    if exc.drop:
                        LOG.error("batch ditolak server, dibuang: %s", exc)
                        del self._pending[: len(batch)]
                        self._save_queue()
                        continue
                    raise
                del self._pending[: len(batch)]
                self._save_queue()
                LOG.debug("terkirim %d data", len(batch))

            self._backoff = 0.0
            self._retry_at = 0.0
            return True
        except UploadError as exc:
            if exc.fatal:
                LOG.error("API key ditolak: %s", exc)
                return False
            self._backoff = min(60.0, max(5.0, self._backoff * 2 or 5.0))
            self._retry_at = time.monotonic() + self._backoff
            LOG.warning("%s — sisa antrean %d, coba lagi %.0fs", exc, len(self._pending), self._backoff)
            return False

    def _send(self, payloads: List[Dict[str, Any]]) -> None:
        body = json.dumps({"readings": payloads}, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "User-Agent": "enviro-raspi/1.0",
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout, context=self.context) as resp:
                if not 200 <= resp.status < 300:
                    raise UploadError(f"HTTP {resp.status}")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            message = f"HTTP {exc.code}: {detail}"
            if exc.code in (401, 403):
                raise UploadError(message, fatal=True) from exc
            if 400 <= exc.code < 500:
                raise UploadError(message, drop=True) from exc
            raise UploadError(message) from exc
        except error.URLError as exc:
            raise UploadError(f"tidak bisa terhubung: {exc.reason}") from exc
        except TimeoutError as exc:
            raise UploadError("permintaan timeout") from exc

    def __len__(self) -> int:
        return len(self._pending)
