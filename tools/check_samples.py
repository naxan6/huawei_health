#!/usr/bin/env python3
"""Check the fine-grained heart-rate feed without a running Home Assistant.

Four things are easy to break and hard to notice, so they are checked here against the
real Home Assistant objects instead of stubs:

1. the sample walker only accepts "a moment plus a plausible heart rate" carried by the
   same object, and never turns a day aggregate into a sample with an invented timestamp;
2. the millisecond *ByTime window is asked for as a real range, and a rejected window
   reports both verdicts rather than only the last;
3. the coordinator carries the whole window plus the spacing measured from it;
4. the sensor files one state row per sample **at the sample's own moment**, and its
   watermark keeps a restart or a repeated poll from filing a sample twice.

The last point is the reason the feed exists at all: Home Assistant's history holds
nothing but states that were written, so one write per poll would cap the chart at one
point per update interval however often the band sampled.

Runs on an interpreter that has Home Assistant installed (a development checkout, or the
HA venv); the integration itself needs nothing beyond the standard library::

    python3 tools/check_samples.py            # from the checkout root

Exits non-zero on the first failed check.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, "custom_components")

from huawei_health import sensor as sensor_module  # noqa: E402
from huawei_health.client import (  # noqa: E402
    HuaweiApiError,
    HuaweiHealthClient,
    Tokens,
    heart_rate_pairs,
    sample_spacing_minutes,
)
from huawei_health.const import (  # noqa: E402
    CONF_HR_SAMPLES,
    CONF_HR_WINDOW_DAYS,
    HR_SAMPLE_STORE_LIMIT,
)
from huawei_health.coordinator import (  # noqa: E402
    HuaweiHealthCoordinator,
    HuaweiHealthData,
)

FAILURES: list[str] = []


def check(condition: bool, what: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'}  {what}")
    if not condition:
        FAILURES.append(what)


def description(key: str) -> sensor_module.HuaweiSensorDescription:
    return next(d for d in sensor_module.DESCRIPTIONS if d.key == key)


def walker_checks(now: float) -> None:
    print("\n== the sample walker ==")
    payload = {"data": {"samplePoints": [
        {"key": "DATA_POINT_DYNAMIC_HEARTRATE", "startTime": int(now * 1000),
         "value": 72},
        {"key": "DATA_POINT_DYNAMIC_HEARTRATE", "startTime": int((now - 300) * 1000),
         "value": 68},
        # implausible reading: an id or a duration, not a heart rate
        {"key": "OTHER", "startTime": int((now - 600) * 1000), "value": 9999},
        # too short to be a moment: a row id, not a stamp
        {"key": "OTHER", "startTime": 123456, "value": 70},
    ]}}
    pairs = sorted(heart_rate_pairs(payload))
    check([bpm for _, bpm in pairs] == [68, 72],
          f"pairs are the two plausible readings, oldest first: {pairs}")
    check(len(pairs) == 2, f"implausible values and stamps are dropped: {pairs}")

    # The getHealthStat shape carries no moment at all, so the sample path cannot quietly
    # invent a timestamp for a day aggregate.
    day_rows = {"heartRateTotal": [{"recordDay": 20261110,
                                    "heartRateBasic": {"lastHeartRate": 71,
                                                       "avgRestingHeartRate": 61}}]}
    check(list(heart_rate_pairs(day_rows)) == [],
          "a day aggregate yields no samples")

    # The app's own export naming, with seconds and nanosecond stamps side by side.
    nested = {"result": {"healthData": [{"type": 7, "samplePoint": [
        {"startTime": now - 120, "value": 64},
        {"startTime": int((now - 60) * 1e9), "value": 66},
    ]}]}}
    nested_pairs = sorted(heart_rate_pairs(nested))
    check([bpm for _, bpm in nested_pairs] == [64, 66],
          f"seconds and nanosecond stamps both parse: {nested_pairs}")

    spacing = sample_spacing_minutes(
        [{"at": now - 300 * (n + 1), "bpm": 60 + n} for n in range(30)])
    check(spacing.get("samples") == 30 and spacing.get("gap_min_median") == 5.0,
          f"spacing is measured from the samples: {spacing}")
    check(spacing.get("gap_min_min") == 5.0 and spacing.get("gap_min_max") == 5.0,
          f"a newest-first window still gives positive gaps: {spacing}")
    check(spacing.get("per_day") == 297.9,
          f"per-day density reported: {spacing.get('per_day')}")
    check(sample_spacing_minutes([]) == {"samples": 0}, "no samples, no gaps")


def endpoint_checks() -> None:
    print("\n== the sample endpoint is asked in both time formats ==")
    seen: list[dict] = []

    class RecordingClient(HuaweiHealthClient):
        def post(self, path, extra=None):
            seen.append(dict(extra or {}))
            raise HuaweiApiError(1001, path, "window too wide")

        def rotate(self):
            return None

    client = RecordingClient(Tokens(access_token="t"), host="https://x")
    try:
        client.health_data(20261001, 20261031, 7)
        check(False, "the second verdict is meant to fail too and reach the reader")
    except HuaweiApiError as err:
        check("milliseconds" in str(err) and "YYYYMMDD" in str(err),
              f"both verdicts reach the reader, not only the last: {err}")
    check([body.get("queryType") for body in seen] == [2, 1],
          f"milliseconds first, the verified day form second: "
          f"{[body.get('queryType') for body in seen]}")
    first = seen[0]
    check(first["startTime"] < first["endTime"],
          "the millisecond window is a range, not a single instant")
    span_days = (first["endTime"] - first["startTime"]) / 86400000
    check(29 <= span_days <= 32,
          f"a month window spans days, not seconds: {span_days:.2f}")
    check(seen[1]["startTime"] == 20261001 and seen[1]["endTime"] == 20261031,
          f"the day form keeps the day numbers: {seen[1]['startTime']}-"
          f"{seen[1]['endTime']}")


class FakeClient:
    """Answers the reads ``_gather`` performs, with no network."""

    host = "https://sportdata-dre.things.dbankcloud.com"
    data_source = 2
    device_code = None

    def __init__(self, samples: list[dict]) -> None:
        self.samples = samples

    def daily_summary(self, days: int = 3) -> list[dict]:
        return []

    def health_summary(self, days: int = 3, **kwargs) -> list[dict]:
        return []

    def sessions(self, days: int = 7) -> list[dict]:
        return []

    def heart_rate_samples(self, days: int = 3, type_id: int = 7) -> list[dict]:
        return list(self.samples)

    def device_codes(self) -> list[str]:
        return []

    def sync_ledger(self) -> list[str]:
        return []


def fake_coordinator(samples: list[dict]) -> SimpleNamespace:
    """A stand-in with just what ``_gather`` touches, running the real body."""
    coordinator = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="e1", title="andrey", options={
            CONF_HR_SAMPLES: True, CONF_HR_WINDOW_DAYS: 3}),
        client=FakeClient(samples),
        last_error=None,
        data=None,
        last_update_success=True,
    )
    coordinator._gather = HuaweiHealthCoordinator._gather.__get__(coordinator)
    return coordinator


def coordinator_checks(now: float) -> None:
    print("\n== the coordinator snapshot ==")
    samples = [{"at": now - 300 * (n + 1), "bpm": 60 + n} for n in range(12)][::-1]
    snapshot = fake_coordinator(samples)._gather()

    check(len(snapshot["hr_samples"]) == 12,
          f"the whole window survives into the snapshot: "
          f"{len(snapshot['hr_samples'])} rows")
    check(snapshot["hr_spacing"].get("samples") == 12
          and snapshot["hr_spacing"].get("gap_min_median") == 5.0,
          f"the measured spacing rides along: {snapshot['hr_spacing']}")
    check(snapshot["failures"] == [],
          f"the sample read is not reported as a failure: {snapshot['failures']}")
    check(snapshot["hr_dropped"] == 0,
          "an ordinary window loses nothing to the memory guard")

    # A pathologically large answer is capped for memory, but the loss has to be visible.
    huge = [{"at": now - 30 * n, "bpm": 60 + n % 160}
            for n in range(HR_SAMPLE_STORE_LIMIT + 7)][::-1]
    capped = fake_coordinator(huge)._gather()
    check(len(capped["hr_samples"]) == HR_SAMPLE_STORE_LIMIT,
          f"the guard caps the snapshot: {len(capped['hr_samples'])} rows")
    check(capped["hr_dropped"] == 7,
          f"and says what it cut: {capped['hr_dropped']}")
    check(data_for(fake_coordinator(samples), {}).hr_dropped == 0,
          "the entity reads the guard's verdict through the data holder")

    # An empty cloud answer stays explainable instead of becoming a failed update: the
    # reader has to see which centre answered with nothing.
    empty = fake_coordinator([])._gather()
    check("answered nothing" in (empty["notice"] or ""),
          f"an empty answer produces the explanation: {empty['notice']}")
    check(description("heart_rate_samples").sample_feed,
          "the feed entity is the one that files samples")


def data_for(coordinator, options: dict) -> HuaweiHealthData:
    return HuaweiHealthData(
        entry=SimpleNamespace(entry_id="e1", title="andrey", options=options),
        client=coordinator.client, tokens=Tokens(), coordinator=coordinator,
        options=dict(options))


async def feed_checks(now: float) -> None:
    print("\n== the feed entity against the real state machine ==")
    from homeassistant.core import HomeAssistant

    hass = HomeAssistant(tempfile.mkdtemp())
    stamps = [now - 1500 + 300 * n for n in range(4)]    # four samples, 5 min apart
    samples = [{"at": at, "bpm": 61 + n} for n, at in enumerate(stamps)]
    coordinator = fake_coordinator(samples)
    coordinator.data = {"hr_samples": samples,
                        "hr_spacing": sample_spacing_minutes(samples),
                        "failures": [], "notice": None}
    entity = sensor_module.HuaweiHeartRateFeedSensor(
        description("heart_rate_samples"),
        data_for(coordinator, {CONF_HR_SAMPLES: True}))
    from homeassistant.helpers.entity import EntityPlatformState
    try:
        entity.hass = hass
        entity.entity_id = "sensor.heart_rate_samples"
        # The thinnest platform that satisfies the translation-key lookups the sensor base
        # class performs; a real platform fills the same attributes.
        entity.platform = SimpleNamespace(
            platform_name="huawei_health", domain="sensor",
            default_language_platform_translations={},
            platform_translations={}, component_translations={},
            object_id_component_translations={}, object_id_platform_translations={})
        entity._platform_state = EntityPlatformState.ADDED

        entity._file_samples()
        state = hass.states.get(entity.entity_id)
        check(state is not None and state.state == "64",
              f"the newest sample became the state: {state and state.state}")
        check(state is not None
              and state.attributes.get("unit_of_measurement") == "bpm",
              f"the unit is carried: {state and state.attributes.get('unit_of_measurement')}")
        check(abs(entity._written_before - stamps[-1]) < 1e-6,
              f"the watermark sits on the newest sample, not on the poll clock: "
              f"{entity._written_before:.1f} vs {stamps[-1]:.1f}")
        check(state is not None
              and abs(state.last_updated.timestamp() - stamps[-1]) < 1e-6,
              f"the row carries the sample's moment, not the poll time: "
              f"{state and state.last_updated.isoformat()}")

        # The watermark, not the wall clock: a pass that files nothing must not push it to
        # "now", or samples the cloud hands over afterwards would be skipped forever.
        entity._handle_coordinator_update()
        entity._file_samples()
        check(abs(entity._written_before - stamps[-1]) < 1e-6,
              "a repeat of the same window files nothing twice and keeps the watermark")

        # Late arrivals past the watermark are filed at their own moments; a stamp ahead of
        # this clock waits, because a future row would sort in front of the dashboard.
        extra = [{"at": now - 900, "bpm": 70}, {"at": now + 10000, "bpm": 99}]
        coordinator.data = {"hr_samples": samples + extra,
                            "hr_spacing": {}, "failures": [], "notice": None}
        entity._file_samples()
        check(hass.states.get(entity.entity_id).state == "64",
              "a sample at or below the watermark is not filed a second time")
        newer = [{"at": now - 120, "bpm": 70}, {"at": now + 10000, "bpm": 99}]
        coordinator.data = {"hr_samples": samples + extra + newer,
                            "hr_spacing": {}, "failures": [], "notice": None}
        entity._file_samples()
        state = hass.states.get(entity.entity_id)
        check(state.state == "70",
              f"the sample the cloud handed over late became the state: {state.state}")
        check(abs(state.last_updated.timestamp() - (now - 120)) < 1e-6,
              f"its row carries its own moment: {state.last_updated.isoformat()}")
        check(entity._written_before < now,
              f"a stamp ahead of this clock is left for a later poll: watermark "
              f"{entity._written_before:.0f}")

        # One pass is capped, so a first wide window cannot block the event loop for long.
        backlogged = [{"at": now - 60 * n, "bpm": 55 + n % 90}
                      for n in range(sensor_module.HR_SAMPLE_WRITE_LIMIT + 500)][::-1]
        coordinator.data = {"hr_samples": backlogged, "hr_spacing": {},
                            "failures": [], "notice": None}
        entity._written_before = 0.0
        entity._file_samples()
        left = [s for s in backlogged if s["at"] > entity._written_before]
        check(len(backlogged) - len(left) == sensor_module.HR_SAMPLE_WRITE_LIMIT,
              f"the pass filed exactly one budget: "
              f"{len(backlogged) - len(left)} of {len(backlogged)}")
        check(len(left) == 500, f"the rest waits for the next poll: {len(left)} rows")
        entity._file_samples()
        left = [s for s in backlogged if s["at"] > entity._written_before]
        check(len(left) == 100,
              f"the next pass files another budget, never more: {len(left)} left")
        entity._file_samples()
        check(abs(entity._written_before - backlogged[-1]["at"]) < 1e-6,
              "the last pass drains the backlog and lands on its newest sample")

        # The watermark is what survives a restart, through the restore helper.
        stored = entity.extra_restore_state_data
        check(isinstance(stored, sensor_module.HeartRateFeedWatermark)
              and stored.written_before == entity._written_before,
              f"the watermark round-trips through as_dict: {stored.as_dict()}")
        check(sensor_module.HeartRateFeedWatermark(
            {"written_before": now - 5}).written_before == now - 5,
              "a restored watermark reads back as a float")
        check(sensor_module.HeartRateFeedWatermark({}).written_before == 0.0,
              "a first run has no watermark yet")

        # A sample read the cloud refuses must not take the entity down.
        before = hass.states.get(entity.entity_id).state
        coordinator.data = {"hr_samples": [], "hr_spacing": {},
                            "failures": ["heart_rate_samples: https://x "
                                         "(resultCode 1001) - window too wide"],
                            "notice": None}
        entity._file_samples()
        check(hass.states.get(entity.entity_id).state == before,
              f"a filing pass over an empty window writes nothing: still {before}")
        entity._handle_coordinator_update()
        state = hass.states.get(entity.entity_id)
        check(state.state == "unknown",
              f"an update over an empty window reads as unknown, not as a stale number: "
              f"{state.state}")
        check(state.attributes.get("read_errors"),
              f"the refusal is readable on the sensor itself: "
              f"{state.attributes.get('read_errors')}")
        check("resultCode 1001" in " ".join(state.attributes.get("read_errors") or []),
              "the cloud's verdict reaches the attributes, not only the log")
    finally:
        await hass.async_stop()


async def main() -> int:
    now = time.time()
    walker_checks(now)
    endpoint_checks()
    coordinator_checks(now)
    await feed_checks(now)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
