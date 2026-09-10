"""Replay filter and schedule failures through production polling and HA states.

Run with Home Assistant installed:
    python tests/ha_issues101_102_smoke.py
    python tests/ha_issues101_102_smoke.py --source-root /path/to/baseline --case schedule

The 0.5 identity is applied to the captured 0.3 TwinFresh frame format, not a
second physical capture. Only the wire is simulated; production encoding,
parsing, retries, coordinator updates, entity listeners and HA serialization
remain active. Cycles are accelerated, not a wall-clock soak. No sockets or
device writes are allowed. Repairs are outside this focused test.
"""

import argparse
import asyncio
import importlib
import json
import logging
from pathlib import Path
import sys
import tempfile
import types

from ecovent_test_helpers import packet_with_payload
from ha_issue100_smoke import Wire
from homeassistant.const import __version__
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.entity_component import EntityComponent


class ScheduleWire(Wire):
    """Extend the captured poll fixture with selector-addressed schedule rows."""

    def __init__(self, fan):
        super().__init__(fan)
        self.values[0x0072] = b"\x00"
        self.missing_period = None
        self.schedule_calls = []
        self.first_end_hour = 6

    def send(self, data):
        assert data[:2] == self.fan.func["read"], "unexpected device write"
        raw = bytes.fromhex(data[2:])
        page, size, pos = 0, 1, 0
        ids, payload = [], bytearray()
        while pos < len(raw):
            code = raw[pos]
            pos += 1
            if code == 0xFF:
                page = raw[pos]
                pos += 1
                continue
            if code == 0xFE:
                size = raw[pos]
                pos += 1
                continue
            pid = page * 256 + code
            ids.append(pid)
            if pid == 0x0077:
                assert size == 2
                day, period = raw[pos : pos + size]
                pos += size
                self.schedule_calls.append((day, period))
                value = bytes(
                    [
                        day,
                        period,
                        1,
                        15,
                        0,
                        {1: self.first_end_hour, 2: 9, 3: 19, 4: 0}[period],
                    ]
                )
                if period == self.missing_period:
                    size = 1
                    continue
            else:
                assert size == 1, "unexpected read selector"
                value = self.values.get(pid)
            size = 1
            if pid in self.omit or (len(raw) > 1 and pid in self.bulk_omit):
                continue
            if pid in self.reject:
                payload.extend([0xFF, page, 0xFD, code])
            elif value is not None:
                payload.extend([0xFF, page])
                if len(value) > 1:
                    payload.extend([0xFE, len(value)])
                payload.append(code)
                payload.extend(value)
        self.calls.append(ids)
        self.pending = (
            packet_with_payload(payload, device_id=self.fan.id.encode())
            if payload
            else False
        )
        return not self.offline


def sensor_for_method(module, hass, entry, method):
    spec = next(spec for spec in module.SENSOR_SPECS if spec.method == method)
    return module.VentoSensor(
        hass,
        entry,
        spec.key,
        spec.name,
        spec.method,
        spec.native_unit_of_measurement,
        spec.device_class,
        spec.state_class,
        spec.entity_category,
        spec.enable_by_default,
        spec.icon,
        translation_key=spec.translation_key,
        suggested_display_precision=spec.suggested_display_precision,
    )


