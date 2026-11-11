"""Coordinator for the Huawei Health integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .client import (
    HuaweiAuthError,
    HuaweiConnectionError,
    HuaweiError,
    HuaweiHealthClient,
    Tokens,
    daily_totals,
)
from .const import (
    CONF_ENABLE_HEALTH,
    CONF_ENABLE_WORKOUTS,
    CONF_HISTORY_DAYS,
    CONF_UPDATE_INTERVAL,
    CONF_WORKOUT_DAYS,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_WORKOUT_DAYS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class HuaweiHealthData:
    """Everything an entity needs, hanging off entry.runtime_data."""

    entry: ConfigEntry
    client: HuaweiHealthClient
    tokens: Tokens
    coordinator: HuaweiHealthCoordinator
    options: dict
    # The data centre this client was built for. Kept because choosing "detect
    # automatically" only clears entry.data, which a plain options comparison never sees.
    region: str | None = None

    def day(self, metric: str, lookback_days: int = 0) -> tuple[str, Any] | None:
        """Newest value of a per-day metric, with the date it belongs to.

        The cloud stamps a night with the day it ended on and fills a day lazily as the
        band syncs, so an empty today is normal for a few hours and the previous day is
        the honest reading rather than "unavailable".
        """
        days = (self.coordinator.data or {}).get("days") or {}
        offset = date.today()
        for _ in range(lookback_days + 1):
            row = days.get(offset.isoformat())
            if row and row.get(metric) is not None:
                return offset.isoformat(), row[metric]
            offset -= timedelta(days=1)
        return None

    @property
    def latest_session(self) -> dict | None:
        return (self.coordinator.data or {}).get("latest_session")

    @property
    def sessions(self) -> list[dict]:
        return (self.coordinator.data or {}).get("sessions") or []

    @property
    def diagnostics(self) -> dict:
        """What the last poll actually asked and what it answered, for the login sensor."""
        data = self.coordinator.data or {}
        # A failed poll leaves the previous snapshot behind, so the live error wins; the
        # snapshot's own verdict only means anything when that poll succeeded. The fields
        # below are what a reader compares against the Health app to tell a wrong data
        # centre, a narrowed deviceCode and "this device uploads nothing" apart.
        notice = self.coordinator.last_error or data.get("notice")
        return {
            "data_host": self.client.host,
            "data_source": self.client.data_source,
            "device_code": self.client.device_code or "whole account",
            "day_rows": len(data.get("days") or {}),
            "workout_rows": len(data.get("sessions") or []),
            "read_errors": data.get("failures") or [],
            "bound_devices": data.get("bound_devices") or [],
            "sync_ledger": data.get("sync_ledger") or [],
            "uid": self.tokens.uid,
            "notice": notice,
        }


class HuaweiHealthCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the account's cloud, in a worker thread, with a self-rotating token."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry,
                 client: HuaweiHealthClient, tokens: Tokens) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=int(entry.options.get(CONF_UPDATE_INTERVAL)
                            or DEFAULT_UPDATE_INTERVAL.total_seconds() // 60)),
        )
        self.client = client
        self.tokens = tokens
        # The reason a poll failed, or the verdict when it succeeded with nothing to say,
        # is kept here: Home Assistant's own error accessor on the base coordinator has
        # been renamed across releases, and a raised error would hide the empty-account
        # explanation from the login sensor that has to show it.
        self.last_error: str | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self.hass.async_add_executor_job(self._gather)
        except HuaweiAuthError as err:
            self.last_error = str(err)
            # Only a fresh browser login clears this, so ask HA for the reauth card.
            raise ConfigEntryAuthFailed(str(err)) from err
        except HuaweiConnectionError as err:
            self.last_error = f"cannot reach the Huawei Health cloud: {err}"
            raise UpdateFailed(self.last_error) from err
        except HuaweiError as err:
            self.last_error = str(err)
            raise UpdateFailed(str(err)) from err
        return data

    def _gather(self) -> dict[str, Any]:
        entry = self.config_entry
        history = int(entry.options.get(CONF_HISTORY_DAYS) or DEFAULT_HISTORY_DAYS)
        days: dict[str, dict] = {}
        failures: list[str] = []

        def run(name: str, fetch, merge) -> None:
            try:
                merge(fetch())
            except HuaweiAuthError:
                raise
            except HuaweiError as err:
                # A family the account's devices never fill must not hide the rest.
                _LOGGER.debug("Huawei Health %s read failed: %s", name, err)
                failures.append(f"{name}: {err}")

        def merge_daily(rows: list[dict]) -> None:
            for day, row in daily_totals(rows).items():
                days.setdefault(day, {}).update(row)

        def merge_health(rows: list[dict]) -> None:
            for row in rows:
                days.setdefault(row["date"], {}).update(row)

        run("activity", lambda: self.client.daily_summary(history), merge_daily)
        if entry.options.get(CONF_ENABLE_HEALTH, True):
            run("health", lambda: self.client.health_summary(history), merge_health)

        workouts: list[dict] = []
        window_days = int(entry.options.get(CONF_WORKOUT_DAYS) or DEFAULT_WORKOUT_DAYS)
        if entry.options.get(CONF_ENABLE_WORKOUTS, True):
            def fetch_sessions() -> list[dict]:
                return self.client.sessions(window_days)

            run("workouts", fetch_sessions, lambda rows: workouts.extend(rows))

        _LOGGER.debug("Huawei Health %s: %d day rows and %d workouts over %d days from "
                      "%s (source %s, failures: %s)", entry.title, len(days),
                      len(workouts), history, self.client.host, self.client.data_source,
                      "; ".join(failures) or "none")
        workouts.sort(key=lambda session: session["start"])
        notice: str | None = None
        if not days and not workouts:
            # An empty answer is what a data centre of another region gives for a valid
            # token, so it is the first thing a reader has to be told. It is not raised as
            # UpdateFailed: a poll that failed to reach the cloud is already visible as a
            # failed update, and an empty-but-successful answer has to stay readable in the
            # login sensor's attributes, which a raised error would freeze out.
            notice = (f"{self.client.host} (dataSource {self.client.data_source}, "
                      f"device {self.client.device_code or 'whole account'}) answered "
                      f"nothing for the last {history} days"
                      + (f"; read errors: {'; '.join(failures)}" if failures else
                         "; the region can be set under Options"))
            # The two extra reads are the evidence a human needs to tell the causes apart:
            # a device list that names the band but no rows means the rows live in another
            # data centre, and the cloud's own sync ledger says what it ever received.
            evidence = {"bound_devices": self.client.device_codes(),
                        "sync_ledger": self.client.sync_ledger()}
            _LOGGER.warning("Huawei Health: %s", notice)
        else:
            evidence = {}
        self.last_error = notice
        return {
            "days": days,
            "sessions": workouts,
            "latest_session": workouts[-1] if workouts else None,
            "window_days": window_days,
            "failures": failures,
            "notice": notice,
            **evidence,
        }
