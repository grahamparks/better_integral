"""Numeric integration of data coming from a source sensor over time."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
import logging
from typing import TYPE_CHECKING, Any, Final, Self

import voluptuous as vol

from homeassistant.components.sensor import (
    DEVICE_CLASS_UNITS,
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    RestoreSensor,
    SensorDeviceClass,
    SensorExtraStoredData,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_METHOD,
    CONF_NAME,
    CONF_UNIQUE_ID,
    STATE_UNAVAILABLE,
    UnitOfTime,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    EventStateReportedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.device import async_device_info_to_link_from_entity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import (
    CONF_MAX_SUB_INTERVAL,
    CONF_ROUND_DIGITS,
    CONF_SOURCE_SENSOR,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_UNIT_PREFIX,
    CONF_UNIT_TIME,
    INTEGRATION_METHODS,
    METHOD_LEFT,
    METHOD_RIGHT,
    METHOD_TRAPEZOIDAL,
)

_LOGGER = logging.getLogger(__name__)

ATTR_SOURCE_ID: Final = "source"

# SI Metric prefixes
UNIT_PREFIXES = {None: 1, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}

# SI Time prefixes
UNIT_TIME = {
    UnitOfTime.SECONDS: 1,
    UnitOfTime.MINUTES: 60,
    UnitOfTime.HOURS: 60 * 60,
    UnitOfTime.DAYS: 24 * 60 * 60,
}

DEVICE_CLASS_MAP = {
    SensorDeviceClass.POWER: SensorDeviceClass.ENERGY,
}

DEFAULT_ROUND = 3

PLATFORM_SCHEMA = vol.All(
    cv.removed(CONF_UNIT_OF_MEASUREMENT),
    SENSOR_PLATFORM_SCHEMA.extend(
        {
            vol.Optional(CONF_NAME): cv.string,
            vol.Optional(CONF_UNIQUE_ID): cv.string,
            vol.Required(CONF_SOURCE_SENSOR): cv.entity_id,
            vol.Optional(CONF_ROUND_DIGITS, default=DEFAULT_ROUND): vol.Any(
                None, vol.Coerce(int)
            ),
            vol.Optional(CONF_UNIT_PREFIX): vol.In(UNIT_PREFIXES),
            vol.Optional(CONF_UNIT_TIME, default=UnitOfTime.HOURS): vol.In(UNIT_TIME),
            vol.Remove(CONF_UNIT_OF_MEASUREMENT): cv.string,
            vol.Optional(CONF_MAX_SUB_INTERVAL): cv.positive_time_period,
            vol.Optional(CONF_METHOD, default=METHOD_TRAPEZOIDAL): vol.In(
                INTEGRATION_METHODS
            ),
        }
    ),
)


class _IntegrationMethod(ABC):
    @staticmethod
    def from_name(method_name: str) -> _IntegrationMethod:
        return _NAME_TO_INTEGRATION_METHOD[method_name]()

    @abstractmethod
    def calculate_area_with_two_values(
        self, elapsed_time: Decimal, left: Decimal, right: Decimal
    ) -> Decimal:
        """Calculate area given two values."""

    def calculate_area_with_uniform_value(
        self, elapsed_time: Decimal, uniform_value: Decimal
    ) -> Decimal:
        return uniform_value * elapsed_time


class _Trapezoidal(_IntegrationMethod):
    def calculate_area_with_two_values(
        self, elapsed_time: Decimal, left: Decimal, right: Decimal
    ) -> Decimal:
        return elapsed_time * (left + right) / 2


class _Left(_IntegrationMethod):
    def calculate_area_with_two_values(
        self, elapsed_time: Decimal, left: Decimal, right: Decimal
    ) -> Decimal:
        return self.calculate_area_with_uniform_value(elapsed_time, left)


class _Right(_IntegrationMethod):
    def calculate_area_with_two_values(
        self, elapsed_time: Decimal, left: Decimal, right: Decimal
    ) -> Decimal:
        return self.calculate_area_with_uniform_value(elapsed_time, right)



def _get_decimal_value_from_state(state: str) -> Decimal | None:
    try:
        return Decimal(state)
    except (InvalidOperation, TypeError):
        return None


_NAME_TO_INTEGRATION_METHOD: dict[str, type[_IntegrationMethod]] = {
    METHOD_LEFT: _Left,
    METHOD_RIGHT: _Right,
    METHOD_TRAPEZOIDAL: _Trapezoidal,
}



@dataclass
class IntegrationSensorExtraStoredData(SensorExtraStoredData):
    """Object to hold extra stored data."""

    source_entity: str | None
    last_valid_total: Decimal | None
    last_integration_time: Decimal | None
    last_source_value: Decimal | None

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the utility sensor data."""
        data = super().as_dict()
        data["source_entity"] = self.source_entity
        data["last_valid_state"] = (
            str(self.last_valid_total) if self.last_valid_total else None
        )
        data["last_integration_time"] = (
            self.last_integration_time.isoformat() if self.last_integration_time else None
        )
        data["last_source_value"] = (
            str(self.last_source_value) if self.last_source_value else None
        )
        return data

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self | None:
        """Initialize previous integration state from a dict."""

        _LOGGER.debug(
            "from_dict = %s", restored
        )


        extra = SensorExtraStoredData.from_dict(restored)
        if extra is None:
            return None

        source_entity = restored.get(ATTR_SOURCE_ID)

        try:
            last_valid_total = (
                Decimal(str(restored.get("last_valid_state")))
                if restored.get("last_valid_state")
                else None
            )
        except InvalidOperation:
            # last_period is corrupted
            _LOGGER.error("Could not use last_valid_state")
            return None

        if last_valid_total is None:
            return None

            
        try:
            last_integration_time = (
                datetime.fromisoformat(restored.get("last_integration_time"))
                if restored.get("last_integration_time")
                else None
            )
        except (ValueError, InvalidOperation):
            last_integration_time = None
            pass

        try:
            last_source_value = (
                Decimal(str(restored.get("last_source_value")))
                if restored.get("last_source_value")
                else None
            )
        except (ValueError, InvalidOperation):
            last_source_value = None
            pass

        return cls(
            extra.native_value,
            extra.native_unit_of_measurement,
            source_entity,
            last_valid_total,
            last_integration_time,
            last_source_value
        )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Initialize Integration - Riemann sum integral config entry."""
    registry = er.async_get(hass)
    # Validate + resolve entity registry id to entity_id
    source_entity_id = er.async_validate_entity_id(
        registry, config_entry.options[CONF_SOURCE_SENSOR]
    )

    device_info = async_device_info_to_link_from_entity(
        hass,
        source_entity_id,
    )

    if (unit_prefix := config_entry.options.get(CONF_UNIT_PREFIX)) == "none":
        # Before we had support for optional selectors, "none" was used for selecting nothing
        unit_prefix = None

    if max_sub_interval_dict := config_entry.options.get(CONF_MAX_SUB_INTERVAL, None):
        max_sub_interval = cv.time_period(max_sub_interval_dict)
    else:
        max_sub_interval = None

    round_digits = config_entry.options.get(CONF_ROUND_DIGITS)
    if round_digits:
        round_digits = int(round_digits)

    integral = IntegrationSensor(
        integration_method=config_entry.options[CONF_METHOD],
        name=config_entry.title,
        round_digits=round_digits,
        source_entity=source_entity_id,
        unique_id=config_entry.entry_id,
        unit_prefix=unit_prefix,
        unit_time=config_entry.options[CONF_UNIT_TIME],
        device_info=device_info,
        max_sub_interval=max_sub_interval,
    )

    async_add_entities([integral])


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the integration sensor."""
    integral = IntegrationSensor(
        integration_method=config[CONF_METHOD],
        name=config.get(CONF_NAME),
        round_digits=config.get(CONF_ROUND_DIGITS),
        source_entity=config[CONF_SOURCE_SENSOR],
        unique_id=config.get(CONF_UNIQUE_ID),
        unit_prefix=config.get(CONF_UNIT_PREFIX),
        unit_time=config[CONF_UNIT_TIME],
        max_sub_interval=config.get(CONF_MAX_SUB_INTERVAL),
    )

    async_add_entities([integral])


