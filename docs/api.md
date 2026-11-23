# Guesty Owners Portal API notes

These notes describe the private API behind the Guesty Owners Portal web app, as
observed in a browser in November 2026. Guesty does not document it and can
change it at any time. Everything the integration does is a `GET`, apart from
the login. IDs below are placeholders.

The portal is a single-page app served from `<name>.guestyowners.com` (or the
property manager's own domain). It talks to `https://app.guesty.com/api`.

## Authentication

| | |
|---|---|
| Portal lookup | `GET /owners/auth/portals/meta/{hostname}` needs no login. `404` means there is no portal at that hostname. |
| Login | `POST /owners/auth/login` with JSON `{"username", "password", "hostname", "apiKey"}` |
| Authenticated calls | `Authorization: Bearer <token>` |

- `hostname` is the portal's hostname, exactly as in the browser's address bar.
- `apiKey` is a fixed client identifier published in the portal's JavaScript.
  It is the same for every portal and is not a secret.
- The login answers with a JWT, either as a bare string or as `{"token": ...}`.
  The token is valid for 21 days. There is no refresh endpoint; the web app
  logs in again.
- A wrong email or password answers `401` with a plain-text body. A missing or
  expired token answers `401` with `{"message": "Not Authorized"}`.
- The web app also sends a `g-aid-cs` header. The API works without it.
- No CAPTCHA or second factor was seen on the login.

## Endpoints the integration uses

| Purpose | Request |
|---|---|
| Owner and feature flags | `GET /owners/me` |
| Listings | `GET /owners/me/listings?skip=0&limit=100` |
| Upcoming reservations | `GET /owners/me/reservations?reservationType=guest\|owner&reservationDateMatchType=anyNightInRange&startDate=YYYY-MM-DD&endDate=YYYY-MM-DD&listingIds[]={listingId}&skip=0&limit=100` |
| Nightly calendar | `GET /revenue-analytics/analytics/listings/{listingId}/calendar?from=&to=&ownerId=&fields=...&applyOwnerRatePlan=true` |
| Revenue tiles | `GET /revenue-analytics/analytics/owner?from=&to=&ownerId=&listingIds={listingId}&confirmedOnly=true` |
| Occupancy | `GET /revenue-analytics-service/occupancy?owner_id=&from=&to=&listing_id={listingId}` |
| Booked nights | `GET /revenue-analytics-service/booked-nights?owner_id=&from=&to=&listing_id={listingId}` |
| Reviews | `GET /owners/me/reviews?listingIds[]={listingId}&skip=0&limit=1` |
| Statements | `GET /statements-v2/owner-portal?skip=0&limit=1&sort=-periodEndDate` |
| Inbox count | `GET /inbox/owners-portal/conversations/count/unread?type=owner` |

## Things that are easy to get wrong

**Guest stays leave the reservation list once they start.** `/owners/me/reservations`
only lists guest reservations that have not begun. A stay in progress, and past
stays, are only visible through the calendar, where each blocked night carries
`blockRefs[].reservation`. Owner reservations stay listed. The integration reads
both and merges them by reservation ID.

**The reservation list runs days behind.** It behaves like a search index that
is refreshed slowly. On the account this was observed on, bookings made in the
previous few days were on the calendar but not yet in the list, and a booking
canceled days earlier was still in the list as `confirmed` while the calendar
had already given its nights to a newer booking. The calendar is live. The
integration therefore treats the calendar as the authority inside its date
range (a listed stay that the calendar does not show there is dropped) and uses
the list only to see further ahead. It applies this only when every booked or
owner night on the calendar carries its reservation; otherwise the list is
believed. Only the calendar's reservations carry `createdAt`, which is what
tells a new booking apart from an old one that has just come into range.

**The reservation list needs its parameters.** `listingIds[]` and
`reservationDateMatchType` (`checkInNightInRange` or `anyNightInRange`) are
required. Without `reservationType` only guest reservations come back, so guest
and owner stays are two requests.

**Two kinds of timestamps.** In the reservation list, `checkIn` and `checkOut`
are real UTC instants. Inside the calendar's `blockRefs`, they are local wall
time with a `+00:00` suffix. Both carry `checkInDateLocalized`,
`checkOutDateLocalized`, `plannedArrival` and `plannedDeparture`, so the
integration always builds instants from those in the listing's timezone. Owner
reservations often have empty planned times; the listing's default check-in and
check-out times fill in.

**Calendar size.** Without the `fields` filter a night is about 26 kB. With the
filter the web app uses it is under 4 kB.

**Calendar day fields.** `status` is `available`, `booked` or `unavailable`.
`blocks` is a set of flags: `b` booked, `o` owner stay, `pt` preparation time
after a checkout, `m` manual block, `r` reserved. `price` and `minNights` are
tonight's rate and minimum stay.

**Reservation statuses.** `confirmed`, `checked_in` and `checked_out` are stays.
`reserved` and `awaiting_payment` are unconfirmed holds. `inquiry`, `canceled`,
`declined`, `expired` and `closed` are not stays.

**Revenue tiles.** `infoBox` is a list of `{"title", "amount", "type"}` with
titles such as `OWNER_REVENUE`, `NET_RENTAL_INCOME`, `OCCUPANCY` (a percentage,
for example `40.0`), `BOOKED_NIGHTS` and `AVG_NIGHTLY_RATE`. Hidden values come
back as `"N/A"` or `null`.

**Two answers for occupancy and booked nights.** The tiles above and the
`revenue-analytics-service` endpoints agree over a month but not over a year,
apparently because they count different days. The portal's
Analytics page shows the service's numbers, so the integration prefers those
and falls back to the tiles. The service returns occupancy as a fraction
(`{"data": [{"occupancy": 0.35}]}`); the tile is a percentage.

**Portals differ.** Property managers choose what owners may see
(`ownersPortalSettings` on `/owners/me`). The web app itself gets `400` and
`404` answers for sections that are switched off. The integration treats every
section except the owner and the listings as optional.

**Listing pagination.** `/owners/me/listings` appeared to ignore `skip` on an
account with one listing. The client stops paging as soon as a page brings
nothing new.

## Not used

The portal also has a reservation report, documents, a conversation inbox and a
server-sent-events stream for messages. The integration does not read them, and
it never creates or changes anything.
