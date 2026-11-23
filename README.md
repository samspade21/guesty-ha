# Guesty Owners Portal for Home Assistant

[![Validate](https://github.com/samspade21/guesty-ha/actions/workflows/validate.yml/badge.svg)](https://github.com/samspade21/guesty-ha/actions/workflows/validate.yml)
[![Test](https://github.com/samspade21/guesty-ha/actions/workflows/test.yml/badge.svg)](https://github.com/samspade21/guesty-ha/actions/workflows/test.yml)

Bring your vacation rental's bookings into Home Assistant. If your property
manager uses [Guesty](https://www.guesty.com) and gave you a login to an owners
portal (an address like `yourmanager.guestyowners.com`), this integration reads
the same information that portal shows you and turns it into entities you can
automate on: who is in the house, when the next guest arrives, what a night
costs, and the revenue figures the portal reports for this month and year.

It is **read-only**. It never creates, changes or cancels anything.

> [!NOTE]
> This is an unofficial project and is not affiliated with or endorsed by
> Guesty. It uses the private API behind the owners portal web app, which Guesty
> can change without notice. If something stops working, please
> [open an issue](https://github.com/samspade21/guesty-ha/issues).

## What you can do with it

- Turn the heating up on the day a guest checks in, and back down after checkout.
- Get a phone notification the moment a new booking comes in or one is canceled.
- Run a "turnover" routine on days with both a checkout and a check-in.
- Skip your own away-mode automations while you are staying there yourself.
- Put the booking calendar and this month's revenue on a dashboard.

## Requirements

- Home Assistant 2026.11 or newer.
- An owner login (email and password) for a Guesty Owners Portal. This is not
  the Guesty Open API, and you do not need API keys from your manager.

## Installation

<!-- ha-marketplace:exclude-start -->

### Marketplace (HACS)

1. In Home Assistant, open **Settings > Marketplace**. On older versions, open
   **HACS**.
2. Open the menu in the top right and choose **Custom repositories**.
3. Add `https://github.com/samspade21/guesty-ha` with the type **Integration**.
4. Find **Guesty Owners Portal** in the list and download it.
5. Restart Home Assistant.

### Manual

Copy the `custom_components/guesty_owners` folder into the `custom_components`
folder of your Home Assistant configuration, then restart Home Assistant.

<!-- ha-marketplace:exclude-end -->

## Setup

1. Go to **Settings > Devices & services > Add integration** and search for
   **Guesty Owners Portal**.
2. Fill in the form:

   | Field | What to enter |
   |---|---|
   | Portal address | The address you open to reach the portal, such as `acme.guestyowners.com`. The part before `.guestyowners.com` is enough. If your manager uses their own domain, enter that. |
   | Email | The email you sign in to the portal with. |
   | Password | Your portal password. |

Home Assistant signs in once to check the details. Every property on the account
becomes a device, and one more device represents the account itself.

You can add more than one login, for example if you own properties with two
different managers. Each is its own entry.

The password is stored in Home Assistant's configuration, because the portal's
session expires after about three weeks and the integration has to sign in
again. If you change your portal password, Home Assistant asks for the new one.

### Options

| Option | Default | What it does |
|---|---|---|
| Calendar days to read ahead | 90 | How many days of nightly rates and availability to read (14 to 365). Reservations are always listed a full year ahead, whatever this is set to. |

## Entities

### For each property

| Entity | What it tells you |
|---|---|
| **Reservations** (calendar) | Every guest and owner stay as an event from check-in time to check-out time. Unconfirmed holds are marked as such. |
| **Reservation** (event) | Fires `booked`, `canceled` or `modified` when a reservation changes, with the stay's details as attributes. |
| **Occupied** | On while a guest or owner stay is in progress. |
| **Owner stay** | On while the stay in progress is your own. |
| **Check-in today** / **Check-out today** | On for the whole local day of an arrival or departure. |
| **Turnover today** | On when there is both a checkout and a check-in today. |
| **Occupancy** | `Vacant`, `Guest` or `Owner`. |
| **Next check-in** / **Next check-out** | Timestamps. The next checkout is the current occupant's, if there is one. |
| **Upcoming stays** | Number of confirmed stays that have not started. |
| **Booked nights next 30 days** | Guest-booked nights in the coming 30 days. |
| **Availability tonight** | `Available`, `Booked` or `Blocked`. |
| **Nightly rate** / **Minimum nights** | Tonight's price and minimum stay. |
| **Owner revenue**, **Net rental income**, **Occupancy rate**, **Booked nights**, **Average nightly rate** | The portal's figures for the whole current calendar month and for the whole current year, so they can include confirmed stays that are still to come. The average nightly rate sensors are disabled by default. |
| **Guest rating**, **Guest reviews**, **Latest review** | Average stars, number of reviews, and when the newest one was written. |

The stay-related sensors carry the reservation's details as attributes:
`reservation_id`, `stay_type`, `status`, `source`, `guest_name`, `check_in`,
`check_out`, `nights`, `guests`, `adults`, `children`, `infants` and `pets`.

### For the account

| Entity | What it tells you |
|---|---|
| **Unread messages** | Unread conversations in the portal inbox. |
| **Statement due to owner**, **Statement ending balance**, **Statement period end**, **Statement payout status** | From your most recent owner statement. |

## Automation examples

Warm the house on arrival day:

```yaml
automation:
  - alias: "Loft: warm up for arriving guests"
    triggers:
      - trigger: state
        entity_id: binary_sensor.harbor_loft_check_in_today
        to: "on"
    actions:
      - action: climate.set_temperature
        target:
          entity_id: climate.loft
        data:
          temperature: 21
```

Tell me about new bookings:

```yaml
automation:
  - alias: "Loft: new booking"
    triggers:
      - trigger: state
        entity_id: event.harbor_loft_reservation
    conditions:
      - condition: state
        entity_id: event.harbor_loft_reservation
        attribute: event_type
        state: booked
    actions:
      - action: notify.mobile_app_my_phone
        data:
          title: "New booking"
          message: >-
            {{ trigger.to_state.attributes.nights }} nights from
            {{ trigger.to_state.attributes.check_in | as_datetime | as_local }}
            via {{ trigger.to_state.attributes.source }}
```

Run a routine two hours before each check-in, using the calendar:

```yaml
automation:
  - alias: "Loft: get ready before check-in"
    triggers:
      - trigger: calendar
        entity_id: calendar.harbor_loft_reservations
        event: start
        offset: "-02:00:00"
    actions:
      - action: script.loft_guest_ready
```

## How the data is updated

Home Assistant asks Guesty for reservations and the calendar every 15 minutes,
and for revenue figures, reviews, statements and the inbox count every hour.

Occupancy does not wait for the next update. The integration knows each stay's
check-in and check-out time, so **Occupied**, **Check-in today** and the other
stay sensors change at the exact moment, in the property's own timezone.

## Known limitations

- **What you see depends on your manager.** Managers choose which figures owners
  may see. A sensor for something hidden from you shows as *Unknown*. Guest
  names appear only if your portal shows them.
- **Reservation events start after setup.** Bookings that already exist when
  Home Assistant starts are the baseline and do not fire an event.
- **Far-off bookings can be announced late or not at all.** The portal's
  reservation list sometimes leaves a booking out. Such a booking becomes known
  when its check-in comes within the calendar range (90 days by default), and
  no `booked` event fires for it then because it is not new. Raise *Calendar
  days to read ahead* if you want earlier notice.
- **Check-in and check-out times are the planned ones.** They come from the
  reservation, or from the property's default times when the reservation has
  none. They are not door-lock or arrival data.
- **No two-factor sign-in.** The portal did not use one when this was written.
  If yours does, setup will fail; please open an issue.
- **Money sensors are not running totals.** The revenue sensors show the figure
  for the current month or year and start over when a new one begins.

## Troubleshooting

- **"No owners portal was found at that address."** Open the portal in your
  browser and copy the address from the address bar.
- **Home Assistant asks you to sign in again.** Your portal password changed, or
  the account was disabled. Enter the current password when prompted.
- **Something else.** Go to the integration's page, open the menu on the entry
  and choose **Download diagnostics**. Passwords, emails, guest names, addresses
  and confirmation codes are removed from the file. Attach it to your issue.

To see what the integration is doing, add this to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.guesty_owners: debug
```

## Removing the integration

1. Go to **Settings > Devices & services** and open **Guesty Owners Portal**.
2. Open the menu on the entry and choose **Delete**.
3. To remove the files as well, uninstall it from the Marketplace (or HACS), or
   delete the `custom_components/guesty_owners` folder, and restart.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The tests run against a fake portal with invented data. To check the client
against your own portal, run the smoke test. It asks for your password, makes
only read requests after signing in, and prints a summary without names or
amounts:

```bash
.venv/bin/python -m scripts.smoke_test acme.guestyowners.com you@example.com
```

Notes on the portal's API are in [docs/api.md](docs/api.md).

## License

[MIT](LICENSE)