async def run_case(source_root, case, missing_period=None, cold_filter=False):
    package = types.ModuleType("issues101_102_ecovent")
    package.__path__ = [str(source_root / "custom_components/ecovent_v2")]
    sys.modules[package.__name__] = package
    coordinator_type = importlib.import_module(
        package.__name__ + ".coordinator"
    ).EcoVentCoordinator
    sensors = importlib.import_module(package.__name__ + ".sensor")

    with tempfile.TemporaryDirectory(prefix="ecovent-ha-101-102-") as tmp:
        hass = HomeAssistant(tmp)
        entry = types.SimpleNamespace(
            data={
                "ip_address": "192.0.2.1",
                "password": "1111",
                "name": "Issue 101/102 audit",
                "auto_clock_sync": False,
            },
            unique_id=None,
            entry_id="issues101_102",
            async_on_unload=lambda _: None,
            pref_disable_polling=True,
        )
        device_registry.async_setup(hass)
        await device_registry.async_load(hass)
        await entity_registry.async_load(hass)
        co = coordinator_type(hass, entry)
        fan = co._fan
        wire = ScheduleWire(fan)
        fan.send, fan.receive = wire.send, wire.receive
        co._update_hardware_profile_mismatch_repair_issue = lambda: None
        if case == "firmware":
            wire.values[0x0086] = bytes.fromhex("00050a07e807")
        if case == "schedule":
            wire.missing_period = missing_period
        if cold_filter:
            wire.omit.add(0x0064)
        await co.async_refresh()
        assert co.last_update_success, co.last_exception
        hass.data["ecovent_v2"] = {entry.entry_id: co}
        countdown = sensor_for_method(sensors, hass, entry, "filter_timer_countdown")
        remaining = sensor_for_method(sensors, hass, entry, "filter_remaining")
        schedule = sensors.WeeklyScheduleSummarySensor(hass, entry)
        component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
        await component.async_add_entities([countdown, remaining, schedule])
        await hass.async_block_till_done()

        def state(entity):
            return hass.states.get(entity.entity_id).state

        async def refresh(full=False):
            if full:
                co.updateCounter = 3
            wire.calls.clear()
            wire.schedule_calls.clear()
            await co.async_refresh()
            await hass.async_block_till_done()
            assert co.last_update_success, co.last_exception

        async def targeted_filter():
            await hass.async_add_executor_job(fan.get_param, "filter_timer_countdown")
            co.async_update_listeners()
            await hass.async_block_till_done()

        if case == "schedule":
            assert not co._weekly_schedule
            schedule_refreshes = []
            for counter in range(2, 22):
                if counter == 11:
                    wire.missing_period = None
                await refresh()
                assert co.updateCounter == counter
                if wire.schedule_calls:
                    schedule_refreshes.append(counter)
                assert bool(wire.schedule_calls) == (counter in (10, 20)), (
                    "unbounded or missing schedule retry",
                    counter,
                    wire.schedule_calls,
                )
                lengths = [
                    len(day["periods"])
                    for day in hass.states.get(schedule.entity_id).attributes["days"]
                ]
                assert lengths == ([0] * 7 if counter < 20 else [4] * 7), lengths
            assert state(schedule) == "Disabled"
            cached = hass.states.get(schedule.entity_id).attributes["days"]
            wire.missing_period = missing_period
            wire.values[0x0072] = b"\x01"
            wire.first_end_hour = 7
            co.updateCounter = 29
            await refresh()
            assert wire.schedule_calls
            assert hass.states.get(schedule.entity_id).attributes["days"] == cached
            wire.missing_period = None
            co.updateCounter = 39
            await refresh()
            assert hass.states.get(schedule.entity_id).attributes["days"] != cached
            assert len(co._weekly_schedule) == 7
            result = {
                "missing_period": missing_period,
                "cold_retry_cycles": schedule_refreshes,
                "recovered_days": len(co._weekly_schedule),
            }
        else:
            if cold_filter:
                assert state(countdown) == state(remaining) == "unknown"
                wire.omit.clear()
                for _ in range(12):
                    await refresh(full=True)
                    if state(countdown) != "unknown":
                        break
            initial = state(countdown), state(remaining)
            assert all(value not in ("unknown", "unavailable") for value in initial), (
                initial
            )
            assert (
                hass.states.get(remaining.entity_id).attributes["unit_of_measurement"]
                == "%"
            )
            wire.omit.add(0x0064)
            wire.bulk_omit.add(0x0064)
            retry_cycles = []
            for cycle in range(12):
                await refresh(full=True)
                assert (state(countdown), state(remaining)) == initial
                if [0x0064] in wire.calls:
                    retry_cycles.append(cycle)
            assert 1 <= len(retry_cycles) <= 2, retry_cycles
            wire.omit.clear()
            wire.values[0x0064] = bytes.fromhex("00009600")
            for recovery_cycle in range(12):
                await refresh(full=True)
                if state(countdown) != initial[0]:
                    break
            assert state(countdown) not in (initial[0], "unknown", "unavailable")
            wire.bulk_omit.clear()
            wire.values[0x0064] = b"\x01\x02"
            await refresh(full=True)
            assert state(countdown) == state(remaining) == "unknown"
            wire.values[0x0064] = bytes.fromhex("00009600")
            await targeted_filter()
            assert state(countdown) not in ("unknown", "unavailable")
            wire.values[0x0064] = b"\x01\x02"
            await targeted_filter()
            assert state(countdown) == state(remaining) == "unknown"
            wire.values[0x0064] = bytes.fromhex("00009600")
            await targeted_filter()
            wire.reject.add(0x0064)
            await refresh(full=True)
            assert state(countdown) == state(remaining) == "unknown"
            assert 0x0064 in fan.unsupported_optional_poll_parameter_ids()
            result = {
                "cold_start": cold_filter,
                "initial": initial,
                "omission_cycles": 12,
                "targeted_retry_cycles": retry_cycles,
                "recovery_cycle": recovery_cycle,
            }

        assert fan.audible_write_command_count == 0
        print(
            json.dumps({"ha": __version__, "case": case, **result, "device_writes": 0})
        )
        await co.async_shutdown()
        await hass.async_stop()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--case", choices=("all", "firmware", "filter", "schedule"), default="all"
    )
    args = parser.parse_args()
    if args.case in ("all", "firmware"):
        await run_case(args.source_root, "firmware")
    if args.case in ("all", "filter"):
        for cold in (False, True):
            await run_case(args.source_root, "filter", cold_filter=cold)
    if args.case in ("all", "schedule"):
        for period in range(1, 5):
            await run_case(args.source_root, "schedule", missing_period=period)


if __name__ == "__main__":
    asyncio.run(main())
