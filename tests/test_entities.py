"""Tests for the sensors, binary sensors, calendar and event entities."""

from datetime import date, timedelta
from unittest.mock import AsyncMock

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.guesty_owners.const import (
    STATS_UPDATE_INTERVAL,
    STAYS_UPDATE_INTERVAL,
)

from .conftest import (
    LISTING_ID,
    STAY_CURRENT,
    STAY_FAR,
    STAY_NEXT,
    FakePortal,
    advance,
    reservation,
)

HOME = "harbor_loft"


def state(hass: HomeAssistant, entity_id: str) -> str:
    found = hass.states.get(entity_id)
    assert found is not None, f"{entity_id} does not exist"
    return found.state


def attributes(hass: HomeAssistant, entity_id: str) -> dict:
    found = hass.states.get(entity_id)
    assert found is not None, f"{entity_id} does not exist"
    return dict(found.attributes)


async def test_stay_sensors(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """Noon on a Wednesday with a guest in the house until Friday."""
    assert state(hass, f"sensor.{HOME}_occupancy") == "guest"
    current = attributes(hass, f"sensor.{HOME}_occupancy")
    assert current["reservation_id"] == STAY_CURRENT
    assert current["guest_name"] == "Alex Example"
    assert current["source"] == "Airbnb"
    assert current["guests"] == 4
    assert current["check_out"] == "2026-06-12T17:00:00+00:00"

    # Friday 16:00 where the listing is.
    assert state(hass, f"sensor.{HOME}_next_check_in") == "2026-06-12T23:00:00+00:00"
    upcoming = attributes(hass, f"sensor.{HOME}_next_check_in")
    assert upcoming["reservation_id"] == STAY_NEXT
    assert upcoming["source"] == "Vrbo"
    assert upcoming["pets"] == 1  # only the reservation list knows about pets

    # The guest in the house leaves before the next one arrives.
    assert state(hass, f"sensor.{HOME}_next_check_out") == "2026-06-12T17:00:00+00:00"
    assert state(hass, f"sensor.{HOME}_upcoming_stays") == "3"
    assert state(hass, f"sensor.{HOME}_booked_nights_next_30_days") == "5"
    assert state(hass, f"sensor.{HOME}_availability_tonight") == "booked"
    assert state(hass, f"sensor.{HOME}_nightly_rate") == "160.0"
    assert attributes(hass, f"sensor.{HOME}_nightly_rate")["unit_of_measurement"] == (
        "USD"
    )
    assert state(hass, f"sensor.{HOME}_minimum_nights") == "3"


async def test_stats_sensors(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    assert state(hass, f"sensor.{HOME}_owner_revenue_this_month") == "1250.5"
    assert state(hass, f"sensor.{HOME}_owner_revenue_this_year") == "1250.5"
    # The manager hides this tile; the portal sends "N/A".
    assert state(hass, f"sensor.{HOME}_net_rental_income_this_month") == STATE_UNKNOWN
    # These two come from the service the portal's Analytics page reads.
    assert state(hass, f"sensor.{HOME}_occupancy_rate_this_month") == "35.0"
    assert state(hass, f"sensor.{HOME}_booked_nights_this_month") == "64"
    assert state(hass, f"sensor.{HOME}_average_nightly_rate_this_month") == "185.25"
    assert state(hass, f"sensor.{HOME}_guest_rating") == "4.65"
    assert state(hass, f"sensor.{HOME}_guest_reviews") == "20"
    assert state(hass, f"sensor.{HOME}_latest_review") == "2026-05-20T09:30:00+00:00"

    revenue = attributes(hass, f"sensor.{HOME}_owner_revenue_this_month")
    assert revenue["unit_of_measurement"] == "USD"
    assert revenue["device_class"] == "monetary"


async def test_stats_fall_back_to_the_revenue_tiles(
    hass: HomeAssistant, config_entry: MockConfigEntry, portal: FakePortal
) -> None:
    portal.fail("occupancy", 404)
    portal.booked_nights = {"data": []}
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert state(hass, f"sensor.{HOME}_occupancy_rate_this_month") == "40.0"
    assert state(hass, f"sensor.{HOME}_booked_nights_this_month") == "12"


async def test_stats_without_revenue_tiles(
    hass: HomeAssistant, config_entry: MockConfigEntry, portal: FakePortal
) -> None:
    portal.fail("analytics", 403)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert state(hass, f"sensor.{HOME}_owner_revenue_this_year") == STATE_UNKNOWN
    assert state(hass, f"sensor.{HOME}_occupancy_rate_this_year") == "35.0"
    assert state(hass, f"sensor.{HOME}_booked_nights_this_year") == "64"


async def test_account_sensors(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    assert state(hass, "sensor.acme_stays_unread_messages") == "3"
    assert state(hass, "sensor.acme_stays_statement_due_to_owner") == "1500.25"
    assert state(hass, "sensor.acme_stays_statement_ending_balance") == "300.0"
    assert state(hass, "sensor.acme_stays_statement_period_end") == "2026-05-31"
    assert state(hass, "sensor.acme_stays_statement_payout_status") == "not_paid"


async def test_stats_refresh(
    hass: HomeAssistant, setup_entry: MockConfigEntry, portal: FakePortal, freezer
) -> None:
    portal.unread = 7
    portal.statements = []
    await advance(hass, freezer, STATS_UPDATE_INTERVAL)

    assert state(hass, "sensor.acme_stays_unread_messages") == "7"
    assert state(hass, "sensor.acme_stays_statement_due_to_owner") == STATE_UNKNOWN


async def test_unique_ids_and_default_disabled(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    occupied = entity_registry.async_get(f"binary_sensor.{HOME}_occupied")
    assert occupied is not None
    assert occupied.unique_id == f"{LISTING_ID}_occupied"
    calendar = entity_registry.async_get(f"calendar.{HOME}_reservations")
    assert calendar is not None
    assert calendar.unique_id == f"{LISTING_ID}_reservations"


async def test_binary_sensors_now(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    assert state(hass, f"binary_sensor.{HOME}_occupied") == STATE_ON
    assert attributes(hass, f"binary_sensor.{HOME}_occupied")["stay_type"] == "guest"
    assert state(hass, f"binary_sensor.{HOME}_owner_stay") == STATE_OFF
    assert state(hass, f"binary_sensor.{HOME}_check_in_today") == STATE_OFF
    assert state(hass, f"binary_sensor.{HOME}_check_out_today") == STATE_OFF
    assert state(hass, f"binary_sensor.{HOME}_turnover_today") == STATE_OFF


async def test_states_follow_the_clock_between_polls(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    freezer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checkout flips the sensors at 10:00 sharp, with no new data needed."""
    # Polls keep succeeding but return the very same data, so nothing but the
    # clock can be changing the states below.
    coordinator = setup_entry.runtime_data.stays
    monkeypatch.setattr(
        coordinator, "_async_update_data", AsyncMock(return_value=coordinator.data)
    )

    # Local midnight into Friday the 12th: both a checkout and a check-in today.
    freezer.move_to("2026-06-12T07:00:01+00:00")
    await advance(hass, freezer, timedelta(seconds=1))
    assert state(hass, f"binary_sensor.{HOME}_occupied") == STATE_ON
    assert state(hass, f"binary_sensor.{HOME}_check_out_today") == STATE_ON
    assert state(hass, f"binary_sensor.{HOME}_check_in_today") == STATE_ON
    assert state(hass, f"binary_sensor.{HOME}_turnover_today") == STATE_ON

    # 10:00 local: the guest has checked out.
    freezer.move_to("2026-06-12T17:00:01+00:00")
    await advance(hass, freezer, timedelta(seconds=1))
    assert state(hass, f"binary_sensor.{HOME}_occupied") == STATE_OFF
    assert state(hass, f"sensor.{HOME}_occupancy") == "vacant"
    assert attributes(hass, f"sensor.{HOME}_occupancy").get("reservation_id") is None
    assert state(hass, f"sensor.{HOME}_next_check_out") == "2026-06-15T17:00:00+00:00"

    # 16:00 local: the next guest has checked in.
    freezer.move_to("2026-06-12T23:00:01+00:00")
    await advance(hass, freezer, timedelta(seconds=1))
    assert state(hass, f"binary_sensor.{HOME}_occupied") == STATE_ON
    assert attributes(hass, f"sensor.{HOME}_occupancy")["reservation_id"] == STAY_NEXT
    assert state(hass, f"sensor.{HOME}_upcoming_stays") == "2"

    # The owner's own stay, which uses the listing's default check-in time.
    freezer.move_to("2026-06-20T23:00:01+00:00")
    await advance(hass, freezer, timedelta(seconds=1))
    assert state(hass, f"sensor.{HOME}_occupancy") == "owner"
    assert state(hass, f"binary_sensor.{HOME}_owner_stay") == STATE_ON


async def test_calendar(
    hass: HomeAssistant, setup_entry: MockConfigEntry, hass_client
) -> None:
    entity_id = f"calendar.{HOME}_reservations"
    # A stay is in progress, so the calendar is "on" and shows that stay.
    assert state(hass, entity_id) == STATE_ON
    current = attributes(hass, entity_id)
    assert current["message"] == "Alex Example"
    assert current["location"] == "1 Example Way, Testville, AZ 85000"
    assert "Source: Airbnb" in current["description"]

    client = await hass_client()
    response = await client.get(
        f"/api/calendars/{entity_id}",
        params={"start": "2026-06-01T00:00:00Z", "end": "2026-07-01T00:00:00Z"},
    )
    events = await response.json()

    assert [event["summary"] for event in events] == [
        "Alex Example",
        "Blake Sample",
        "Owner stay: Olive Owner",
    ]
    # 16:00 to 10:00 at the listing, shown in Home Assistant's own zone (UTC here).
    assert events[0]["start"]["dateTime"] == "2026-06-08T23:00:00+00:00"
    assert events[0]["end"]["dateTime"] == "2026-06-12T17:00:00+00:00"
    assert events[0]["uid"] == STAY_CURRENT

    response = await client.get(
        f"/api/calendars/{entity_id}",
        params={"start": "2026-07-01T00:00:00Z", "end": "2026-08-01T00:00:00Z"},
    )
    assert await response.json() == []


async def test_calendar_shows_holds_as_unconfirmed(
    hass: HomeAssistant, config_entry: MockConfigEntry, portal: FakePortal
) -> None:
    portal.reservations = {
        "hold": reservation(
            "hold", date(2026, 6, 11), date(2026, 6, 13), status="reserved", guest=""
        )
    }
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    upcoming = attributes(hass, f"calendar.{HOME}_reservations")
    assert upcoming["message"] == "Guest stay (unconfirmed)"
    # A hold is on the calendar but does not make the home occupied or expected.
    assert state(hass, f"sensor.{HOME}_upcoming_stays") == "0"
    assert state(hass, f"sensor.{HOME}_next_check_in") == STATE_UNKNOWN


async def test_calendar_empty(
    hass: HomeAssistant, config_entry: MockConfigEntry, portal: FakePortal
) -> None:
    portal.reservations = {}
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert state(hass, f"calendar.{HOME}_reservations") == STATE_OFF
    assert state(hass, f"sensor.{HOME}_occupancy") == "vacant"
    assert state(hass, f"sensor.{HOME}_next_check_out") == STATE_UNKNOWN
    assert state(hass, f"sensor.{HOME}_availability_tonight") == "available"


async def test_event_nothing_at_startup(
    hass: HomeAssistant, setup_entry: MockConfigEntry, portal: FakePortal, freezer
) -> None:
    """Existing reservations are the baseline, not news."""
    entity_id = f"event.{HOME}_reservation"
    assert state(hass, entity_id) == STATE_UNKNOWN

    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)
    assert state(hass, entity_id) == STATE_UNKNOWN


async def test_event_booked(
    hass: HomeAssistant, setup_entry: MockConfigEntry, portal: FakePortal, freezer
) -> None:
    portal.reservations["new"] = reservation(
        "new",
        date(2026, 7, 3),
        date(2026, 7, 6),
        guest="Casey New",
        guests=3,
        created="2026-06-10T19:05:00.000Z",
    )
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)

    fired = attributes(hass, f"event.{HOME}_reservation")
    assert fired["event_type"] == "booked"
    assert fired["reservation_id"] == "new"
    assert fired["guest_name"] == "Casey New"
    assert fired["check_in"] == "2026-07-03T23:00:00+00:00"
    assert fired["nights"] == 3
    assert state(hass, f"sensor.{HOME}_upcoming_stays") == "4"


async def test_event_canceled(
    hass: HomeAssistant, setup_entry: MockConfigEntry, portal: FakePortal, freezer
) -> None:
    del portal.reservations[STAY_NEXT]
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)

    fired = attributes(hass, f"event.{HOME}_reservation")
    assert fired["event_type"] == "canceled"
    assert fired["reservation_id"] == STAY_NEXT


async def test_event_canceled_beyond_the_calendar(
    hass: HomeAssistant, setup_entry: MockConfigEntry, portal: FakePortal, freezer
) -> None:
    """A stay months out is only in the reservation list, which is enough."""
    del portal.reservations[STAY_FAR]
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)

    fired = attributes(hass, f"event.{HOME}_reservation")
    assert fired["event_type"] == "canceled"
    assert fired["reservation_id"] == STAY_FAR


async def test_event_modified(
    hass: HomeAssistant, setup_entry: MockConfigEntry, portal: FakePortal, freezer
) -> None:
    portal.reservations[STAY_NEXT] = reservation(
        STAY_NEXT, date(2026, 6, 12), date(2026, 6, 17), source="VRBO", guests=2
    )
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)

    fired = attributes(hass, f"event.{HOME}_reservation")
    assert fired["event_type"] == "modified"
    assert fired["check_out"] == "2026-06-17T17:00:00+00:00"


async def test_no_cancellation_when_a_finished_stay_drops_out(
    hass: HomeAssistant, setup_entry: MockConfigEntry, portal: FakePortal, freezer
) -> None:
    """A stay that ends and scrolls out of the window was not canceled."""
    freezer.move_to("2026-06-15T19:00:00+00:00")
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)

    assert state(hass, f"event.{HOME}_reservation") == STATE_UNKNOWN
    assert state(hass, f"sensor.{HOME}_occupancy") == "vacant"


async def test_no_cancellation_when_future_stays_cannot_be_seen(
    hass: HomeAssistant, setup_entry: MockConfigEntry, portal: FakePortal, freezer
) -> None:
    """If the reservation list stops working, a far-off stay is just unknown."""
    portal.fail("reservations", 500)
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)

    # The stay in November is beyond the 90-day calendar and no longer listed.
    assert state(hass, f"sensor.{HOME}_upcoming_stays") == "2"
    assert state(hass, f"event.{HOME}_reservation") == STATE_UNKNOWN

    # When the list comes back the stay reappears; it is not a new booking.
    portal.failures.clear()
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)
    assert state(hass, f"sensor.{HOME}_upcoming_stays") == "3"
    assert state(hass, f"event.{HOME}_reservation") == STATE_UNKNOWN


async def test_event_booked_far_ahead(
    hass: HomeAssistant, setup_entry: MockConfigEntry, portal: FakePortal, freezer
) -> None:
    """A booking beyond the calendar is news as soon as the list shows it."""
    portal.reservations["far"] = reservation(
        "far", date(2027, 2, 1), date(2027, 2, 4), created="2026-06-10T19:05:00.000Z"
    )
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)

    fired = attributes(hass, f"event.{HOME}_reservation")
    assert fired["event_type"] == "booked"
    assert fired["reservation_id"] == "far"


async def test_old_booking_entering_the_calendar_is_not_news(
    hass: HomeAssistant, setup_entry: MockConfigEntry, portal: FakePortal, freezer
) -> None:
    """The list leaves some bookings out; they surface as the calendar advances."""
    unlisted = reservation("unlisted", date(2026, 9, 20), date(2026, 9, 23))
    portal.reservations["unlisted"] = unlisted
    portal.unlisted.add("unlisted")
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)
    # 102 days out: beyond the 90-day calendar and missing from the list.
    assert state(hass, f"sensor.{HOME}_upcoming_stays") == "3"

    freezer.move_to("2026-06-25T19:00:00+00:00")
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)

    # Now inside the calendar window, so it is known, but it was booked in April.
    # (Two earlier stays have ended meanwhile: November's and this one remain.)
    assert state(hass, f"sensor.{HOME}_upcoming_stays") == "2"
    stays = setup_entry.runtime_data.stays.data[LISTING_ID].stays
    assert "unlisted" in [stay.id for stay in stays]
    assert state(hass, f"event.{HOME}_reservation") == STATE_UNKNOWN


async def test_no_cancellation_when_the_calendar_cannot_be_read(
    hass: HomeAssistant, setup_entry: MockConfigEntry, portal: FakePortal, freezer
) -> None:
    """A stay only the calendar knew about is not canceled by a calendar outage."""
    portal.unlisted.add(STAY_NEXT)
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)
    assert state(hass, f"sensor.{HOME}_upcoming_stays") == "3"

    portal.fail("calendar", 500)
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)

    assert state(hass, f"sensor.{HOME}_upcoming_stays") == "2"
    assert state(hass, f"event.{HOME}_reservation") == STATE_UNKNOWN
