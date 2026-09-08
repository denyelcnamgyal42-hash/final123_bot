# WhatsApp Booking Assistant

A Python hotel-booking project that connects WhatsApp conversations to room availability in Google Sheets. A LangChain agent calls booking tools, while a staff dashboard supports approval, rejection, cancellation, and bot controls.

## Booking flow

```text
WhatsApp -> Flask webhook -> LangChain agent -> availability / booking tools
                                                    |
                                               Google Sheets
                                                    |
                                            Staff approval dashboard
```

The project includes date parsing, availability checks across a stay, booking-status lookup, conversation state, and staff notifications. A booking request goes through an approval workflow; asking the bot for a room is not itself a confirmed reservation.

## Requirements

- Python **3.13 or later**, as declared in `pyproject.toml`.
- An OpenAI API key for the current `ChatOpenAI` implementation.
- A WhatsApp Cloud API setup with an access token and phone-number ID.
- A Google spreadsheet and a service account with permission to edit it.
- A public HTTPS callback URL when connecting real WhatsApp events.

Use a dedicated test spreadsheet and test WhatsApp setup during development. Model calls and messaging can incur provider charges.

## Local setup

```sh
git clone https://github.com/denyelcnamgyal42-hash/final123_bot.git
cd final123_bot
python -m venv .venv
```

Activate the environment:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```sh
# macOS / Linux
source .venv/bin/activate
```

Install the dependencies used by the deployment configuration:

```sh
python -m pip install -r requirements.txt
```

**Use `requirements.txt` for this application.** The current `pyproject.toml` lists only `langchain-groq`, while the application imports Flask, Google Sheets libraries, LangChain, and OpenAI. The dependency ranges are not a reproducible lock; an installation may require resolving version compatibility, particularly around the older LangChain agent APIs.

Create a local `.env` file with your own values:

```dotenv
OPENAI_API_KEY=replace-with-your-key
WHATSAPP_ACCESS_TOKEN=replace-with-your-token
WHATSAPP_PHONE_NUMBER_ID=replace-with-your-phone-number-id
WHATSAPP_VERIFY_TOKEN=replace-with-your-own-verification-string
GOOGLE_SHEET_ID=replace-with-your-spreadsheet-id
GOOGLE_SHEETS_CREDENTIALS_PATH=credentials.json
DASHBOARD_AUTH_TOKEN=replace-with-a-long-random-secret
HOTELS_SHEET=room_allocation
BOOKINGS_SHEET=bookings
PORT=5000
BOT_ENABLED=true
CONTACT_PHONE_NUMBER=replace-with-your-staff-contact-number
```

Place your service-account JSON at the configured credentials path. Share the spreadsheet with that account's `client_email` as an **Editor**. `.env` and `credentials.json` are ignored by Git; keep real credentials out of commits, screenshots, and issue reports.

`config.py` also supports `CREDENTIALS_JSON` for hosted environments and `MODEL_NAME` for model selection. Set an explicit `DASHBOARD_AUTH_TOKEN`: the fallback value in the source is not suitable for protecting a deployment.

## Prepare the room sheet

Create the worksheet named by `HOTELS_SHEET` (default: `room_allocation`). The reader expects dates in column A, header information in rows 1–3, and date rows beginning at row 4. Each room has its own column; room types are read from row 1, with a fallback to row 2.

A minimal example:

| Sheet row | A | B | C |
| --- | --- | --- | --- |
| 1 | Date | Twin | Double |
| 2 | | Room 1 | Room 2 |
| 3 | | | |
| 4 | 2026-10-01 | | 2 |
| 5 | 2026-10-02 | | |

An empty room cell means available; a non-empty cell means occupied. In this example, the Double room is occupied on October 1. Replace the sample dates with your test dates and keep the layout consistent with [excel_handler.py](excel_handler.py).

The automatically created placeholder sheet is not a complete availability dataset. Populate valid room headers and date rows before trying bookings. Booking and state worksheets can be created by the application when permissions allow.

## Start and check

```sh
python main.py
```

The main entry point starts the webhook and registers the dashboard routes on the **same port** (5000 by default).

| Route | Purpose |
| --- | --- |
| `GET /health` | Basic process health response |
| `GET /webhook` | WhatsApp callback verification |
| `POST /webhook` | Incoming WhatsApp events |
| `GET /dashboard` | Staff dashboard; authentication required |
| `GET /api/bookings` | Staff booking list; authentication required |

For local browser access, the dashboard accepts `/dashboard?token=YOUR_DASHBOARD_AUTH_TOKEN`. This puts the token in browser history and potentially logs; use it only in a controlled local setup. Staff API calls also accept an `Authorization: Bearer ...` header.

Configure your WhatsApp callback as `https://YOUR_HOST/webhook` and use the same verification string as `WHATSAPP_VERIFY_TOKEN`. A localhost URL cannot receive external WhatsApp events. A successful `/health` response does not verify OpenAI, Sheets access, or the end-to-end booking flow.

## Project map

| File | Responsibility |
| --- | --- |
| [main.py](main.py) | Startup and shared webhook/dashboard server |
| [whatsapp_webhook.py](whatsapp_webhook.py) | WhatsApp verification, events, queue, and message sending |
| [langchain_agent.py](langchain_agent.py) / [langchain_tools.py](langchain_tools.py) | Model orchestration and booking tools |
| [excel_handler.py](excel_handler.py) | Google Sheets room availability |
| [booking_manager.py](booking_manager.py) | Booking requests and status persistence |
| [employee_dashboard.py](employee_dashboard.py) | Staff interface and booking actions |
| [session_manager.py](session_manager.py) / [state_store.py](state_store.py) | Conversation and persistent state |
| [bot_control.py](bot_control.py) | Bot enablement and human-mode controls |
| [config.py](config.py) | Environment settings |

## Current limitations

This repository is a learning project and needs further hardening before handling real guest data. The entry point uses Flask's built-in server, and the webhook's POST handler does not validate a Meta request signature. Verification logging includes token details. Date lookup can match by month and day without checking the year; avoid mixed-year test data until that behavior is corrected.

The included [render.yaml](render.yaml) uses `pip install -r requirements.txt` and `python main.py`. Its older comment about separate dashboard ports does not reflect the shared-port behavior in `main.py`. Hosting configuration alone does not establish production readiness.

Setup documentation was checked against the source. Live WhatsApp, OpenAI, and Google Sheets integration requires your own credentials and has not been validated as part of this documentation update.
