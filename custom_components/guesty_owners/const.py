"""Constants for the Guesty Owners Portal integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "guesty_owners"

API_BASE: Final = "https://app.guesty.com/api"
PORTAL_DOMAIN_SUFFIX: Final = ".guestyowners.com"

# Client identifier the Owners Portal web app sends with every login. It is
# published in the portal's own JavaScript and is the same for every portal.
PORTAL_API_KEY: Final = "KdDnXIZcVp0HBF2pzzVDHvNqE0CwxVnp"

CONF_DAYS_AHEAD: Final = "days_ahead"
DEFAULT_DAYS_AHEAD: Final = 90
MIN_DAYS_AHEAD: Final = 14
MAX_DAYS_AHEAD: Final = 365

# The calendar is the only source for stays that have already started, so the
# window always reaches back a little to cover a stay that is in progress.
DAYS_BEHIND: Final = 2
# Reservations are listed further out than the calendar because the list is cheap.
RESERVATION_LIST_DAYS: Final = 365
BOOKED_NIGHTS_WINDOW_DAYS: Final = 30

STAYS_UPDATE_INTERVAL: Final = timedelta(minutes=15)
STATS_UPDATE_INTERVAL: Final = timedelta(hours=1)

MANUFACTURER: Final = "Guesty"
