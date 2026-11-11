"""Sensors for the Huawei Health integration."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from .const import CONF_HR_SAMPLES, HR_SAMPLE_WRITE_LIMIT, SPORT_TYPES
from .coordinator import HuaweiHealthData
from .entity import HuaweiHealthEntity

_LOGGER = logging.getLogger(__name__)

UNIT_STEPS: str = "steps"
UNIT_BPM: str = "bpm"
UNIT_SCORE: str = "score"


@dataclass(kw_only=True)
class HuaweiSensorDescription(SensorEntityDescription):
    """A sensor plus how to pull its number out of the coordinator snapshot."""

    value: Callable[[HuaweiHealthData], Any] = lambda data: None
    attributes: Callable[[HuaweiHealthData], dict[str, Any]] = lambda data: {}
    # False for the sensors that describe the login rather than the data, which must stay
    # readable when the cloud answers with empty lists - that is where the reason appears.
    data_dependent: bool = True
    # True only for the fine-grained heart-rate feed. Home Assistant's history holds
    # nothing but states that were actually written, so a sensor written once per poll can
    # show at most one point per update interval no matter how often the band measured.
    # That entity writes one row per sample, at the sample's own moment.
    sample_feed: bool = False


def day_value(metric: str, lookback: int = 0, scale: float = 1.0
              ) -> Callable[[HuaweiHealthData], Any]:
    """Read one per-day metric, falling back to the previous days if needed."""

    def get(data: HuaweiHealthData) -> Any:
        found = data.day(metric, lookback)
        return None if found is None else found[1] * scale

    return get


def day_date(metric: str, lookback: int = 0) -> Callable[[HuaweiHealthData], dict]:
    def attributes(data: HuaweiHealthData) -> dict[str, Any]:
        found = data.day(metric, lookback)
        return {} if found is None else {"date": found[0]}

    return attributes


def session_value(key: str, scale: float = 1.0
                  ) -> Callable[[HuaweiHealthData], Any]:
    def get(data: HuaweiHealthData) -> Any:
        session = data.latest_session
        value = None if session is None else session.get(key)
        return None if value is None else value * scale

    return get


def session_attributes(data: HuaweiHealthData) -> dict[str, Any]:
    session = data.latest_session
    if not session:
        return {}
    return {
        "start": session["start"].strftime("%Y-%m-%d %H:%M"),
        "end": session["end"].strftime("%Y-%m-%d %H:%M"),
        "duration_min": session["duration_min"],
        "distance_m": session["distance_m"],
        "calories_kcal": session["kcal"],
        "segments": session["segments"],
        "sport_type": session["sport_type"],
        "device_code": session["device_code"],
    }


def activity_name(data: HuaweiHealthData) -> str | None:
    session = data.latest_session
    if not session:
        return None
    # A state outside the declared options makes HA log a warning, so unknown ids from a
    # newer app version collapse to one value.
    return SPORT_TYPES.get(session["sport_type"], "unknown")


def _days_left(data: HuaweiHealthData) -> float | None:
    deadline = data.tokens.refresh_expires_at
    return None if not deadline else round((deadline - time.time()) / 86400, 1)


def _token_dates(data: HuaweiHealthData) -> dict[str, Any]:
    def stamp(seconds: float) -> str:
        return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")

    info = {
        "access_token_expires": stamp(data.tokens.expires_at),
        "refresh_token_expires": stamp(data.tokens.refresh_expires_at),
    }
    # When every other sensor of the account reads unknown, this is the one that has to
    # say why: which cloud was asked, which device was narrowed to, what it answered.
    info.update(data.diagnostics)
    return info


def sample_feed_attributes(data: HuaweiHealthData) -> dict[str, Any]:
    """How dense the feed really is - measured, never promised.

    Identical for every sample written in one pass on purpose: the state machine only
    adds a history row when the state or the attributes change, so attributes that moved
    per sample would double the rows for no added information.
    """
    spacing = data.hr_spacing
    snapshot = data.coordinator.data or {}
    return {
        "spacing": spacing,
        "samples_in_window": spacing.get("samples", 0),
        "asked_for": "getHealthData",
        # An empty feed is the option being off, not the account having no samples: the
        # read is off by default because it is the most expensive one of the integration.
        "enabled_by_option": bool(data.options.get(CONF_HR_SAMPLES, False)),
        # A refused sample read has to be readable here: the state going quiet is otherwise
        # indistinguishable from an account that has no finer samples at all.
        "read_errors": snapshot.get("failures") or [],
        # Only ever non-zero when the answer was bigger than the memory guard allows;
        # silence here would mean points that never reach the history at all.
        "dropped_by_memory_guard": data.hr_dropped,
    }


DESCRIPTIONS: tuple[HuaweiSensorDescription, ...] = (
    # ── the day's totals, matching the Health app's activity rings ─────────
    HuaweiSensorDescription(
        key="steps", translation_key="steps", name="Steps today", native_unit_of_measurement=UNIT_STEPS,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:walk",
        value=day_value("steps"), attributes=day_date("steps")),
    HuaweiSensorDescription(
        key="distance", translation_key="distance", name="Distance today",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=2, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-distance",
        value=day_value("distance_m", scale=1 / 1000), attributes=day_date("distance_m")),
    HuaweiSensorDescription(
        key="calories", translation_key="calories", name="Calories today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_CALORIE,
        suggested_display_precision=0, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fire",
        value=day_value("kcal"), attributes=day_date("kcal")),
    HuaweiSensorDescription(
        key="activity_duration", translation_key="activity_duration", name="Activity today",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:timer-outline",
        value=day_value("duration_min"), attributes=day_date("duration_min")),
    HuaweiSensorDescription(
        key="walking_duration", translation_key="walking_duration", name="Walking today",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:shoe-print",
        value=day_value("walk_min"), attributes=day_date("walk_min")),
    HuaweiSensorDescription(
        key="active_hours", translation_key="active_hours", name="Active hours today",
        native_unit_of_measurement=UnitOfTime.HOURS, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:clock-outline",
        value=day_value("active_hours"), attributes=day_date("active_hours")),
    HuaweiSensorDescription(
        key="moderate_high_intensity_minutes",
        translation_key="moderate_high_intensity_minutes", name="Moderate-to-vigorous minutes today",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:run-fast",
        value=day_value("moderate_high_intensity_minutes"),
        attributes=day_date("moderate_high_intensity_minutes")),
    HuaweiSensorDescription(
        key="step_goal", translation_key="step_goal", name="Step goal", native_unit_of_measurement=UNIT_STEPS,
        icon="mdi:target-variant", entity_registry_enabled_default=False,
        value=day_value("step_goal"), attributes=day_date("step_goal")),
    # ── heart rate ─────────────────────────────────────────────────────────
    HuaweiSensorDescription(
        key="resting_heart_rate", translation_key="resting_heart_rate", name="Resting heart rate",
        native_unit_of_measurement=UNIT_BPM, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart",
        value=day_value("resting_heart_rate"),
        attributes=day_date("resting_heart_rate")),
    HuaweiSensorDescription(
        key="heart_rate", translation_key="heart_rate", name="Heart rate",
        native_unit_of_measurement=UNIT_BPM, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-pulse",
        value=day_value("heart_rate"), attributes=day_date("heart_rate")),
    HuaweiSensorDescription(
        key="max_heart_rate", translation_key="max_heart_rate", name="Max heart rate today",
        native_unit_of_measurement=UNIT_BPM, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-plus", entity_registry_enabled_default=False,
        value=day_value("max_heart_rate"), attributes=day_date("max_heart_rate")),
    HuaweiSensorDescription(
        key="min_heart_rate", translation_key="min_heart_rate", name="Min heart rate today",
        native_unit_of_measurement=UNIT_BPM, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-minus", entity_registry_enabled_default=False,
        value=day_value("min_heart_rate"), attributes=day_date("min_heart_rate")),
    # ── the fine-grained feed: a row per sample, not a row per poll ────────
    # Only available when the "heart_rate_samples" option is on: the read is the most
    # expensive one of the integration (every *ByTime window of the account's sample
    # table), and it is the one whose answer shape is not verified against the live cloud.
    # Disabled in the registry by default so nobody pays for it by accident.
    HuaweiSensorDescription(
        key="heart_rate_samples", translation_key="heart_rate_samples",
        name="Heart rate (samples)", native_unit_of_measurement=UNIT_BPM,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:heart-half-full",
        entity_registry_enabled_default=False, sample_feed=True,
        value=lambda data: (data.hr_samples[-1]["bpm"] if data.hr_samples else None),
        attributes=sample_feed_attributes),
    # ── sleep: the night that just ended is stamped with the day it woke up in ──
    HuaweiSensorDescription(
        key="sleep_duration", translation_key="sleep_duration", name="Sleep duration",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:sleep",
        value=day_value("sleep_duration", lookback=1),
        attributes=day_date("sleep_duration", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_deep", translation_key="sleep_deep", name="Deep sleep",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:weather-night",
        value=day_value("sleep_deep", lookback=1),
        attributes=day_date("sleep_deep", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_rem", translation_key="sleep_rem", name="REM sleep",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:brain",
        value=day_value("sleep_rem", lookback=1),
        attributes=day_date("sleep_rem", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_light", translation_key="sleep_light", name="Light sleep",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:weather-night",
        entity_registry_enabled_default=False,
        value=day_value("sleep_light", lookback=1),
        attributes=day_date("sleep_light", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_awake", translation_key="sleep_awake", name="Awake during sleep",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:eye",
        entity_registry_enabled_default=False,
        value=day_value("sleep_awake", lookback=1),
        attributes=day_date("sleep_awake", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_score", translation_key="sleep_score", name="Sleep score",
        native_unit_of_measurement=UNIT_SCORE, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:star-four-points-circle",
        value=day_value("sleep_score", lookback=1),
        attributes=day_date("sleep_score", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_efficiency", translation_key="sleep_efficiency", name="Sleep efficiency",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:progress-clock", entity_registry_enabled_default=False,
        value=day_value("sleep_efficiency", lookback=1),
        attributes=day_date("sleep_efficiency", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_hrv", translation_key="sleep_hrv", name="HRV during sleep",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:waveform",
        value=day_value("sleep_hrv", lookback=1),
        attributes=day_date("sleep_hrv", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_spo2", translation_key="sleep_spo2", name="SpO2 during sleep",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:oximeter",
        value=day_value("sleep_spo2", lookback=1),
        attributes=day_date("sleep_spo2", lookback=1)),
    # ── stress ─────────────────────────────────────────────────────────────
    HuaweiSensorDescription(
        key="stress", translation_key="stress", name="Stress", native_unit_of_measurement=UNIT_SCORE,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:gauge",
        value=day_value("stress_average"), attributes=day_date("stress_average")),
    HuaweiSensorDescription(
        key="stress_last", translation_key="stress_last", name="Stress (last measurement)",
        native_unit_of_measurement=UNIT_SCORE, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gauge", entity_registry_enabled_default=False,
        value=day_value("stress_last"), attributes=day_date("stress_last")),
    # ── workouts, rebuilt from the per-minute segments ─────────────────────
    HuaweiSensorDescription(
        key="last_activity", translation_key="last_activity", name="Last activity",
        device_class=SensorDeviceClass.ENUM,
        options=list(SPORT_TYPES.values()),
        icon="mdi:sport", value=activity_name, attributes=session_attributes),
    HuaweiSensorDescription(
        key="last_activity_duration", translation_key="last_activity_duration", name="Last activity duration",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:timer",
        value=session_value("duration_min"), attributes=session_attributes),
    HuaweiSensorDescription(
        key="last_activity_distance", translation_key="last_activity_distance", name="Last activity distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=2, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-distance",
        value=session_value("distance_m", scale=1 / 1000),
        attributes=session_attributes),
    HuaweiSensorDescription(
        key="last_activity_calories", translation_key="last_activity_calories", name="Last activity calories",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_CALORIE,
        suggested_display_precision=0, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fire", value=session_value("kcal"), attributes=session_attributes),
    HuaweiSensorDescription(
        key="workouts", translation_key="workouts", name="Workouts in period", native_unit_of_measurement="workouts",
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:trophy-outline",
        value=lambda data: len(data.sessions),
        attributes=lambda data: {
            "window_days": (data.coordinator.data or {}).get("window_days")}),
    # ── session health: the one number that says when a login is due again ──
    HuaweiSensorDescription(
        key="relogin_in", translation_key="relogin_in", name="Re-login due in days",
        native_unit_of_measurement=UnitOfTime.DAYS, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:account-clock", suggested_display_precision=0,
        data_dependent=False,
        value=_days_left, attributes=_token_dates),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    data: HuaweiHealthData = entry.runtime_data
    async_add_entities(
        HuaweiHeartRateFeedSensor(description, data)
        if description.sample_feed else HuaweiHealthSensor(description, data)
        for description in DESCRIPTIONS)


class HuaweiHealthSensor(HuaweiHealthEntity, SensorEntity):
    entity_description: HuaweiSensorDescription

    def __init__(self, description: HuaweiSensorDescription,
                 data: HuaweiHealthData) -> None:
        super().__init__(data)
        self.entity_description = description
        self._attr_unique_id = f"{data.entry.entry_id}_{description.key}"
        # translation_key comes from the description, so names live in translations/

    @property
    def available(self) -> bool:
        if not self.entity_description.data_dependent:
            # The login is described by the token pair itself, so it survives a poll that
            # brought nothing back - which is exactly when its attributes carry the reason
            # every other sensor of the account went quiet.
            return True
        return super().available

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self._data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attributes(self._data)


class HeartRateFeedWatermark(ExtraStoredData):
    """The newest sample already filed, kept across restarts."""

    def __init__(self, json_dict: dict[str, Any]) -> None:
        self.json_dict = json_dict

    def as_dict(self) -> dict[str, Any]:
        return {"written_before": self.json_dict.get("written_before", 0.0)}

    @property
    def written_before(self) -> float:
        return float(self.json_dict.get("written_before") or 0.0)


class HuaweiHeartRateFeedSensor(HuaweiHealthSensor, RestoreEntity):
    """The fine-grained heart-rate sensor: one history row per sample.

    Home Assistant's history holds nothing but states that were written, so polling the
    sample endpoint and writing once per poll would cap the chart at one point per update
    interval no matter how often the band measured. Each sample is therefore written at
    its own moment: ``states.async_set`` takes a ``timestamp``, and the recorder files the
    row under ``state.last_updated`` - the column the history queries order by.

    The watermark has to be stored on its own rather than read back from the restored
    state: a poll with nothing new still writes the state and its attributes, and that
    write carries the wall clock. Deriving the watermark from ``last_updated`` would then
    skip every sample the cloud hands over afterwards, because those stamps sit behind a
    write that happened "now".
    """

    _written_before: float = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        restored = await self.async_get_last_extra_data()
        if isinstance(restored, HeartRateFeedWatermark):
            self._written_before = restored.written_before
        self._file_samples()

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        return HeartRateFeedWatermark({"written_before": self._written_before})

    def _handle_coordinator_update(self) -> None:
        if not self._file_samples():
            # Nothing new to file: behave like every other sensor of the entry, so the
            # state and its attributes keep refreshing.
            super()._handle_coordinator_update()

    def _file_samples(self) -> bool:
        """File the backlog past the watermark, oldest first, at most one pass's worth.

        Returns whether at least one sample was filed. Two guards matter here. A sample
        stamped ahead of this clock is left alone: a future row in the history would sort
        in front of everything the dashboard shows, and the watermark stays behind it so a
        later poll files it normally. And the pass is capped - a first run over a wide
        window can hold days of samples, and all of them in one event-loop turn would
        block it; the watermark drains the rest over the polls that follow.
        """
        if self.entity_id is None or not self.available:
            return False
        deadline = time.time() + 300
        # Sorted here on purpose: the budget below keeps the *oldest* slice, and a window
        # handed over newest-first would make that slice the newest samples while the
        # watermark stayed behind everything in between - the backlog would never drain.
        samples = sorted((sample for sample in self._data.hr_samples
                          if self._written_before < sample["at"] <= deadline),
                         key=lambda sample: sample["at"])
        if not samples:
            return False
        backlog = len(samples)
        samples = samples[:HR_SAMPLE_WRITE_LIMIT]
        # One attribute calculation for the whole pass: identical attributes across the
        # samples is what keeps the state machine from treating an attribute-only change
        # as a row of its own.
        attributes = self._async_calculate_state().attributes
        for sample in samples:
            # The sample's own reading, never the newest one: rows that all carry the same
            # number would be a flat line pretending to be history. force_update, because
            # two equal readings in a row are still two measurements and collapsing them
            # would thin the chart again.
            self.hass.states.async_set(self.entity_id, str(sample["bpm"]), attributes,
                                       force_update=True, timestamp=sample["at"])
            self._written_before = sample["at"]
        _LOGGER.debug("Huawei Health %s: filed %d of %d heart-rate samples, newest at %s",
                      self._data.entry.title, len(samples), backlog,
                      datetime.fromtimestamp(samples[-1]["at"])
                      .strftime("%Y-%m-%d %H:%M"))
        return True