class IntegrationSensor(RestoreSensor):
    """Representation of an integration sensor."""

    _attr_state_class = SensorStateClass.TOTAL
    _attr_should_poll = False

    def __init__(
        self,
        *,
        integration_method: str,
        name: str | None,
        round_digits: int | None,
        source_entity: str,
        unique_id: str | None,
        unit_prefix: str | None,
        unit_time: UnitOfTime,
        max_sub_interval: timedelta | None,
        device_info: DeviceInfo | None = None,
    ) -> None:
        """Initialize the integration sensor."""
        self._attr_unique_id = unique_id
        self._sensor_source_id = source_entity
        self._round_digits = round_digits
        self._integration_total: Decimal = 0
        self._method = _IntegrationMethod.from_name(integration_method)

        self._attr_name = name if name is not None else f"{source_entity} integral"
        self._unit_prefix_string = "" if unit_prefix is None else unit_prefix
        self._unit_of_measurement: str | None = None
        self._unit_prefix = UNIT_PREFIXES[unit_prefix]
        self._unit_time = UNIT_TIME[unit_time]
        self._unit_time_str = unit_time
        self._attr_icon = "mdi:chart-histogram"
        self._source_entity: str = source_entity
        self._attr_device_info = device_info
        self._max_sub_interval: timedelta | None = (
            None  # disable time based integration
            if max_sub_interval is None or max_sub_interval.total_seconds() == 0
            else max_sub_interval
        )
        self._max_sub_interval_exceeded_callback: CALLBACK_TYPE = lambda *args: None
        self._last_integration_time: datetime = datetime.now(tz=UTC)
        self._last_source_value: Decimal | None = None
        self._attr_suggested_display_precision = round_digits or 2

    def _calculate_unit(self, source_unit: str) -> str:
        """Multiply source_unit with time unit of the integral.

        Possibly cancelling out a time unit in the denominator of the source_unit.
        Note that this is a heuristic string manipulation method and might not
        transform all source units in a sensible way.

        Examples:
        - Speed to distance: 'km/h' and 'h' will be transformed to 'km'
        - Power to energy: 'W' and 'h' will be transformed to 'Wh'

        """
        unit_time = self._unit_time_str
        if source_unit.endswith(f"/{unit_time}"):
            integral_unit = source_unit[0 : (-(1 + len(unit_time)))]
        else:
            integral_unit = f"{source_unit}{unit_time}"

        return f"{self._unit_prefix_string}{integral_unit}"

    def _calculate_device_class(
        self,
        source_device_class: SensorDeviceClass | None,
        unit_of_measurement: str | None,
    ) -> SensorDeviceClass | None:
        """Deduce device class if possible from source device class and target unit."""
        if source_device_class is None:
            return None

        if (device_class := DEVICE_CLASS_MAP.get(source_device_class)) is None:
            return None

        if unit_of_measurement not in DEVICE_CLASS_UNITS.get(device_class, set()):
            return None
        return device_class

    def _derive_and_set_attributes_from_state(self, source_state: State) -> None:
        source_unit = source_state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if source_unit is not None:
            self._unit_of_measurement = self._calculate_unit(source_unit)
        else:
            # If the source has no defined unit we cannot derive a unit for the integral
            self._unit_of_measurement = None

        self._attr_device_class = self._calculate_device_class(
            source_state.attributes.get(ATTR_DEVICE_CLASS), self.unit_of_measurement
        )
        if self._attr_device_class:
            self._attr_icon = None  # Remove this sensors icon default and allow to fallback to the device class default
        else:
            self._attr_icon = "mdi:chart-histogram"

    def _update_integral(self, area: Decimal) -> None:
        area_scaled = area / (self._unit_prefix * self._unit_time)
        if isinstance(self._integration_total, Decimal):
            self._integration_total += area_scaled
        else:
            self._integration_total = area_scaled
        _LOGGER.debug(
            "area = %s, area_scaled = %s new total = %s", area, area_scaled, self._integration_total
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()

        if (last_sensor_data := await self.async_get_last_sensor_data()) is not None:
            self._integration_total = last_sensor_data.last_valid_total if last_sensor_data.last_valid_total is not None else 0
            self._attr_native_value = last_sensor_data.native_value
            self._unit_of_measurement = last_sensor_data.native_unit_of_measurement
            self._last_source_value = last_sensor_data.last_source_value
            
            # Restore last integration time as long as it is in the past
            if last_sensor_data.last_integration_time is not None and last_sensor_data.last_integration_time < self._last_integration_time:
                self._last_integration_time = last_sensor_data.last_integration_time
                _LOGGER.debug(
                     "Restored _last_integration_time %s",
                     self._last_integration_time
                )

            _LOGGER.debug(
                "Restored total %s and last value %s",
                self._integration_total,
                self._last_source_value
            )
        else:
            _LOGGER.debug("Unable to restore previous data")

        if self._max_sub_interval is not None:
            source_state = self.hass.states.get(self._sensor_source_id)
            self._schedule_max_sub_interval_exceeded_if_state_is_numeric(source_state)
            self.async_on_remove(self._cancel_max_sub_interval_exceeded_callback)

        if (
            state := self.hass.states.get(self._source_entity)
        ) and state.state != STATE_UNAVAILABLE:
            self._derive_and_set_attributes_from_state(state)

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                self._sensor_source_id,
                self._integrate_on_state_change_callback,
            )
        )
        self.async_on_remove(
            async_track_state_report_event(
                self.hass,
                self._sensor_source_id,
                self._integrate_on_state_report_callback,
            )
        )

    @callback
    def _integrate_on_state_change_callback(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Handle sensor state update when sub interval is configured."""
        _LOGGER.debug("_integrate_on_state_change_callback triggered %s", self._sensor_source_id)

        self._integrate_on_state_update(event.data["new_state"])

    @callback
    def _integrate_on_state_report_callback(
        self, event: Event[EventStateReportedData]
    ) -> None:
        """Handle sensor state report when sub interval is configured."""
        _LOGGER.debug("_integrate_on_state_report_callback triggered %s", self._sensor_source_id)

        self._integrate_on_state_update(event.data["new_state"])

    def _integrate_on_state_update(
        self,
        new_state: State | None,
    ) -> None:
        """Integrate based on state change and time.

        Next to doing the integration based on state change this method cancels and
        reschedules time based integration.
        """

        if self._max_sub_interval is not None:
            self._cancel_max_sub_interval_exceeded_callback()

        try:
            self._integrate_on_state_change(new_state)
        except e:
            _LOGGER.error("Error integrating %s", e)
        finally:
            if self._max_sub_interval is not None:
                # When max_sub_interval exceeds without state change the source is assumed
                # constant with the last known state (new_state).
                self._schedule_max_sub_interval_exceeded_if_state_is_numeric(new_state)
        
    def _integrate_on_state_change(
        self,
        new_state: State | None,
    ) -> None:

        if new_state is None:
            _LOGGER.debug("Skipping as sensor state is missing")
            return

        if new_state.state == STATE_UNAVAILABLE:
            _LOGGER.debug("Skipping as sensor is unavailable")
            self._attr_available = False
            self.async_write_ha_state()
            return


        self._attr_available = True
        self._derive_and_set_attributes_from_state(new_state)
        
        start_time = self._last_integration_time
        start_value = self._last_source_value;
        end_time = new_state.last_reported
        end_value = _get_decimal_value_from_state(new_state.state);

        self._update_and_save_new_total(start_time, start_value, end_time, end_value);
        
    def _update_and_save_new_total(self, start_time, start_value, end_time, end_value):

        self._last_integration_time = end_time
        self._last_source_value = end_value

        if start_time is not None and start_value is not None:
            elapsed_seconds = Decimal((end_time - start_time).total_seconds())
        
            _LOGGER.debug(
                "start_time = %s, end_time = %s", start_time, end_time
            )
            _LOGGER.debug(
                "start_value = %s, end_value = %s, elapsed_seconds = %s", start_value, end_value, elapsed_seconds
            )

            area = self._method.calculate_area_with_two_values(elapsed_seconds, start_value, end_value)

            self._update_integral(area)
        else:
            _LOGGER.debug(
                "Skipping because no previous value %s %s", start_time, start_value
            )
            
        
        self.async_write_ha_state()

    def _schedule_max_sub_interval_exceeded_if_state_is_numeric(
        self, source_state: State | None
    ) -> None:
        """Schedule possible integration using the source state and max_sub_interval.

        The callback reference is stored for possible cancellation if the source state
        reports a change before max_sub_interval has passed.

        If the callback is executed, meaning there was no state change reported, the
        source_state is assumed constant and integration is done using its value.
        """
        if (
            self._max_sub_interval is not None
            and source_state is not None
            and (source_state_dec := _get_decimal_value_from_state(source_state.state))
        ):

            @callback
            def _integrate_on_max_sub_interval_exceeded_callback(now: datetime) -> None:
                """Integrate based on time and reschedule."""

                self._derive_and_set_attributes_from_state(source_state)

                start_time = self._last_integration_time
                end_time = now
                
                self._update_and_save_new_total(start_time, source_state_dec, end_time, source_state_dec)

                self._schedule_max_sub_interval_exceeded_if_state_is_numeric(
                    source_state
                )

            self._max_sub_interval_exceeded_callback = async_call_later(
                self.hass,
                self._max_sub_interval,
                _integrate_on_max_sub_interval_exceeded_callback,
            )

    def _cancel_max_sub_interval_exceeded_callback(self) -> None:
        self._max_sub_interval_exceeded_callback()

    @property
    def native_value(self) -> Decimal | None:
        """Return the value of the sensor."""
        if isinstance(self._integration_total, Decimal) and self._round_digits:
            return round(self._integration_total, self._round_digits)
        return self._integration_total

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit the value is expressed in."""
        return self._unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return the state attributes of the sensor."""
        return {
            ATTR_SOURCE_ID: self._source_entity,
        }

    @property
    def extra_restore_state_data(self) -> IntegrationSensorExtraStoredData:
        """Return sensor specific state data to be restored."""
        
        return IntegrationSensorExtraStoredData(
            self.native_value,
            self.native_unit_of_measurement,
            self._source_entity,
            self._integration_total,
            self._last_integration_time,
            self._last_source_value
        )

    async def async_get_last_sensor_data(
        self,
    ) -> IntegrationSensorExtraStoredData | None:
        """Restore Utility Meter Sensor Extra Stored Data."""
          
        if (restored_last_extra_data := await self.async_get_last_extra_data()) is None:
            return None

        return IntegrationSensorExtraStoredData.from_dict(
            restored_last_extra_data.as_dict()
        )
